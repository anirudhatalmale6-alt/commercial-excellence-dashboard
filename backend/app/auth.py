"""
Authentication for the MVP.

Deliberately small: HMAC-signed stateless tokens, no session store, no external
dependency. Identity comes from the CRM user list, so there is no second user
directory to keep in sync.

For production this is the seam to replace with the corporate IdP: swap
`verify_credentials` for an OIDC code exchange and keep `current_user` as-is.
The rest of the app only ever asks "who is this request?" and gets a User back.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Header, HTTPException

from .models import Dataset, User

SECRET = os.getenv("APP_SECRET", "dev-secret-change-me").encode()
TOKEN_TTL = int(os.getenv("TOKEN_TTL_SECONDS", "43200"))  # 12h

# MVP demo credentials. Any user in the CRM can sign in with this password;
# replace with the IdP before this leaves the test environment.
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "demo1234")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(user_id: str) -> str:
    payload = {"sub": user_id, "exp": int(time.time()) + TOKEN_TTL}
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_token(token: str) -> str:
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=401, detail="malformed token")
    expected = _b64e(hmac.new(SECRET, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="bad signature")
    payload = json.loads(_b64d(body))
    if payload.get("exp", 0) < time.time():
        raise HTTPException(status_code=401, detail="token expired")
    return payload["sub"]


def verify_credentials(ds: Dataset, email: str, password: str) -> User:
    if password != DEMO_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    email = email.strip().lower()
    for u in ds.users:
        if u.email.lower() == email:
            return u
    raise HTTPException(status_code=401, detail="Invalid email or password")


def user_from_header(ds: Dataset, authorization: str | None) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    uid = read_token(authorization.split(" ", 1)[1].strip())
    for u in ds.users:
        if u.id == uid:
            return u
    raise HTTPException(status_code=401, detail="user no longer exists")


def auth_header(authorization: str | None = Header(default=None)) -> str | None:
    return authorization
