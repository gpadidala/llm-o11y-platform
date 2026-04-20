"""Stale virtual key policy — tiered escalation for rotation-nudge.

Implements the v2.2 auto-disable policy discussed with Neeraj Kumar Singh B.
on LinkedIn ("notification vs auto-disable in regulated environments").

Default posture (safe for regulated-but-not-paranoid environments):

| Threshold | Action                                        | Default   |
|-----------|-----------------------------------------------|-----------|
| 30 days   | Notify (log + metric + optional webhook)      | always on |
| 60 days   | Soft-disable (``needs_review`` flag, still valid) | on    |
| 90+ days  | Hard-disable (flip ``enabled=false``)         | opt-in    |

Rationale: a quarterly batch job running against a scheduled key shouldn't
break silently because nobody logged in for 60 days. Teams in stricter
environments (fintech, healthcare, gov) can enable hard-disable via env var.

Config via env vars — pick up at startup (settings-agnostic so we can run
in tests without monkey-patching ``src.config.settings``):

- ``STALE_KEY_NOTIFY_AFTER_DAYS``        (default 30, 0 disables notify tier)
- ``STALE_KEY_SOFT_DISABLE_AFTER_DAYS``  (default 60, 0 disables soft tier)
- ``STALE_KEY_HARD_DISABLE_AFTER_DAYS``  (default 0 = off, opt-in)
- ``STALE_KEY_NOTIFY_COOLDOWN_HOURS``    (default 24, dedupe webhook spam)
- ``STALE_KEY_EXEMPT_TAGS``              (comma-separated ``key=value`` pairs)
- ``STALE_KEY_EXEMPT_OWNERS``            (comma-separated owner names)
- ``STALE_KEY_WEBHOOK_URL``              (optional — POST JSON on each action)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import structlog

from src.gateway.virtual_keys import VirtualKey, VirtualKeyManager


logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StalePolicyConfig:
    """Env-driven policy thresholds. 0 disables that tier."""

    notify_after_days: int = 30
    soft_disable_after_days: int = 60
    hard_disable_after_days: int = 0  # opt-in only
    notify_cooldown_hours: int = 24
    exempt_tags: tuple[tuple[str, str], ...] = ()  # ((tag_key, tag_value), ...)
    exempt_owners: tuple[str, ...] = ()
    webhook_url: Optional[str] = None

    @classmethod
    def from_env(cls) -> "StalePolicyConfig":
        """Read config from environment. Called once at startup."""

        def _int_env(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return max(0, int(raw))
            except ValueError:
                logger.warning("invalid_int_env", name=name, value=raw)
                return default

        def _csv_env(name: str) -> tuple[str, ...]:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return ()
            return tuple(p.strip() for p in raw.split(",") if p.strip())

        def _tag_env(name: str) -> tuple[tuple[str, str], ...]:
            pairs: list[tuple[str, str]] = []
            for chunk in _csv_env(name):
                if "=" in chunk:
                    k, v = chunk.split("=", 1)
                    pairs.append((k.strip(), v.strip()))
                else:
                    # bare tag key matches any value
                    pairs.append((chunk, ""))
            return tuple(pairs)

        webhook = os.environ.get("STALE_KEY_WEBHOOK_URL", "").strip() or None

        return cls(
            notify_after_days=_int_env("STALE_KEY_NOTIFY_AFTER_DAYS", 30),
            soft_disable_after_days=_int_env("STALE_KEY_SOFT_DISABLE_AFTER_DAYS", 60),
            hard_disable_after_days=_int_env("STALE_KEY_HARD_DISABLE_AFTER_DAYS", 0),
            notify_cooldown_hours=_int_env("STALE_KEY_NOTIFY_COOLDOWN_HOURS", 24),
            exempt_tags=_tag_env("STALE_KEY_EXEMPT_TAGS"),
            exempt_owners=_csv_env("STALE_KEY_EXEMPT_OWNERS"),
            webhook_url=webhook,
        )

    def as_dict(self) -> dict:
        """Serializable form for the /api/keys/stale/policy endpoint."""
        return {
            "notify_after_days": self.notify_after_days,
            "soft_disable_after_days": self.soft_disable_after_days,
            "hard_disable_after_days": self.hard_disable_after_days,
            "notify_cooldown_hours": self.notify_cooldown_hours,
            "exempt_tags": [{"key": k, "value": v} for k, v in self.exempt_tags],
            "exempt_owners": list(self.exempt_owners),
            "webhook_configured": self.webhook_url is not None,
            "notify_enabled": self.notify_after_days > 0,
            "soft_disable_enabled": self.soft_disable_after_days > 0,
            "hard_disable_enabled": self.hard_disable_after_days > 0,
        }


# ---------------------------------------------------------------------------
# Exemption check
# ---------------------------------------------------------------------------


def _is_exempt(vk: VirtualKey, config: StalePolicyConfig) -> bool:
    """A key is exempt if its owner or any of its tags matches the exempt list."""
    if vk.owner and vk.owner in config.exempt_owners:
        return True
    for ex_key, ex_val in config.exempt_tags:
        if ex_key in vk.tags:
            # Empty exempt value means "any value of this tag"
            if not ex_val or vk.tags.get(ex_key) == ex_val:
                return True
    return False


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


def _send_webhook(url: str, payload: dict, timeout: float = 5.0) -> bool:
    """POST a generic JSON payload. Failures are logged but non-fatal."""
    try:
        resp = httpx.post(url, json=payload, timeout=timeout)
        if resp.status_code >= 400:
            logger.warning(
                "stale_policy_webhook_failed",
                status=resp.status_code,
                url=url,
            )
            return False
        return True
    except Exception as exc:
        logger.warning("stale_policy_webhook_error", error=str(exc), url=url)
        return False


# ---------------------------------------------------------------------------
# Policy application
# ---------------------------------------------------------------------------


@dataclass
class PolicyActionSummary:
    """What happened in a single policy sweep — useful for logs + tests."""

    notified: list[str]
    soft_disabled: list[str]
    hard_disabled: list[str]
    skipped_exempt: list[str]

    def as_dict(self) -> dict:
        return {
            "notified": self.notified,
            "soft_disabled": self.soft_disabled,
            "hard_disabled": self.hard_disabled,
            "skipped_exempt": self.skipped_exempt,
        }


def apply_stale_policy(
    manager: VirtualKeyManager,
    config: StalePolicyConfig,
    now: Optional[float] = None,
) -> PolicyActionSummary:
    """Apply the tiered policy to all enabled keys.

    Escalation is monotonic: a key already soft-disabled only gets a notify
    re-ping after the cooldown; hard-disable implies prior notify + soft
    states have already been set (or are being set in this call).
    """
    now = now or time.time()
    notified: list[str] = []
    soft_disabled: list[str] = []
    hard_disabled: list[str] = []
    skipped_exempt: list[str] = []

    cooldown_seconds = config.notify_cooldown_hours * 3600

    # Snapshot keys under the lock, then act without holding it (webhooks
    # are slow and would block validation calls otherwise).
    for vk in manager.list_keys(enabled_only=True):
        reference_ts = vk.last_used_at if vk.last_used_at is not None else vk.created_at
        if reference_ts is None:
            continue
        days_idle = (now - reference_ts) / 86400

        # Does this key hit ANY tier? If not, move on cheaply.
        hit_notify = (
            config.notify_after_days > 0 and days_idle >= config.notify_after_days
        )
        if not hit_notify:
            continue

        if _is_exempt(vk, config):
            skipped_exempt.append(vk.key_id)
            continue

        hit_soft = (
            config.soft_disable_after_days > 0
            and days_idle >= config.soft_disable_after_days
        )
        hit_hard = (
            config.hard_disable_after_days > 0
            and days_idle >= config.hard_disable_after_days
        )

        base_payload = {
            "event": "stale_key_policy",
            "key_id": vk.key_id,
            "name": vk.name,
            "owner": vk.owner,
            "team": vk.team,
            "days_idle": int(days_idle),
            "never_used": vk.last_used_at is None,
            "last_used_at": vk.last_used_at,
            "created_at": vk.created_at,
            "timestamp": now,
        }

        # --- Tier 3: hard-disable (opt-in) ---
        if hit_hard:
            if manager.revoke_key(vk.key_id):
                hard_disabled.append(vk.key_id)
                logger.warning(
                    "stale_policy_hard_disabled",
                    key_id=vk.key_id,
                    days_idle=int(days_idle),
                )
                if config.webhook_url:
                    _send_webhook(
                        config.webhook_url,
                        {
                            **base_payload,
                            "policy": "hard_disable",
                            "threshold_days": config.hard_disable_after_days,
                        },
                    )
            continue  # already terminal — no need to apply lower tiers

        # --- Tier 2: soft-disable (flag as needs_review) ---
        if hit_soft and not vk.needs_review:
            if manager.mark_needs_review(vk.key_id):
                soft_disabled.append(vk.key_id)
                logger.info(
                    "stale_policy_soft_disabled",
                    key_id=vk.key_id,
                    days_idle=int(days_idle),
                )
                if config.webhook_url:
                    _send_webhook(
                        config.webhook_url,
                        {
                            **base_payload,
                            "policy": "soft_disable",
                            "threshold_days": config.soft_disable_after_days,
                        },
                    )
                manager.mark_stale_notified(vk.key_id)
                continue

        # --- Tier 1: notify (respect cooldown to avoid webhook spam) ---
        last_notify = vk.last_stale_notified_at
        if last_notify is not None and (now - last_notify) < cooldown_seconds:
            continue

        notified.append(vk.key_id)
        logger.info(
            "stale_policy_notified",
            key_id=vk.key_id,
            days_idle=int(days_idle),
        )
        if config.webhook_url:
            _send_webhook(
                config.webhook_url,
                {
                    **base_payload,
                    "policy": "notify",
                    "threshold_days": config.notify_after_days,
                },
            )
        manager.mark_stale_notified(vk.key_id)

    return PolicyActionSummary(
        notified=notified,
        soft_disabled=soft_disabled,
        hard_disabled=hard_disabled,
        skipped_exempt=skipped_exempt,
    )
