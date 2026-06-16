"""
Admin panel authentication using signed session cookies.
Credentials are read from environment variables.
"""
import secrets
from datetime import timedelta

from fastapi import Cookie, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, TimestampSigner

from app.core.config import get_settings

SESSION_COOKIE = "tbc_admin_session"
SESSION_MAX_AGE = int(timedelta(hours=8).total_seconds())


def _get_signer() -> TimestampSigner:
    return TimestampSigner(get_settings().secret_key)


def create_session(response: Response, username: str) -> None:
    """Set a signed session cookie."""
    signer = _get_signer()
    token = signer.sign(username).decode()
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )


def destroy_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def _verify_session(token: str) -> str:
    """Verify session cookie and return the username. Raises HTTPException on failure."""
    signer = _get_signer()
    try:
        username = signer.unsign(token, max_age=SESSION_MAX_AGE).decode()
        return username
    except SignatureExpired:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/admin/login"})
    except BadSignature:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/admin/login"})


def get_current_admin(request: Request) -> str:
    """
    FastAPI dependency: return the authenticated admin username.
    Redirects to login if not authenticated.
    """
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/admin/login"})
    return _verify_session(token)


def check_credentials(username: str, password: str) -> bool:
    """Constant-time credential check."""
    settings = get_settings()
    username_ok = secrets.compare_digest(username.encode(), settings.admin_username.encode())
    password_ok = secrets.compare_digest(password.encode(), settings.admin_password.encode())
    return username_ok and password_ok
