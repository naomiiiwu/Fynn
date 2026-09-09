-- Fynn — Migration 005: settlement reconciliation schema.
-- Run this in the Supabase SQL Editor.
--
-- This is the schema for the reconciliation product. Migrations 001-004 belong
-- to the earlier seller-facing P&L product and are kept only as history — the
-- tables they create (seller_profiles, pnl_reports, csv_uploads,
-- journal_entries) are no longer written to or read by any code. Drop them
-- once you have exported anything you still need; see 006_drop_legacy.sql.

-- One row per accounting firm, keyed by the WhatsApp number Twilio delivers.
create table if not exists firm_profiles (
    phone           text primary key,             -- "whatsapp:+6591234567" — the firm_id
    firm            text not null default 'Your firm',
    actor           text not null default '',      -- who signs off, named in the audit trail
    language        text not null default 'en',
    platforms       jsonb not null default '["Shopee","Lazada","TikTok Shop"]'::jsonb,
    ledger          text not null default 'dry-run',
    onboarding_step text,                          -- null = setup complete
    updated_at      timestamptz not null default now()
);

-- The accumulating asset. One row per label a firm has decided how to treat.
-- The unique constraint is what lets a re-decision overwrite the firm's earlier
-- treatment rather than leave two rules that disagree; services/database.py
-- upserts on exactly these three columns.
--
-- `platform` is '*' rather than null for a rule that applies to every platform:
-- Postgres treats nulls as distinct in a unique constraint, so a nullable
-- column here would let duplicate firm-wide rules accumulate silently.
create table if not exists firm_rules (
    id         bigint generated always as identity primary key,
    firm_id    text not null references firm_profiles(phone) on delete cascade,
    platform   text not null default '*',
    label      text not null,
    account    text not null,
    side       text not null check (side in ('debit', 'credit')),
    decided_by text,
    decided_at timestamptz,
    unique (firm_id, platform, label)
);

-- Source files, retained for the statutory period. Any posted figure has to be
-- walkable back to the file it came from — that requirement came up in every
-- accountant conversation, unprompted.
create table if not exists settlement_files (
    id          text primary key,                  -- firm_id|cycle|filename
    firm_id     text not null references firm_profiles(phone) on delete cascade,
    cycle       text not null,
    filename    text not null,
    line_count  integer not null default 0,
    csv_data    text not null,
    ingested_at timestamptz not null default now()
);

create index if not exists idx_settlement_files_firm_cycle
    on settlement_files (firm_id, cycle);

-- Journals that were sent to a ledger, with the working paper as it stood at
-- the moment of posting. Entries are small and always read as a whole, so the
-- lines are inlined rather than split into a second table.
create table if not exists posted_entries (
    id        bigint generated always as identity primary key,
    firm_id   text not null references firm_profiles(phone) on delete cascade,
    cycle     text not null,
    platform  text not null,
    reference text not null,
    adapter   text not null,                       -- 'dry-run' | 'xero'
    actor     text not null,
    lines     jsonb not null,                      -- [{account, side, amount}, ...]
    audit_csv text not null,
    posted_at timestamptz not null default now()
);

create index if not exists idx_posted_entries_firm_cycle
    on posted_entries (firm_id, cycle);
