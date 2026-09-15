-- Fynn — Migration 016: send settlements to the ledger without a click.
--
-- A close used to end with a button: every exception decided, then Post. For a
-- firm that reviews in Xero anyway — where a document arrives as a draft and is
-- approved there — that click adds nothing. Off unless the firm turns it on:
-- sending to a client's books is not something to start doing silently.

alter table firm_profiles
    add column if not exists auto_post boolean not null default false;
