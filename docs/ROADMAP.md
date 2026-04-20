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
| **Auto-disable policy** | 🚧 | See design note below |
| Webhook notifications for stale keys | 🚧 | Design alongside auto-disable |
| Scheduled auto-rotation wiring | 📋 | Hook `check_auto_rotations()` into the hourly sweep |

#### Design question — auto-disable policy (asked by Neeraj Kumar Singh B. on LinkedIn)

> "In regulated environments, do you typically see auto-disable after N days,
> or just a notification to the owner? Auto-disable is stricter but could
> break a quarterly batch job running against a scheduled key."

Proposed config surface:

```yaml
stale_key_policy:
  notify_after_days: 30       # emit webhook + metric
  soft_disable_after_days: 60 # mark as needs_review in UI, still valid
  hard_disable_after_days: 90 # set enabled=false (opt-in per env)
  exclusions:
    - tag: "scheduled-batch"   # keys labelled this skip auto-disable
    - owner: "ops-team"
```

**Open items:**
- ❓ Default policy: `notify-only` or `soft-disable`? Leaning notify-only for v2.2,
  add soft-disable in v2.3 once we have real customer feedback
- ❓ Webhook payload schema — slack-formatted? generic JSON? both?
- ❓ Grace period before hard-disable — 7 days after last notification?

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
