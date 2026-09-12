-- Fynn — the whole schema, for a fresh Supabase project.
--
-- Paste this into the Supabase SQL editor and run it once. It is 005 + 007 +
-- 008 already applied, so a new project does not have to replay a rename.
--
-- On a database that already has the 005 schema, run 007 and 008 instead of
-- this file; `create table if not exists` would skip the existing tables and
-- leave firm_profiles keyed by the old `phone` column.
--
-- Migrations 001-004 belong to the earlier seller-facing P&L product and are
-- kept only as history. Nothing here depends on them.

-- One row per accounting firm. A deployment is a single workspace for now, so
-- in practice this holds one row under the fixed id 'workspace'. Every other
-- table hangs off it, which is what makes deleting a firm a single statement.
create table if not exists firm_profiles (
    workspace_id    text primary key,
    firm            text not null default 'Your firm',
    actor           text not null default '',      -- who signs off, named in the trail
    platforms       jsonb not null default '["Shopee","Lazada","TikTok Shop"]'::jsonb,
    ledger          text not null default 'dry-run',
    onboarding_step text,                          -- null = setup complete
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
    ingested_at timestamptz not null default now()
);

create index if not exists idx_settlement_files_firm_cycle
    on settlement_files (firm_id, cycle);

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
