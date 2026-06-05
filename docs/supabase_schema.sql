-- KMU Campus Life Action Agent shared storage schema.
-- Run this in Supabase SQL Editor once before setting SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY.

create table if not exists public.settings (
  key text primary key,
  value text
);

create table if not exists public.profile (
  key text primary key,
  value text
);

create table if not exists public.preferences (
  category text primary key,
  cycle text,
  channel text
);

create table if not exists public.kv (
  key text primary key,
  value text
);

create table if not exists public.recommendations (
  url text primary key,
  title text,
  category text,
  source text,
  score double precision,
  hours double precision,
  deadline text,
  reason text,
  domain text,
  status text,
  body text,
  summary text,
  created_at double precision
);

create index if not exists idx_recommendations_status
  on public.recommendations(status);

create index if not exists idx_recommendations_category_status
  on public.recommendations(category, status);

create index if not exists idx_recommendations_created_at
  on public.recommendations(created_at desc);
