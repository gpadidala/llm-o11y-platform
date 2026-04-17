"""LLM response caching -- exact-match and semantic (similarity-based).

Two cache modes are supported:

- **SIMPLE** -- SHA-256 hash of (model + serialised messages).  Lightning fast,
  zero false positives.
- **SEMANTIC** -- character n-gram cosine similarity against all cached entries
  for the same model.  Catches rephrased questions without requiring an
  external vector database or embedding model.

The module-level ``cache_engine`` singleton is thread-safe and maintains
hit/miss/eviction statistics for monitoring.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections import Counter
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from src.providers.base import MODEL_PRICING


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class CacheMode(str, Enum):
    """Cache lookup strategy."""

    NONE = "none"
    SIMPLE = "simple"
    SEMANTIC = "semantic"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


class CacheEntry(BaseModel):
    """A single cached LLM response."""

    key: str
    response: dict
    created_at: float
    ttl: int  # seconds
    hit_count: int = 0
    model: str
    tokens_saved: int = 0
    # Pre-computed n-gram vector for semantic matching (stored as dict for Pydantic)
    _ngram_vector: Optional[dict[str, int]] = None


# ---------------------------------------------------------------------------
# N-gram similarity helpers (no external deps)
# ---------------------------------------------------------------------------

_NGRAM_N = 3  # character tri-grams


def _ngrams(text: str, n: int = _NGRAM_N) -> Counter:
    """Return a Counter of character n-grams from *text*."""
    text = text.lower().strip()
    return Counter(text[i : i + n] for i in range(len(text) - n + 1))


def _cosine_similarity(a: Counter, b: Counter) -> float:
    """Cosine similarity between two Counter vectors."""
    if not a or not b:
        return 0.0
    common_keys = set(a.keys()) & set(b.keys())
    dot = sum(a[k] * b[k] for k in common_keys)
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _messages_text(messages: list) -> str:
    """Flatten a list of message dicts/objects into a single string."""
    parts: list[str] = []
    for m in messages:
        if isinstance(m, dict):
            parts.append(f"{m.get('role', '')}:{m.get('content', '')}")
        else:
            # Pydantic model
            parts.append(f"{getattr(m, 'role', '')}:{getattr(m, 'content', '')}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Cache engine
# ---------------------------------------------------------------------------


class CacheEngine:
    """In-memory LLM response cache with TTL, LRU eviction, and stats.

    Thread-safe: all mutable state is protected by ``_lock``.
    """

    def __init__(self, max_entries: int = 10_000, default_ttl: int = 3600) -> None:
        self._store: dict[str, CacheEntry] = {}
        # Separate index for semantic matching: model -> list of (key, ngram_counter)
        self._semantic_index: dict[str, list[tuple[str, Counter]]] = {}
        self._lock = threading.Lock()
        self._max_entries = max_entries
        self._default_ttl = default_ttl
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "tokens_saved": 0,
            "cost_saved": 0.0,
            "semantic_hits": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        messages: list,
        model: str,
        mode: CacheMode = CacheMode.SIMPLE,
    ) -> Optional[dict]:
        """Look up a cached response.

        Returns the cached response dict or ``None`` on miss.
        """
        if mode == CacheMode.NONE:
            return None

        # Try exact match first (always)
        key = self._compute_key(messages, model)
        with self._lock:
            entry = self._store.get(key)
            if entry is not None:
                if self._is_expired(entry):
                    self._remove_entry(key)
                else:
                    entry.hit_count += 1
                    self._stats["hits"] += 1
                    tokens = entry.tokens_saved
                    self._stats["tokens_saved"] += tokens
                    self._stats["cost_saved"] += self._estimate_cost_saved(model, tokens)
                    return entry.response

        # Semantic fallback
        if mode == CacheMode.SEMANTIC:
            result = self._semantic_match(messages, model, threshold=0.85)
            if result is not None:
                with self._lock:
                    result.hit_count += 1
                    self._stats["hits"] += 1
                    self._stats["semantic_hits"] += 1
                    tokens = result.tokens_saved
                    self._stats["tokens_saved"] += tokens
                    self._stats["cost_saved"] += self._estimate_cost_saved(model, tokens)
                return result.response

        with self._lock:
            self._stats["misses"] += 1
        return None

    def put(
        self,
        messages: list,
        model: str,
        response: dict,
        ttl: int | None = None,
    ) -> None:
        """Cache a response."""
        key = self._compute_key(messages, model)
        effective_ttl = ttl if ttl is not None else self._default_ttl

        # Calculate tokens saved from usage in response
        usage = response.get("usage", {})
        tokens_saved = 0
        if isinstance(usage, dict):
            tokens_saved = usage.get("total_tokens", 0)
        elif hasattr(usage, "total_tokens"):
            tokens_saved = getattr(usage, "total_tokens", 0)

        entry = CacheEntry(
            key=key,
            response=response,
            created_at=time.time(),
            ttl=effective_ttl,
            model=model,
            tokens_saved=tokens_saved,
        )

        # Pre-compute n-gram vector for semantic index
        text = _messages_text(messages)
        ngram_vec = _ngrams(text)

        with self._lock:
            # Evict if at capacity
            if len(self._store) >= self._max_entries and key not in self._store:
                self._evict_one()

            self._store[key] = entry

            # Update semantic index
            if model not in self._semantic_index:
                self._semantic_index[model] = []
            # Replace if key already present, else append
            self._semantic_index[model] = [
                (k, v) for k, v in self._semantic_index[model] if k != key
            ]
            self._semantic_index[model].append((key, ngram_vec))

    def get_stats(self) -> dict:
        """Return a copy of cache statistics."""
        with self._lock:
            total = self._stats["hits"] + self._stats["misses"]
            hit_rate = (self._stats["hits"] / total * 100) if total > 0 else 0.0
            return {
                **self._stats.copy(),
                "entries": len(self._store),
                "max_entries": self._max_entries,
                "hit_rate_pct": round(hit_rate, 2),
            }

    def clear(self) -> None:
        """Clear all cache entries and reset stats."""
        with self._lock:
            self._store.clear()
            self._semantic_index.clear()
            self._stats = {
                "hits": 0,
                "misses": 0,
                "evictions": 0,
                "tokens_saved": 0,
                "cost_saved": 0.0,
                "semantic_hits": 0,
            }

    def evict_expired(self) -> int:
        """Remove all expired entries. Returns the number evicted."""
        evicted = 0
        with self._lock:
            expired_keys = [
                k for k, entry in self._store.items() if self._is_expired(entry)
            ]
            for key in expired_keys:
                self._remove_entry(key)
                evicted += 1
        return evicted

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_key(self, messages: list, model: str) -> str:
        """Compute a deterministic cache key from messages + model."""
        # Normalise messages to list[dict]
        normalised: list[dict] = []
        for m in messages:
            if isinstance(m, dict):
                normalised.append({"role": m.get("role", ""), "content": m.get("content", "")})
            else:
                normalised.append({"role": getattr(m, "role", ""), "content": getattr(m, "content", "")})
        payload = json.dumps({"model": model, "messages": normalised}, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    def _semantic_match(
        self,
        messages: list,
        model: str,
        threshold: float = 0.85,
    ) -> Optional[CacheEntry]:
        """Find the best semantically similar cached entry above *threshold*.

        Uses character n-gram cosine similarity (no external vector DB).
        """
        text = _messages_text(messages)
        query_vec = _ngrams(text)

        best_key: Optional[str] = None
        best_score = 0.0

        with self._lock:
            candidates = self._semantic_index.get(model, [])
            for key, ngram_vec in candidates:
                entry = self._store.get(key)
                if entry is None or self._is_expired(entry):
                    continue
                score = _cosine_similarity(query_vec, ngram_vec)
                if score >= threshold and score > best_score:
                    best_score = score
                    best_key = key

            if best_key is not None:
                return self._store.get(best_key)
        return None

    def _is_expired(self, entry: CacheEntry) -> bool:
        return (time.time() - entry.created_at) > entry.ttl

    def _evict_one(self) -> None:
        """Evict the least-recently-hit entry (LRU-like). Must hold _lock."""
        if not self._store:
            return
        # Pick the entry with the lowest hit_count and oldest creation
        victim_key = min(
            self._store,
            key=lambda k: (self._store[k].hit_count, -self._store[k].created_at),
        )
        self._remove_entry(victim_key)
        self._stats["evictions"] += 1

    def _remove_entry(self, key: str) -> None:
        """Remove an entry from store and semantic index. Must hold _lock."""
        entry = self._store.pop(key, None)
        if entry is not None:
            model = entry.model
            if model in self._semantic_index:
                self._semantic_index[model] = [
                    (k, v) for k, v in self._semantic_index[model] if k != key
                ]

    @staticmethod
    def _estimate_cost_saved(model: str, tokens: int) -> float:
        """Rough cost estimate for tokens saved by cache hit."""
        pricing = MODEL_PRICING.get(model)
        if pricing is None or tokens == 0:
            return 0.0
        input_price, output_price = pricing
        # Assume roughly 30% of tokens are output, 70% input
        avg_price = (input_price * 0.7 + output_price * 0.3) / 1_000_000
        return tokens * avg_price


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

cache_engine = CacheEngine()
