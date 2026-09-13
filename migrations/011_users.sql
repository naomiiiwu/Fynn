-- Fynn — Migration 011: accounts, and the workspaces they own.
--
-- Until now a deployment was one workspace behind one shared password, and
-- firm_id was the literal string 'workspace' everywhere. That is fine for one
-- firm and wrong for two: a second firm signing in would have seen, and
-- overwritten, the first firm's rules, mappings and ledger connection.
--
-- A user's id IS their workspace id. Every scoped table already keys on
-- firm_id, so nothing else in the schema changes — the constant simply becomes
-- the signed-in person's id.

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
    -- Matched on in preference to the email for exactly that reason.
    google_sub    text not null default '',
    created_at    timestamptz not null default now()
);

create unique index if not exists idx_users_google_sub
    on users (google_sub) where google_sub <> '';


-- Claiming the pre-accounts workspace.
--
-- Data created before this migration sits under firm_id = 'workspace' and
-- belongs to nobody. To adopt it, sign up, find your new id, and re-point the
-- rows at it. Run this as ONE statement so nothing is half-moved; the
-- firm_profiles update must come first because the others reference it.
--
--   with me as (select id from users where email = 'you@example.com')
--   update firm_profiles set workspace_id = (select id from me)
--    where workspace_id = 'workspace';
--
-- The child rows follow by cascade only if the foreign keys were declared
-- `on update cascade`, which they are not — so update each explicitly:
--
--   update firm_rules         set firm_id = '<new id>' where firm_id = 'workspace';
--   update account_mappings   set firm_id = '<new id>' where firm_id = 'workspace';
--   update settlement_files   set firm_id = '<new id>' where firm_id = 'workspace';
--   update posted_entries     set firm_id = '<new id>' where firm_id = 'workspace';
--   update ledger_connections set firm_id = '<new id>' where firm_id = 'workspace';
--
-- Leaving it alone is also fine: the old rows simply become unreachable, and
-- every new account starts clean.
