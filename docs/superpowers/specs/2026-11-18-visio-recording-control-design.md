# Visio Recording Stop/Start Control and Default Stopped State - Design Spec

**Date:** 2026-11-18
**Status:** Accepted; the four decisions below have been ruled on.
Committed with phase 1 of the phased plan (interruptible capture and bounded shutdown); phases 2-5 are not implemented yet, so the LED states, the button dispatch and the default stopped state described here are the design, not the current behaviour.
**Repo state verified against:** `vision` `main` at `7f713a9`, plus the open PR https://github.com/vietwillnguyen/vision/pull/98
**Supersedes:** the start/stop half of `data/vision-control-surface/report.md` (2026-08-03); the safe-power-off half of that report is out of scope here

---

## BLUF

The pendant gets a hold-to-toggle record button, a stopped state the wearer can see on the LED and in the app, and a boot that comes up stopped.
Stop means stop capturing, in under five seconds, by sending `rpicam-vid` its documented exit signal rather than waiting for the segment to end; everything already queued still uploads whenever connectivity allows.
Every boot comes up stopped and recording starts only on a button press, as the captain ruled; the only nuance left open is whether a daemon crash-restart inside one power cycle counts as a boot.
Two of the August prerequisites have already shipped on `main`: the pipeline measures every segment's real duration (PR #72), and the daemon reports `recording_active=false` when the loop exits (PR #70).
Three have not: the interruptible capture, the long-press button dispatch, and the STOPPED and SHUTTING_DOWN LED states.
No Supabase migration is needed for the recommended first ship, because `device_status.recording_active` already exists with the right default and the app already renders it.
The design needs four captain decisions, listed in "Decisions needed"; the recommended answers give a four-ship plan in which every intermediate PR leaves the device usable.

---

## Overview

Today `run_recording_loop` begins capturing unconditionally once the daemon starts, checks the stop event only between segments, and has no idle state at all.
A stop can only come from `SIGTERM`, which ends the process; a wearer has no way to stop recording short of powering the pendant off.
The LED has four states and no way to say "powered but deliberately not recording".
This spec adds an in-process recording state machine with a persistent-looking but boot-cleared default of stopped, the button gesture that drives it, the LED states that show it, and the status reporting that lets the app confirm it.

## Captain rulings this design honours

Recorded 2026-08-04 and 2026-08-05 in the backlog notes for `vision-stop-recording-control` and `vision-default-stopped-state`.

1. "Stop" means stop RECORDING only.
   Already-queued segments and flag markers still upload whenever connectivity allows; nothing in this design holds an upload back.
2. Verbatim: "should stay stopped, recording should only start when the record button is pressed."
   A device that was stopped stays stopped across a reboot, a cold boot also comes up stopped, and recording starts only on a button press.
   The backlog note reads the second clause as the general rule, so this spec treats every boot as stopped and asks the captain only about the crash-restart nuance (decision 2).

## Goals

- A deliberate stop takes effect within a few seconds, not within a segment length, and loses no footage already captured.
- A stopped pendant is visibly different from a dead one on the LED and is reported as stopped in the app within one status push.
- The record button is the existing GPIO 17 button, overloaded by hold time, so no hardware, wiring or BOM change is needed.
- Every boot comes up stopped and nothing but a button press starts recording.
- Everything above is unit-testable with the repo's existing fakes and simulated devices; nothing in the plan needs the physical Pi to land.

## Non-Goals

- Safe power off, PiJuice, `gpio-shutdown`, and read-only root (report sections 5 and 6); only the two systemd lines that bound shutdown time (`KillMode=mixed`, `TimeoutStopSec`) are pulled in because the interruptible capture is what makes them meaningful.
- A second physical button (report section 4.5's GPIO 27 fallback) unless the hold gesture fails its wear test.
- LED animation; the SPI driver renders every pattern as a solid colour and this design works within that.
- Remote start from the app; the captain's rule reserves starting for the button, so the app can at most stop (decision 1).
- The pipeline; its duration contract already handles short segments (see "Where the August findings stand").

---

## Where the August findings stand on `main`

Every recommendation the brief listed was re-verified against `main` at `7f713a9` and the open PR #98.

| August finding or recommendation | Status now | Evidence |
|---|---|---|
| R1: `rpicam-vid --signal` and an interruptible runner, so a stop does not wait for the segment | Not landed. `build_rpicam_command` passes no `-s`, and `SubprocessCommandRunner.run` is a blocking `subprocess.run(check=True)` | `firmware/visio_recorder/capture.py:63-98`, `firmware/visio_recorder/muxer.py:10-12` |
| 3.1: stop is observed only between segments, so a stop waits up to 300 s | Still true in shape, smaller in size. The default segment is now 60 s, derived from the 50 MiB upload limit (PR #86), with 300 s as the hard cap | `firmware/visio_recorder/daemon.py:329-368`, `firmware/visio_recorder/segment_size.py:31-34` |
| R2: `KillMode=mixed` and `TimeoutStopSec=30` | Not landed; the unit still has neither | `firmware/systemd/visio-recorder.service` |
| R3: fix the pipeline's hard-coded 300 s `SEGMENT_DURATION_SEC` first | Done in PR #72. The pipeline probes every file with ffprobe and that measurement drives clip extraction, frame sampling, flag windows and `segments.duration_sec`; the restart-gap tolerance explicitly treats a marker further into a gap as "recording was stopped" | `pipeline/pipeline/ingestion.py:1-8,16-20,77-122`, spec "Segment Duration Contract", `pipeline/scripts/e2e_segment_durations.py` (a 40 s stop-shaped segment is in its device day) |
| R4: GPIO 17 long-press toggles, short press still flags | Not landed. `GpioZeroFlagButton` binds `when_pressed` only, with a 50 ms bounce time; the press handler has a 2 s cooldown | `firmware/visio_recorder/drivers.py:51-65`, `firmware/visio_recorder/flag_button.py:12,71-96` |
| 4.4: STOPPED (dim white, solid) and SHUTTING_DOWN (white, pulsing) LED states | Not landed; `LedState` still has exactly RECORDING, UPLOADING, LOW_BATTERY and CRITICAL. PR #98 changes how the LED state is derived (an `upload_critical` input to `next_led_state`, a run-long `UploadHealth` counter, and `_upload_showing_led` bracketing every upload) but adds no state | `firmware/visio_recorder/led.py:5-9`, PR #98 diff of `recording_loop.py` |
| 4.4: a gpiozero callback thread setting the LED breaks the single-owner invariant, and the real driver has no lock | Half moved. The SPI driver that replaced the PWM one (PR #85) takes a lock around every frame, so two threads can no longer interleave bytes on the wire. The ownership invariant itself is unchanged and PR #98 leans on it harder: the worker owns every upload LED call and the main thread applies only the halt CRITICAL after joining it | `firmware/visio_recorder/ws2812_spi.py:97-117`, `firmware/visio_recorder/daemon.py:255-263` (PR #98 docstring) |
| 4.6: `recording_active` hard-coded `True`, so a stopped device never reaches the app | Mostly moved in PR #70. `_push_recording_status` upserts `true` before the first capture and `false` after the loop exits for any reason. The per-segment upsert inside `on_segment_complete` still passes the literal `True`, which is correct today only because the loop has no in-process stopped state | `firmware/visio_recorder/daemon.py:210-240,318-323,369-377`, `firmware/visio_recorder/recording_loop.py:223` |
| 8 note: the stopped state must be persisted on the data partition, not an overlay | Not landed, and under the unconditional ruling it is no longer needed for the reboot case; see "Persistence" | `firmware/visio_recorder/daemon.py:392-396` (the data dir holds `session.json`, `device.json`, `device_registered`, `queue/`) |
| 3.2: a killed capture leaks an orphan `.h264` forever | Fixed in PR #70; `recover_orphaned_segments` runs at boot and the audio-failure path already queues a non-empty partial capture in the same run | `firmware/visio_recorder/recording_loop.py:106-156`, `daemon.py:352-367` |
| 9.1-9.3: non-idempotent, mislabelled, whole-file-read uploads | Fixed in PRs #77 and #83 | `firmware/visio_recorder/supabase_clients.py:27-78` |
| App renders `Recording` or `Paused` from `recording_active` | Unchanged and ready; realtime on `device_status` is enabled | `app/src/screens/DeviceScreen.tsx:48-50`, `app/src/hooks/useDeviceStatus.ts:62-92`, `supabase/migrations/20260720120000_enable_device_status_realtime.sql` |

Consequence for the backlog: `vision-stop-recording-control` is still marked blocked by `vision-segment-duration-fix`, but that fix shipped as PR #72 on 2026-08 and the blocker is stale.

---

## Architecture

```
gpiozero thread          main thread (daemon)                    upload worker
--------------          --------------------                    -------------
hold >= 2 s  ------->  RecordingControl.toggle()
short press  ------->  flag marker (only while recording)
                        |
                        v
                 run_recording_loop
                   STOPPED: wait (battery poll)  ---LED---> LedController <---LED--- on_segment_complete
                   RECORDING: record_segment(cancel) --------------------> work queue -> mux, enqueue, upload
                        |                                                     |
                        +---- StatusReporter.push(recording_active) <---------+
                                      |
                                      v
                            Supabase device_status  --realtime-->  app DeviceScreen
```

### Firmware state machine

The daemon gains one small object, `RecordingControl`, that owns three `threading.Event`s and nothing else: `recording` (the wearer's intent), `cancel_capture` (cut the in-flight `rpicam-vid` short), and `wake` (something changed, re-evaluate).
It exposes `start()`, `stop_recording()`, `toggle()` and `request_exit()`, each of which only sets or clears events, so it is safe to call from the gpiozero callback thread and from a signal handler.

States, with the LED each one shows:

| State | Entered when | LED | Leaves when |
|---|---|---|---|
| BOOTING | process start; battery check, onboarding, registration, orphan recovery, startup flush | STOPPED (dim white), or LOW_BATTERY | the recording loop starts |
| STOPPED | loop start on every boot; a hold while RECORDING | STOPPED | a hold (to RECORDING); SIGTERM (to SHUTTING_DOWN); battery halt (to HALTED) |
| RECORDING | a hold while STOPPED | RECORDING, UPLOADING, LOW_BATTERY or CRITICAL as today | a hold (to STOPPED, after the in-flight capture is cut); SIGTERM; battery halt |
| SHUTTING_DOWN | SIGTERM or SIGINT in any state | SHUTTING_DOWN (white) | the process exits |
| HALTED | battery under the halt threshold, observed while recording or while idle | CRITICAL (red), applied last as today | the process exits 0 |

The loop body becomes: check battery (halt if needed); if `stop` is set, show SHUTTING_DOWN and break; if `recording` is clear, make sure the LED and status say stopped, then `wake.wait(timeout=IDLE_BATTERY_POLL_SEC)` and continue; otherwise record one segment with `cancel_capture` and hand the file to the worker exactly as today.
Transitions are observed on the main thread only, so the status push and LED change for a transition happen once, in order, on one thread.
A stop press while a capture is in flight sets `cancel_capture`; the capture returns early with a finalised short file, the loop sees `recording` clear on its next iteration, and the worker muxes, queues and uploads that short segment with no special case.
The battery is still read while idle, every `IDLE_BATTERY_POLL_SEC` (30 s), so a stopped pendant on a dying battery still ends in the CRITICAL halt rather than discharging to zero with a white LED.

### Interruptible capture

`build_rpicam_command` adds `--signal` (`-s`).
Per the Raspberry Pi camera documentation, `--signal` lets `SIGUSR1` toggle output and `SIGUSR2` exit, and the process "always starts in the recording state", so behaviour is unchanged until a signal is sent.
A new `InterruptibleCommandRunner` protocol, `run(args, cancel: threading.Event) -> CaptureExit`, sits beside the existing `CommandRunner`; the mux and probe keep the simple runner.
The real implementation uses `Popen`, polls `wait(timeout=0.2)` until the process exits or `cancel` is set, and on cancel sends `SIGUSR2`, waits a grace period (5 s), then `SIGTERM`, then `SIGKILL`.
`record_segment` passes the cancel event through and returns the output path whether the capture ran to its `-t` or was cut short.
If `rpicam-vid` exits non-zero after a cancel (BENCH-1 in the August report is still unverified), the runner reports it as a cancelled exit rather than raising, and the loop queues the partial file when it is non-empty, exactly as the audio-failure path does today at `daemon.py:359-366`; a raw `.h264` has no trailer and a `.ts` is readable when cut, which is why the capture module already chose those containers.
`SIGTERM` handling changes from "set `stop`" to `control.request_exit()`, which sets `stop`, `cancel_capture` and `wake`, so a `systemctl stop` or poweroff that lands mid-segment now ends the capture in seconds instead of running into systemd's 90 s default and a `SIGKILL`.
With that bounded, the unit gets `KillMode=mixed` (systemd signals only the daemon, which owns its child) and `TimeoutStopSec=30`.

### LED

Two new `LedState`s: `STOPPED` as dim white `(30, 30, 30)` solid and `SHUTTING_DOWN` as white `(255, 255, 255)` pulsing.
Never "LED off": a dark pendant is indistinguishable from a dead one, and a wearer who cannot tell will yank power, which is what this whole feature exists to avoid.
The SPI driver renders pulsing as solid, so on today's hardware the two states differ by brightness alone; that is acceptable because they never need to be told apart at the same moment (one is a resting state, the other lasts seconds and ends with the power).

`next_led_state` gains a `recording` input and a `shutting_down` input, with precedence SHUTTING_DOWN > CRITICAL > LOW_BATTERY > UPLOADING > RECORDING or STOPPED.
The base state is RECORDING when the loop is capturing and STOPPED when it is idle; a segment uploading after a stop press shows UPLOADING then returns to STOPPED, not to RECORDING as `_upload_showing_led` would do today.
The battery-halt path keeps applying CRITICAL last, after the worker join, so it remains the final LED state.

Ownership: the single-owner invariant stays, generalised.
A `LedController` wraps the driver and holds the current condition set (`recording`, `uploading`, `battery_low`, `upload_critical`, `shutting_down`) under one lock; callers `update(...)` a condition and the controller recomputes the state and writes the driver only when it changed.
PR #98's `_show_upload_led` becomes a call into it, so the worker and the main thread share one owner instead of racing two `apply_led_state` calls, and the gpiozero callback thread never touches it at all (it only flips events on `RecordingControl`).
The driver-level lock added in PR #85 remains the last line of defence on the wire.
Lands after PR #98 merges and is rebased onto its `UploadHealth` shape.

### Button

The existing GPIO 17 button is overloaded by hold time; no new GPIO, BOM or enclosure change.

- Short press (released before the hold threshold) while RECORDING: write a flag marker, as today.
- Hold for 2 s or longer, in either state: toggle recording.
- Short press while STOPPED: ignored (decision 4); a marker written then would match nothing in the pipeline's restart-gap window and would be DLQ'd as unmatched.

gpiozero supports this natively: `Button(hold_time=2.0, hold_repeat=False)` fires `when_held` once the button has stayed down for `hold_time`, and `when_released` on release.
There is no built-in "was held" flag on `Button`, so a small pure-Python `ButtonDispatcher` tracks it: `on_hold` sets a `held` flag and calls `toggle()`; `on_release` writes the flag marker only when `held` is clear, then clears it.
The flag therefore moves from press to release; a hold never also flags.
`FlagButton` (the protocol at `flag_button.py:17-18`) grows `on_hold` and `on_release` beside `on_press`, and `GpioZeroFlagButton` binds all three.
The existing 2 s press cooldown stays for flags.
This was verified today with gpiozero 2.0.1's mock pin factory and no hardware: a 50 ms tap fires `pressed` then `released`; a 400 ms hold at `hold_time=0.2` fires `pressed`, `held`, `released`, with `is_held` true during the hold.
The accidental-hold risk (pendant pressed against a chair or strap for 2 s) is mitigated by the 2 s threshold, the STOPPED LED, and the immediate status push to the app; whether 2 s is right through clothing is BENCH-2 and remains a wear test.

### Persistence

Under the ruling, every boot comes up stopped, so there is no reboot state to persist and nothing to write to the data partition.
The one case that needs state is a daemon crash-restart inside a single power cycle: the unit has `Restart=on-failure`, and a daemon that crashes at noon while recording would, without state, come back stopped and silently lose the afternoon.
The recommended mechanism is a marker file in `/run/visio-recorder/recording`, with `RuntimeDirectory=visio-recorder` and `RuntimeDirectoryPreserve=restart` in the unit.
`/run` is tmpfs, so a reboot or cold boot clears it unconditionally; `RuntimeDirectoryPreserve=restart` keeps the directory across a `Restart=` restart but systemd removes it on a `systemctl stop` and at shutdown.
The daemon creates the marker on `start()`, removes it on `stop_recording()`, and at loop start reads it: present means a crash-restart interrupted a recording session the wearer had started, so resume; absent means a boot, so come up stopped.
Whether a crash-restart should resume at all is decision 2; if the captain prefers the strictest reading, the marker is simply not written and every daemon start is stopped.
Nothing is written to the overlay or the data partition for this, so the August note's warning about read-only root does not apply.

### Status reporting and the Supabase row

`device_status.recording_active` already exists (`boolean not null default false`), the firmware already owns the row, and the app already subscribes to it; for the recommended first ship there is no migration.
Its meaning sharpens to "the loop is currently capturing": `true` while RECORDING, `false` in every other state.
The daemon pushes it at four points: once at boot after registration (so the app leaves "Loading device status..." on first boot and shows Stopped even before any press), on every STOPPED-to-RECORDING and RECORDING-to-STOPPED transition, and at loop exit as today.
The per-segment upsert in `on_segment_complete` stops passing the literal `True` and reads the live value through a small `StatusReporter` that stamps `recording_active` from `RecordingControl` at send time under a lock, so a short segment uploaded after a stop press reports `false`, and the worker's upsert can no longer paint over the main thread's transition push.
All transition pushes log and swallow their own failures, as the start and exit pushes already do; a stop may never depend on reaching Supabase.

Who may write: the firmware authenticates as the device owner's own user session, not `service_role`, and the app authenticates as the same user, so both are `authenticated` and pass the same `device_status_owner_access` row policy.
Column-level grants cannot distinguish them.
Today that is harmless because only the firmware writes the row in practice.
If a remote control is added (decision 1, options b or c), the command must not share `device_status`: a `device_commands` table (owner-RLS, `device_id`, `command`, `requested_at`, `acked_at`) keeps the app's intent separate from the device's report, and the firmware acknowledges by updating `acked_at` after acting.
The firmware has no inbound channel today (it only upserts), so a remote stop also needs either a REST poll (every 15 s while idle and at each segment boundary, which costs radio time on battery) or supabase-py's async realtime client on a new thread; that is the bulk of the cost of options b and c.

### App

`DeviceScreen` already branches on `recordingActive`; the label changes from "Paused" to "Stopped" (a pendant that is stopped is not paused), the style name follows, and `DeviceScreen.test.tsx` gains the stopped case.
`mapRow` is unchanged for the recommended ship.
Under decision 1 option b, the screen gains a single "Stop recording" button (never a start) that inserts a `device_commands` row, with the local button disabled until `recording_active` flips or a timeout shows "device did not respond"; option c adds the start button and has to explain to the captain why an app start does not violate the ruling.

### Pipeline

Nothing to ship.
PR #72 made the measured duration the only segment length the pipeline uses, the e2e script already carries a 40 s stop-shaped segment with a flag inside it, and a marker further than 3 s past a segment's end is treated as "recording was stopped" and DLQ'd as unmatched.
Short segments are routine once the button exists; the clip is `min(15 s, duration)` so a 6 s segment yields a 6 s clip rather than a seek past EOF.

### Onboarding and first boot

Today `run_startup_sequence` shows RECORDING green at boot, before onboarding and before any capture.
It changes to STOPPED (or LOW_BATTERY when the battery is low; CRITICAL and halt when it is critical, unchanged), so a pendant scanning a QR code is honestly not recording.
Onboarding itself is unchanged; after it writes the keyfile and session and activates the connection, registration runs, the boot status push reports stopped, and the device waits for a hold.
Whether completing onboarding should start recording once is decision 3; the ruling says no, and this spec recommends no, because the first thing a new wearer should learn is that the button is what starts it.

### systemd unit

Four lines change in `firmware/systemd/visio-recorder.service`: `KillMode=mixed`, `TimeoutStopSec=30`, `RuntimeDirectory=visio-recorder`, `RuntimeDirectoryPreserve=restart`.
The provisioning script copies the unit as a whole, so no script change is needed.

---

## Decisions needed

Each is a product call the captain owns; everything else in this spec is an engineering recommendation.
The decisions are consolidated on the review board as one card keyed to the backlog task `vision-stop-recording-control`.

**Ruled on 2026-11-18:** 1 (a) app display only; 2 (b) a crash-restart resumes the wearer's last choice via a `/run` marker; 3 (a) onboarding leaves the device stopped; 4 (a) a short press while stopped is ignored.
Every option is kept below as it was put to the captain, so what the ruling chose between stays on the record.

1. **Remote control from the app.**
   (a) Display only: the app shows Recording or Stopped live and has no control. Recommended for the first ship: it honours "recording starts only on a button press", needs no migration, no inbound firmware channel and no new attack surface on a wearable camera.
   (b) Remote stop only: the app can stop a recording pendant but never start it; adds a `device_commands` table, a firmware poll or realtime subscription, and an app button. Privacy-safe and useful ("I left it on"), at roughly one more ship task.
   (c) Full remote toggle: start and stop from the app. Conflicts with the verbatim ruling unless the captain counts the app button as "the record button".
   Recommendation: (a) now, with (b) as a follow-up ship if the captain wants it; the schema sketch for (b) is in "Status reporting and the Supabase row".

2. **Daemon crash-restart inside one power cycle.**
   (a) Every daemon start comes up stopped, including a `Restart=on-failure` restart after a crash; the strictest reading of the ruling, and a mid-day crash silently costs the rest of the day.
   (b) A crash-restart resumes the state the wearer last chose, via a `/run` marker that any reboot or shutdown clears; a reboot and a cold boot still come up stopped. Recommended: it continues a session the button started rather than starting one.
   (c) Persist the state on the data partition so even a reboot resumes recording; listed only to record that it was considered, since it contradicts the ruling.
   Recommendation: (b).

3. **Completing first-boot onboarding.**
   (a) The device stays stopped after a successful QR onboarding and waits for a hold. Recommended, per the ruling.
   (b) A successful first onboarding starts recording once, so the setup flow ends with a visibly working pendant.
   Recommendation: (a).

4. **Short press while stopped.**
   (a) Ignored; a hold toggles in both directions, so the gesture is symmetric and blind-operable. Recommended.
   (b) A short press starts recording (tap to start, hold to stop); faster to start, but an accidental tap in a bag starts a camera.
   Recommendation: (a).

---

## Testing

Simulated hardware only; nothing below needs the Pi.

- **Interruptible capture:** `FakeCommandRunner` grows a cancel-aware `run` that blocks until its scripted duration or the cancel event, writes a non-empty file either way, and records whether it saw a cancel. Tests assert stop-to-return time well under the segment length (the August probe 1 shape), that a cancelled segment is muxed, queued and uploaded exactly like a full one, and that a scripted non-zero exit after cancel still queues the partial file.
- **State machine:** drive `RecordingControl` from a second thread in `test_daemon.py` using the existing `on_record` hook pattern; assert the loop idles without recording, resumes on `start()`, polls the battery while idle (a `TriggerableBatteryReader` halt observed with no capture in flight), and that every transition pushes exactly one status with the right `recording_active`.
- **LED:** extend `test_startup_and_loop_drive_the_spi_led_through_every_colour`, which already decodes the real SPI frames through `SimulatedSpiDevice`, with a stop-start-stop sequence and assert the exact colour list including dim white, and `overlapping_writes == 0`.
- **Button:** unit-test `ButtonDispatcher` with injected hold and release events (hold never flags, release after hold is swallowed, short press flags, short press while stopped is ignored). Add gpiozero as a firmware dev dependency (`uv lock` in `firmware/`) and test the real `GpioZeroFlagButton` through `gpiozero.pins.mock.MockFactory` driving pin 17 low and high, the probe run for this spec.
- **Persistence:** `RecordingControl` takes the marker path; tests cover start writes it, stop removes it, loop start reads it, and an absent directory (a reboot) means stopped.
- **Status:** `FakeStatusClient` upserts asserted for the boot push, each transition, and the per-segment `recording_active` after a stop.
- **App:** `DeviceScreen.test.tsx` asserts "Stopped" for `recordingActive: false`; `useDeviceStatus.live.test.tsx` already proves the realtime path against a local Supabase and needs no change.
- **Database:** no migration in the recommended ship, so pgTAP, `app/src/generated/database.ts` and `integration/tests/fixtures/public_schema.json` are untouched. If decision 1 picks (b) or (c), the `device_commands` migration ships with its pgTAP file and a `scripts/gen-db-types.sh` regeneration in the same PR, as `AGENTS.md` requires.
- **Docs gate:** the first ship commits this spec under `docs/superpowers/specs/` and regenerates `docs/ARCHITECTURE.html` with a new manifest line, or the pre-commit hook and `hooks.yml` reject it.

Bench checks that remain open and must be run on the device before the feature is called done, none of which blocks a PR:

- BENCH-1: `rpicam-vid -s` exits 0 on `SIGUSR2` and the cut `.h264` and `.ts` remux with `ffmpeg -c copy`. The runner tolerates a non-zero exit, so a failure here degrades to the orphan-style recovery path rather than a lost segment.
- BENCH-2: a 2 s hold is reliably distinguishable from a tap through clothing, and the accidental-hold rate over a day of wear is acceptable; the fallback is a recessed button on GPIO 27.
- BENCH-3: wall-clock of cancel, mux, enqueue and status push on a Zero 2W fits inside `TimeoutStopSec=30`.

---

## Phased implementation plan

Each phase is one ship task through `no-mistakes` to a green PR, with the project's Summary, Test plan and Evidence sections and no attribution footers.
Each leaves the device usable and the previous behaviour intact where the phase does not change it.

| # | Ship | Changes | Depends on | Size |
|---|---|---|---|---|
| 0 | Pipeline duration prerequisite | None; shipped as PR #72. Close `vision-segment-duration-fix` and unblock `vision-stop-recording-control` | - | 0 |
| 1 | Interruptible capture and bounded shutdown | `capture.py` adds `--signal`; new `InterruptibleCommandRunner` with a cancel-aware fake; `record_segment` takes `cancel`; `SIGTERM` cancels the in-flight capture; unit gets `KillMode=mixed` and `TimeoutStopSec=30`; this spec committed and `ARCHITECTURE.html` regenerated | PR #98 merged (shared `recording_loop.py`) | M |
| 2 | LED controller and the two new states | `LedState.STOPPED`, `LedState.SHUTTING_DOWN`; `next_led_state(recording, shutting_down)`; `LedController` replacing `_show_upload_led` and the halt CRITICAL call; SIGTERM shows SHUTTING_DOWN; SPI colour-sequence test extended. Boot and loop still start recording, so nothing is visibly different yet except shutdown | 1 | M |
| 3 | Hold-to-toggle and the in-process stopped state | `RecordingControl`; the loop idles when `recording` is clear and polls the battery; `ButtonDispatcher`, `FlagButton.on_hold`/`on_release`, `GpioZeroFlagButton(hold_time=2)`; gpiozero dev dependency and mock-pin test; transition status pushes; per-segment `recording_active` from live state; `StatusReporter`. Boot still starts recording, so a wearer who never holds sees no change | 2 | L |
| 4 | Default stopped | Loop starts with `recording` clear; `run_startup_sequence` shows STOPPED; boot status push; `/run` marker and `RuntimeDirectory` lines (decision 2); onboarding leaves the device stopped (decision 3); app label "Stopped" and test | 3, decisions 2 and 3 | S |
| 5 | Remote stop (only if decision 1 is b or c) | `device_commands` migration with pgTAP and type regeneration; firmware command poll; app button and hook | 4, decision 1 | L |

Phases 1 and 2 can be reviewed as one PR if the reviewer prefers fewer rebases on top of PR #98, at the cost of a larger diff.

---

## Risks

- BENCH-1 is the single hardware assumption the design leans on; the runner's non-zero-exit tolerance is the mitigation, and the first on-device session should run it before anything else.
- PR #98 is open and touches the same `recording_loop.py` and `daemon.py` regions; phases 1 and 2 must land after it to avoid two refactors of the LED path.
- A 2 s accidental hold stops recording silently if the wearer cannot see the LED; the status push to the app is the second signal, and the app label must be unambiguous.
- `RuntimeDirectoryPreserve=restart` semantics are from systemd's documentation and need one on-device confirmation that a `Restart=on-failure` cycle keeps the marker and a reboot clears it.

## Open Questions / Future Work

- A dedicated ONBOARDING LED state, so a device scanning for a QR code is distinguishable from one that is stopped; today both would be dim white.
- LED animation (a timed pattern thread) would let SHUTTING_DOWN and STOPPED differ by more than brightness and would make a brief acknowledgement blink for an ignored short press possible.
- Remote stop (decision 1 option b) if the captain wants it after living with the button.
