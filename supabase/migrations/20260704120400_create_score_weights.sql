create table public.score_weights (
  user_id uuid primary key references auth.users(id) on delete cascade,
  scene_weight numeric not null default 0.4,
  audio_weight numeric not null default 0.3,
  motion_weight numeric not null default 0.2
);

alter table public.score_weights enable row level security;
