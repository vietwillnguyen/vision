-- One reel per device per day, so same-day pipeline re-runs (manual
-- workflow_dispatch or retry after partial failure) can upsert the day's
-- reel row instead of inserting duplicates.
alter table public.reels
  add constraint reels_device_id_date_key unique (device_id, date);
