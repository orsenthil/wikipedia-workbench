"""Single-user login: scrypt password hash, signed session cookie, Origin check.

The deployed app holds Wikipedia credentials, so every route except /login is
behind a login. Set EDITOR_LOCAL=1 to skip it on your own machine.
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import os
import secrets
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse

PUBLIC_PATHS = {"/login", "/favicon.ico"}
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=_N, r=_R, p=_P)
    return hmac.compare_digest(digest.hex(), digest_hex)


def is_local() -> bool:
    return os.environ.get("EDITOR_LOCAL") == "1"


def password_hash() -> str | None:
    return os.environ.get("APP_PASSWORD_HASH") or None


def session_secret() -> str:
    # Local runs get a throwaway secret; deployments must set one so sessions
    # survive restarts and can't be forged.
    return os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(32)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if origin is None:  # non-browser clients; SameSite cookies still apply
        return True
    return urlsplit(origin).netloc == request.headers.get("host")


async def require_login(request: Request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and not _same_origin(request):
        return PlainTextResponse("Cross-origin request refused", status_code=403)
    if is_local() or request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if not password_hash() or not os.environ.get("SESSION_SECRET"):
        return PlainTextResponse(
            "Login is not configured: set APP_PASSWORD_HASH and SESSION_SECRET "
            "(run `editor-hash-password`), or EDITOR_LOCAL=1 for local use.",
            status_code=503,
        )
    if request.session.get("user") != "owner":
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not logged in"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return await call_next(request)


def cli() -> None:
    """Print .env lines for APP_PASSWORD_HASH and SESSION_SECRET."""
    pw = getpass.getpass("App password: ")
    if pw != getpass.getpass("Again: "):
        raise SystemExit("Passwords don't match")
    if len(pw) < 12:
        raise SystemExit("Use at least 12 characters")
    print(f"APP_PASSWORD_HASH={hash_password(pw)}")
    print(f"SESSION_SECRET={secrets.token_urlsafe(32)}")
