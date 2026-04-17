"""User and session manager with JSON file persistence."""

import hashlib
import secrets
import time
import threading
import json
from pathlib import Path
from typing import Optional

from src.auth.models import User, Role, Session

# Avatar colour palette for new users
_AVATAR_COLORS = [
    "#a855f7", "#3b82f6", "#14b8a6", "#22c55e", "#f59e0b",
    "#f43f5e", "#ec4899", "#06b6d4", "#f97316", "#8b5cf6",
]

# Permission matrix: action -> set of roles that are allowed
_PERMISSIONS: dict[str, set[Role]] = {
    "view_dashboard":    {Role.ADMIN, Role.MANAGER, Role.VIEWER},
    "view_logs":         {Role.ADMIN, Role.MANAGER, Role.VIEWER},
    "view_providers":    {Role.ADMIN, Role.MANAGER, Role.VIEWER},
    "use_playground":    {Role.ADMIN, Role.MANAGER, Role.VIEWER},
    "manage_keys":       {Role.ADMIN, Role.MANAGER},
    "manage_prompts":    {Role.ADMIN, Role.MANAGER},
    "manage_guardrails": {Role.ADMIN, Role.MANAGER},
    "manage_routing":    {Role.ADMIN, Role.MANAGER},
    "run_eval":          {Role.ADMIN, Role.MANAGER},
    "manage_settings":   {Role.ADMIN},
    "manage_users":      {Role.ADMIN},
    "view_admin":        {Role.ADMIN},
}

SESSION_TTL = 86400  # 24 hours


def _hash_password(password: str, salt: str = "") -> str:
    """Hash a password with SHA-256 + salt."""
    if not salt:
        salt = secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return f"{salt}${digest}"


def _verify_password(password: str, password_hash: str) -> bool:
    """Verify a password against a stored hash."""
    if "$" not in password_hash:
        return False
    salt, stored_digest = password_hash.split("$", 1)
    check_digest = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    return secrets.compare_digest(stored_digest, check_digest)


class AuthManager:
    """Manage users, sessions, and RBAC."""

    def __init__(self, storage_path: str = ".data/users.json"):
        self._users: dict[str, User] = {}       # user_id -> User
        self._sessions: dict[str, Session] = {}  # session_id -> Session
        self._storage = Path(storage_path)
        self._lock = threading.Lock()
        self._start_time = time.time()
        self._load()
        self._ensure_default_admin()

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def _ensure_default_admin(self):
        """Create default admin user if no users exist."""
        if not self._users:
            self.create_user(
                username="admin",
                email="admin@llm-o11y.local",
                password="admin",
                role=Role.ADMIN,
                display_name="Administrator",
            )

    # ------------------------------------------------------------------
    # User CRUD
    # ------------------------------------------------------------------

    def create_user(
        self,
        username: str,
        email: str,
        password: str,
        role: Role = Role.VIEWER,
        display_name: str = "",
        teams: Optional[list[str]] = None,
    ) -> User:
        """Create a new user. Returns the User object."""
        with self._lock:
            # Check username uniqueness
            for u in self._users.values():
                if u.username.lower() == username.lower():
                    raise ValueError(f"Username already exists: {username}")

            # Generate user_id (usr_xxxx format)
            user_id = f"usr_{secrets.token_hex(8)}"
            while user_id in self._users:
                user_id = f"usr_{secrets.token_hex(8)}"

            # Pick avatar colour based on number of users
            color_idx = len(self._users) % len(_AVATAR_COLORS)

            user = User(
                user_id=user_id,
                username=username,
                email=email,
                password_hash=_hash_password(password),
                role=role,
                display_name=display_name or username,
                avatar_color=_AVATAR_COLORS[color_idx],
                created_at=time.time(),
                last_login=None,
                enabled=True,
                teams=teams or [],
            )
            self._users[user_id] = user
            self._save()
            return user

    def authenticate(self, username: str, password: str) -> Optional[str]:
        """Authenticate user. Returns session_id if valid, None if not."""
        with self._lock:
            user: Optional[User] = None
            for u in self._users.values():
                if u.username.lower() == username.lower():
                    user = u
                    break

            if user is None:
                return None

            if not user.enabled:
                return None

            if not _verify_password(password, user.password_hash):
                return None

            # Create session
            session_id = f"sess_{secrets.token_hex(24)}"
            now = time.time()
            session = Session(
                session_id=session_id,
                user_id=user.user_id,
                created_at=now,
                expires_at=now + SESSION_TTL,
                ip_address="",
            )
            self._sessions[session_id] = session

            # Update last_login
            user.last_login = now
            self._save()

            return session_id

    def get_session_user(self, session_id: str) -> Optional[User]:
        """Get user from session_id. Returns None if expired/invalid."""
        session = self._sessions.get(session_id)
        if session is None:
            return None

        # Check expiry
        if time.time() > session.expires_at:
            self._sessions.pop(session_id, None)
            return None

        user = self._users.get(session.user_id)
        if user is None or not user.enabled:
            self._sessions.pop(session_id, None)
            return None

        return user

    def logout(self, session_id: str):
        """Invalidate a session."""
        self._sessions.pop(session_id, None)

    def list_users(self) -> list[dict]:
        """List all users (passwords redacted)."""
        result = []
        for u in self._users.values():
            d = u.model_dump()
            d["password_hash"] = "***"
            result.append(d)
        return result

    def get_user(self, user_id: str) -> Optional[User]:
        """Get user by ID."""
        return self._users.get(user_id)

    def update_user(
        self,
        user_id: str,
        role: Optional[Role] = None,
        enabled: Optional[bool] = None,
        display_name: Optional[str] = None,
        teams: Optional[list[str]] = None,
        email: Optional[str] = None,
    ) -> Optional[User]:
        """Update user properties."""
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return None

            if role is not None:
                user.role = role
            if enabled is not None:
                user.enabled = enabled
            if display_name is not None:
                user.display_name = display_name
            if teams is not None:
                user.teams = teams
            if email is not None:
                user.email = email

            self._save()
            return user

    def delete_user(self, user_id: str) -> bool:
        """Delete a user and all their sessions."""
        with self._lock:
            if user_id not in self._users:
                return False

            del self._users[user_id]

            # Remove associated sessions
            to_remove = [
                sid for sid, s in self._sessions.items()
                if s.user_id == user_id
            ]
            for sid in to_remove:
                del self._sessions[sid]

            self._save()
            return True

    def change_password(self, user_id: str, new_password: str) -> bool:
        """Change user password."""
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return False
            user.password_hash = _hash_password(new_password)
            self._save()
            return True

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def list_sessions(self) -> list[dict]:
        """List all active (non-expired) sessions with user info."""
        now = time.time()
        result = []
        expired = []
        for sid, session in self._sessions.items():
            if now > session.expires_at:
                expired.append(sid)
                continue
            user = self._users.get(session.user_id)
            result.append({
                "session_id": session.session_id,
                "user_id": session.user_id,
                "username": user.username if user else "unknown",
                "display_name": user.display_name if user else "unknown",
                "role": user.role.value if user else "unknown",
                "created_at": session.created_at,
                "expires_at": session.expires_at,
                "ip_address": session.ip_address,
            })
        # Clean up expired sessions
        for sid in expired:
            self._sessions.pop(sid, None)
        return result

    def revoke_session(self, session_id: str) -> bool:
        """Revoke a specific session."""
        return self._sessions.pop(session_id, None) is not None

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    def check_permission(self, user: User, action: str) -> bool:
        """Check if user has permission for an action.

        Permission matrix:
        Action                  | Admin | Manager | Viewer
        -------------------------|-------|---------|-------
        view_dashboard           | yes   | yes     | yes
        view_logs                | yes   | yes     | yes
        view_providers           | yes   | yes     | yes
        use_playground           | yes   | yes     | yes
        manage_keys              | yes   | yes     | no
        manage_prompts           | yes   | yes     | no
        manage_guardrails        | yes   | yes     | no
        manage_routing           | yes   | yes     | no
        run_eval                 | yes   | yes     | no
        manage_settings          | yes   | no      | no
        manage_users             | yes   | no      | no
        view_admin               | yes   | no      | no
        """
        allowed_roles = _PERMISSIONS.get(action)
        if allowed_roles is None:
            return False
        return user.role in allowed_roles

    # ------------------------------------------------------------------
    # System info
    # ------------------------------------------------------------------

    def get_system_info(self) -> dict:
        """Get system info for admin page."""
        now = time.time()
        role_counts = {"admin": 0, "manager": 0, "viewer": 0}
        for u in self._users.values():
            role_counts[u.role.value] = role_counts.get(u.role.value, 0) + 1

        active_sessions = sum(
            1 for s in self._sessions.values()
            if now < s.expires_at
        )

        return {
            "total_users": len(self._users),
            "active_sessions": active_sessions,
            "roles_distribution": role_counts,
            "platform_version": "0.2.0",
            "uptime_seconds": round(now - self._start_time),
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self):
        """Save users to JSON file on disk."""
        try:
            self._storage.parent.mkdir(parents=True, exist_ok=True)
            data = {
                uid: user.model_dump()
                for uid, user in self._users.items()
            }
            self._storage.write_text(json.dumps(data, indent=2, default=str))
        except Exception:
            pass  # Fail silently -- sessions are in-memory anyway

    def _load(self):
        """Load users from JSON file on disk."""
        try:
            if self._storage.exists():
                raw = json.loads(self._storage.read_text())
                for uid, udata in raw.items():
                    self._users[uid] = User(**udata)
        except Exception:
            self._users = {}


# Module-level singleton
auth_manager = AuthManager()
