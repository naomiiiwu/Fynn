-- Fynn — the whole schema, for a fresh Supabase project.
--
-- Paste this into the Supabase SQL editor and run it once. It is 005 + 007
-- through 016 already applied, so a new project does not have to replay a
-- rename.
--
-- On a database that already has the 005 schema, run 007 through 016 instead
-- of this file; `create table if not exists` would skip the existing tables and
-- leave firm_profiles keyed by the old `phone` column.
--
-- Keep this file in step when a migration adds a table. It was the source of a
-- real gap: a project built from an earlier copy of this file was missing
-- ledger_connections, so authorising Xero succeeded and stored nothing.
--
-- Migrations 001-004 belong to the earlier seller-facing P&L product and are
-- kept only as history. Nothing here depends on them.

-- One row per person with an account. A user's id IS their workspace id, which
-- is how every table below became per-account without learning about users:
-- the firm_id they already key on is simply the signed-in person's id.
create table if not exists users (
    id            text primary key,
    -- Unique however they sign in, so signing in with Google after registering
    -- a password lands in the existing workspace rather than a second one.
    email         text not null unique,
    name          text not null default '',
    -- scrypt, self-describing: algorithm, cost, salt and digest. Empty for an
    -- account that only ever signs in with Google.
    password_hash text not null default '',
    -- Google's subject id, which survives the user changing their address.
    google_sub    text not null default '',
    created_at    timestamptz not null default now()
);

create unique index if not exists idx_users_google_sub
    on users (google_sub) where google_sub <> '';

-- One row per workspace, keyed by its owner's user id. Every other table hangs
-- off it, which is what makes deleting a workspace a single statement.
-- Deliberately not foreign-keyed to users: a deployment that predates accounts
-- has a row here under the literal id 'workspace', and that history should stay
-- readable rather than being deleted by a constraint.
create table if not exists firm_profiles (
    workspace_id    text primary key,
    firm            text not null default 'Your firm',
    actor           text not null default '',      -- who signs off, named in the trail
    platforms       jsonb not null default '["Shopee","Lazada","TikTok Shop"]'::jsonb,
    ledger          text not null default 'dry-run',
    onboarding_step text,                          -- null = setup complete
    -- The cycle being worked on, or null when none is open. The reconciliation
    -- itself is in memory; this is what says whether to rebuild it from the
    -- retained files after a restart.
    open_cycle      text,
    -- Send a settlement to the ledger as soon as it is ready, without a click.
    auto_post       boolean not null default false,
    updated_at      timestamptz not null default now()
);

-- The accumulating asset: one row per label, or per classification, that a firm
-- has decided how to treat. The unique constraint lets a re-decision overwrite
-- the earlier treatment rather than leaving two rules that disagree.
--
-- `platform` is '*' rather than null for a rule that applies everywhere:
-- Postgres treats nulls as distinct in a unique constraint, so a nullable
-- column here would let duplicate firm-wide rules accumulate silently.
create table if not exists firm_rules (
    id         bigint generated always as identity primary key,
    firm_id    text not null references firm_profiles(workspace_id) on delete cascade,
    platform   text not null default '*',
    label      text not null,
    account    text not null,
    side       text not null check (side in ('debit', 'credit')),
    decided_by text,
    decided_at timestamptz,
    unique (firm_id, platform, label)
);

-- Fynn's account names against the firm's own chart of accounts. Keyed by
-- ledger as well as firm: a Xero AccountCode and a QuickBooks account Id are
-- not interchangeable, so switching ledgers means mapping again rather than
-- posting to whatever account happens to share a number.
create table if not exists account_mappings (
    id           bigint generated always as identity primary key,
    firm_id      text not null references firm_profiles(workspace_id) on delete cascade,
    ledger       text not null,                 -- 'xero' | 'quickbooks'
    fynn_account text not null,                 -- e.g. 'Marketing Expense'
    code         text not null,                 -- Xero AccountCode / QBO account Id
    name         text not null default '',      -- what the ledger calls it
    -- Xero TaxType or QuickBooks TaxCodeRef for lines on this account. Blank
    -- means say nothing and let the ledger apply the account's own default.
    tax          text not null default '',
    updated_at   timestamptz not null default now(),
    unique (firm_id, ledger, fynn_account)
);

create index if not exists idx_account_mappings_firm_ledger
    on account_mappings (firm_id, ledger);

-- Source files, retained for the statutory period. Traceability was the
-- requirement every accountant raised unprompted: a posted figure has to be
-- walkable back to the file it came from.
create table if not exists settlement_files (
    id          text primary key,                  -- firm_id|cycle|filename
    firm_id     text not null references firm_profiles(workspace_id) on delete cascade,
    cycle       text not null,
    filename    text not null,
    line_count  integer not null default 0,
    csv_data    text not null,
    -- Payouts typed at upload where the file did not state them; needed to
    -- rebuild the cycle without reconciling against zero.
    reported    text not null default '',
    ingested_at timestamptz not null default now()
);

create index if not exists idx_settlement_files_firm_cycle
    on settlement_files (firm_id, cycle);

-- Decisions that belong to one cycle rather than to a rule. A refund with no
-- matching sale, a withheld balance, an unexplained residual — each is about
-- one settlement, not about a label that will recur, so no rule is written.
-- Without these a restart re-opens them and a finished close comes back undone.
create table if not exists cycle_resolutions (
    firm_id    text not null references firm_profiles(workspace_id) on delete cascade,
    cycle      text not null,
    key        text not null,                 -- one line, or every line sharing a label
    account    text not null,
    side       text not null check (side in ('debit', 'credit')),
    actor      text not null default '',
    decided_at timestamptz not null default now(),
    primary key (firm_id, cycle, key)
);

-- Journals sent to a ledger, with the working paper as it stood at the moment
-- of posting. Entries are small and always read as a whole, so the lines are
-- inlined rather than split into a second table.
create table if not exists posted_entries (
    id        bigint generated always as identity primary key,
    firm_id   text not null references firm_profiles(workspace_id) on delete cascade,
    cycle     text not null,
    platform  text not null,
    reference text not null,
    adapter   text not null,                       -- 'dry-run' | 'xero' | 'quickbooks'
    actor     text not null,
    lines     jsonb not null,                      -- [{account, side, amount}, ...]
    audit_csv text not null,
    posted_at timestamptz not null default now()
);

create index if not exists idx_posted_entries_firm_cycle
    on posted_entries (firm_id, cycle);

-- OAuth tokens for Xero and QuickBooks Online, one row per firm per ledger.
-- The refresh token is the valuable one: access tokens last 30 minutes (Xero)
-- or an hour (QBO), so every call refreshes as needed, and losing the refresh
-- token means the firm has to authorise again.
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
    -- What the authorisation actually covers. A connection that may read but
    -- not write is useful — it carries the chart of accounts — but must not
    -- look identical to one that can post. Empty means "not recorded".
    scopes        text not null default '',
    connected_at  timestamptz not null default now(),
    primary key (firm_id, ledger)
);

-- One row per settlement — one payout, one document in the ledger — kept for
-- every month. See 015_settlements.sql for why a row has two halves: what Fynn
-- computes, rewritten on every reconciliation, and what was sent, never
-- recomputed.
create table if not exists settlements (
    firm_id           text not null references firm_profiles(workspace_id) on delete cascade,
    reference         text not null,                 -- the document number, e.g. JE-LAZ-2026-01-05JAN2026…
    cycle             text not null,                 -- the month it closes in
    platform          text not null,
    period            text not null default '',      -- the platform's own wording
    period_end        text not null default '',      -- ISO date, for sorting and dating
    total             numeric(14, 2) not null default 0,
    lines             jsonb not null default '[]'::jsonb,
    open_exceptions   integer not null default 0,
    ledger            text not null default '',      -- where it was sent: 'xero' | 'dry-run'
    ledger_id         text not null default '',      -- Xero's InvoiceID
    ledger_status     text not null default '',      -- DRAFT | AUTHORISED | PAID | VOIDED | DELETED
    document          text not null default '',      -- ACCREC | ACCPAY
    posted_total      numeric(14, 2),
    posted_lines      jsonb,
    posted_at         timestamptz,
    posted_by         text not null default '',
    status_checked_at timestamptz,
    updated_at        timestamptz not null default now(),
    primary key (firm_id, reference)
);

create index if not exists idx_settlements_firm_cycle
    on settlements (firm_id, cycle);
