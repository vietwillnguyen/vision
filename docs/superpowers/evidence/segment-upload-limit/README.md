# Segment upload limit - end-to-end evidence

Every run here is firmware's own code against a local `supabase start` stack: the daemon's `load_config` defaults, `run_recording_loop` with the real subprocess runner, the ffmpeg mux, and the real Supabase storage and status adapters signed in as a device owner.
Only the camera is simulated.
[`sim-bin/rpicam-vid`](sim-bin/rpicam-vid) is put first on `PATH`, takes the arguments the firmware passes, and writes H.264 at exactly the bitrate the real encoder targets for them: 640x480 at 10 Mbps when no size or bitrate is given (the rpicam-apps v1.15.0 and bcm2835-codec V4L2 defaults), and x264 held at a constant bitrate with filler, so every file is the worst case its target allows.
The pipeline runs with the real `SupabaseStore`, ffmpeg media probe and ffmpeg runner.
The vision LLM is stubbed because no API keys are configured here; the transcriber is never called, since these segments are silent.

## 00 - before: the local stack did not enforce 50 MiB (origin/main, b809807)

[`00-before-local-limit-not-enforced.log`](00-before-local-limit-not-enforced.log) ([`e2e_default_segment.py`](e2e_default_segment.py)): the default 300 s segment is 375 232 372 bytes and uploads with 200 OK.
`supabase/config.toml` says `file_size_limit = "50MiB"`, but the CLI's storage container is also started with `UPLOAD_FILE_SIZE_LIMIT=52428800000` (50 GB), and that is what it enforces.
A bucket-level `file_size_limit` is enforced (checked with curl: 60 MB is answered `413 Payload too large`, exactly 52 428 800 bytes is accepted), so the buckets now carry the cap themselves.

## 01 - before: a real 300 s segment is rejected and retried forever (origin/main firmware, capped buckets)

[`01-before-300s-rejected.log`](01-before-300s-rejected.log): same script, same firmware, on a stack with this branch's bucket cap.

- `rpicam-vid -t 300000 --framerate 30 --codec h264` (no size, no bitrate) produces a 375 368 628-byte segment.
- Storage answers `{'statusCode': 413, 'error': Payload too large}`.
- The next boot's `flush_pending` sends it again and gets the same 413; it stays queued (`segments_pending: 1`), so the device would retry it on every boot forever.

## 02 - after: the default segment fits

[`02-after-default-segment.log`](02-after-default-segment.log): the daemon now runs `rpicam-vid -t 60000 --width 1280 --height 720 --framerate 30 --bitrate 4000000`, and the 29 983 634-byte segment uploads first time.

## 03 - after: a recorded day, the oversize guard, and the nightly pipeline

[`03-after-recorded-day.log`](03-after-recorded-day.log) ([`e2e_segment_day.py`](e2e_segment_day.py)): three recording sessions ("boots") of 16 default segments each, timestamped the way per-segment `rpicam-vid` restarts leave them (1 s gap), plus flag presses at the boundaries and three oversize cases.

- All 48 captures reached storage; every full segment is 29.97-29.98 MB (about 60 s at 3.998 Mbps, 1280x720, 30 fps), 57 % of the limit.
- Oversize guard, local split: a 30 s capture at 20 Mbps (74 977 960 bytes) is split with `ffmpeg -c copy -f segment` into `200000.mp4` (40.1 MB) and `200015.mp4` (34.9 MB), both uploaded.
- Oversize guard, server 413: the same capture with `VISIO_MAX_UPLOAD_BYTES` raised to 100 MiB passes the local check, storage answers 413, and the guard re-splits it under half its size into three parts (30.2, 24.8, 20.0 MB), all uploaded.
- Oversize guard, unsplittable: a 60 MB file that is not video fails `ffprobe`, is moved to `rejected/`, `device_status.segments_rejected` reads 1, and a second boot's flush uploads nothing (it is not retried).
- Nightly pipeline: 1 device processed, 0 failed, 53 `segments` rows (48 one-minute segments and 5 guard parts), no `pipeline_dlq` rows, and a 115 s 1280x720 reel.
- Flag presses: mid-segment (08:30:30) flags `083000`; the last second of a segment (08:32:00) and the restart gap after it (08:32:01) both flag `083101`; the first second of the next segment (08:32:02) flags `083202`.

## 04 - before: a press in the restart gap went to the DLQ (pipeline before the gap fix)

[`04-before-restart-gap-marker.log`](04-before-restart-gap-marker.log): an earlier two-segments-per-session run of the same harness, with this branch's firmware but the pipeline from origin/main.
The press at 08:32:01, in the 1 s restart gap after `083101`, is recorded in `pipeline_dlq` as `unmatched`; it would have been retried for five nights and then escalated to the owner as footage needing attention.
One-minute segments make these gaps five times as frequent.
(In that run 08:32:02 is unmatched too, but only because the run recorded two segments per session, so no segment starts there.)
Run 03 shows both presses flagging the right segments after the fix.

## Reproducing

```sh
supabase start   # a stack with this branch's migrations
cd integration
eval "$(supabase status -o env)"
export SUPABASE_URL=$API_URL SUPABASE_ANON_KEY=$ANON_KEY SUPABASE_SERVICE_ROLE_KEY=$SERVICE_ROLE_KEY
uv run --locked --extra dev python ../docs/superpowers/evidence/segment-upload-limit/e2e_default_segment.py
uv run --locked --extra dev python ../docs/superpowers/evidence/segment-upload-limit/e2e_segment_day.py --out /tmp/segment-day
```
