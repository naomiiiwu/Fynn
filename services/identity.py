"""Who is signed in.

Three pieces, deliberately small:

  passwords   Hashed with scrypt from the standard library. Not a choice of
              taste — a password database is the thing you most regret storing
              carelessly, and scrypt is memory-hard, so a stolen table cannot be
              run through a GPU the way a SHA-256 table can. Every hash carries
              its own salt and its own parameters, so the cost can be raised
              later without invalidating what is already stored.

  sessions    A signed cookie naming the user and an expiry. Keyed on a server
              secret rather than on anyone's password, so changing a password
              does not sign everyone else out — and so the secret can be rotated
              deliberately when it needs to be.

  Google      The same authorization-code flow the ledgers use, for identity
              rather than for access. Google is asked who the person is; nothing
              is kept but their subject id, email and name.

Nothing here decides what a user may see. That is workspace scoping, and it
lives in main.py where the data does.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional
from urllib.parse import urlencode

import httpx


class AuthError(RuntimeError):
    """A sign-in that cannot be completed, with a reason safe to show."""


# ── Passwords ─────────────────────────────────────────────────────────────────

# Tuned so a single verification costs a browser-imperceptible fraction of a
# second on a small server, while a brute-force run costs an attacker real
# memory. Stored per hash, so raising them later is a one-line change that
# leaves existing hashes verifiable.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1

MIN_PASSWORD = 10


def hash_password(password: str) -> str:
    """A self-describing hash: algorithm, cost, salt and digest in one string."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return "$".join(["scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
                     salt.hex(), digest.hex()])


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check against a stored hash, false for anything malformed."""
    try:
        scheme, n, r, p, salt_hex, digest_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest_hex)),
        )
    except Exception:
        return False
    return hmac.compare_digest(candidate, bytes.fromhex(digest_hex))


def password_problem(password: str) -> Optional[str]:
    """Why this password is not acceptable, or None.

    Length only. Composition rules push people towards `Password1!` and away
    from the long ordinary phrases that are actually harder to guess.
    """
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    return None


# ── Sessions ──────────────────────────────────────────────────────────────────

SESSION_DAYS = 14


def _secret() -> bytes:
    """The key sessions are signed with.

    Falls back to a per-process random value when unset, which is correct but
    unhelpful: every restart signs everyone out. It says so rather than failing
    silently, because a deployment that logs people out at random is a bug that
    is hard to attribute later.
    """
    configured = os.getenv("FYNN_SECRET", "").strip()
    if configured:
        return configured.encode()
    global _EPHEMERAL
    if _EPHEMERAL is None:
        _EPHEMERAL = secrets.token_bytes(32)
        print("  [Auth] FYNN_SECRET is not set — sessions will not survive a "
              "restart. Set it to a long random string in the environment.")
    return _EPHEMERAL


_EPHEMERAL: Optional[bytes] = None


def issue_session(user_id: str, days: int = SESSION_DAYS) -> str:
    expires = int(time.time()) + days * 86400
    body = f"{user_id}.{expires}"
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def read_session(token: str) -> Optional[str]:
    """The user id this token vouches for, or None if it vouches for nothing."""
    try:
        user_id, expires, signature = token.rsplit(".", 2)
    except ValueError:
        return None
    expected = hmac.new(
        _secret(), f"{user_id}.{expires}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        if int(expires) < time.time():
            return None
    except ValueError:
        return None
    return user_id


# ── Google ────────────────────────────────────────────────────────────────────

GOOGLE_AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_SCOPES = "openid email profile"


def google_configured() -> bool:
    return bool(os.getenv("GOOGLE_CLIENT_ID", "").strip()
                and os.getenv("GOOGLE_CLIENT_SECRET", "").strip())


def google_redirect_uri(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/auth/google/callback"


def google_authorize_url(base_url: str, state: str) -> str:
    if not google_configured():
        raise AuthError("Google sign-in is not configured on this deployment.")
    return GOOGLE_AUTHORIZE + "?" + urlencode({
        "response_type": "code",
        "client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip(),
        "redirect_uri": google_redirect_uri(base_url),
        "scope": GOOGLE_SCOPES,
        "state": state,
        # Identity only: no refresh token is wanted, and none is asked for.
        "access_type": "online",
        "prompt": "select_account",
    })


def google_identity(code: str, base_url: str) -> dict:
    """Trade the code for the person's identity: {sub, email, name}.

    The access token is used once, here, to ask Google who this is, and is then
    discarded — Fynn holds no ongoing access to anyone's Google account.
    """
    try:
        response = httpx.post(GOOGLE_TOKEN, data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
            "redirect_uri": google_redirect_uri(base_url),
        }, headers={"Accept": "application/json"}, timeout=30)
    except Exception as exc:
        raise AuthError(f"Could not reach Google: {exc}") from exc
    if response.status_code >= 400:
        raise AuthError(f"Google rejected the sign-in ({response.status_code}): "
                        f"{response.text[:200]}")

    access_token = response.json().get("access_token", "")
    if not access_token:
        raise AuthError("Google returned no access token.")

    try:
        info = httpx.get(GOOGLE_USERINFO,
                         headers={"Authorization": f"Bearer {access_token}"},
                         timeout=30)
        info.raise_for_status()
        claims = info.json()
    except Exception as exc:
        raise AuthError(f"Could not read the Google profile: {exc}") from exc

    # An unverified address must not be able to claim a workspace: Google will
    # hand back whatever the account says until the address is confirmed.
    if not claims.get("email_verified", False):
        raise AuthError("That Google account's email address is not verified.")
    email = (claims.get("email") or "").strip().lower()
    if not claims.get("sub") or not email:
        raise AuthError("Google did not return an account to sign in as.")
    return {"sub": claims["sub"], "email": email, "name": claims.get("name", "")}


def new_state() -> str:
    return secrets.token_urlsafe(24)


def b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
