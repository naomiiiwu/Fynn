-- Fynn — Migration 012: the tax treatment to send with each mapped account.
--
-- A2X treats accounts and taxes as one prerequisite — "ensure... you have
-- mapped your accounts and taxes appropriately" — and it is the same idea:
-- a line posted to the right account with the wrong tax rate is still wrong,
-- and wrong in a way that reaches a return.
--
-- Blank means "say nothing", and the ledger applies whatever default the
-- account carries. Tax codes are jurisdiction specific and cannot be derived
-- from a settlement file, so deferring to the account the firm chose is the
-- honest default; inventing a rate would be worse than saying nothing.

alter table account_mappings
    add column if not exists tax text not null default '';
