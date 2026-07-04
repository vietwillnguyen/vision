create table public.reels (
  id uuid primary key default gen_random_uuid(),
  device_id uuid not null references public.devices(device_id) on delete cascade,
  date date not null,
  s3_key text not null,
  duration_sec integer not null,
  style text not null default 'clean',
  created_at timestamptz not null default now()
);

alter table public.reels enable row level security;
