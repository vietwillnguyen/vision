-- Whether the pendant is recording its microphone. The firmware probes the
-- ALSA capture device at startup and records video only when it does not open
-- (microphone not wired, I2S overlay missing), and drops to video only if an
-- audio capture fails mid-run, so the pendant keeps recording either way. This
-- column is how the owner learns which: the daemon reports it on its start and
-- stop upserts, and per segment for the segment it just uploaded.
alter table public.device_status
  add column audio_active boolean not null default false;
