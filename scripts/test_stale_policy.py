"""End-to-end smoke test for stale-key tiered policy.

Seeds synthetic keys at varying idle ages (0, 35, 65, 95 days) and runs
``apply_stale_policy()`` with each configuration tier to verify:

- Notify-only tier fires the webhook at 30+ days
- Soft-disable tier flags ``needs_review`` at 60+ days (key stays enabled)
- Hard-disable tier flips ``enabled=false`` at 90+ days (opt-in)
- Exempt tags + owners skip ALL tiers
- Webhook cooldown dedupes notifications on a second sweep

Run: ``python -m scripts.test_stale_policy``
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.gateway.virtual_keys import VirtualKeyManager  # noqa: E402


def _seed_key(manager: VirtualKeyManager, name: str, days_idle: float,
              owner: str = "alice", tags: dict | None = None) -> str:
    raw, vk = manager.create_key(name=name, owner=owner, tags=tags or {})
    vk.last_used_at = time.time() - (days_idle * 86400)
    manager._save()
    return vk.key_id


def _reset_env():
    for k in list(os.environ):
        if k.startswith("STALE_KEY_"):
            del os.environ[k]


def main() -> int:
    tmp = Path(tempfile.mkdtemp()) / "keys.json"
    manager = VirtualKeyManager(storage_path=str(tmp))

    # Seed keys at various idle ages
    fresh_id = _seed_key(manager, "fresh", days_idle=0)
    stale35_id = _seed_key(manager, "stale-35d", days_idle=35)
    stale65_id = _seed_key(manager, "stale-65d", days_idle=65)
    stale95_id = _seed_key(manager, "stale-95d", days_idle=95)
    exempt_tag_id = _seed_key(manager, "batch-job", days_idle=95,
                               tags={"scheduled-batch": "true"})
    exempt_owner_id = _seed_key(manager, "ops-key", days_idle=95, owner="ops-team")

    failures = 0

    # --- Case 1: defaults (notify at 30, soft at 60, no hard) ---
    _reset_env()
    os.environ["STALE_KEY_EXEMPT_TAGS"] = "scheduled-batch"
    os.environ["STALE_KEY_EXEMPT_OWNERS"] = "ops-team"
    from src.gateway.stale_policy import StalePolicyConfig, apply_stale_policy
    cfg = StalePolicyConfig.from_env()
    summary = apply_stale_policy(manager, cfg)

    assert fresh_id not in summary.notified, "fresh key should not notify"
    assert stale35_id in summary.notified, f"35d key should notify, got {summary.notified}"
    assert stale65_id in summary.soft_disabled, \
        f"65d key should soft-disable, got {summary.soft_disabled}"
    # 95d with default (hard=0) falls to soft tier
    assert stale95_id in summary.soft_disabled, \
        f"95d key should soft-disable when hard=off, got {summary.soft_disabled}"
    assert exempt_tag_id in summary.skipped_exempt, "tag-exempt should skip"
    assert exempt_owner_id in summary.skipped_exempt, "owner-exempt should skip"

    # Verify soft-disable flag stuck but key still valid
    vk65 = manager.get_key(stale65_id)
    assert vk65.needs_review is True, "needs_review should be set"
    assert vk65.enabled is True, "soft-disabled key must stay enabled"
    assert vk65.soft_disabled_at is not None
    print(f"PASS case 1 (defaults): notified={len(summary.notified)} "
          f"soft={len(summary.soft_disabled)} exempt={len(summary.skipped_exempt)}")

    # --- Case 2: hard-disable opt-in ---
    _reset_env()
    os.environ["STALE_KEY_HARD_DISABLE_AFTER_DAYS"] = "90"
    os.environ["STALE_KEY_EXEMPT_TAGS"] = "scheduled-batch"
    os.environ["STALE_KEY_EXEMPT_OWNERS"] = "ops-team"
    # Re-seed a 95d key since stale95_id was soft-disabled (but still enabled)
    cfg2 = StalePolicyConfig.from_env()
    summary2 = apply_stale_policy(manager, cfg2)
    assert stale95_id in summary2.hard_disabled, \
        f"95d key with hard=90 should hard-disable, got {summary2.hard_disabled}"
    vk95 = manager.get_key(stale95_id)
    assert vk95.enabled is False, "hard-disabled key must have enabled=false"
    print(f"PASS case 2 (hard-disable opt-in): hard_disabled={summary2.hard_disabled}")

    # --- Case 3: cooldown dedupes notifications on second sweep ---
    _reset_env()
    # Seed a fresh 35d key so we get a clean notify state
    cooldown_test_id = _seed_key(manager, "cooldown-test", days_idle=35)
    cfg3 = StalePolicyConfig.from_env()

    summary3a = apply_stale_policy(manager, cfg3)
    assert cooldown_test_id in summary3a.notified, "first sweep should notify"

    summary3b = apply_stale_policy(manager, cfg3)
    assert cooldown_test_id not in summary3b.notified, \
        "second sweep inside cooldown should NOT re-notify"
    print(f"PASS case 3 (cooldown): 1st={summary3a.notified} 2nd={summary3b.notified}")

    # --- Case 4: notify tier disabled via 0 ---
    _reset_env()
    os.environ["STALE_KEY_NOTIFY_AFTER_DAYS"] = "0"
    disable_test_id = _seed_key(manager, "tier-off-test", days_idle=35)
    cfg4 = StalePolicyConfig.from_env()
    summary4 = apply_stale_policy(manager, cfg4)
    assert disable_test_id not in summary4.notified, "notify tier disabled"
    assert disable_test_id not in summary4.soft_disabled
    print("PASS case 4 (notify tier=0 disables escalation)")

    # --- Case 5: config serialization ---
    _reset_env()
    os.environ["STALE_KEY_WEBHOOK_URL"] = "https://example.com/hook"
    cfg5 = StalePolicyConfig.from_env()
    d = cfg5.as_dict()
    assert d["webhook_configured"] is True
    assert d["notify_enabled"] is True
    assert d["hard_disable_enabled"] is False
    print("PASS case 5 (config.as_dict)")

    print("\nAll stale-policy smoke tests passed.")
    return failures


if __name__ == "__main__":
    sys.exit(main())
