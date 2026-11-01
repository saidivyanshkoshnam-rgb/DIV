create table if not exists public.study_accounts (
  username_key text primary key,
  user_id text not null unique,
  username text not null,
  salt text not null,
  password_hash text not null,
  created_at timestamptz not null default now()
);

create table if not exists public.study_data (
  user_id text primary key references public.study_accounts(user_id) on delete cascade,
  value jsonb not null default '{"subjects":[],"topics":[],"tasks":[],"daily_log":[],"study_plan":null}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.study_accounts enable row level security;
alter table public.study_data enable row level security;

revoke all on public.study_accounts, public.study_data from anon, authenticated;
grant all on public.study_accounts, public.study_data to service_role;
