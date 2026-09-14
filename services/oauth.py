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
    client_id_env: str
    client_secret_env: str
    # Scope sets to try, best first. A provider may not accept the first: Xero
    # replaced its broad scopes with granular ones, and an app created after
    # 2 March 2026 can only request the new names while an older app keeps the
    # old ones until September 2027. Neither set works everywhere, so the right
    # answer is to ask and find out rather than to pick one and be wrong for
    # half the accounts.
    scope_tiers: tuple = ()
    # Holding any one of these means the connection may write. More than one
    # because the same permission has two names during the changeover.
    post_scopes: tuple = ()

    @property
    def scopes(self) -> str:
        """The preferred set. Used where one has to be named."""
        return self.scope_tiers[0] if self.scope_tiers else ""

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
        scope_tiers=(
            # Granular, and able to post. What a Xero app created from March
            # 2026 onwards can request.
            #
            # Deliberately without app.connections: it is listed in the
            # developer portal but asking for it is answered access_denied, and
            # the connections endpoint works without it.
            "offline_access openid profile email "
            "accounting.settings accounting.invoices",
            # Broad, and able to post. What an older app still has.
            "offline_access openid profile email "
            "accounting.settings accounting.transactions",
            # Read only, either vocabulary: enough to import the chart of
            # accounts and finish the mapping, which is most of setup.
            "offline_access openid profile email accounting.settings.read",
            "offline_access openid profile email accounting.settings",
        ),
        post_scopes=("accounting.invoices", "accounting.transactions"),
        client_id_env="XERO_CLIENT_ID",
        client_secret_env="XERO_CLIENT_SECRET",
    ),
    "quickbooks": Provider(
        name="quickbooks",
        label="QuickBooks Online",
        authorize_url="https://appcenter.intuit.com/connect/oauth2",
        token_url="https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        # QuickBooks has one accounting scope covering both reading and
        # writing, so there is nothing to fall back to — a reduced connection
        # would be no connection.
        scope_tiers=("com.intuit.quickbooks.accounting",),
        post_scopes=("com.intuit.quickbooks.accounting",),
        client_id_env="QBO_CLIENT_ID",
        client_secret_env="QBO_CLIENT_SECRET",
    ),
}


def can_post(provider: Provider, granted: str) -> bool:
    """Whether a connection holding `granted` may write.

    An empty `granted` means the scopes were never recorded — a connection made
    before Fynn tracked them, or one whose scopes the database would not store.
    Those are assumed capable rather than locked out; the ledger refuses them
    at the point of posting and the adapter explains why.
    """
    if not provider.post_scopes or not granted:
        return True
    held = set(granted.split())
    return any(scope in held for scope in provider.post_scopes)


def get_provider(name: str) -> Provider:
    provider = PROVIDERS.get((name or "").lower())
    if provider is None:
        raise OAuthError(f"Unknown ledger {name!r}.")
    return provider


def redirect_uri(base_url: str, provider: Provider) -> str:
    """Must match the redirect URI registered with the provider, exactly."""
    return f"{base_url.rstrip('/')}/oauth/{provider.name}/callback"


def authorize_url(
    provider: Provider, base_url: str, state: str, scopes: Optional[str] = None
) -> str:
    if not provider.configured:
        raise OAuthError(
            f"{provider.label} is not configured. Set {provider.client_id_env} and "
            f"{provider.client_secret_env} in the environment."
        )
    return provider.authorize_url + "?" + urlencode({
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri(base_url, provider),
        "scope": scopes or provider.scopes,
        "state": state,
    })


def preflight(provider: Provider, url: str) -> Optional[str]:
    """Why the provider will refuse this authorize request, or None to proceed.

    A provider validates the redirect URI *before* showing a consent screen, and
    renders the refusal on its own error page. There is no redirect back, so the
    accountant is left on a page belonging to someone else, with an opaque error
    id and no way home. Asking first costs one request on a button press and
    keeps the failure inside Fynn, where it can say what to do about it.

    Fails open on purpose. A preflight that cannot reach the provider, or that
    sees a page it does not recognise, returns None and lets the real attempt
    happen: blocking a working connection because a check was inconclusive is
    worse than the dead end this avoids.
    """
    if provider.name != "xero":
        # Only Xero's refusals have been observed. Guessing at another
        # provider's shapes risks blocking a valid connection.
        return None

    # Walked by hand rather than followed, because a refusal arrives in two
    # different places. An invalid redirect URI or scope lands on Xero's own
    # error page; a denied one comes back to *our* redirect URI carrying
    # ?error=access_denied, which following redirects blindly would swallow —
    # it did, and a scope set that Xero refuses outright looked acceptable.
    from urllib.parse import parse_qs, urlparse

    seen = url
    try:
        for _ in range(8):
            response = httpx.get(seen, follow_redirects=False, timeout=10)
            if response.status_code not in (301, 302, 303, 307, 308):
                return None            # a login or consent page: nothing refused
            seen = str(httpx.URL(seen).join(response.headers.get("location", "")))

            if "/identity/error" in seen:
                return _error_page(httpx.get(seen, follow_redirects=True, timeout=10))

            query = parse_qs(urlparse(seen).query)
            if query.get("error"):
                return _denial(query["error"][0])
            if query.get("code"):
                return None            # Xero would grant this
    except Exception:
        return None
    return None


def _denial(error: str) -> str:
    """Xero's ?error= values, in words that name the thing to change."""
    if error == "access_denied":
        return ("access_denied — Xero refused one of the permissions asked for. "
                "This is the app's scope list, not your sign-in")
    return error


def _error_page(response) -> Optional[str]:
    """The reason off Xero's error page; its URL carries only an opaque id."""
    import html as _html
    import re
    text = re.sub(r"<(script|style).*?</\1>", "", response.text, flags=re.S)
    text = " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    match = re.search(r"Error:\s*(\S+)\s+([A-Z][^.]{0,60}?)\s+Error code", text)
    if match:
        return f"{match.group(2)} ({match.group(1)})"
    return "the request was refused"


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
    provider: Provider,
    code: str,
    base_url: str,
    realm_id: Optional[str] = None,
    requested_scopes: Optional[str] = None,
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
        # What the connection may actually do. The provider's own answer is
        # authoritative — a user can decline part of a request — so prefer it,
        # and fall back to what we asked for only if it says nothing.
        "scopes": payload.get("scope") or requested_scopes or provider.scopes,
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


def refresh(provider: Provider, refresh_token: str, known_scopes: str = "") -> dict:
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
        # A refresh must not silently widen or narrow what the connection can
        # do; carry the known scopes forward when the provider omits them.
        "scopes": payload.get("scope") or known_scopes,
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
