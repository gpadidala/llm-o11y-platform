-- LLM O11y Platform — ClickHouse Schema for Long-Term Analytics
CREATE DATABASE IF NOT EXISTS llm_o11y;

-- Main LLM request log table (columnar, optimized for analytics)
CREATE TABLE IF NOT EXISTS llm_o11y.llm_requests (
    request_id String,
    timestamp DateTime64(3),
    provider LowCardinality(String),
    model LowCardinality(String),
    status LowCardinality(String),
    error_type LowCardinality(String) DEFAULT '',
    latency_ms Float64,
    ttft_ms Float64 DEFAULT 0,
    prompt_tokens UInt32,
    completion_tokens UInt32,
    total_tokens UInt32,
    cost_usd Float64,
    user_id String DEFAULT '',
    session_id String DEFAULT '',
    cache_hit UInt8 DEFAULT 0,
    stream UInt8 DEFAULT 0,
    input_preview String DEFAULT '',
    output_preview String DEFAULT '',
    tags Map(String, String) DEFAULT map()
) ENGINE = MergeTree()
ORDER BY (provider, model, timestamp)
PARTITION BY toYYYYMM(timestamp)
TTL timestamp + INTERVAL 365 DAY;

-- MCP tool call log
CREATE TABLE IF NOT EXISTS llm_o11y.mcp_tool_calls (
    server_name LowCardinality(String),
    tool_name LowCardinality(String),
    status LowCardinality(String),
    latency_ms Float64,
    input_tokens UInt32,
    output_tokens UInt32,
    cost_usd Float64,
    session_id String DEFAULT '',
    user_id String DEFAULT '',
    timestamp DateTime64(3)
) ENGINE = MergeTree()
ORDER BY (server_name, tool_name, timestamp)
PARTITION BY toYYYYMM(timestamp);

-- Guardrail events
CREATE TABLE IF NOT EXISTS llm_o11y.guardrail_events (
    timestamp DateTime64(3),
    rule_name LowCardinality(String),
    action LowCardinality(String),
    violation_type String DEFAULT '',
    pii_type String DEFAULT '',
    request_id String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY (rule_name, timestamp)
PARTITION BY toYYYYMM(timestamp);

-- Evaluation scores
CREATE TABLE IF NOT EXISTS llm_o11y.eval_scores (
    timestamp DateTime64(3),
    request_id String,
    criterion LowCardinality(String),
    score Float64,
    reasoning String DEFAULT '',
    judge_model LowCardinality(String),
    judge_latency_ms Float64,
    input_text String DEFAULT '',
    output_text String DEFAULT ''
) ENGINE = MergeTree()
ORDER BY (criterion, timestamp)
PARTITION BY toYYYYMM(timestamp);

-- Rate limit events
CREATE TABLE IF NOT EXISTS llm_o11y.rate_limit_events (
    timestamp DateTime64(3),
    key_id String DEFAULT '',
    dimension LowCardinality(String),
    limit_value UInt32,
    current_value UInt32
) ENGINE = MergeTree()
ORDER BY (dimension, timestamp);

-- Circuit breaker state changes
CREATE TABLE IF NOT EXISTS llm_o11y.circuit_breaker_events (
    timestamp DateTime64(3),
    provider LowCardinality(String),
    previous_state LowCardinality(String),
    new_state LowCardinality(String),
    failure_count UInt32
) ENGINE = MergeTree()
ORDER BY (provider, timestamp);

-- Materialized view for hourly cost aggregation
CREATE MATERIALIZED VIEW IF NOT EXISTS llm_o11y.hourly_cost_mv
ENGINE = SummingMergeTree()
ORDER BY (provider, model, hour)
AS SELECT
    provider,
    model,
    toStartOfHour(timestamp) AS hour,
    sum(cost_usd) AS total_cost,
    sum(total_tokens) AS total_tokens,
    count() AS request_count,
    avg(latency_ms) AS avg_latency
FROM llm_o11y.llm_requests
GROUP BY provider, model, hour;

-- Materialized view for daily model comparison
CREATE MATERIALIZED VIEW IF NOT EXISTS llm_o11y.daily_model_stats_mv
ENGINE = SummingMergeTree()
ORDER BY (model, day)
AS SELECT
    model,
    provider,
    toDate(timestamp) AS day,
    count() AS requests,
    sum(cost_usd) AS cost,
    sum(total_tokens) AS tokens,
    avg(latency_ms) AS avg_latency,
    countIf(status = 'error') AS errors
FROM llm_o11y.llm_requests
GROUP BY model, provider, day;
