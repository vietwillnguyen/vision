# Visio Pendant - Epics Overview

> This is an index/roadmap document, not an executable plan itself.
> Each epic below links to its own executable plan under `docs/superpowers/plans/`.
> Execute each linked plan with `superpowers:subagent-driven-development` or `superpowers:executing-plans`.

**Source spec:** [`docs/superpowers/specs/2026-07-04-visio-pendant-design.md`](../specs/2026-07-04-visio-pendant-design.md)

**Goal:** Ship a working Visio pendant end-to-end - hardware that records all day, a cloud pipeline that turns footage into a nightly highlight reel, and a mobile app to view it - matching the approved design spec.

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

### Current status (as of 2026-08-01)

- **Epics 0, 1, 2, 3:** done and merged. Epic 3's last software item, issue [#8](https://github.com/vietwillnguyen/vision/issues/8) (screen wiring), landed via [PR #22](https://github.com/vietwillnguyen/vision/pull/22), and the WiFi + auth re-onboarding QR screen it deferred landed via [PR #25](https://github.com/vietwillnguyen/vision/pull/25).
- **Every filed issue is closed** - all 8 of them, zero open. Nothing on this roadmap is now gated by a software issue.
- **Epic 4:** no task recorded as done - every `[human]` task in [`2026-07-04-visio-hardware.md`](2026-07-04-visio-hardware.md) is still unchecked. The PiJuice Zero pHAT is blocked on availability, so bring-up is planned on a generic USB power bank instead (`VISIO_BATTERY_SOURCE=none`); see that plan's 2026-07-21 status note.
- **Epic 5:** the only remaining gate, and it is a hardware gate, not a software one - the seven `[human]` checklist items below all need the physical Pi, which in turn needs Epic 4.
- **Open work in flight:** [PR #26](https://github.com/vietwillnguyen/vision/pull/26) (device provisioning script + runtime preflight check) is a draft with all 5 checks green, held open pending validation on the physical device rather than pending review or CI.

---

## Epic 0: Supabase Foundation

**Plan:** [`2026-07-04-visio-supabase-foundation.md`](2026-07-04-visio-supabase-foundation.md)

Creates the `devices`, `device_status`, `segments`, `reels`, and `score_weights` tables, row-level security policies scoping every row to its owning user, and the `segments`/`reels` storage buckets. This is the contract every other subsystem codes against.

**Done when:** all five tables exist with the columns from the spec's Data Model section (plus a `push_token` column on `devices` for notification delivery), RLS prevents cross-user reads, and both storage buckets exist with policies restricting access to the owning user's device.

## Epic 1: Firmware - Recording Daemon

**Plan:** [`2026-07-04-visio-firmware.md`](2026-07-04-visio-firmware.md)

The `visio-recorder` Python systemd service: battery check on boot, WiFi onboarding via QR code, rolling 5-minute H.264→MP4 segment capture, background upload to Supabase Storage, manual flag button, LED state machine, and per-segment device status reporting.

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

**Done when:** a physically assembled pendant boots Raspberry Pi OS, all peripherals respond, and measured battery life is within range of the spec's ~7.4 hour estimate.

## Epic 5: Integration & End-to-End Validation

### Follow-up issues gating or shaping Epic 5

Filed 2026-07-09 after the review and merge of PRs #2 (Epic 1), #3 (Epic 2), and #4 (Epic 3):

- [#5 Pipeline: nightly orchestrator, entrypoint, and real adapters](https://github.com/vietwillnguyen/vision/issues/5) - landed as `pipeline/orchestrator.py` + `python -m pipeline` with real adapters, the `pipeline_dlq` retry/escalation table, and the `nightly-reel` GitHub Actions workflow; its `workflow_dispatch` `day` input is the "trigger the pipeline manually" checklist step below.
- [#6 CI: run firmware, pipeline, and app test suites on PRs](https://github.com/vietwillnguyen/vision/issues/6) - landed as `.github/workflows/tests.yml`; all three suites now gate PRs to main alongside GitGuardian.
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

- [ ] [human] Flash the device from Epic 4 with the Epic 1 firmware image; confirm it boots and reaches "Recording" LED state.
- [ ] [human] Onboard the device to WiFi via the Epic 3 app's QR flow; confirm the NetworkManager keyfile is written and the device reconnects.
- [ ] [human] Record for at least 15 minutes; confirm segments appear in Supabase Storage and `segments` rows are created.
- [ ] [human] Press the flag button during recording; confirm a `FLAG_YYYYMMDD_HHMMSS.marker` reaches the upload queue and the corresponding segment is later marked `manually_flagged`.
- [ ] [human] Trigger the Epic 2 pipeline manually (not waiting for the nightly cron) against the recorded segments; confirm a `reels` row and push notification are produced.
- [ ] [human] Open the Epic 3 app; confirm the reel plays, the device tab shows live battery/storage, and the archive tab shows today's entry.
- [ ] [human] Drain the battery below 20% (or fake it via PiJuice API for a faster check) and confirm the LED transitions to pulsing yellow, then red/halt below 10%.
      Blocked while bring-up runs on a USB power bank: with `VISIO_BATTERY_SOURCE=none` there is no charge telemetry at all (`UnmeteredPowerReader` reports a fixed 100%), so this step needs real battery hardware to be sourced first - see Epic 4's 2026-07-21 status note.

---

## Suggested Execution Order

1. Epic 0 (Supabase Foundation) - short, unblocks everything else.
2. Epics 1, 2, 3, 4 in parallel (separate sessions/workers - no shared files).
3. Epic 5 once all four are individually complete.
