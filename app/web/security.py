"""Cookie session + CSRF helpers for the server-rendered (Jinja2) UI.

The session is the same JWT `create_access_token`/`decode_access_token` already
produce — just carried in an HttpOnly cookie instead of an `Authorization` header, so
`app/core/security.py` needs no changes at all.

CSRF uses the double-submit pattern: a second, non-HttpOnly cookie holds an HMAC of the
session token (so it's unforgeable without the JWT secret, and needs no server-side
session store). Every state-changing form includes the same value as a hidden field or
`X-CSRF-Token` header; a cross-site attacker can't read that cookie to copy it.
"""

import hashlib
import hmac

from fastapi import HTTPException, Request
from fastapi.responses import Response

from app.core.config import get_settings

SESSION_COOKIE = "agentcare_session"
CSRF_COOKIE = "csrf_token"


def csrf_token_for(session_token: str) -> str:
    settings = get_settings()
    return hmac.new(
        settings.jwt_secret.encode("utf-8"), session_token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def set_session_cookies(response: Response, *, token: str) -> None:
    max_age = get_settings().jwt_expire_minutes * 60
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", secure=False, max_age=max_age, path="/"
    )
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token_for(token),
        httponly=False,
        samesite="lax",
        secure=False,
        max_age=max_age,
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


async def verify_csrf(request: Request) -> None:
    cookie_value = request.cookies.get(CSRF_COOKIE)
    submitted = request.headers.get("X-CSRF-Token")
    if not submitted:
        form = await request.form()
        value = form.get("csrf_token")
        submitted = value if isinstance(value, str) else None
    if not cookie_value or not submitted or not hmac.compare_digest(cookie_value, submitted):
        raise HTTPException(
            status_code=403, detail="Your session looks out of date. Please refresh and try again."
        )
