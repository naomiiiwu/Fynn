-- Fynn — Migration 004: Internal ledger (journal entries)
-- Run this in Supabase SQL Editor.
--
-- Chart of accounts is fixed and lives in code (models/ledger.py), not a table,
-- since it isn't user-editable yet. Each row here is one journal entry with its
-- lines inlined as jsonb — entries are small (a handful of lines) and always
-- read/written as a whole, so a separate lines table isn't worth the join.

create table if not exists journal_entries (
    entry_id     uuid primary key,
    phone        text not null references seller_profiles(phone) on delete cascade,
    platform     text not null,
    period       text not null,                      -- e.g. "March 2026"
    currency     text not null,
    description  text not null,
    lines        jsonb not null,                      -- [{account_code, account_name, debit, credit}, ...]
    generated_at timestamptz not null default now()
);

create index if not exists idx_journal_entries_phone_period
    on journal_entries (phone, period);
