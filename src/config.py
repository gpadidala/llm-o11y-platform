"""Central configuration for the LLM O11y Gateway.

All settings are loaded from environment variables or a .env file.
"""

from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings sourced from environment variables."""

    # --- Server ---
    gateway_port: int = 8080
    log_level: str = "info"

    # --- OpenAI ---
    openai_api_key: Optional[str] = None

    # --- Azure OpenAI ---
    azure_openai_api_key: Optional[str] = None
    azure_openai_endpoint: Optional[str] = None
    azure_openai_api_version: str = "2024-02-01"

    # --- Anthropic ---
    anthropic_api_key: Optional[str] = None

    # --- Vertex AI (Google) ---
    google_application_credentials: Optional[str] = None
    vertex_project_id: Optional[str] = None
    vertex_location: str = "us-central1"

    # --- AWS Bedrock ---
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_region: str = "us-east-1"

    # --- Cohere ---
    cohere_api_key: Optional[str] = None

    # --- OpenTelemetry ---
    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "llm-o11y-gateway"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
