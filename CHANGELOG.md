# Changelog

All notable changes to the LLM O11y Platform. Dates in ISO-8601.

---

## [1.6.0] — 2026-04-20 · Stale-Key Rotation-Nudge

**Feature:** proactive detection of virtual keys that haven't been used in
a long time — the first half of v2.2's enterprise rotation story.

### Added
- `find_stale_keys(days)` and `stale_key_stats()` on `VirtualKeyManager`
- `GET /api/keys/stale?days=30` — list stale keys with `days_idle`,
  `never_used`, `last_used_at`
- `GET /api/keys/stale/stats` — cumulative 30 / 60 / 90 / 180-day
  bucket counts
- `gateway_stale_keys{age_bucket}` Prometheus up/down counter
- Hourly background sweep (configurable via `STALE_KEY_SWEEP_SECONDS`)
- "Stale Virtual Keys — Rotation Nudge" bar-gauge panel on the Gateway
  Operations Grafana dashboard

### Design
- Cumulative buckets: a key idle 100 days counts in the 30, 60, AND 90-day
  buckets so Grafana queries read naturally ("how many keys are 30+ days old")
- Never-used keys fall back to `created_at` timestamp for idle calculation
- Revoked keys are excluded (they're dead, not stale)

---

## [1.5.0] — 2026-04-19 · 14-Layer LLM API Alignment

**Feature:** closed every realistic gap against the industry-standard
14-layer LLM API architecture (API gateway → load balancer → tokenization
→ router → inference → post-processing → response → logging).

### Added
- **Layer 3** `src/gateway/context_window.py` — pre-flight token counting
  + context window validation. Rejects overflow with HTTP 400 BEFORE the
  provider round-trip (saves cost + latency)
- **Layer 6** OpenAI-compatible passthroughs: `stop`, `logprobs`,
  `top_logprobs`, `seed`, `response_format` on `ChatCompletionRequest`
- **Layer 7** prompt cache token tracking: `cache_creation_input_tokens`
  and `cache_read_input_tokens` on `Usage` (OpenAI + Anthropic)
- **Layer 7** `BaseProvider.estimate_cost_breakdown()` — tiered cost
  calculation with cache write (1.25×) and cache read (0.10×) discounts
- **Layer 7** cost metric split by `cost_component` label: input / output / cache
- **Layer 8** `finish_reason` label on `llm_requests_total` metric (no longer
  hardcoded to "stop")
- **Layer 8** span attributes: `gen_ai.response.finish_reasons`,
  `gen_ai.usage.cache_{creation,read}_input_tokens`, `llm.cost.{input,output,cache}_usd`

### Fixed
- `finish_reason` on `LLMRequestRecord` now extracted from actual response
  (was hardcoded to `["stop"]` on every span)
- Anthropic `tool_use` stop reason now maps to `tool_calls` (was missing)
- Router's generic `except Exception` was rewrapping intentional 400s as
  500 — added explicit `HTTPException` re-raise before it

---

## [1.4.0] — 2026-04-19 · Key Rotation + Audit Trail

**Feature:** backed the claims made in a LinkedIn reply — key rotation
with grace period, per-key IP audit trail, per-key Prometheus metrics.

### Added
- `rotate_key(key_id, grace_period_seconds=3600)` — creates a new key
  with identical config, keeps old key valid during grace period
- `rotation_ttl_seconds` field for scheduled auto-rotation
- `check_auto_rotations()` — scan for keys past their TTL
- `KeyUsageEvent` ring buffer (last 100 per key): timestamp, IP, UA,
  endpoint, status, model, tokens, cost
- `recent_ips` unique-IP set (last 100 per key)
- `POST /api/keys/{id}/rotate?grace_period_seconds=N` endpoint
- `GET /api/keys/{id}/audit?limit=50` endpoint
- `key_id` and `team` labels on `llm_requests_total` and related metrics
- IP capture from `X-Forwarded-For` header (proxy-aware)

---

## [1.3.0] — 2026-04-18 · Trace-Log-Metric Deep Linking

**Feature:** complete correlation across Tempo / Prometheus / Loki — click
any signal and jump to the related one.

### Added
- OTLP log exporter in `src/otel/setup.py` — logs now flow to Loki via
  OTel Collector (previously stdout-only)
- Structlog processor `_inject_trace_context` — injects `trace_id`,
  `span_id`, `trace_flags` into every JSON log line
- Grafana datasource provisioning:
  - Tempo → Loki via `tracesToLogs` (filter by `trace_id`, ±1min window)
  - Tempo → Prometheus via `tracesToMetrics`
  - Loki → Tempo via `derivedFields` matching `trace_id` in JSON logs
  - Prometheus → Tempo via `exemplarTraceIdDestinations`
- ClickHouse sink for long-term analytics (365-day TTL, materialized views
  for hourly cost + daily model stats)
- 15 new metrics across gateway operations, guardrails, and evaluation
  (cache hits/misses/savings, rate limit rejections, circuit breaker trips,
  PII detections, eval scores)

---

## [1.2.0] · Prompt Management + Guardrails + Evaluation

- Versioned prompt templates with `{{variable}}` interpolation
- A/B variants for split-testing
- 18 PII regex patterns with inline redaction
- Content safety filters, topic restriction, JSON schema validation
- LLM-as-judge scoring (relevance, faithfulness, helpfulness, coherence,
  toxicity, custom)
- Batch evaluation across datasets
- Request log store with ring buffer + filtering API

---

## [1.1.0] · Intelligent Routing + Gateway Engine

- 6 routing strategies: single, fallback, load-balance, cost-optimized,
  latency-optimized, canary
- Response caching (simple SHA-256 + semantic trigram)
- Token-bucket rate limiter with sliding windows
- Circuit breaker (3-state per provider)
- Exponential backoff retry with jitter
- Virtual keys (`sk-llmo-xxx`) with budgets, rate limits, permissions

---

## [1.0.0] · Initial Release

- Unified OpenAI-compatible gateway for 6 providers (OpenAI, Anthropic,
  Vertex AI, Bedrock, Cohere, Azure OpenAI)
- Full OpenTelemetry instrumentation (traces + metrics + structured logs)
- 19 Grafana dashboards (overview, per-provider, KPI, QoS, reliability,
  cost intelligence, model comparison, agent sessions, etc.)
- Web UI: dashboard, playground, prompt studio, request explorer, key
  management, evaluation, guardrails, routing, providers, settings
- Docker Compose stack with LGTM + ClickHouse
- Kubernetes manifests for AKS deployment
- Admin / Manager / Viewer RBAC with local auth
