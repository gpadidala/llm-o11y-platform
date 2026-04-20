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


class KeyUsageEvent(BaseModel):
    """A single usage event recorded against a virtual key for audit purposes."""

    timestamp: float
    ip_address: str = ""
    user_agent: str = ""
    endpoint: str = ""
    status: str = "success"  # "success", "error", "auth_failure"
    model: Optional[str] = None
    tokens: int = 0
    cost_usd: float = 0.0


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

    # Rotation
    rotation_ttl_seconds: Optional[int] = None  # auto-rotate every N seconds
    rotated_from_key_id: Optional[str] = None  # new key that replaced this one
    rotation_grace_expires_at: Optional[float] = None  # old key valid until this time
    last_rotated_at: Optional[float] = None

    # Audit trail (capped ring buffer per key)
    recent_usage: list[KeyUsageEvent] = []
    recent_ips: list[str] = []  # unique IPs seen (last 100)
    last_used_at: Optional[float] = None


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

            now = time.time()

            # Expiry check
            if vk.expires_at is not None and now > vk.expires_at:
                return None

            # Rotation grace period: if this key has been rotated and the
            # grace period has passed, reject. During grace, both old and
            # new keys are accepted.
            if (vk.rotated_from_key_id is not None
                    and vk.rotation_grace_expires_at is not None
                    and now > vk.rotation_grace_expires_at):
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

    def rotate_key(
        self,
        key_id: str,
        grace_period_seconds: int = 3600,
    ) -> Optional[tuple[str, VirtualKey]]:
        """Rotate a key: create a new key with identical config; keep the old
        one valid until ``grace_period_seconds`` expires.

        Returns the new raw key and its ``VirtualKey`` object. During the grace
        window BOTH keys are accepted so callers can roll forward safely.
        """
        with self._lock:
            old = self._keys.get(key_id)
            if old is None:
                return None

            # Generate a new raw key
            raw_key = f"{_KEY_PREFIX}{secrets.token_hex(16)}"
            new_key_id = f"key_{secrets.token_hex(8)}"
            hashed = hashlib.sha256(raw_key.encode()).hexdigest()
            now = time.time()

            new_vk = VirtualKey(
                key_id=new_key_id,
                name=old.name,
                hashed_key=hashed,
                created_at=now,
                owner=old.owner,
                team=old.team,
                allowed_providers=old.allowed_providers,
                allowed_models=old.allowed_models,
                budget_usd=old.budget_usd,
                budget_tokens=old.budget_tokens,
                rate_limit=old.rate_limit,
                tags=dict(old.tags),
                enabled=True,
                expires_at=old.expires_at,
                rotation_ttl_seconds=old.rotation_ttl_seconds,
                last_rotated_at=now,
            )

            # Mark the old key as rotated — accepts requests only during grace
            old.rotated_from_key_id = new_key_id
            old.rotation_grace_expires_at = now + grace_period_seconds
            old.last_rotated_at = now

            self._keys[new_key_id] = new_vk
            self._key_lookup[hashed] = new_key_id
            self._save()

            return raw_key, new_vk

    def record_usage_event(
        self,
        key_id: str,
        event: KeyUsageEvent,
        max_events: int = 100,
    ) -> None:
        """Append a usage event to a key's audit trail (capped ring buffer)."""
        with self._lock:
            vk = self._keys.get(key_id)
            if vk is None:
                return
            vk.recent_usage.append(event)
            if len(vk.recent_usage) > max_events:
                vk.recent_usage = vk.recent_usage[-max_events:]
            vk.last_used_at = event.timestamp
            # Track unique IPs (last 100 unique)
            if event.ip_address and event.ip_address not in vk.recent_ips:
                vk.recent_ips.append(event.ip_address)
                if len(vk.recent_ips) > 100:
                    vk.recent_ips = vk.recent_ips[-100:]
            self._save()

    def get_usage_events(
        self,
        key_id: str,
        limit: int = 50,
    ) -> list[KeyUsageEvent]:
        """Return the most recent usage events for a key."""
        with self._lock:
            vk = self._keys.get(key_id)
            if vk is None:
                return []
            return list(reversed(vk.recent_usage[-limit:]))

    def check_auto_rotations(self) -> list[str]:
        """Find keys whose ``rotation_ttl_seconds`` has elapsed and auto-rotate
        them. Returns a list of rotated key_ids.
        """
        now = time.time()
        to_rotate: list[str] = []
        with self._lock:
            for key_id, vk in list(self._keys.items()):
                if vk.rotation_ttl_seconds is None or not vk.enabled:
                    continue
                age = now - (vk.last_rotated_at or vk.created_at)
                if age >= vk.rotation_ttl_seconds:
                    to_rotate.append(key_id)

        rotated: list[str] = []
        for key_id in to_rotate:
            result = self.rotate_key(key_id)
            if result is not None:
                rotated.append(key_id)
        return rotated

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

    def find_stale_keys(self, days: int = 30) -> list[dict]:
        """Return enabled keys unused for more than *days* days.

        A stale key is one where ``last_used_at`` is older than ``now - days``,
        OR where ``last_used_at`` is None but ``created_at`` is older than
        ``now - days`` (i.e. created and never used).

        Each result dict includes ``key_id``, ``name``, ``owner``, ``team``,
        ``days_idle``, ``last_used_at``, ``created_at`` — enough for a
        rotation-nudge notification.
        """
        now = time.time()
        threshold = now - (days * 86400)
        results: list[dict] = []
        with self._lock:
            for vk in self._keys.values():
                if not vk.enabled:
                    continue  # revoked keys aren't "stale", they're dead
                reference_ts = vk.last_used_at if vk.last_used_at is not None else vk.created_at
                if reference_ts is None or reference_ts >= threshold:
                    continue
                days_idle = int((now - reference_ts) / 86400)
                results.append({
                    "key_id": vk.key_id,
                    "name": vk.name,
                    "owner": vk.owner,
                    "team": vk.team,
                    "days_idle": days_idle,
                    "last_used_at": vk.last_used_at,
                    "created_at": vk.created_at,
                    "never_used": vk.last_used_at is None,
                })
        # Most-stale first so notifications prioritise oldest offenders
        results.sort(key=lambda k: k["days_idle"], reverse=True)
        return results

    def stale_key_stats(self) -> dict:
        """Return bucketed counts of stale keys for dashboards + metrics.

        Buckets: 30, 60, 90, 180 days. A key idle 75 days counts once under
        the 60-day bucket (highest bucket it exceeds).
        """
        now = time.time()
        buckets = {30: 0, 60: 0, 90: 0, 180: 0}
        total_enabled = 0
        never_used = 0
        with self._lock:
            for vk in self._keys.values():
                if not vk.enabled:
                    continue
                total_enabled += 1
                reference_ts = vk.last_used_at if vk.last_used_at is not None else vk.created_at
                if reference_ts is None:
                    continue
                if vk.last_used_at is None:
                    never_used += 1
                days_idle = (now - reference_ts) / 86400
                # Increment every bucket this key exceeds (30-day bucket counts
                # 60+, 90+, 180+ too) — makes Grafana bar charts read naturally
                for bucket in sorted(buckets.keys()):
                    if days_idle >= bucket:
                        buckets[bucket] += 1
        return {
            "total_enabled": total_enabled,
            "never_used": never_used,
            "stale_30d": buckets[30],
            "stale_60d": buckets[60],
            "stale_90d": buckets[90],
            "stale_180d": buckets[180],
        }

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
