-- Fynn — Migration 009: OAuth tokens for Xero and QuickBooks Online.
--
-- One row per firm per ledger. The refresh token is the valuable one: access
-- tokens last 30 minutes (Xero) or an hour (QBO), so every call refreshes as
-- needed, and losing the refresh token means the firm has to authorise again.
--
-- These are secrets at rest. The table is only reachable with the service_role
-- key, which the application holds and no browser ever sees. Encrypting them
-- properly wants a key-management story Fynn does not have yet.

create table if not exists ledger_connections (
    firm_id       text not null references firm_profiles(workspace_id) on delete cascade,
    ledger        text not null,                 -- 'xero' | 'quickbooks'
    access_token  text not null,
    refresh_token text not null,
    expires_at    timestamptz not null,          -- of the access token
    -- Which organisation the tokens are for: Xero calls it a tenant, QuickBooks
    -- a realm. One column, because a connection only ever has one.
    org_id        text not null default '',
    org_name      text not null default '',
    connected_at  timestamptz not null default now(),
    primary key (firm_id, ledger)
);
