"""OAuth 2.0 for Xero and QuickBooks Online.

Both are the standard authorization-code flow, differing in details that matter:

  Xero          Access tokens last 30 minutes. The token response says nothing
                about which organisation was authorised, so the connections
                endpoint is called afterwards to find the tenant id, which every
                subsequent request must carry in a header.
                <https://developer.xero.com/documentation/guides/oauth2/auth-flow/>

  QuickBooks    The company — Intuit calls it a realm — comes back as a query
                parameter on the redirect rather than from an API call, and is
                part of the request path thereafter.
                <https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0>

Refresh tokens are the valuable half: an access token expiring is routine, a
lost refresh token means the firm has to authorise again. They are stored per
firm per ledger in services/database.

Client credentials come from the environment and are never logged, returned by
an endpoint, or sent to a browser.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx


class OAuthError(RuntimeError):
    """Anything that stops a connection being made or kept."""


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    authorize_url: str
    token_url: str
    scopes: str
    client_id_env: str
    client_secret_env: str

    @property
    def client_id(self) -> str:
        return os.getenv(self.client_id_env, "").strip()

    @property
    def client_secret(self) -> str:
        return os.getenv(self.client_secret_env, "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


PROVIDERS: dict[str, Provider] = {
    "xero": Provider(
        name="xero",
        label="Xero",
        authorize_url="https://login.xero.com/identity/connect/authorize",
        token_url="https://identity.xero.com/connect/token",
        # offline_access is what makes a refresh token come back at all.
        scopes="offline_access openid profile email accounting.transactions accounting.settings",
        client_id_env="XERO_CLIENT_ID",
        client_secret_env="XERO_CLIENT_SECRET",
    ),
    "quickbooks": Provider(
        name="quickbooks",
        label="QuickBooks Online",
        authorize_url="https://appcenter.intuit.com/connect/oauth2",
        token_url="https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        scopes="com.intuit.quickbooks.accounting",
        client_id_env="QBO_CLIENT_ID",
        client_secret_env="QBO_CLIENT_SECRET",
    ),
}


def get_provider(name: str) -> Provider:
    provider = PROVIDERS.get((name or "").lower())
    if provider is None:
        raise OAuthError(f"Unknown ledger {name!r}.")
    return provider


def redirect_uri(base_url: str, provider: Provider) -> str:
    """Must match the redirect URI registered with the provider, exactly."""
    return f"{base_url.rstrip('/')}/oauth/{provider.name}/callback"


def authorize_url(provider: Provider, base_url: str, state: str) -> str:
    if not provider.configured:
        raise OAuthError(
            f"{provider.label} is not configured. Set {provider.client_id_env} and "
            f"{provider.client_secret_env} in the environment."
        )
    return provider.authorize_url + "?" + urlencode({
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri(base_url, provider),
        "scope": provider.scopes,
        "state": state,
    })


def _basic_auth(provider: Provider) -> str:
    raw = f"{provider.client_id}:{provider.client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _token_request(provider: Provider, form: dict) -> dict:
    """Both providers authenticate the token call with HTTP Basic."""
    try:
        response = httpx.post(
            provider.token_url,
            data=form,
            headers={
                "Authorization": _basic_auth(provider),
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=30,
        )
    except Exception as exc:
        raise OAuthError(f"Could not reach {provider.label}: {exc}") from exc

    if response.status_code >= 400:
        # The body names the actual problem — a redirect_uri that does not match
        # what was registered, most often — and is worth surfacing verbatim.
        raise OAuthError(
            f"{provider.label} rejected the token request "
            f"({response.status_code}): {response.text[:300]}"
        )
    payload = response.json()
    if "access_token" not in payload or "refresh_token" not in payload:
        raise OAuthError(
            f"{provider.label} returned no refresh token. For Xero this means the "
            "offline_access scope was not granted."
        )
    return payload


def _expiry(payload: dict) -> str:
    seconds = int(payload.get("expires_in", 1800))
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def exchange_code(
    provider: Provider, code: str, base_url: str, realm_id: Optional[str] = None
) -> dict:
    """Swap the one-time code for tokens, and work out which organisation."""
    payload = _token_request(provider, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(base_url, provider),
    })
    tokens = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": _expiry(payload),
        "org_id": "",
        "org_name": "",
    }

    if provider.name == "xero":
        # Xero's token says nothing about the organisation; ask which tenants
        # this authorisation covers.
        org_id, org_name = _xero_tenant(tokens["access_token"])
        tokens["org_id"], tokens["org_name"] = org_id, org_name
    else:
        # Intuit puts the company id on the redirect itself.
        if not realm_id:
            raise OAuthError(
                "QuickBooks did not return a realmId on the redirect, so there is "
                "no company to post to."
            )
        tokens["org_id"] = realm_id
    return tokens


def _xero_tenant(access_token: str) -> tuple[str, str]:
    try:
        response = httpx.get(
            "https://api.xero.com/connections",
            headers={"Authorization": f"Bearer {access_token}",
                     "Accept": "application/json"},
            timeout=30,
        )
        response.raise_for_status()
        connections = response.json()
    except Exception as exc:
        raise OAuthError(f"Could not read Xero connections: {exc}") from exc

    if not connections:
        raise OAuthError("No Xero organisation was connected.")
    first = connections[0]
    return first.get("tenantId", ""), first.get("tenantName", "")


def refresh(provider: Provider, refresh_token: str) -> dict:
    """Trade a refresh token for a new pair.

    Both providers rotate the refresh token, so the new one has to be stored or
    the connection is lost at the next expiry.
    """
    payload = _token_request(provider, {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    })
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", refresh_token),
        "expires_at": _expiry(payload),
    }


def is_expired(expires_at: str, margin_seconds: int = 120) -> bool:
    """True when the token has expired, or is close enough that it might.

    The margin covers the round trip: a token with ten seconds left will expire
    mid-request, and a failed post is worse than an unnecessary refresh.
    """
    try:
        expiry = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) + timedelta(seconds=margin_seconds) >= expiry
