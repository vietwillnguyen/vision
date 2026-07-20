-- device_status is never inserted by the app in normal operation (firmware
-- upserts it directly), so useDeviceStatus's postgres_changes subscription is
-- the only way the app ever learns about a status change without polling.
-- Without this, that subscription silently never fires: Supabase Realtime
-- only streams postgres_changes for tables explicitly added to the
-- `supabase_realtime` publication, and no earlier migration ever added one.
alter publication supabase_realtime add table public.device_status;
