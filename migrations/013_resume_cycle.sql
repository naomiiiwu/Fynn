-- Fynn — Migration 013: resuming an open cycle after a restart.
--
-- Source files were already retained for the statutory period, so the data to
-- rebuild an open cycle was always there — nothing read it back. A redeploy
-- therefore looked like the upload had been lost, when only the in-memory
-- reconciliation had.
--
-- Two things were missing to rebuild it faithfully:
--
--   open_cycle   which cycle is being worked on, so a cycle closed on purpose
--                stays closed instead of reappearing at the next restart.
--   reported     the payout figures typed at upload where the file did not
--                state them. Without these a rebuilt cycle reconciles against
--                zero and reports the entire payout as a residual.

alter table firm_profiles
    add column if not exists open_cycle text;

alter table settlement_files
    add column if not exists reported text not null default '';
