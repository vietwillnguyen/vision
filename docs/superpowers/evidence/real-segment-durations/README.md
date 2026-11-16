# Real segment durations - end-to-end evidence

Each run is `pipeline/scripts/e2e_segment_durations.py` against a local `supabase start` stack.
It generates a device day with ffmpeg the way the pendant does (raw H.264, muxed with the command in `firmware/visio_recorder/muxer.py`, so silent video), uploads it to the real `segments` bucket, and runs `run_nightly` with the real Supabase store, ffmpeg media probe and ffmpeg runner.
Only the paid LLM calls and the push are stubbed.
The `.json` file is the harness report (database rows, per-clip and reel ffprobe results); the `.log` file is the pipeline's error and warning output.

The device day on 2026-07-14:

| Key | Real length | Meaning |
|---|---|---|
| `090000.mp4` | 300 s | full segment |
| `090500.mp4` | 40 s | segment ended early by a stop press |
| `090600.mp4` | 300 s | segment recorded after the restart |
| `100000.mp4` | 300 s | full segment later in the day |
| `FLAG_..._090520.marker` | - | press inside the 40 s segment |
| `FLAG_..._090700.marker` | - | press inside the restarted segment |

## 00 - before: silent segments (origin/main, ffed638)

[`00-before-silent-segments.log`](00-before-silent-segments.log): the device day fails in Stage 1 with `Output file does not contain any stream`, because ffmpeg cannot extract audio from the pendant's video-only files.
Nothing is persisted and no reel is made.
This run used an earlier harness revision without the `100000` segment; the failure does not depend on the segment set.
This had to be fixed first (commit "score silent segments") to reach the duration bug at all.

## 01 - before: hard-coded 300 s (75590aa)

[`01-before-hardcoded-300s.json`](01-before-hardcoded-300s.json):

- Every `segments` row says `duration_sec: 300`, including the 40 s file.
- The press at 09:07:00 flags the 40 s segment (its nominal window runs to 09:10:00), and the restarted `090600` segment it belongs to stays unflagged.
- Vision gets 0 frames for the 40 s segment (`fps=3/300` yields none in 40 s).
- The 40 s segment's clip is cut at 142.5 s: `clip_000.mp4` is 262 bytes with 0 frames, and ffmpeg exited 0 writing it.
- Concat then fails because the first clip has no stream, so the whole device day fails and no reel is delivered (`reels_row: null`).
  With the empty clip later in the list, the report's probe 4 showed the other outcome: concat exits 0 and silently drops it, while the row claims the full length.

## 02 - after: measured durations (caf35c5)

[`02-after-measured-durations.json`](02-after-measured-durations.json):

- `segments.duration_sec` is 300 / 40 / 300 / 300, matching the files.
- Both markers land where they belong: `090500` and `090600` are flagged, the other two are not.
- Vision gets 3 frames for every segment, the 40 s one included.
- Four clips of 15.0 s and 450 frames each, the 40 s segment's cut from 12.5 s.
- The reel file is 60.0 s with 1800 frames, and `reels.duration_sec` is 60.
- No DLQ rows, no errors in [`02-after-measured-durations.log`](02-after-measured-durations.log).
