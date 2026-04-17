"""FastAPI middleware for session-based auth."""

from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse
from typing import Optional

from src.auth.manager import auth_manager
from src.auth.models import User, Role

# Pages that don't require auth
PUBLIC_PATHS = {
    "/", "/health", "/metrics", "/login", "/static",
    "/docs", "/openapi.json", "/redoc",
}


async def get_current_user(request: Request) -> Optional[User]:
    """Extract user from session cookie. Returns None if not logged in."""
    session_id = request.cookies.get("session_id")
    if not session_id:
        return None
    return auth_manager.get_session_user(session_id)


def require_auth(request: Request) -> User:
    """Require authentication. Raises HTTPException 401 if not logged in."""
    session_id = request.cookies.get("session_id")
    if not session_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = auth_manager.get_session_user(session_id)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    return user


def require_role(user: User, *roles: Role):
    """Require specific role. Raises HTTPException 403 if insufficient permissions."""
    if user.role not in roles:
        raise HTTPException(
            status_code=403,
            detail=f"Insufficient permissions. Required role: {', '.join(r.value for r in roles)}",
        )
