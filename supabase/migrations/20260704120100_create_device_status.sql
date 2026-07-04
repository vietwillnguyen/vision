create table public.device_status (
  device_id uuid primary key references public.devices(device_id) on delete cascade,
  battery_pct integer not null default 100,
  storage_used_gb numeric not null default 0,
  storage_free_gb numeric not null default 0,
  segments_pending integer not null default 0,
  segments_uploaded_today integer not null default 0,
  recording_active boolean not null default false,
  updated_at timestamptz not null default now()
);

alter table public.device_status enable row level security;
