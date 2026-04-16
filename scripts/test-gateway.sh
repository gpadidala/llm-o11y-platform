#!/usr/bin/env bash
# =============================================================================
# LLM O11y Gateway Smoke Test
# =============================================================================
# Validates that the gateway is running, accepts requests, and telemetry flows
# through to the observability backends (Prometheus, Tempo).
# =============================================================================

set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://localhost:8080}"
PROMETHEUS_URL="${PROMETHEUS_URL:-http://localhost:9090}"
TEMPO_URL="${TEMPO_URL:-http://localhost:3200}"

PASS=0
FAIL=0
TOTAL=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
check() {
  local name="$1"
  local result="$2"
  TOTAL=$((TOTAL + 1))
  if [ "$result" -eq 0 ]; then
    PASS=$((PASS + 1))
    echo "  [PASS] $name"
  else
    FAIL=$((FAIL + 1))
    echo "  [FAIL] $name"
  fi
}

separator() {
  echo ""
  echo "--- $1 ---"
}

# ---------------------------------------------------------------------------
# 1. Health Check
# ---------------------------------------------------------------------------
separator "Health Check"

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${GATEWAY_URL}/health" 2>/dev/null || echo "000")
check "GET /health returns 200" "$([ "$HTTP_CODE" = "200" ] && echo 0 || echo 1)"

# ---------------------------------------------------------------------------
# 2. Chat Completion (mock or real)
# ---------------------------------------------------------------------------
separator "Chat Completion"

COMPLETION_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "${GATEWAY_URL}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o-mini",
    "messages": [
      {"role": "user", "content": "Say hello in one word."}
    ],
    "max_tokens": 10
  }' 2>/dev/null || echo -e "\n000")

COMPLETION_CODE=$(echo "$COMPLETION_RESPONSE" | tail -1)
COMPLETION_BODY=$(echo "$COMPLETION_RESPONSE" | head -n -1)

check "POST /v1/chat/completions returns 2xx" "$(echo "$COMPLETION_CODE" | grep -qE '^2[0-9]{2}$' && echo 0 || echo 1)"

# Check response body has expected fields
echo "$COMPLETION_BODY" | grep -q '"choices"' 2>/dev/null
check "Response contains 'choices' field" "$?"

# ---------------------------------------------------------------------------
# 3. Metrics in Prometheus
# ---------------------------------------------------------------------------
separator "Prometheus Metrics"

# Wait briefly for metrics to propagate
sleep 3

PROM_QUERY="llm_requests_total"
PROM_RESPONSE=$(curl -s "${PROMETHEUS_URL}/api/v1/query?query=${PROM_QUERY}" 2>/dev/null || echo '{"status":"error"}')

echo "$PROM_RESPONSE" | grep -q '"success"' 2>/dev/null
check "Prometheus query for llm_requests_total succeeds" "$?"

PROM_HAS_DATA=$(echo "$PROM_RESPONSE" | grep -c '"result":\[{' 2>/dev/null || echo 0)
check "Prometheus has llm_requests_total data" "$([ "$PROM_HAS_DATA" -gt 0 ] && echo 0 || echo 1)"

# Check for duration histogram
DURATION_RESPONSE=$(curl -s "${PROMETHEUS_URL}/api/v1/query?query=llm_request_duration_bucket" 2>/dev/null || echo '{"status":"error"}')
echo "$DURATION_RESPONSE" | grep -q '"success"' 2>/dev/null
check "Prometheus query for llm_request_duration_bucket succeeds" "$?"

# ---------------------------------------------------------------------------
# 4. Traces in Tempo
# ---------------------------------------------------------------------------
separator "Tempo Traces"

# Search for recent traces
TEMPO_RESPONSE=$(curl -s "${TEMPO_URL}/api/search?limit=5&start=$(date -v-5M +%s 2>/dev/null || date -d '5 minutes ago' +%s)&end=$(date +%s)" 2>/dev/null || echo '{"error":"unreachable"}')

echo "$TEMPO_RESPONSE" | grep -qv '"error"' 2>/dev/null
check "Tempo search API is reachable" "$?"

TRACE_COUNT=$(echo "$TEMPO_RESPONSE" | python3 -c "import sys,json; data=json.load(sys.stdin); print(len(data.get('traces',[])))" 2>/dev/null || echo 0)
check "Tempo has recent traces" "$([ "$TRACE_COUNT" -gt 0 ] && echo 0 || echo 1)"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "==========================================="
echo "  Smoke Test Results: ${PASS}/${TOTAL} passed"
if [ "$FAIL" -gt 0 ]; then
  echo "  ${FAIL} check(s) FAILED"
  echo "==========================================="
  exit 1
else
  echo "  All checks PASSED"
  echo "==========================================="
  exit 0
fi
