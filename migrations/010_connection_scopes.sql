-- Fynn — Migration 010: what a ledger connection is allowed to do.
--
-- A firm whose app may not request the posting scope can still authorise Fynn
-- for reading, which is enough to import the chart of accounts and finish the
-- account mapping. That connection must not look identical to a full one, or
-- the difference surfaces as a failed post at the end of a close.
--
-- Empty means "not recorded": connections made before this column existed were
-- always full-scope, and are treated as such rather than being locked out.
--
-- Only needed on a database created from the original 009. A project built
-- from 000_schema.sql already has the column.

alter table ledger_connections
    add column if not exists scopes text not null default '';
