-- Fynn — Migration 003: onboarding reconciliation requirements
-- Run this in Supabase SQL Editor after 001_initial.sql.

alter table seller_profiles
    add column if not exists platforms text[] not null default array['shopee']::text[],
    add column if not exists required_cost_files text[] not null default array['cogs', 'ads']::text[];
