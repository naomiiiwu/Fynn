"""Live ledger connections: tokens that stay usable.

Everything that posts goes through here rather than reading tokens directly, so
there is one place that knows how to refresh an expired access token and store
the rotated refresh token. Both providers rotate on refresh, and dropping the
new refresh token loses the connection at the next expiry.
"""
from __future__ import annotations

from typing import Optional

from services import oauth
from services.database import delete_connection, load_connection, save_connection


class Connection:
    """One firm's authorisation for one ledger."""

    def __init__(self, firm_id: str, ledger: str, row: dict) -> None:
        self.firm_id = firm_id
        self.ledger = ledger
        self._row = dict(row)

    @property
    def org_id(self) -> str:
        return self._row.get("org_id", "")

    @property
    def org_name(self) -> str:
        return self._row.get("org_name", "")

    @property
    def connected_at(self) -> str:
        return self._row.get("connected_at", "")

    def access_token(self) -> str:
        """A token that is valid now, refreshing first if it is not."""
        if not oauth.is_expired(self._row.get("expires_at", "")):
            return self._row["access_token"]

        provider = oauth.get_provider(self.ledger)
        fresh = oauth.refresh(provider, self._row["refresh_token"])
        self._row.update(fresh)
        # Store immediately: the rotated refresh token is now the only one that
        # works, and losing it means the firm has to authorise again.
        save_connection(self.firm_id, self.ledger, {
            **fresh, "org_id": self.org_id, "org_name": self.org_name,
        })
        return self._row["access_token"]

    def to_dict(self) -> dict:
        return {
            "ledger": self.ledger,
            "org_id": self.org_id,
            "org_name": self.org_name,
            "connected_at": self.connected_at,
            "expires_at": self._row.get("expires_at", ""),
        }


def get(firm_id: str, ledger: str) -> Optional[Connection]:
    row = load_connection(firm_id, ledger)
    return Connection(firm_id, ledger, row) if row else None


def store(firm_id: str, ledger: str, tokens: dict) -> Connection:
    save_connection(firm_id, ledger, tokens)
    return Connection(firm_id, ledger, tokens)


def forget(firm_id: str, ledger: str) -> None:
    delete_connection(firm_id, ledger)


def status(firm_id: str) -> dict:
    """What each ledger's connection looks like, for the settings screen."""
    out = {}
    for name, provider in oauth.PROVIDERS.items():
        connection = get(firm_id, name)
        out[name] = {
            "label": provider.label,
            "configured": provider.configured,   # client id/secret present
            "connected": connection is not None,
            **(connection.to_dict() if connection else {}),
        }
    return out
