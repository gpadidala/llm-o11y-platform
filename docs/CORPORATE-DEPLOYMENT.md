# Corporate Network Deployment Guide

Deploy the LLM O11y Platform behind corporate firewalls, HTTP proxies, and TLS-inspecting networks.

---

## Quick Start (Corporate)

```bash
# 1. Clone
git clone https://github.com/gpadidala/llm-o11y-platform.git
cd llm-o11y-platform

# 2. Configure
cp .env.example .env
# Edit .env with your proxy settings and API keys (see below)

# 3. (Optional) Add corporate CA certificate
cp /path/to/corp-ca-bundle.crt ./certs/corp-ca-bundle.crt

# 4. Start
make up

# 5. Verify
make status
```

---

## Corporate Proxy Configuration

Add these to your `.env` file:

```bash
# HTTP proxy (used by all LLM SDK calls and OTel exporters)
HTTP_PROXY=http://proxy.corp.example.com:8080
HTTPS_PROXY=http://proxy.corp.example.com:8080

# Bypass proxy for internal services (Docker network)
NO_PROXY=localhost,127.0.0.1,otel-collector,tempo,prometheus,loki,grafana,clickhouse,.corp.example.com
```

### Proxy with Authentication

```bash
HTTP_PROXY=http://username:password@proxy.corp.example.com:8080
HTTPS_PROXY=http://username:password@proxy.corp.example.com:8080
```

### SOCKS5 Proxy

```bash
HTTP_PROXY=socks5://proxy.corp.example.com:1080
HTTPS_PROXY=socks5://proxy.corp.example.com:1080
```

---

## TLS / SSL Certificate Configuration

If your corporate network uses TLS inspection (MITM proxy), you need to trust the corporate CA:

### Option 1: Mount CA bundle into gateway

Create a `certs/` directory and add your corporate CA certificate:

```bash
mkdir -p certs
cp /path/to/corp-ca-bundle.crt certs/
```

Add to `.env`:
```bash
REQUESTS_CA_BUNDLE=/app/certs/corp-ca-bundle.crt
SSL_CERT_FILE=/app/certs/corp-ca-bundle.crt
```

Add volume mount to `docker-compose.yaml` under the gateway service:
```yaml
gateway:
  volumes:
    - ./certs:/app/certs:ro
```

### Option 2: Disable SSL verification (development only)

```bash
# NOT RECOMMENDED FOR PRODUCTION
PYTHONHTTPSVERIFY=0
CURL_CA_BUNDLE=""
```

---

## Air-Gapped / Offline Deployment

For networks with no internet access:

### 1. Pre-pull images on a connected machine

```bash
# On a machine with internet access
docker pull python:3.11-slim
docker pull otel/opentelemetry-collector-contrib:0.96.0
docker pull grafana/tempo:2.4.1
docker pull prom/prometheus:v2.51.0
docker pull grafana/loki:2.9.6
docker pull grafana/grafana:10.4.1
docker pull clickhouse/clickhouse-server:24.3

# Save to tar
docker save -o llm-o11y-images.tar \
  python:3.11-slim \
  otel/opentelemetry-collector-contrib:0.96.0 \
  grafana/tempo:2.4.1 \
  prom/prometheus:v2.51.0 \
  grafana/loki:2.9.6 \
  grafana/grafana:10.4.1 \
  clickhouse/clickhouse-server:24.3
```

### 2. Transfer and load on air-gapped machine

```bash
# On the air-gapped machine
docker load -i llm-o11y-images.tar

# Build gateway image locally
docker compose build gateway

# Start
make up
```

### 3. Pre-download Python dependencies

```bash
# On connected machine
pip download -r requirements.txt -d ./wheels/

# Transfer wheels/ directory to air-gapped machine
# Then in Dockerfile, use:
# COPY wheels/ /tmp/wheels/
# RUN pip install --no-index --find-links=/tmp/wheels/ -r requirements.txt
```

---

## Private Container Registry

If you use a private registry (Harbor, Artifactory, ECR, ACR):

### 1. Re-tag and push images

```bash
REGISTRY=registry.corp.example.com/llm-o11y

docker tag otel/opentelemetry-collector-contrib:0.96.0 $REGISTRY/otel-collector:0.96.0
docker tag grafana/tempo:2.4.1 $REGISTRY/tempo:2.4.1
docker tag grafana/grafana:10.4.1 $REGISTRY/grafana:10.4.1
docker tag prom/prometheus:v2.51.0 $REGISTRY/prometheus:2.51.0
docker tag grafana/loki:2.9.6 $REGISTRY/loki:2.9.6
docker tag clickhouse/clickhouse-server:24.3 $REGISTRY/clickhouse:24.3

# Push all
for img in otel-collector:0.96.0 tempo:2.4.1 grafana:10.4.1 prometheus:2.51.0 loki:2.9.6 clickhouse:24.3; do
  docker push $REGISTRY/$img
done
```

### 2. Create docker-compose.override.yaml

```yaml
services:
  otel-collector:
    image: registry.corp.example.com/llm-o11y/otel-collector:0.96.0
  tempo:
    image: registry.corp.example.com/llm-o11y/tempo:2.4.1
  prometheus:
    image: registry.corp.example.com/llm-o11y/prometheus:2.51.0
  loki:
    image: registry.corp.example.com/llm-o11y/loki:2.9.6
  grafana:
    image: registry.corp.example.com/llm-o11y/grafana:10.4.1
  clickhouse:
    image: registry.corp.example.com/llm-o11y/clickhouse:24.3
```

---

## Port Configuration

Default ports and how to change them if they conflict:

| Service | Default | Env/Config |
|---------|---------|------------|
| Gateway | 8080 | `GATEWAY_PORT` in .env |
| Grafana | 3001 | Change `3001:3000` in docker-compose.yaml |
| Prometheus | 9091 | Change `9091:9090` in docker-compose.yaml |
| Tempo | 3202 | Change `3202:3200` in docker-compose.yaml |
| Loki | 3100 | Change `3100:3100` in docker-compose.yaml |
| ClickHouse | 8123/9000 | Change in docker-compose.yaml |
| OTel Collector | 4317/4318 | Change in docker-compose.yaml + otel-collector-config.yaml |

---

## Azure OpenAI (Corporate Preferred)

Most corporate environments use Azure OpenAI instead of direct OpenAI:

```bash
# .env
AZURE_OPENAI_API_KEY=your-key
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
AZURE_OPENAI_API_VERSION=2024-02-01
```

Use `provider: "azure_openai"` in API calls:
```bash
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","provider":"azure_openai","messages":[{"role":"user","content":"Hello"}]}'
```

---

## Kubernetes Deployment

For corporate Kubernetes clusters (AKS, EKS, GKE):

```bash
# Apply namespace and configs
kubectl apply -f k8s/base/namespace.yaml
kubectl apply -f k8s/base/configmap.yaml

# Create secrets (replace with your values)
kubectl create secret generic llm-provider-keys \
  --namespace llm-o11y \
  --from-literal=OPENAI_API_KEY=sk-xxx \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-xxx \
  --from-literal=AZURE_OPENAI_API_KEY=xxx

# Deploy
kubectl apply -f k8s/base/gateway-deployment.yaml
kubectl apply -f k8s/base/otel-collector.yaml

# Verify
kubectl get pods -n llm-o11y
kubectl port-forward -n llm-o11y svc/llm-o11y-gateway 8080:8080
```

---

## Security Checklist

Before deploying in a corporate environment:

- [ ] Change default admin password (`admin/admin`)
- [ ] Configure HTTPS/TLS termination (via reverse proxy or ingress)
- [ ] Set `HTTP_PROXY` / `HTTPS_PROXY` if behind corporate proxy
- [ ] Mount corporate CA certificates if TLS inspection is used
- [ ] Restrict port exposure (only gateway port needs to be external)
- [ ] Configure virtual key budgets and rate limits
- [ ] Enable guardrails (PII detection + content safety)
- [ ] Review Grafana admin password (`llm-o11y`)
- [ ] Set up RBAC roles for team members
- [ ] Configure log retention policies

---

## Troubleshooting

### Proxy Issues

```bash
# Test proxy connectivity from gateway container
make proxy-test

# Or manually
docker compose exec gateway python -c "
import httpx
try:
    r = httpx.get('https://api.openai.com/v1/models', timeout=10)
    print(f'OpenAI reachable: {r.status_code}')
except Exception as e:
    print(f'Error: {e}')
"
```

### SSL Certificate Errors

```
ssl.SSLCertVerificationError: certificate verify failed
```

Solution: Mount your corporate CA certificate (see TLS section above).

### DNS Resolution

If services can't resolve external DNS:

```yaml
# Add to docker-compose.yaml under gateway service
gateway:
  dns:
    - 10.0.0.1        # Corporate DNS
    - 8.8.8.8         # Fallback
```

### Memory Limits

For resource-constrained environments:

```yaml
# Add to docker-compose.yaml per service
gateway:
  deploy:
    resources:
      limits:
        memory: 512M
        cpus: '0.5'
```
