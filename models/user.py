"""One person with an account, and the workspace they own.

A user's id *is* their workspace id. Every scoped table already keys on
`firm_id`, so nothing in the schema had to learn about users — the constant
"workspace" simply becomes the signed-in person's id, and two accounts can no
more see each other's rules than two rows can merge on their own.

Identity is one of two kinds, and both may be true at once:

  password    An email address and a scrypt hash.
  Google      A Google subject id, which never changes for an account even when
              the address on it does — which is why it, and not the email, is
              what a Google sign-in is matched on.

An email address is unique across users regardless of how they sign in, so
signing in with Google after registering a password lands in the same
workspace rather than quietly creating a second one.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class User:
    id: str
    email: str
    name: str = ""
    password_hash: str = ""      # empty for a Google-only account
    google_sub: str = ""         # empty until Google is used
    created_at: str = field(default_factory=_now)

    @property
    def workspace_id(self) -> str:
        """A user owns exactly one workspace, and it is theirs alone."""
        return self.id

    def display(self) -> str:
        return self.name.strip() or self.email

    def to_dict(self) -> dict:
        # Never the hash, and never the Google subject id: this crosses to a
        # browser, and neither is anything a browser needs.
        return {"id": self.id, "email": self.email, "name": self.name,
                "has_password": bool(self.password_hash),
                "has_google": bool(self.google_sub)}

    def to_row(self) -> dict:
        return {"id": self.id, "email": self.email, "name": self.name,
                "password_hash": self.password_hash, "google_sub": self.google_sub,
                "created_at": self.created_at}

    @classmethod
    def from_row(cls, row: dict) -> "User":
        return cls(
            id=row["id"],
            email=(row.get("email") or "").strip().lower(),
            name=row.get("name") or "",
            password_hash=row.get("password_hash") or "",
            google_sub=row.get("google_sub") or "",
            created_at=row.get("created_at") or _now(),
        )


def new_id() -> str:
    return secrets.token_hex(16)


class UserStore:
    """Users, in memory and written through to storage.

    The in-memory copy is the working set; the database is the durable one. With
    Supabase unconfigured this is purely in-memory, which is what local
    development runs on — and which means accounts do not survive a restart, so
    the diagnostics screen says as much.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, User] = {}
        self._loaded = False

    # -- reading ---------------------------------------------------------------

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            from services.database import load_users
            for row in load_users():
                user = User.from_row(row)
                self._by_id[user.id] = user
        except Exception as exc:  # pragma: no cover - storage is best-effort
            print(f"  [Users] Could not load users: {exc}")

    def get(self, user_id: str) -> Optional[User]:
        self._load()
        return self._by_id.get(user_id)

    def by_email(self, email: str) -> Optional[User]:
        self._load()
        wanted = (email or "").strip().lower()
        return next((u for u in self._by_id.values() if u.email == wanted), None)

    def by_google(self, sub: str) -> Optional[User]:
        self._load()
        return next((u for u in self._by_id.values() if u.google_sub and
                     u.google_sub == sub), None)

    def count(self) -> int:
        self._load()
        return len(self._by_id)

    # -- writing ---------------------------------------------------------------

    def save(self, user: User) -> User:
        self._load()
        self._by_id[user.id] = user
        try:
            from services.database import save_user
            save_user(user.to_row())
        except Exception as exc:  # pragma: no cover - storage is best-effort
            print(f"  [Users] Could not persist {user.email}: {exc}")
        return user

    def create(self, email: str, *, name: str = "", password_hash: str = "",
               google_sub: str = "") -> User:
        user = User(id=new_id(), email=(email or "").strip().lower(), name=name,
                    password_hash=password_hash, google_sub=google_sub)
        return self.save(user)
