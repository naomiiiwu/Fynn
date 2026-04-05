-- Fynn — Migration 002: CSV uploads table
-- Run this in Supabase SQL Editor.

create table if not exists csv_uploads (
    id         text primary key default 'latest',  -- single-row upsert pattern
    period     text not null,
    csv_data   text not null,                      -- raw CSV content
    uploaded_at timestamptz not null default now()
);
