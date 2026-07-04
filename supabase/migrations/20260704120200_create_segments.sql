create table public.segments (
  id uuid primary key default gen_random_uuid(),
  device_id uuid not null references public.devices(device_id) on delete cascade,
  recorded_at timestamptz not null,
  duration_sec integer not null,
  s3_key text not null,
  motion_score numeric,
  audio_score numeric,
  scene_score numeric,
  composite_score numeric,
  manually_flagged boolean not null default false,
  user_feedback text check (user_feedback in ('include', 'exclude'))
);

alter table public.segments enable row level security;
