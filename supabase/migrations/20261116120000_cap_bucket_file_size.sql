-- Cap every object in the segments and reels buckets at 50 MiB.
--
-- The hosted project's free plan rejects any upload over 50 MiB, and that
-- global limit cannot be raised on the free plan. Locally,
-- supabase/config.toml's [storage] file_size_limit = "50MiB" is not what the
-- storage container enforces (the CLI also sets UPLOAD_FILE_SIZE_LIMIT to
-- 50 GB), so a 375 MB segment uploaded fine locally and would have been
-- rejected in production. A bucket-level limit is enforced by storage-api on
-- every stack, so local runs now fail exactly where production does.
--
-- The firmware sizes segments to fit under this (VISIO_MAX_UPLOAD_BYTES,
-- default 52428800) and splits any file that does not; see the pendant design
-- spec's Segment Size Contract. Raise both together after a plan upgrade.
update storage.buckets
set file_size_limit = 52428800
where id in ('segments', 'reels');
