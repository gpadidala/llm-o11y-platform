#!/usr/bin/env bash
# =============================================================================
# AKS Deployment Script for LLM O11y Platform
# =============================================================================
# Creates Azure resources (RG, ACR, AKS) and deploys the platform manifests.
# Prerequisites: az CLI logged in, kubectl, helm
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-llm-o11y}"
LOCATION="${LOCATION:-eastus}"
ACR_NAME="${ACR_NAME:-acrllmo11y}"
AKS_CLUSTER="${AKS_CLUSTER:-aks-llm-o11y}"
AKS_NODE_COUNT="${AKS_NODE_COUNT:-3}"
AKS_NODE_VM_SIZE="${AKS_NODE_VM_SIZE:-Standard_D4s_v3}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
K8S_MANIFESTS_DIR="${K8S_MANIFESTS_DIR:-$(cd "$(dirname "$0")/../k8s/base" && pwd)}"

echo "============================================"
echo "  LLM O11y Platform — AKS Deployment"
echo "============================================"
echo "  Resource Group : ${RESOURCE_GROUP}"
echo "  Location       : ${LOCATION}"
echo "  ACR            : ${ACR_NAME}"
echo "  AKS Cluster    : ${AKS_CLUSTER}"
echo "  Manifests      : ${K8S_MANIFESTS_DIR}"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Create Resource Group
# ---------------------------------------------------------------------------
echo ">>> Step 1: Creating resource group '${RESOURCE_GROUP}' in '${LOCATION}'..."
az group create \
  --name "${RESOURCE_GROUP}" \
  --location "${LOCATION}" \
  --output table

# ---------------------------------------------------------------------------
# Step 2: Create Azure Container Registry and build/push image
# ---------------------------------------------------------------------------
echo ""
echo ">>> Step 2: Creating ACR '${ACR_NAME}'..."
az acr create \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${ACR_NAME}" \
  --sku Basic \
  --output table

echo ">>> Building and pushing image to ACR..."
az acr build \
  --registry "${ACR_NAME}" \
  --image "llm-o11y-gateway:${IMAGE_TAG}" \
  --file "$(dirname "$0")/../Dockerfile" \
  "$(dirname "$0")/.." \
  --output table

# ---------------------------------------------------------------------------
# Step 3: Create AKS Cluster
# ---------------------------------------------------------------------------
echo ""
echo ">>> Step 3: Creating AKS cluster '${AKS_CLUSTER}'..."
az aks create \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${AKS_CLUSTER}" \
  --node-count "${AKS_NODE_COUNT}" \
  --node-vm-size "${AKS_NODE_VM_SIZE}" \
  --attach-acr "${ACR_NAME}" \
  --enable-managed-identity \
  --generate-ssh-keys \
  --network-plugin azure \
  --network-policy azure \
  --output table

echo ">>> Getting AKS credentials..."
az aks get-credentials \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${AKS_CLUSTER}" \
  --overwrite-existing

# ---------------------------------------------------------------------------
# Step 4: Deploy Kubernetes manifests
# ---------------------------------------------------------------------------
echo ""
echo ">>> Step 4: Deploying Kubernetes manifests..."

# Apply in order: namespace, configmap, then workloads
kubectl apply -f "${K8S_MANIFESTS_DIR}/namespace.yaml"
kubectl apply -f "${K8S_MANIFESTS_DIR}/configmap.yaml"
kubectl apply -f "${K8S_MANIFESTS_DIR}/otel-collector.yaml"

# Update the gateway image to point to ACR
ACR_LOGIN_SERVER=$(az acr show --name "${ACR_NAME}" --query loginServer -o tsv)
kubectl apply -f "${K8S_MANIFESTS_DIR}/gateway-deployment.yaml"
kubectl set image deployment/llm-o11y-gateway \
  gateway="${ACR_LOGIN_SERVER}/llm-o11y-gateway:${IMAGE_TAG}" \
  -n llm-o11y

echo ""
echo ">>> Waiting for deployments to be ready..."
kubectl rollout status deployment/otel-collector -n llm-o11y --timeout=120s
kubectl rollout status deployment/llm-o11y-gateway -n llm-o11y --timeout=120s

# ---------------------------------------------------------------------------
# Step 5: Print access URLs
# ---------------------------------------------------------------------------
echo ""
echo "============================================"
echo "  Deployment Complete"
echo "============================================"

GATEWAY_IP=$(kubectl get svc llm-o11y-gateway -n llm-o11y -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "N/A (ClusterIP)")

echo ""
echo "  Gateway Service : ${GATEWAY_IP}:8080"
echo "  OTel Collector  : otel-collector.llm-o11y.svc:4317 (gRPC)"
echo "                    otel-collector.llm-o11y.svc:4318 (HTTP)"
echo ""
echo "  To port-forward the gateway locally:"
echo "    kubectl port-forward svc/llm-o11y-gateway 8080:8080 -n llm-o11y"
echo ""
echo "  To view pods:"
echo "    kubectl get pods -n llm-o11y"
echo ""
echo "============================================"
