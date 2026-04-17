"""User and role models for RBAC."""

from enum import Enum
from typing import Optional
from pydantic import BaseModel


class Role(str, Enum):
    ADMIN = "admin"       # Full access: user management, all settings, all features
    MANAGER = "manager"   # Manage keys, prompts, routing, guardrails -- no user management
    VIEWER = "viewer"     # Read-only: dashboard, logs, providers -- no mutations


class User(BaseModel):
    user_id: str
    username: str
    email: str
    password_hash: str  # SHA-256 of password with salt
    role: Role = Role.VIEWER
    display_name: str = ""
    avatar_color: str = "#a855f7"  # Default purple
    created_at: float = 0.0
    last_login: Optional[float] = None
    enabled: bool = True
    teams: list[str] = []


class Session(BaseModel):
    session_id: str
    user_id: str
    created_at: float
    expires_at: float
    ip_address: str = ""
