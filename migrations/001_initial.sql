-- Fynn — Initial Supabase schema
-- Run this in Supabase SQL Editor to create all tables.

-- ── Seller profiles ────────────────────────────────────────────────────────────
-- Stores onboarding settings per WhatsApp number.
-- One row per seller.

create table if not exists seller_profiles (
    phone               text primary key,           -- WhatsApp number e.g. whatsapp:+6591234567
    name                text not null default 'Seller',
    language            text not null default 'en', -- 'en' or 'zh'
    currency            text not null default 'SGD',
    report_time_hour    int  not null default 8,
    daily_enabled       bool not null default false,
    weekly_enabled      bool not null default true,
    monthly_enabled     bool not null default true,
    weekly_day          text not null default 'mon',
    monthly_day         int  not null default 1,
    anomaly_sensitivity text not null default 'normal',
    onboarding_step     text,                       -- null = onboarding complete
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);

-- ── P&L reports ────────────────────────────────────────────────────────────────
-- Stores the last generated P&L per seller per period.
-- Used by /ask and conversation context after server restarts.

create table if not exists pnl_reports (
    id          uuid primary key default gen_random_uuid(),
    phone       text not null references seller_profiles(phone) on delete cascade,
    period      text not null,                      -- e.g. "March 2026"
    platform    text not null default 'Shopee MY',
    report_data jsonb not null,                     -- full P&L dict
    created_at  timestamptz not null default now()
);

-- Index for fast lookup of latest report per seller
create index if not exists idx_pnl_reports_phone_created
    on pnl_reports (phone, created_at desc);

-- ── Auto-update updated_at on seller_profiles ──────────────────────────────────
create or replace function update_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

create or replace trigger seller_profiles_updated_at
    before update on seller_profiles
    for each row execute function update_updated_at();
