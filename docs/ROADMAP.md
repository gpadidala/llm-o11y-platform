# Roadmap — LLM O11y Platform

Status key:  ✅ shipped  ·  🚧 in design  ·  📋 planned  ·  ❓ needs input

---

## V2.2 — Enterprise Regulated-Environment Readiness

Goal: unblock production use in regulated environments (fintech, healthcare,
gov) where audit trails, credential rotation policies, and org-level
controls are compliance prerequisites.

### 1. Virtual-Key Rotation & Lifecycle (partially shipped)

| Feature | Status | Notes |
|---|---|---|
| Raw keys stored as SHA-256 hashes | ✅ | `src/gateway/virtual_keys.py` — never persists raw key |
| Instant revoke via `enabled=false` | ✅ | `/api/keys/{id}` DELETE flips the flag |
| Optional `expires_at` TTL | ✅ | Rejected at `validate_key()` |
| Manual rotation with grace period | ✅ | `POST /api/keys/{id}/rotate?grace_period_seconds=3600` |
| `rotation_ttl_seconds` for auto-rotation | ✅ | `check_auto_rotations()` — not yet wired to scheduler |
| **Stale-key detection (rotation nudge)** | ✅ | v1.6 shipped — `GET /api/keys/stale`, `gateway_stale_keys{age_bucket}`, hourly sweep |
| **Auto-disable policy** | ✅ | v1.7 shipped — tiered notify → soft-disable → hard-disable |
| Webhook notifications for stale keys | ✅ | v1.7 shipped — `STALE_KEY_WEBHOOK_URL`, generic JSON payload |
| Scheduled auto-rotation wiring | 📋 | Hook `check_auto_rotations()` into the hourly sweep |

#### Auto-disable policy — shipped in v1.7 (design discussion below)

Design question originally raised by **Neeraj Kumar Singh B.** on LinkedIn:

> "In regulated environments, do you typically see auto-disable after N days,
> or just a notification to the owner? Auto-disable is stricter but could
> break a quarterly batch job running against a scheduled key."

**Shipped decision — tiered escalation with safe defaults:**

| Threshold | Action | Default |
|---|---|---|
| 30 days idle | **Notify** (log + metric + optional webhook) | ✅ always on |
| 60 days idle | **Soft-disable** (flag `needs_review`, key still works) | ✅ on |
| 90+ days idle | **Hard-disable** (`enabled=false`) | ❌ opt-in only |

Soft-disable is the sweet spot: the key keeps working so scheduled batch
jobs don't break, but the UI surfaces `needs_review=true` so the owner
gets pushed to rotate. Hard-disable is opt-in for orgs that want it.

Config surface (env vars, see [`.env.example`](../.env.example)):

```bash
STALE_KEY_NOTIFY_AFTER_DAYS=30        # 0 disables tier
STALE_KEY_SOFT_DISABLE_AFTER_DAYS=60
STALE_KEY_HARD_DISABLE_AFTER_DAYS=0   # set to 90 to enable
STALE_KEY_NOTIFY_COOLDOWN_HOURS=24    # webhook dedupe
STALE_KEY_EXEMPT_TAGS=scheduled-batch,tier=dr
STALE_KEY_EXEMPT_OWNERS=ops-team,platform
STALE_KEY_WEBHOOK_URL=https://hooks.slack.com/...
```

**Closed items:**
- ✅ Default policy: `notify + soft-disable` (not auto-revoke) — chose the
  safer posture; regulated environments opt into hard-disable with one env var
- ✅ Webhook payload: **generic JSON** (keys: `event`, `policy`, `key_id`, `name`,
  `owner`, `team`, `days_idle`, `threshold_days`, `never_used`, `last_used_at`,
  `created_at`, `timestamp`) — consumers format as needed downstream
- ✅ Notification cooldown: 24 hours by default, tunable via
  `STALE_KEY_NOTIFY_COOLDOWN_HOURS`

### 2. Audit Trail (shipped)

| Feature | Status | Notes |
|---|---|---|
| Per-key `recent_usage` ring buffer | ✅ | Last 100 requests per key |
| IP address capture (X-Forwarded-For aware) | ✅ | Extracted in `app.py` enhanced chat endpoint |
| User-Agent capture | ✅ | Truncated to 200 chars |
| Unique-IP tracking | ✅ | Last 100 unique IPs per key |
| `GET /api/keys/{id}/audit` endpoint | ✅ | Returns events + IP set + rotation state |
| ClickHouse long-term archival | ✅ | 365-day TTL via OTel collector pipeline |
| **Export to SIEM** (Splunk / Sentinel / ELK) | 📋 | Kafka sink for v2.3 |

### 3. Org-Level Rate Limiting

| Feature | Status | Notes |
|---|---|---|
| Per-key rate limits (RPM/RPH/RPD/TPM/TPD) | ✅ | `src/gateway/rate_limiter.py` |
| Per-key budget caps (USD + tokens) | ✅ | Enforced at `validate_key()` |
| **Org aggregation** | 📋 | Add `org_id` to `VirtualKey`, hierarchical buckets |
| **Team aggregation** | 📋 | Similar but using existing `team` field |
| Provider-level rate limits | 📋 | Guard against any one key saturating a provider |

**Design:** hierarchical token-bucket lookup — `check(org, team, key, tokens)` —
org limit checked first, then team, then key. First denial wins, error
response includes which dimension failed.

### 4. SSO / SAML Authentication

| Feature | Status | Notes |
|---|---|---|
| Local username + password auth | ✅ | `src/auth/manager.py` with SHA-256 + salt |
| Session cookies with 24h TTL | ✅ | Httponly cookies |
| Admin / Manager / Viewer RBAC | ✅ | 12-action permission matrix |
| **OIDC provider integration** | 📋 | Authentik, Okta, Keycloak, AzureAD |
| **SAML 2.0** | 📋 | Via `python3-saml` — SP-initiated flow |
| **JIT user provisioning** | 📋 | Auto-create User on first SSO login, map role from group claim |
| **Audit log of role changes** | 📋 | Who changed whose role, when |

### 5. Multi-Tenant Isolation

| Feature | Status | Notes |
|---|---|---|
| Team field on virtual keys | ✅ | Used for budget + metrics labels |
| Per-tenant data segregation | 📋 | `tenant_id` on every API request |
| Tenant-scoped dashboard views | 📋 | Grafana org per tenant |
| Budget walls (tenant A can't spend tenant B's budget) | 📋 | Enforced at middleware layer |
| Tenant-specific guardrail rules | 📋 | PII patterns, blocked topics, JSON schemas per tenant |

---

## V2.3 — Streaming & Packaging

- Streaming SSE support for chat completions
- TTFT metric emission during streaming (not just at completion)
- WebSocket real-time updates for the playground
- Helm chart for AKS / EKS / GKE
- GitHub Actions CI/CD pipeline (lint, test, build, push to ghcr.io)
- Kafka sink for audit events (SIEM forwarding)

## V2.4 — Agents & RAG

- LangGraph / CrewAI integration guide
- RAG pipeline tracing (embedding → retrieve → generate all on one trace)
- Embedding model support (text-embedding-3, voyage, cohere-embed)
- Agent session cost attribution (already partially in MCP tracer)

## V2.5 — Advanced Capabilities

- Semantic caching with real vector embeddings (pgvector / Qdrant)
- Custom guardrail plugins (Python entry points)
- Batch API passthrough (OpenAI batch, Anthropic message batches) — 50% cost savings
- Cross-region replication for disaster recovery

---

## Feedback & Reviews That Shaped This Roadmap

- **Neeraj Kumar Singh B.** (fintech security, Ex-Meta / JPMC / Wayfair) —
  pushed on rotation TTLs, audit log depth, proactive stale-key nudges.
  His review directly shaped sections 1 and 2.

If you want to influence v2.2, open an issue or comment on the LinkedIn
posts — the roadmap is public on purpose.
