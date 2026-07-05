-- Postgres does not auto-index the referencing side of a foreign key;
-- these back ON DELETE CASCADE from devices/auth.users and per-device queries.
create index devices_user_id_idx on public.devices (user_id);
create index segments_device_id_idx on public.segments (device_id);
create index reels_device_id_idx on public.reels (device_id);
