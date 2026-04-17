<p align="center">
  <h1 align="center">LLM O11y Platform</h1>
  <p align="center">
    <strong>Open-Source Unified AI Gateway & Observability Platform</strong>
  </p>
  <p align="center">
    Production-grade AI gateway with intelligent routing, prompt management, guardrails,<br/>
    evaluation engine, virtual keys, and full-stack observability — for every major LLM provider.
  </p>
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> &bull;
  <a href="#architecture">Architecture</a> &bull;
  <a href="#features">Features</a> &bull;
  <a href="#web-ui">Web UI</a> &bull;
  <a href="#api-reference">API Reference</a> &bull;
  <a href="#grafana-dashboards">Dashboards</a> &bull;
  <a href="#deployment">Deployment</a>
</p>

---

## Why This Platform

Modern AI applications use multiple LLM providers, each with different APIs, pricing, latency profiles, and failure modes. Managing this complexity across teams — with cost control, safety guardrails, prompt versioning, and deep observability — requires a unified control plane.

**This platform provides:**

- **Single API** for 6 providers and 16+ models with automatic cost tracking
- **Intelligent routing** that optimizes for cost, latency, or reliability
- **Built-in guardrails** with PII detection, content safety, and output validation
- **Prompt versioning** with A/B testing and a template studio
- **LLM-as-judge evaluation** for automated quality scoring
- **Virtual API keys** with per-key budgets, rate limits, and permissions
- **Full-stack observability** via OpenTelemetry, Grafana, Tempo, Prometheus, and Loki
- **19 Grafana dashboards** covering reliability engineering, cost intelligence, and agent sessions

All open-source. Self-hosted. Zero vendor lock-in.

---

## Quick Start

### Prerequisites

- Docker & Docker Compose
- (Optional) LLM provider API keys

### 1. Clone and configure

```bash
git clone https://github.com/gpadidala/llm-o11y-platform.git
cd llm-o11y-platform
cp .env.example .env
# Edit .env with your API keys (optional — the platform runs without them)
```

### 2. Start the stack

```bash
docker compose up -d
```

This starts 6 services:

| Service | Port | Purpose |
|---------|------|---------|
| **Gateway** | [localhost:8080](http://localhost:8080) | AI Gateway + Web UI |
| **Grafana** | [localhost:3001](http://localhost:3001) | Dashboards (admin / llm-o11y) |
| **Prometheus** | [localhost:9091](http://localhost:9091) | Metrics database |
| **Tempo** | [localhost:3202](http://localhost:3202) | Distributed tracing |
| **Loki** | [localhost:3100](http://localhost:3100) | Log aggregation |
| **OTel Collector** | localhost:4317/4318 | Telemetry pipeline |

### 3. Verify

```bash
curl http://localhost:8080/health
# {"status": "healthy", "service": "llm-o11y-gateway"}
```

### 4. Open the UI

```bash
open http://localhost:8080
```

### 5. Run smoke tests

```bash
bash scripts/test-gateway.sh
```

---

## Architecture

```
                         Your Applications
                    (Apps, Agents, MCP Clients)
                               |
                    OpenAI-compatible API
                               |
   +---------------------------v-----------------------------+
   |              LLM O11y Gateway (FastAPI)                 |
   |                                                         |
   |  +---------------------------------------------------+  |
   |  |              Gateway Pipeline                      |  |
   |  |  Auth -> RateLimit -> Cache -> CircuitBreaker ->   |  |
   |  |  Route -> Retry -> Provider -> Cache(store) -> Log |  |
   |  +---------------------------------------------------+  |
   |                                                         |
   |  +-----------+ +------------+ +-----------+ +--------+  |
   |  | Routing   | | Cache      | | Rate      | | Circuit|  |
   |  | Engine    | | Engine     | | Limiter   | | Breaker|  |
   |  | 6 strats  | | Simple +   | | Token     | | 3-state|  |
   |  |           | | Semantic   | | Bucket    | | per-   |  |
   |  |           | |            | | + Sliding  | | provider|  |
   |  +-----------+ +------------+ +-----------+ +--------+  |
   |                                                         |
   |  +-----------+ +------------+ +-----------+ +--------+  |
   |  | Virtual   | | Prompt     | | Guardrails| | Eval   |  |
   |  | Keys      | | Store      | | Engine    | | Judge  |  |
   |  | Budgets + | | Versioned  | | PII + Safety| | LLM-as|  |
   |  | Perms     | | Templates  | | + Validation| | -judge|  |
   |  +-----------+ +------------+ +-----------+ +--------+  |
   |                                                         |
   |  +-----------+ +------------+ +-----------+ +--------+  |
   |  | Provider  | | Provider   | | Provider  | | Provider|  |
   |  | OpenAI    | | Anthropic  | | Vertex AI | | Bedrock|  |
   |  | Azure     | | Cohere     | |           | |        |  |
   |  +-----------+ +------------+ +-----------+ +--------+  |
   |                                                         |
   |  +---------------------------------------------------+  |
   |  |  OTel Instrumentation (Traces + Metrics + Logs)    |  |
   |  +---------------------------------------------------+  |
   +----------------------------|----------------------------+
                                | OTLP (gRPC)
                                v
   +------------------------------------------------------------+
   |               OpenTelemetry Collector                       |
   |          (batch, memory_limiter, resource)                  |
   +--------+-----------------+-----------------+---------------+
            |                 |                 |
            v                 v                 v
   +--------------+  +----------------+  +---------------+
   | Grafana Tempo|  |  Prometheus    |  | Grafana Loki  |
   |  (Traces)    |  |  (Metrics)     |  |   (Logs)      |
   +-------+------+  +-------+--------+  +-------+-------+
           |                  |                   |
           +------------------+-------------------+
                              |
                 +------------v-----------+
                 |        Grafana         |
                 |   19 Dashboards        |
                 |   Single Pane of Glass |
                 +------------------------+
```

### Request Flow

```
Client Request
     |
     v
[Virtual Key Auth] -- Validates sk-llmo-xxx key, checks budget/permissions
     |
     v
[Rate Limiter] -- Token bucket + sliding window (RPM/RPH/RPD/TPM/TPD)
     |
     v
[Cache Check] -- SHA-256 exact match or trigram semantic similarity
     |
     v (cache miss)
[Circuit Breaker] -- Checks provider health (CLOSED/OPEN/HALF_OPEN)
     |
     v
[Routing Engine] -- Selects target via strategy (cost/latency/fallback/canary)
     |
     v
[Retry with Backoff] -- Exponential backoff with jitter on transient failures
     |
     v
[Provider Adapter] -- Translates to provider-native API (OpenAI/Anthropic/etc)
     |
     v
[Cache Store] -- Caches response for future requests
     |
     v
[OTel Emit] -- Traces (Tempo) + Metrics (Prometheus) + Logs (Loki)
     |
     v
Response to Client
```

---

## Features

### AI Gateway Engine

| Feature | Description |
|---------|-------------|
| **Intelligent Routing** | 6 strategies: Single, Fallback, Load Balance, Cost-Optimized, Latency-Optimized, Canary |
| **Response Caching** | Simple (exact SHA-256 match) and Semantic (trigram cosine similarity @ 0.85 threshold) |
| **Rate Limiting** | Multi-dimensional: requests per minute/hour/day, tokens per minute/day, max concurrent |
| **Circuit Breaker** | Per-provider 3-state machine (Closed/Open/Half-Open) with configurable thresholds |
| **Retry Logic** | Exponential backoff with full jitter, configurable retryable error patterns |
| **Virtual Keys** | `sk-llmo-xxx` format keys with budgets, rate limits, provider/model permissions, team ownership |
| **Middleware Pipeline** | Full request lifecycle: Auth -> RateLimit -> Cache -> CircuitBreaker -> Route -> Retry -> Provider -> Log |

#### Routing Strategies

| Strategy | How It Works |
|----------|-------------|
| `single` | Direct to one provider/model |
| `fallback` | Try providers in priority order, skip if error rate > 50% |
| `loadbalance` | Weighted round-robin across targets |
| `cost` | Route to cheapest model using built-in pricing table |
| `latency` | Route to fastest provider based on rolling P50 latency |
| `canary` | Split traffic: primary gets (100 - weight)%, canary gets weight% |

### Prompt Management

| Feature | Description |
|---------|-------------|
| **Template Store** | Create, version, and manage prompt templates with `{{variable}}` interpolation |
| **Version History** | Every update creates a new version with change notes |
| **A/B Variants** | Create named template variants for testing different prompt strategies |
| **Render API** | Render templates with variables via API |
| **Live Testing** | Test templates against live models directly from the API |
| **Auto-detection** | Variables like `{{topic}}` are automatically detected from template content |

### Guardrails Engine

| Feature | Description |
|---------|-------------|
| **PII Detection** | 18 regex patterns: email, phone (US/UK/intl), SSN, credit cards (Visa/MC/Amex/Discover), IPv4/IPv6, dates, addresses, API keys |
| **PII Redaction** | Replace detected PII with `[EMAIL_REDACTED]`, `[SSN_REDACTED]`, etc. |
| **Content Safety** | Pattern-based detection of harmful content |
| **Topic Restriction** | Block specific topics with word-boundary matching |
| **Output Validation** | Validate LLM output against JSON schemas or regex patterns |
| **Custom Rules** | Add custom regex patterns for domain-specific blocking |

#### Detected PII Types

| Type | Examples | Confidence |
|------|----------|-----------|
| Email | `user@example.com` | 0.95 |
| Phone (US) | `(555) 123-4567`, `555-123-4567` | 0.85 |
| Phone (UK) | `+44 7911 123456` | 0.80 |
| SSN | `123-45-6789` | 0.90 |
| Credit Card (Visa) | `4111-1111-1111-1111` | 0.90 |
| Credit Card (Amex) | `3782-822463-10005` | 0.90 |
| IPv4 | `192.168.1.1` | 0.80 |
| API Key (OpenAI) | `sk-xxxxxxxx` | 0.95 |
| API Key (GitHub) | `ghp_xxxxxxxx` | 0.95 |
| API Key (AWS) | `AKIA...` | 0.95 |

### Evaluation Engine

| Feature | Description |
|---------|-------------|
| **LLM-as-Judge** | Automated quality scoring using an LLM evaluator |
| **6 Criteria** | Relevance, Faithfulness, Helpfulness, Coherence, Toxicity, Custom |
| **Batch Evaluation** | Run evaluations across entire datasets with concurrency control |
| **Dataset Management** | Create, store, and manage evaluation test sets |
| **Score Analytics** | Distribution analysis, per-criterion breakdowns, trend tracking |

#### Evaluation Criteria

| Criterion | What It Measures | Score Range |
|-----------|-----------------|-------------|
| Relevance | How well the response addresses the input | 0.0 - 1.0 |
| Faithfulness | Accuracy vs reference material | 0.0 - 1.0 |
| Helpfulness | Practical utility and actionability | 0.0 - 1.0 |
| Coherence | Logical flow and clarity | 0.0 - 1.0 |
| Toxicity | Safety (1.0 = completely safe) | 0.0 - 1.0 |
| Custom | User-defined criterion with custom rubric | 0.0 - 1.0 |

### Supported Providers & Models

| Provider | Models | Input / Output (per 1M tokens) |
|----------|--------|-------------------------------|
| **OpenAI** | gpt-4o | $2.50 / $10.00 |
| | gpt-4o-mini | $0.15 / $0.60 |
| | o1 | $15.00 / $60.00 |
| | o1-mini | $3.00 / $12.00 |
| **Anthropic** | claude-opus-4-6 | $15.00 / $75.00 |
| | claude-sonnet-4-6 | $3.00 / $15.00 |
| | claude-haiku-4-5 | $0.80 / $4.00 |
| **Google Vertex AI** | gemini-1.5-pro | $1.25 / $5.00 |
| | gemini-1.5-flash | $0.075 / $0.30 |
| | gemini-2.0-flash | $0.10 / $0.40 |
| **AWS Bedrock** | Claude, Llama, Titan | AWS pricing |
| **Azure OpenAI** | GPT-4o, GPT-4o-mini | Azure pricing |
| **Cohere** | command-r-plus | $2.50 / $10.00 |
| | command-r | $0.15 / $0.60 |

### MCP Observability

| Feature | Description |
|---------|-------------|
| **Tool Call Ingestion** | POST tool call records with latency, token attribution, and cost |
| **Session Tracking** | Start/end sessions, aggregate tool calls, emit session spans |
| **@trace_mcp_tool Decorator** | Auto-instrument Python MCP tool functions |
| **Cost Attribution** | Track per-tool and per-session cost breakdown |

---

## Web UI

The platform includes a 10-page dark-themed web UI with glassmorphism effects, real-time data, and vibrant gradient accents.

### Pages

| Page | URL | Description |
|------|-----|-------------|
| **Dashboard** | `/` | Real-time analytics with stat cards, sparkline charts, provider health, recent requests |
| **AI Playground** | `/playground` | Interactive LLM testing with parameter sliders, compare mode for side-by-side model testing |
| **Prompt Studio** | `/prompts` | Template editor with variable detection, version history, A/B variants, live preview |
| **Request Explorer** | `/logs` | Searchable request logs with filters (provider, model, status), expandable detail panels |
| **API Keys** | `/keys` | Virtual key management with budget gauges, rate limit indicators, usage sparklines |
| **Evaluation** | `/eval` | Run evaluations with animated score bars, batch eval on datasets, results table |
| **Guardrails** | `/guardrails` | Toggle PII detection, content safety, topic restriction; live PII test panel |
| **Routing** | `/routing` | Visual strategy selection cards, dynamic target configuration, circuit breaker status |
| **Providers** | `/providers` | Per-provider health, latency, error rate, configured models |
| **Settings** | `/settings` | API key configuration, MCP server management, gateway settings |

### Design System

- **Theme**: Dark (#0f1117) with vibrant accent colours
- **Accents**: Purple (AI/primary), Blue (info), Teal (success), Amber (warning), Coral (error)
- **Effects**: Glassmorphism (`backdrop-filter: blur`), gradient borders, pulse animations
- **Charts**: Inline SVG sparklines, ring charts, bar charts
- **Layout**: CSS Grid with responsive breakpoints (1024px, 768px)

---

## API Reference

### Gateway API (OpenAI-Compatible)

#### Chat Completions

```bash
POST /v1/chat/completions
```

Drop-in replacement for the OpenAI API — just change the base URL:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="sk-llmo-your-virtual-key",  # or your provider key
)

response = client.chat.completions.create(
    model="gpt-4o",
    messages=[{"role": "user", "content": "Hello!"}],
    extra_body={
        "provider": "openai",          # openai, anthropic, vertex_ai, bedrock, cohere
        "user_id": "user-123",
        "session_id": "session-456",
        "cache_mode": "simple",         # none, simple, semantic
        "routing_strategy": "cost",     # single, fallback, loadbalance, cost, latency, canary
        "tags": {"team": "ml-platform"},
    },
)
```

**Response headers:**

| Header | Description |
|--------|-------------|
| `X-Request-ID` | Unique request identifier for tracing |
| `X-Cache-Status` | `HIT` or `MISS` |
| `X-Provider` | Provider that served the request |
| `X-Latency-Ms` | End-to-end latency in milliseconds |

#### List Models

```bash
GET /v1/models
```

Returns all supported models across all providers in OpenAI-compatible format.

### Virtual Keys API

#### Create Key

```bash
POST /api/keys
Content-Type: application/json

{
    "name": "production-key",
    "owner": "ml-team",
    "team": "platform",
    "budget_usd": 100.0,
    "allowed_providers": ["openai", "anthropic"],
    "allowed_models": ["gpt-4o-mini", "claude-haiku-4-5"]
}
```

Response:
```json
{
    "key": "sk-llmo-a1b2c3d4e5f6...",
    "key_id": "key_abc123",
    "name": "production-key"
}
```

The raw key is shown **once** — store it securely.

#### List Keys

```bash
GET /api/keys
```

#### Revoke Key

```bash
DELETE /api/keys/{key_id}
```

### Prompt Management API

#### Create Template

```bash
POST /api/prompts
Content-Type: application/json

{
    "name": "Summarizer",
    "content": "Summarize the following text in {{style}} style:\n\n{{text}}",
    "description": "Flexible text summarizer",
    "tags": ["summarize", "utility"]
}
```

Variables (`{{style}}`, `{{text}}`) are auto-detected.

#### Render Template

```bash
POST /api/prompts/{template_id}/render
Content-Type: application/json

{
    "variables": {
        "style": "bullet-point",
        "text": "The quick brown fox..."
    }
}
```

#### Test Template

```bash
POST /api/prompts/{template_id}/test
Content-Type: application/json

{
    "variables": {"style": "concise", "text": "..."},
    "model": "gpt-4o-mini",
    "provider": "openai"
}
```

### Guardrails API

#### Check Input

```bash
POST /api/guardrails/check-input
Content-Type: application/json

{
    "messages": [{"role": "user", "content": "My SSN is 123-45-6789"}],
    "config": {"enable_pii_detection": true}
}
```

#### Redact PII

```bash
POST /api/guardrails/redact
Content-Type: application/json

{
    "text": "Contact john@example.com or call 555-123-4567"
}
```

Response:
```json
{
    "redacted_text": "Contact [EMAIL_REDACTED] or call [PHONE_REDACTED]",
    "pii_found": 2,
    "matches": [
        {"pii_type": "email", "value": "john@example.com", "confidence": 0.95},
        {"pii_type": "phone_us", "value": "555-123-4567", "confidence": 0.85}
    ]
}
```

### Evaluation API

#### Run Evaluation

```bash
POST /api/eval/run
Content-Type: application/json

{
    "input_text": "What is quantum computing?",
    "output_text": "Quantum computing uses qubits...",
    "criteria": ["relevance", "helpfulness", "coherence"],
    "judge_model": "gpt-4o-mini",
    "judge_provider": "openai"
}
```

#### Create Dataset

```bash
POST /api/eval/datasets
Content-Type: application/json

{
    "name": "QA Test Set",
    "entries": [
        {"input_text": "What is AI?", "expected_output": "AI is..."},
        {"input_text": "Explain ML", "expected_output": "Machine learning is..."}
    ]
}
```

#### Batch Evaluation

```bash
POST /api/eval/batch
Content-Type: application/json

{
    "dataset_id": "ds_xxx",
    "model": "gpt-4o-mini",
    "provider": "openai",
    "criteria": ["relevance", "helpfulness"]
}
```

### MCP Telemetry API

#### Report Tool Call

```bash
POST /v1/mcp/tool-call
Content-Type: application/json

{
    "server_name": "docs-server",
    "tool_name": "search_docs",
    "input_params": {"query": "deployment guide"},
    "output_data": {"results": ["doc1", "doc2"]},
    "latency_ms": 120.5,
    "status": "success",
    "session_id": "agent-run-001",
    "attributed_input_tokens": 500,
    "attributed_output_tokens": 200,
    "attributed_cost_usd": 0.005
}
```

#### Session Lifecycle

```bash
# Start session
POST /v1/mcp/session/start
{"session_id": "agent-001", "agent_name": "research-agent", "user_id": "user-123"}

# ... tool calls happen ...

# End session (emits aggregated span)
POST /v1/mcp/session/end
{"session_id": "agent-001"}
```

### Routing API

#### Get Configuration

```bash
GET /api/routing/config
```

#### Update Strategy

```bash
PUT /api/routing/config
Content-Type: application/json

{
    "strategy": "fallback",
    "targets": [
        {"provider": "openai", "model": "gpt-4o", "weight": 1.0},
        {"provider": "anthropic", "model": "claude-sonnet-4-6", "weight": 1.0}
    ]
}
```

#### Circuit Breaker Status

```bash
GET /api/routing/circuit-breaker
```

### Cache API

```bash
GET /api/cache/stats       # Get hit/miss/eviction stats
POST /api/cache/clear      # Clear all cached responses
```

### Dashboard Stats API

```bash
GET /api/dashboard/stats   # Aggregated stats for the UI dashboard
```

### Python MCP Decorator

```python
from src.mcp_tracer import trace_mcp_tool

@trace_mcp_tool("my-server", "1.0.0")
async def search_docs(query: str) -> dict:
    """Search documentation — automatically traced."""
    results = await do_search(query)
    return {"results": results}
```

---

## Grafana Dashboards

19 pre-provisioned dashboards in the "LLM Observability" folder.

### Overview & Analysis

| Dashboard | UID | Panels | Description |
|-----------|-----|--------|-------------|
| **Overview** | `llm-overview` | 15 | Request rate, error rate, latency P50/P95/P99, TTFT, tokens, cost, MCP tool calls |
| **Cost & Token Analysis** | `llm-cost-tokens` | 6 | Cost deep-dive, token breakdown, top expensive models |
| **Trace Explorer** | `llm-trace-explorer` | 4 | TraceQL search, trace detail, recent traces, span distribution |
| **Advanced Traces** | `llm-traces-advanced` | 13 | Span metrics, service map, correlated logs, error analysis |

### Per-Provider

| Dashboard | UID | Panels | Filtered To |
|-----------|-----|--------|-------------|
| **OpenAI** | `llm-openai` | 13 | gpt-4o, gpt-4o-mini, o1, o1-mini |
| **Anthropic** | `llm-anthropic` | 13 | claude-opus-4-6, claude-sonnet-4-6, claude-haiku-4-5 |
| **Vertex AI** | `llm-vertex` | 13 | gemini-1.5-pro, gemini-1.5-flash, gemini-2.0-flash |
| **AWS Bedrock** | `llm-bedrock` | 13 | All Bedrock models |
| **Cohere** | `llm-cohere` | 13 | command-r-plus, command-r |

### KPI & Quality

| Dashboard | UID | Panels | Description |
|-----------|-----|--------|-------------|
| **KPI Scorecard** | `llm-kpi` | 25 | Availability, performance, cost efficiency, volume, cross-provider comparison |
| **Quality of Service** | `llm-qos` | 18 | SLA compliance, error budgets, capacity planning, cost anomaly detection |

### Advanced Intelligence

| Dashboard | UID | Panels | Description |
|-----------|-----|--------|-------------|
| **Reliability Engineering** | `ai-reliability` | 20 | SLI/SLO tracking, error budget burn rate, anomaly detection, circuit breaker timeline |
| **Cost Intelligence** | `ai-cost-intel` | 20 | Cost forecasting, budget tracking, cost anomaly detection with 2-sigma bands |
| **Model Comparison** | `ai-model-compare` | 20 | Performance matrix, cost matrix, value scoring (throughput/cost), adoption trends |
| **Agent & MCP Sessions** | `ai-agent-sessions` | 20 | Tool analytics, session cost, server health, session timeline |

### Telemetry Signals

| Signal | Metric Name | Labels | Backend |
|--------|-------------|--------|---------|
| Request count | `llm_requests_total` | provider, model, status | Prometheus |
| Token usage | `llm_tokens_total` | provider, model, token_type | Prometheus |
| Cost (USD) | `llm_cost_usd_total` | provider, model | Prometheus |
| Request latency | `llm_request_duration_milliseconds` | provider, model | Prometheus |
| Time to first token | `llm_ttft_milliseconds` | provider, model | Prometheus |
| MCP tool calls | `mcp_tool_calls_total` | server_name, tool_name, status | Prometheus |
| MCP tool latency | `mcp_tool_duration_milliseconds` | server_name, tool_name | Prometheus |
| MCP session cost | `mcp_session_cost_usd_total` | session_id, agent_name | Prometheus |
| Traces | GenAI semantic convention spans | - | Tempo |
| Logs | Structured JSON | - | Loki |

---

## Project Structure

```
llm-o11y-platform/
├── src/
│   ├── app.py                     # FastAPI app (1,100+ lines) - all routes, APIs, middleware
│   ├── config.py                  # Pydantic Settings (env vars, API keys)
│   ├── gateway/
│   │   ├── router.py              # OpenAI-compatible /v1/chat/completions
│   │   ├── routing.py             # 6 routing strategies (cost, latency, canary, etc.)
│   │   ├── cache.py               # Simple + semantic response caching
│   │   ├── rate_limiter.py        # Token bucket + sliding window rate limiting
│   │   ├── circuit_breaker.py     # Per-provider circuit breaker (3-state)
│   │   ├── retry.py               # Exponential backoff with jitter
│   │   ├── virtual_keys.py        # sk-llmo-xxx key management with budgets
│   │   └── middleware.py          # Full gateway pipeline orchestrator
│   ├── providers/
│   │   ├── base.py                # BaseProvider ABC + MODEL_PRICING (16 models)
│   │   ├── openai_provider.py     # OpenAI GPT-4o, o1 adapter
│   │   ├── anthropic_provider.py  # Anthropic Claude adapter
│   │   ├── vertex_provider.py     # Google Gemini adapter
│   │   ├── bedrock_provider.py    # AWS Bedrock Converse API adapter
│   │   └── cohere_provider.py     # Cohere Command R adapter
│   ├── prompts/
│   │   ├── templates.py           # Versioned prompt template store
│   │   └── router.py              # Prompt CRUD + render + test API
│   ├── guardrails/
│   │   ├── engine.py              # Guardrails pipeline (PII, safety, validation)
│   │   ├── pii.py                 # 18 PII detection regex patterns
│   │   └── router.py              # Guardrails API endpoints
│   ├── eval/
│   │   ├── judge.py               # LLM-as-judge scoring engine (6 criteria)
│   │   ├── datasets.py            # Evaluation dataset management
│   │   └── router.py              # Evaluation API endpoints
│   ├── mcp_tracer/
│   │   ├── router.py              # MCP tool call + session ingestion API
│   │   └── interceptor.py         # @trace_mcp_tool decorator
│   ├── otel/
│   │   ├── setup.py               # OTel bootstrap (traces + 8 metrics)
│   │   ├── llm_spans.py           # GenAI semantic convention spans
│   │   └── mcp_spans.py           # MCP spans + session tracker
│   ├── models/
│   │   ├── telemetry.py           # Core data models (Request, Response, Provider enum)
│   │   ├── keys.py                # Virtual key models
│   │   ├── prompts.py             # Prompt template models
│   │   └── eval.py                # Evaluation models
│   ├── templates/                 # 11 Jinja2 HTML templates (dark theme UI)
│   │   ├── base.html              # Master layout (1,500+ lines of CSS)
│   │   ├── index.html             # Dashboard with SVG sparklines
│   │   ├── playground.html        # AI playground with compare mode
│   │   ├── prompts.html           # Prompt studio
│   │   ├── logs.html              # Request explorer
│   │   ├── keys.html              # Key management
│   │   ├── eval.html              # Evaluation dashboard
│   │   ├── guardrails.html        # Guardrails config
│   │   ├── routing.html           # Routing builder
│   │   ├── providers.html         # Provider status
│   │   └── settings.html          # Settings page
│   └── static/
│       └── app.js                 # Frontend JavaScript (1,000+ lines)
├── grafana/
│   ├── dashboards/                # 19 dashboard JSON files
│   └── provisioning/              # Auto-provisioning for datasources + dashboards
├── k8s/
│   └── base/                      # Kubernetes manifests (Deployment, Service, ConfigMap)
├── docker-compose.yaml            # Full 6-service local stack
├── Dockerfile                     # Multi-stage Python 3.11 build
├── otel-collector-config.yaml     # OTel Collector pipeline config
├── tempo-config.yaml              # Grafana Tempo config
├── loki-config.yaml               # Grafana Loki config
├── prometheus.yaml                # Prometheus scrape config
├── pyproject.toml                 # Python project metadata + dependencies
├── requirements.txt               # Pinned pip dependencies
├── .env.example                   # Environment template
└── scripts/
    ├── test-gateway.sh            # Smoke tests
    └── deploy-aks.sh              # AKS deployment script
```

---

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and configure:

```bash
# LLM Provider API Keys
OPENAI_API_KEY=sk-your-openai-key
ANTHROPIC_API_KEY=sk-ant-your-key
COHERE_API_KEY=your-cohere-key

# Azure OpenAI
AZURE_OPENAI_API_KEY=your-azure-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_VERSION=2024-02-01

# Google Vertex AI
VERTEX_PROJECT_ID=your-gcp-project
VERTEX_LOCATION=us-central1
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json

# AWS Bedrock
AWS_ACCESS_KEY_ID=your-access-key
AWS_SECRET_ACCESS_KEY=your-secret-key
AWS_REGION=us-east-1

# Gateway
GATEWAY_PORT=8080
LOG_LEVEL=info
```

### OpenTelemetry Config

The OTel Collector pipeline is defined in `otel-collector-config.yaml`:

- **Receivers**: OTLP gRPC (:4317) and HTTP (:4318)
- **Processors**: memory_limiter (512 MiB), batch (5s/512 items), resource enrichment
- **Exporters**: Traces to Tempo, Metrics to Prometheus (remote write), Logs to Loki

---

## Deployment

### Local (Docker Compose)

```bash
docker compose up -d        # Start all services
docker compose logs -f       # Follow logs
docker compose down          # Stop all services
```

### Kubernetes (AKS)

```bash
# Configure
export RESOURCE_GROUP=llm-o11y-rg
export CLUSTER_NAME=llm-o11y-aks
export ACR_NAME=llmo11yacr

# Deploy
bash scripts/deploy-aks.sh
```

Kubernetes manifests in `k8s/base/`:
- `namespace.yaml` — `llm-o11y` namespace
- `configmap.yaml` — Gateway environment config
- `gateway-deployment.yaml` — 2-replica Deployment + ClusterIP Service
- `otel-collector.yaml` — OTel Collector Deployment + ConfigMap + Service

### Custom Deployment

The gateway is a standard FastAPI application:

```bash
pip install -r requirements.txt
uvicorn src.app:app --host 0.0.0.0 --port 8080
```

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to point to your OTel Collector.

---

## Development

### Prerequisites

- Python 3.11+
- Docker & Docker Compose

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Run locally (without Docker)

```bash
uvicorn src.app:app --reload --port 8080
```

### Run tests

```bash
pytest tests/
```

### Lint

```bash
ruff check src/
mypy src/
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Gateway** | Python 3.11, FastAPI, Uvicorn, Pydantic |
| **LLM SDKs** | openai, anthropic, google-cloud-aiplatform, boto3, cohere |
| **Telemetry** | OpenTelemetry SDK + OTLP exporters |
| **Collector** | OpenTelemetry Collector Contrib 0.96.0 |
| **Traces** | Grafana Tempo 2.4.1 |
| **Metrics** | Prometheus 2.51.0 |
| **Logs** | Grafana Loki 2.9.6 |
| **Dashboards** | Grafana 10.4.1 |
| **Frontend** | Jinja2 templates, vanilla JS, CSS Grid |
| **Containers** | Docker, Docker Compose |
| **Orchestration** | Kubernetes (AKS manifests) |

---

## License

MIT

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

<p align="center">
  Built with FastAPI, OpenTelemetry, and the Grafana LGTM Stack
</p>
