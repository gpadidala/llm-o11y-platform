"""Virtual API key management with permissions, budgets, and rate limits.

Virtual keys decouple consumer identity from underlying provider credentials.
Each key has:

- **Permissions** -- allowed providers and models
- **Budget** -- spending caps in USD and/or tokens
- **Rate limits** -- plugs into the gateway rate limiter
- **Metadata** -- owner, team, tags for chargeback / auditing

Keys are generated in the format ``sk-llmo-<32 hex chars>`` and stored as
SHA-256 hashes so that raw keys are never persisted.  State is saved to a
JSON file for durability across restarts.

The module-level ``key_manager`` singleton is thread-safe.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

from src.gateway.rate_limiter import RateLimitConfig


# ---------------------------------------------------------------------------
# Key model
# ---------------------------------------------------------------------------


class VirtualKey(BaseModel):
    """A single virtual API key with its configuration."""

    key_id: str
    name: str
    hashed_key: str  # SHA-256 hex digest of the raw key
    created_at: float
    owner: Optional[str] = None
    team: Optional[str] = None

    # Permissions
    allowed_providers: Optional[list[str]] = None  # None = all providers
    allowed_models: Optional[list[str]] = None  # None = all models

    # Budget
    budget_usd: Optional[float] = None
    spent_usd: float = 0.0
    budget_tokens: Optional[int] = None
    used_tokens: int = 0

    # Rate limits (applied via the gateway rate limiter)
    rate_limit: Optional[RateLimitConfig] = None

    # Metadata
    tags: dict[str, str] = {}
    enabled: bool = True
    expires_at: Optional[float] = None


# ---------------------------------------------------------------------------
# Key manager
# ---------------------------------------------------------------------------

_KEY_PREFIX = "sk-llmo-"


class VirtualKeyManager:
    """Manage virtual API keys with budgets and permissions.

    Thread-safe: all mutable state is guarded by ``_lock``.  Persistence is
    best-effort -- failures to write are logged but do not crash the process.
    """

    def __init__(self, storage_path: str = ".data/keys.json") -> None:
        self._keys: dict[str, VirtualKey] = {}  # key_id -> VirtualKey
        self._key_lookup: dict[str, str] = {}  # hashed_key -> key_id
        self._storage_path = Path(storage_path)
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Key lifecycle
    # ------------------------------------------------------------------

    def create_key(
        self,
        name: str,
        owner: str | None = None,
        team: str | None = None,
        budget_usd: float | None = None,
        budget_tokens: int | None = None,
        rate_limit: RateLimitConfig | None = None,
        allowed_providers: list[str] | None = None,
        allowed_models: list[str] | None = None,
        tags: dict[str, str] | None = None,
        expires_at: float | None = None,
    ) -> tuple[str, VirtualKey]:
        """Create a new virtual key.

        Returns:
            A tuple of ``(raw_key, VirtualKey)``.  The raw key is shown
            **once** and cannot be recovered later.
        """
        raw_key = f"{_KEY_PREFIX}{secrets.token_hex(16)}"
        hashed = hashlib.sha256(raw_key.encode()).hexdigest()
        key_id = f"key_{secrets.token_hex(8)}"

        vk = VirtualKey(
            key_id=key_id,
            name=name,
            hashed_key=hashed,
            created_at=time.time(),
            owner=owner,
            team=team,
            budget_usd=budget_usd,
            budget_tokens=budget_tokens,
            rate_limit=rate_limit,
            allowed_providers=allowed_providers,
            allowed_models=allowed_models,
            tags=tags or {},
            expires_at=expires_at,
        )

        with self._lock:
            self._keys[key_id] = vk
            self._key_lookup[hashed] = key_id
            self._save()

        return raw_key, vk

    def validate_key(self, raw_key: str) -> Optional[VirtualKey]:
        """Validate a raw API key and return its ``VirtualKey`` if valid.

        Returns ``None`` if the key is unknown, disabled, expired, or over
        budget.
        """
        hashed = hashlib.sha256(raw_key.encode()).hexdigest()

        with self._lock:
            key_id = self._key_lookup.get(hashed)
            if key_id is None:
                return None

            vk = self._keys.get(key_id)
            if vk is None:
                return None

            # Enabled check
            if not vk.enabled:
                return None

            # Expiry check
            if vk.expires_at is not None and time.time() > vk.expires_at:
                return None

            # Budget checks (return None if fully exhausted so the request
            # is rejected at the auth layer rather than after execution)
            if vk.budget_usd is not None and vk.spent_usd >= vk.budget_usd:
                return None
            if vk.budget_tokens is not None and vk.used_tokens >= vk.budget_tokens:
                return None

            return vk

    def check_permissions(
        self,
        key_id: str,
        provider: str | None = None,
        model: str | None = None,
    ) -> bool:
        """Check whether *key_id* is allowed to access *provider* / *model*."""
        with self._lock:
            vk = self._keys.get(key_id)
            if vk is None or not vk.enabled:
                return False

            if provider and vk.allowed_providers is not None:
                if provider not in vk.allowed_providers:
                    return False

            if model and vk.allowed_models is not None:
                if model not in vk.allowed_models:
                    return False

            return True

    def record_usage(self, key_id: str, tokens: int, cost_usd: float) -> None:
        """Record usage against a key's budget."""
        with self._lock:
            vk = self._keys.get(key_id)
            if vk is None:
                return
            vk.spent_usd += cost_usd
            vk.used_tokens += tokens
            self._save()

    def revoke_key(self, key_id: str) -> bool:
        """Revoke (disable) a key.  Returns ``True`` if the key existed."""
        with self._lock:
            vk = self._keys.get(key_id)
            if vk is None:
                return False
            vk.enabled = False
            self._save()
            return True

    def enable_key(self, key_id: str) -> bool:
        """Re-enable a previously revoked key."""
        with self._lock:
            vk = self._keys.get(key_id)
            if vk is None:
                return False
            vk.enabled = True
            self._save()
            return True

    def delete_key(self, key_id: str) -> bool:
        """Permanently delete a key."""
        with self._lock:
            vk = self._keys.pop(key_id, None)
            if vk is None:
                return False
            self._key_lookup.pop(vk.hashed_key, None)
            self._save()
            return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_key(self, key_id: str) -> Optional[VirtualKey]:
        """Get key details by key_id."""
        with self._lock:
            return self._keys.get(key_id)

    def list_keys(
        self,
        team: str | None = None,
        owner: str | None = None,
        enabled_only: bool = False,
    ) -> list[VirtualKey]:
        """List keys with optional filters."""
        with self._lock:
            results: list[VirtualKey] = []
            for vk in self._keys.values():
                if team is not None and vk.team != team:
                    continue
                if owner is not None and vk.owner != owner:
                    continue
                if enabled_only and not vk.enabled:
                    continue
                results.append(vk)
            return results

    def get_budget_status(self, key_id: str) -> Optional[dict]:
        """Return budget usage details for monitoring."""
        with self._lock:
            vk = self._keys.get(key_id)
            if vk is None:
                return None
            status: dict = {
                "key_id": key_id,
                "name": vk.name,
            }
            if vk.budget_usd is not None:
                status["budget_usd"] = vk.budget_usd
                status["spent_usd"] = round(vk.spent_usd, 6)
                status["remaining_usd"] = round(vk.budget_usd - vk.spent_usd, 6)
                status["budget_pct_used"] = round(
                    (vk.spent_usd / vk.budget_usd * 100) if vk.budget_usd > 0 else 0, 2
                )
            if vk.budget_tokens is not None:
                status["budget_tokens"] = vk.budget_tokens
                status["used_tokens"] = vk.used_tokens
                status["remaining_tokens"] = vk.budget_tokens - vk.used_tokens
            return status

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self) -> None:
        """Persist keys to disk.  Must hold ``_lock``.

        Failures are silently caught -- the gateway keeps running even if
        the storage directory is read-only.
        """
        try:
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = {kid: vk.model_dump() for kid, vk in self._keys.items()}
            tmp_path = self._storage_path.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(data, indent=2, default=str))
            tmp_path.rename(self._storage_path)
        except Exception:
            # Best-effort persistence -- do not crash the gateway
            pass

    def _load(self) -> None:
        """Load keys from disk."""
        if not self._storage_path.exists():
            return
        try:
            raw = json.loads(self._storage_path.read_text())
            for key_id, key_data in raw.items():
                vk = VirtualKey(**key_data)
                self._keys[key_id] = vk
                self._key_lookup[vk.hashed_key] = key_id
        except Exception:
            # Corrupted file -- start fresh
            pass


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

key_manager = VirtualKeyManager()
