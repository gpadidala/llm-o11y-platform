# =============================================================================
# LLM O11y Platform — Makefile
# =============================================================================
# Quick commands for development, testing, and deployment.
#
# Usage:
#   make up          Start all services
#   make down        Stop all services
#   make restart     Restart gateway only (fast reload)
#   make build       Rebuild gateway image
#   make logs        Follow gateway logs
#   make test        Run smoke tests
#   make status      Check all service health
#   make shell       Open shell in gateway container
#   make clean       Remove all volumes and images
# =============================================================================

.PHONY: up down restart build logs test status shell clean proxy-up help

# Default target
help:
	@echo ""
	@echo "  LLM O11y Platform"
	@echo "  ──────────────────────────────────────────"
	@echo ""
	@echo "  Quick Start:"
	@echo "    make up          Start all services"
	@echo "    make down        Stop all services"
	@echo "    make restart     Rebuild + restart gateway"
	@echo "    make logs        Follow gateway logs"
	@echo "    make status      Check service health"
	@echo ""
	@echo "  Development:"
	@echo "    make build       Rebuild gateway image"
	@echo "    make shell       Shell into gateway container"
	@echo "    make test        Run smoke tests"
	@echo ""
	@echo "  Corporate Network:"
	@echo "    make proxy-up    Start with proxy settings"
	@echo "    make proxy-test  Test proxy connectivity"
	@echo ""
	@echo "  Cleanup:"
	@echo "    make clean       Remove volumes + images"
	@echo ""

# ── Lifecycle ────────────────────────────────────────────────────────
up:
	@echo "Starting LLM O11y Platform..."
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example — edit with your API keys")
	docker compose up -d
	@echo ""
	@echo "  Platform UI:  http://localhost:8080"
	@echo "  Grafana:      http://localhost:3001  (admin / llm-o11y)"
	@echo "  API Docs:     http://localhost:8080/docs"
	@echo "  Login:        http://localhost:8080/login  (admin / admin)"
	@echo ""

down:
	docker compose down

restart:
	docker compose build gateway
	docker compose up -d gateway
	@echo "Gateway restarted."

build:
	docker compose build --no-cache gateway

logs:
	docker compose logs -f gateway

# ── Health Check ─────────────────────────────────────────────────────
status:
	@echo "Service Health:"
	@echo "──────────────────────────────────────"
	@curl -sf http://localhost:8080/health > /dev/null 2>&1 && echo "  Gateway:       ✓ healthy" || echo "  Gateway:       ✗ down"
	@curl -sf http://localhost:9091/-/ready > /dev/null 2>&1 && echo "  Prometheus:    ✓ ready" || echo "  Prometheus:    ✗ down"
	@curl -sf http://localhost:3202/ready > /dev/null 2>&1 && echo "  Tempo:         ✓ ready" || echo "  Tempo:         ✗ down"
	@curl -sf http://localhost:3100/ready > /dev/null 2>&1 && echo "  Loki:          ✓ ready" || echo "  Loki:          ✗ down"
	@curl -sf http://localhost:3001/api/health > /dev/null 2>&1 && echo "  Grafana:       ✓ ready" || echo "  Grafana:       ✗ down"
	@curl -sf http://localhost:8123/ping > /dev/null 2>&1 && echo "  ClickHouse:    ✓ ready" || echo "  ClickHouse:    ✗ down"
	@echo "──────────────────────────────────────"

# ── Testing ──────────────────────────────────────────────────────────
test:
	bash scripts/test-gateway.sh

# ── Shell ────────────────────────────────────────────────────────────
shell:
	docker compose exec gateway /bin/bash

# ── Corporate Proxy ──────────────────────────────────────────────────
proxy-up:
	@echo "Starting with corporate proxy settings from .env..."
	@test -f .env || (echo "ERROR: .env file not found. Run: cp .env.example .env" && exit 1)
	@grep -q "HTTP_PROXY" .env && echo "Proxy configured: $$(grep HTTP_PROXY .env)" || echo "WARNING: No HTTP_PROXY in .env"
	docker compose up -d

proxy-test:
	@echo "Testing proxy connectivity..."
	@docker compose exec gateway python -c "import httpx; print(httpx.get('https://api.openai.com/v1/models', headers={'Authorization':'Bearer test'}).status_code)" 2>/dev/null && echo "  OpenAI API: reachable" || echo "  OpenAI API: unreachable (check proxy)"
	@docker compose exec gateway python -c "import httpx; print(httpx.get('https://api.anthropic.com/v1/messages').status_code)" 2>/dev/null && echo "  Anthropic API: reachable" || echo "  Anthropic API: unreachable (check proxy)"

# ── Cleanup ──────────────────────────────────────────────────────────
clean:
	@echo "Removing all containers, volumes, and images..."
	docker compose down -v --rmi local
	rm -rf .data/
	@echo "Clean complete."
