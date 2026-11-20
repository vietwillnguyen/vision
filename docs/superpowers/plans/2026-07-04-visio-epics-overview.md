# Visio Pendant - Epics Overview

> This is an index/roadmap document, not an executable plan itself.
> Each epic below links to its own executable plan under `docs/superpowers/plans/`.
> Execute each linked plan with `superpowers:subagent-driven-development` or `superpowers:executing-plans`.

**Source spec:** [`docs/superpowers/specs/2026-07-04-visio-pendant-design.md`](../specs/2026-07-04-visio-pendant-design.md)

**Goal:** Ship a working Visio pendant end-to-end - hardware that records the moments the wearer chooses (about 2 h on a typical day, started and stopped with a 2 s hold), a cloud pipeline that turns footage into a nightly highlight reel, and a mobile app to view it - matching the approved design spec.

**Product source of truth:** the v1 PRD, `docs/prd/2026-11-18-visio-v1-prd.html`, and the LED state machine in the pendant spec, both on [PR #102](https://github.com/vietwillnguyen/vision/pull/102) until it merges.

---

## Why split into multiple plans

The spec covers four largely independent subsystems (firmware, cloud pipeline, mobile app, hardware) plus one piece of shared infrastructure (the Supabase schema) that all three software subsystems depend on. Each plan below produces working, independently testable software (or, for hardware, a working physical device) on its own. Bundling them into one plan would force serialized review of unrelated work.

---

## Epic Dependency Graph

```
Epic 0: Supabase Foundation (schema + storage + RLS)
   |
   +--------------------+--------------------+
   |                    |                    |
Epic 1: Firmware   Epic 2: Cloud Pipeline   Epic 3: Mobile App
   |                    |                    |
   +--------------------+--------------------+
                         |
              Epic 5: Integration & E2E Validation

Epic 4: Hardware Assembly (parallel track, gates Epic 5 only)
```

- **Epic 0 blocks Epics 1, 2, 3** - all three read/write the same tables and storage buckets.
- **Epics 1, 2, 3 are independent of each other** - they only share the Epic 0 contract (table schemas, bucket names). Assign to separate workers/sessions freely.
- **Epic 4 (hardware)** has no software dependency and can start immediately, in parallel with Epic 0.
- **Epic 5** requires a physical assembled device (Epic 4) running the firmware (Epic 1), a deployed pipeline (Epic 2), and an installed app (Epic 3). It is the only epic that requires real hardware.
- **Epics 6, 7, 8** (added 2026-11-19 for the v1 PRD) build on Epics 1-3 and are sequenced by milestone rather than by this graph; see Milestones and Suggested Execution Order.

### Current status (as of 2026-11-19)

- **Epics 0, 1, 2, 3:** done and merged for the original spec.
  The v1 PRD (PR #102) changes the product direction: users record chosen moments, not all day, every boot comes up Stopped, and the LED follows a new state machine.
  That remaining software work is Epics 6-8 below.
- **Epic 4 (hardware):** no task recorded as done; bring-up runs on USB power (`VISIO_BATTERY_SOURCE=none`) while the PiJuice is unavailable.
- **Open PRs:** [#26](https://github.com/vietwillnguyen/vision/pull/26) provisioning (draft, conflicting with main), [#93](https://github.com/vietwillnguyen/vision/pull/93) unprivileged user (draft, stacked on #26), [#100](https://github.com/vietwillnguyen/vision/pull/100) local-first flag button and `LedController`, [#101](https://github.com/vietwillnguyen/vision/pull/101) battery hardening and `LedState.HALTED`, [#102](https://github.com/vietwillnguyen/vision/pull/102) v1 PRD and LED state machine, [#103](https://github.com/vietwillnguyen/vision/pull/103) recording control phase 1, [#104](https://github.com/vietwillnguyen/vision/pull/104) market research, [#105](https://github.com/vietwillnguyen/vision/pull/105) login fix part 1 (persist rotated tokens).
- **Open issues:** [#49](https://github.com/vietwillnguyen/vision/issues/49) (`setup-device.sh` swap sizing) plus the backlog below, [#106](https://github.com/vietwillnguyen/vision/issues/106)-[#132](https://github.com/vietwillnguyen/vision/issues/132).
- **Owner decisions (2026-11-19):** the pendant gets its own login through a one-time pairing code (#106); the M1 bench starts and stops recording with a `visio-record start|stop` CLI built with recording-control phase 3 (#111); the backlog below is the plan of record.

## Milestones

Defined in the v1 PRD (PR #102).
GitHub labels `M1`, `M2` and `M3` mark each issue's milestone.

- **M1 - Bench prototype, first reel.** Pi Zero 2W, camera and USB power; no LED or button, so recording is started with `visio-record` over SSH and the device is never worn.
  J1-J6 pass, the `visio-reonboard` CLI exists, #26 and #93 land and #49 is fixed.
  Exit: one real day of moments recorded and one reel watched on the phone.
- **M2 - Feature-complete device, v1 done.** Adds the button, the WS2812B LED and the INMP441 mic.
  J7, J8, J12 (10 s hold), J14 (2 s hold) and J15 (5 s hold) pass, and every LED state is distinguishable at 2 m, including under colour-blind simulation.
  Exit: 5 test days, at least 4 reels judged worth watching.
- **M3 - Wearable (v2).** Adds a battery and an enclosure.
  J9 passes, standby draw is measured, the upload policy is decided, the BOM stays under $110, and J10 and J11 ship.

---

## Epic 0: Supabase Foundation

**Plan:** [`2026-07-04-visio-supabase-foundation.md`](2026-07-04-visio-supabase-foundation.md)

Creates the `devices`, `device_status`, `segments`, `reels`, and `score_weights` tables, row-level security policies scoping every row to its owning user, and the `segments`/`reels` storage buckets. This is the contract every other subsystem codes against.

**Done when:** all five tables exist with the columns from the spec's Data Model section (plus a `push_token` column on `devices` for notification delivery), RLS prevents cross-user reads, and both storage buckets exist with policies restricting access to the owning user's device.

## Epic 1: Firmware - Recording Daemon

**Plan:** [`2026-07-04-visio-firmware.md`](2026-07-04-visio-firmware.md)

The `visio-recorder` Python systemd service: battery check on boot, WiFi onboarding via QR code, rolling 5-minute H.264→MP4 segment capture, background upload to Supabase Storage, manual flag button, LED state machine, and per-segment device status reporting.

> The LED states this epic built (including blue pulsing Uploading) are superseded by the LED state machine in the pendant spec (PR #102); Epic 6 carries the change.

**Done when:** the daemon can be pointed at a (mocked, in tests / real, on device) Pi camera and Supabase project and produces uploaded MP4 segments plus `device_status` rows, matching the startup sequence and recording loop described in the spec.

## Epic 2: Cloud AI Pipeline

**Plan:** [`2026-07-04-visio-pipeline.md`](2026-07-04-visio-pipeline.md)

The nightly serverless job: Whisper transcription scoring, FFmpeg motion scoring (with cost-gating), Claude Haiku vision scene scoring, composite scoring, highlight selection with a diversity constraint, FFmpeg assembly (with optional vintage filter), and Expo push delivery.

**Done when:** given a set of `segments` rows and raw footage, the pipeline produces a `reels` row and pushes a notification, reproducing the composite-scoring formula and diversity constraint from the spec exactly.

## Epic 3: Mobile Companion App

**Plan:** [`2026-07-04-visio-app.md`](2026-07-04-visio-app.md)

React Native (Expo) app: auth, the four tabs (Today's Reel, Raw Footage, Device, Archive), realtime device status via Supabase Realtime, and WiFi re-onboarding QR display.

**Done when:** a user can sign in, watch today's reel, scrub raw footage and leave include/exclude feedback, see live device status, and browse the reel archive.

## Epic 4: Hardware Assembly & Enclosure

**Plan:** [`2026-07-04-visio-hardware.md`](2026-07-04-visio-hardware.md)

Physical build: source the BOM, wire PiJuice + camera + LED + button, 3D print or source the enclosure, assemble, and validate the power budget. Not code - a checklist plan with physical verification steps instead of automated tests.

**Done when:** a physically assembled pendant boots Raspberry Pi OS, all peripherals respond, and measured battery life covers a typical day of moments (about 2 h recording plus Stopped the rest of a 14 h day); the ~7.4 hour continuous figure is the stress case, not the target.

## Epic 5: Integration & End-to-End Validation

### Follow-up issues gating or shaping Epic 5

Filed 2026-07-09 after the review and merge of PRs #2 (Epic 1), #3 (Epic 2), and #4 (Epic 3):

- [#5 Pipeline: nightly orchestrator, entrypoint, and real adapters](https://github.com/vietwillnguyen/vision/issues/5) - landed as `pipeline/orchestrator.py` + `python -m pipeline` with real adapters, the `pipeline_dlq` retry/escalation table, and the `nightly-reel` GitHub Actions workflow; its `workflow_dispatch` `day` input is the "trigger the pipeline manually" checklist step below.
- [#6 CI: run firmware, pipeline, and app test suites on PRs](https://github.com/vietwillnguyen/vision/issues/6) - landed as `.github/workflows/tests.yml`; those three suites gate PRs to main alongside GitGuardian. CI has grown since; the README documents what runs today.
  Hardened per [#12](https://github.com/vietwillnguyen/vision/issues/12): the app job's `npm ci` now retries registry fetches (5 retries, 10-60s backoff) after a transient ECONNRESET flaked the post-merge run for PR #11, so a red main during Epic 5 integration means a real regression.
- [#7 Firmware: Epic 5 daemon glue](https://github.com/vietwillnguyen/vision/issues/7) - landed as `firmware/daemon.py`'s `rpicam-vid` supervision, the `YYYYMMDD_HHMMSS.mp4` segment naming contract, real disk stats, and a `__main__` entry for the systemd unit.
- [#8 App: Epic 5 screen wiring](https://github.com/vietwillnguyen/vision/issues/8) - landed as the bottom tab navigator, real Supabase Auth, `expo-video` playback, timeline thumbnails, styling/accessibility, and realtime channel-error handling via [PR #22](https://github.com/vietwillnguyen/vision/pull/22). The WiFi + auth re-onboarding QR screen was deliberately left out of scope at the time (see [`2026-07-04-visio-app.md`](2026-07-04-visio-app.md)'s Handoff) but has since landed per [`2026-07-20-wifi-reonboard-qr-screen-design.md`](../specs/2026-07-20-wifi-reonboard-qr-screen-design.md) - the checklist's "Onboard the device to WiFi via the Epic 3 app's QR flow" step below now has a screen to run.
- [#9 Cross-epic: add date component to FLAG marker filename](https://github.com/vietwillnguyen/vision/issues/9) - landed: coordinated firmware writer + pipeline parser rename (`FLAG_YYYYMMDD_HHMMSS.marker`) before flag-button integration testing.

Two more daemon-glue gaps surfaced after #7 landed and were closed via PR #21:

- [#18 Firmware: wire GPIO flag button listener into the running daemon](https://github.com/vietwillnguyen/vision/issues/18) - landed: `main()` registers a gpiozero button listener (GPIO 17, pull-up, 50ms bounce) that hands marker writes to a dedicated upload worker thread so uploads never block later presses.
- [#19 Firmware: activate NetworkManager connection after QR onboarding writes the keyfile](https://github.com/vietwillnguyen/vision/issues/19) - landed: `main()` runs `nmcli connection reload` then `nmcli connection up visio` behind a `ConnectionActivator` protocol after onboarding writes the NetworkManager keyfile.

Every issue above is closed as of 2026-08-01, so none of them still gates Epic 5.
One piece of work is still open, as a draft PR rather than an issue:

- [PR #26 Firmware: device provisioning script and runtime preflight check](https://github.com/vietwillnguyen/vision/pull/26) - `firmware/scripts/setup-device.sh` plus a `preflight.py` startup check, per [`2026-07-21-visio-device-provisioning-design.md`](../specs/2026-07-21-visio-device-provisioning-design.md). All 5 checks are green; it is held in draft awaiting validation on the physical Pi, which makes it a companion to the checklist below rather than a blocker of it.

### Checklist

No separate detailed plan - this is a checklist run once Epics 0-4 are done, using the real assembled device:

- [ ] [human] Flash the device from Epic 4 with the Epic 1 firmware image; confirm it boots to Stopped (dim white once the LED is fitted) and starts recording with `visio-record start` (M1, [#111](https://github.com/vietwillnguyen/vision/issues/111)) or a 2 s hold (M2, [#109](https://github.com/vietwillnguyen/vision/issues/109)).
- [ ] [human] Onboard the device to WiFi via the Epic 3 app's QR flow; confirm the NetworkManager keyfile is written and the device reconnects.
- [ ] [human] Record for at least 15 minutes; confirm segments appear in Supabase Storage and `segments` rows are created.
- [ ] [human] Press the flag button during recording; confirm a `FLAG_YYYYMMDD_HHMMSS.marker` reaches the upload queue and the corresponding segment is later marked `manually_flagged`.
- [ ] [human] Trigger the Epic 2 pipeline manually (not waiting for the nightly cron) against the recorded segments; confirm a `reels` row and push notification are produced.
- [ ] [human] Open the Epic 3 app; confirm the reel plays, the device tab shows live battery/storage, and the archive tab shows today's entry.
- [ ] [human] Drain the battery below 20% (or fake it via PiJuice API for a faster check) and confirm the LED transitions to pulsing yellow while Recording, then solid red (Halted, battery) below 10% (M3).
      Blocked while bring-up runs on a USB power bank, which exposes no charge telemetry at all: the `VISIO_BATTERY_SOURCE=none` path that copes with that (`UnmeteredPowerReader`, a fixed 100%) is still unmerged, landing with [PR #26](https://github.com/vietwillnguyen/vision/pull/26). This step needs real battery hardware to be sourced first - see Epic 4's 2026-07-21 status note.

---

## Epic 6: Device control and LED state machine

**Source:** the pendant spec's LED state machine (PR #102) and the [recording-control design](https://github.com/vietwillnguyen/vision/blob/fm/vision-recording-p1-interruptible-capture/docs/superpowers/specs/2026-11-18-visio-recording-control-design.md) (PR #103).

| B# | Issue | Area | Size | Milestone | Order |
|---|---|---|---|---|---|
| B2 | [#107](https://github.com/vietwillnguyen/vision/issues/107) Land #103, #100 and #101 in an agreed order and settle the LED owner | firmware | S each | M1 | Before B3 |
| B5b | [#111](https://github.com/vietwillnguyen/vision/issues/111) `visio-record start\|stop` CLI for the M1 bench | firmware | S | M1 | With recording-control phase 3 |
| B3 | [#108](https://github.com/vietwillnguyen/vision/issues/108) LED resolver with capture and health regions | firmware | M | M2 | After B2 |
| B4 | [#109](https://github.com/vietwillnguyen/vision/issues/109) One-button hold ladder with LED previews | firmware | L | M2 | After B3 |
| B5 | [#110](https://github.com/vietwillnguyen/vision/issues/110) Every boot comes up Stopped | firmware, app | S | M2 | After B4 |
| B6 | [#112](https://github.com/vietwillnguyen/vision/issues/112) Graceful power-off on a 5 s hold (J15) | firmware | M | M2 | After B4, #101 |
| B8 | [#115](https://github.com/vietwillnguyen/vision/issues/115) Halted (camera) after 3 capture failures | firmware | M | M2 | After B4 |
| B10 | [#118](https://github.com/vietwillnguyen/vision/issues/118) SD low Critical and stop when the card is nearly full | firmware | M | M2 | After B3, B11 |
| B13 | [#120](https://github.com/vietwillnguyen/vision/issues/120) systemd hardening: watchdog, start limits, `ExecStopPost=` | firmware | M | M2 | After #26, #93 |
| B22 | [#129](https://github.com/vietwillnguyen/vision/issues/129) M2 bench checks for the LED, the button and power-on | hardware | S | M2 | After B3, B4, B6 |
| - | [#130](https://github.com/vietwillnguyen/vision/issues/130) Measure standby power and decide the upload policy | hardware | - | M3 | Battery sourced |

**Done when:** every row of the spec's capture-region and resolver tables is built and tested, and the M2 LED and button checks pass on the device.

## Epic 7: Backup visibility and retention

**Source:** the pendant spec's upload region, Device Status Reporting and Retention and Cleanup sections (PR #102).

| B# | Issue | Area | Size | Milestone | Order |
|---|---|---|---|---|---|
| B9 | [#116](https://github.com/vietwillnguyen/vision/issues/116) Offline is normal: gate sends on NetworkManager `full` connectivity | firmware | M | M2 | After B2 |
| B11 | [#117](https://github.com/vietwillnguyen/vision/issues/117) `device_status` fields: `upload_status`, `oldest_pending_at`, `stop_reason`, `clock_synced`, `throttled` | supabase, firmware | M | M2 | - |
| B12 | [#119](https://github.com/vietwillnguyen/vision/issues/119) Device tab: backup status, last seen, Storage full | app | M | M2 | After B11 |
| B14 | [#121](https://github.com/vietwillnguyen/vision/issues/121) Nightly push when a backup is stale (> 24 h) | pipeline | S | M2 | After B11 |
| B15 | [#122](https://github.com/vietwillnguyen/vision/issues/122) Retention schema: `raw_expires_at`, `raw_deleted_at`, `raw_retention_days` | supabase | S | M2 | - |
| B16 | [#123](https://github.com/vietwillnguyen/vision/issues/123) Nightly cleanup step: mark, warn window, delete, cap, `--dry-run` | pipeline | L | M2 | After B15 |
| B17 | [#124](https://github.com/vietwillnguyen/vision/issues/124) App expiry warning, Keep and retention setting | app | M | M2 | After B15, B16 |
| B18 | [#125](https://github.com/vietwillnguyen/vision/issues/125) Account and device deletion removes storage objects; delete a day | supabase, app, pipeline | M | M2 | After B15 |
| B19 | [#126](https://github.com/vietwillnguyen/vision/issues/126) One command to download a day's raw footage | pipeline | S | M2 | - |
| B20 | [#127](https://github.com/vietwillnguyen/vision/issues/127) Suffixed storage key on a same-key, different-size collision | firmware | S | M2 | - |
| B21 | [#128](https://github.com/vietwillnguyen/vision/issues/128) Prune `rejected/` after 30 days at boot | firmware | S | M2 | - |

**Done when:** an offline day never turns the LED red, the phone shows what is waiting and since when, and raw footage older than the wearer's window is warned about and then deleted while kept clips and reels stay.

## Epic 8: Device identity and pairing

| B# | Issue | Area | Size | Milestone | Order |
|---|---|---|---|---|---|
| B1 | [#106](https://github.com/vietwillnguyen/vision/issues/106) Pendant's own login via a one-time pairing code, anonymous sign-in and `claim_pairing`; Remove device (part 1 done in #105) | firmware, app, supabase | L | M1 | First, M1 blocker |
| B7a | [#113](https://github.com/vietwillnguyen/vision/issues/113) `visio-reonboard` CLI that keeps the old Wi-Fi on failure | firmware | M | M1 | After B5b |
| B7b | [#114](https://github.com/vietwillnguyen/vision/issues/114) Re-onboard by a 10 s hold; Setup needed instead of the exit-1 loop | firmware | M | M2 | After B4, B7a |

**Done when:** the pendant's login survives phone logouts and refreshes, a device can be removed from the app, and Wi-Fi can change from the bench (M1) or the button (M2) without stranding the device.

## Cross-cutting

- [#132](https://github.com/vietwillnguyen/vision/issues/132) M1 bench: resolve #26's conflict, rebase #93, validate both on the Pi, close [#49](https://github.com/vietwillnguyen/vision/issues/49), BENCH-1, J1-J6, one real day (M1).
- [#131](https://github.com/vietwillnguyen/vision/issues/131) Product open questions from the PRD: auto-stop after N minutes, a reel for a day with under 60 s of footage, low-power standby for v2 (B23).
- Hardware plan refresh (B22 docs): done in the backlog-refresh PR; see the 2026-11-19 note in [`2026-07-04-visio-hardware.md`](2026-07-04-visio-hardware.md).

## Obsolete

- Recording-control phase 5 (remote stop): dropped, decision D1-a makes the app display-only.
- The Uploading LED look (blue, pulsing): upload progress is never on the LED; `LedState.UPLOADING` goes with B3.
- Epic 5's "boots and reaches Recording" step: every boot now comes up Stopped.

## Suggested Execution Order

Updated 2026-11-19.
The original order (Epic 0, then Epics 1-4 in parallel, then Epic 5) still holds for Epics 0-5, of which 0-3 are done; the remaining work runs in this order:

1. **Now (simulation, no hardware):** B1 [#106](https://github.com/vietwillnguyen/vision/issues/106), B2 [#107](https://github.com/vietwillnguyen/vision/issues/107), merge PR #102, B7a [#113](https://github.com/vietwillnguyen/vision/issues/113) and B5b [#111](https://github.com/vietwillnguyen/vision/issues/111), resolve #26's conflict then rebase #93.
2. **M1 bench (hardware):** validate #26 and #93 on the Pi, close #49, run BENCH-1 and J1-J6, record one real day ([#132](https://github.com/vietwillnguyen/vision/issues/132)).
3. **M2 software, in parallel with M1:**
   - Firmware chain: B3 -> B4 -> B5 -> B6, B7b, B8; plus B9, B11, B10, B13, B20, B21.
   - Backend and app: B11, B12, B14, B15, B16, B17, B18, B19.
4. **M2 hardware:** B22 [#129](https://github.com/vietwillnguyen/vision/issues/129), BENCH-2, the mic, 5 test days.
5. **M3:** upload policy, battery, J9 ([#130](https://github.com/vietwillnguyen/vision/issues/130)).
