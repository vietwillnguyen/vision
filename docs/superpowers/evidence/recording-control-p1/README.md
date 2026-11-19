# Recording control, phase 1: interruptible capture and bounded shutdown

Evidence for the first phase of
[`docs/superpowers/specs/2026-11-18-visio-recording-control-design.md`](../../specs/2026-11-18-visio-recording-control-design.md):
a stop no longer waits for the segment to end.

All of it runs on simulated hardware - no Pi, no camera, no Supabase.

| File | What it shows |
|---|---|
| `unit-before.txt` | The reproduction, run against `main` before the fix: a real SIGTERM lands 0 s into a 5 s segment and the loop returns 5.0 s later. The surrounding assertions (one capture, one upload) pass, so the defect is isolated to the stop latency. |
| `unit-after.txt` | The same test passing after the fix, then the whole firmware suite. |
| `e2e_stop_latency.py` | End to end with real processes and real signals: a child process stands in for `rpicam-vid` (real H.264 through ffmpeg, finalised on SIGUSR2 the way `rpicam-vid -s` documents), the daemon's own SIGTERM handler, `run_recording_loop` on the main thread, and the real ffmpeg mux of whatever the cut-short capture left. It measures the old blocking runner and the new interruptible one against the same camera. |
| `e2e-stop-latency.txt` | That script's output: stop latency 6.23 s -> 0.29 s on an 8 s segment, with the 2.10 s recorded before the stop still muxed, queued and uploaded. |

Run the end-to-end script from `firmware/`:

```sh
uv run --locked --extra dev python \
    ../docs/superpowers/evidence/recording-control-p1/e2e_stop_latency.py
```

Three bench checks still need the device and none of them blocks this change;
they are listed in the spec's Testing section as BENCH-1, BENCH-2 and BENCH-3.
BENCH-1 is the one this phase leans on: that `rpicam-vid -s` exits 0 on
SIGUSR2. The runner reports a non-zero exit after a cancel instead of raising,
so a failure there costs an exit code in the log, not the segment.
