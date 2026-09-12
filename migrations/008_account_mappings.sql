-- Fynn — Migration 008: Fynn's account names → the firm's chart of accounts.
--
-- A journal line carries an account name ("Marketing Expense"). Xero needs an
-- AccountCode, QuickBooks needs the account's Id, and both come from the firm's
-- own chart of accounts. Two firms will use different codes for the same idea,
-- so this belongs to the firm in the same way the rules do.
--
-- Keyed by ledger as well as firm: a Xero code and a QuickBooks id are not
-- interchangeable, so switching ledgers means mapping again rather than
-- silently posting to whatever account happens to share a number.

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
