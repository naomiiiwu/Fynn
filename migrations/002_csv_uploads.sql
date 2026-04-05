-- Fynn — Migration 002: CSV uploads table (multi-platform)
-- Run this in Supabase SQL Editor.
-- If you already ran the previous version of this migration, run:
--   drop table if exists csv_uploads;
-- first, then run this.

create table if not exists csv_uploads (
    id           text primary key,              -- "{platform}_{file_type}" e.g. "shopee_transactions"
    period       text not null,
    platform     text not null default 'shopee',
    file_type    text not null default 'transactions',
    csv_data     text not null,
    uploaded_at  timestamptz not null default now()
);
