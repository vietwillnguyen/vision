-- Segments the firmware gave up uploading because they are over the storage
-- size limit and could not be split under it (see
-- firmware/visio_recorder/upload_limit.py). Such a file is moved out of the
-- upload queue so it is never retried, which also takes it out of
-- segments_pending; this count is how the owner still learns footage exists
-- that did not reach the cloud. It is the number of files in the device's
-- rejected/ directory at the time of the upsert.
alter table public.device_status
  add column segments_rejected integer not null default 0;
