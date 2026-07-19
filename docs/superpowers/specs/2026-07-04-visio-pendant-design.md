# Visio Pendant - Design Spec

**Date:** 2026-07-04
**Status:** Approved
**Project dir:** `/home/viet/git/vision`

---

## Overview

Visio is a cheap wearable pendant AI life-logger for home and personal vlogging use.
It is inspired by looki.ai but targets a DIY maker audience using off-the-shelf embedded components.
The device records passively all day, uploads footage to the cloud over home WiFi, and an AI pipeline assembles a 60-120 second highlight reel automatically each night.

---

## Goals

- Total hardware BOM under $110.
- Full-day (7-8 hr) passive recording without user interaction.
- Nightly AI-generated highlight reel delivered via push notification.
- Vintage / nostalgic aesthetic available as a post-process filter option.
- Mobile companion app for viewing reels, browsing raw footage, and monitoring device status.

---

## Non-Goals

- On-device AI inference.
- Real-time streaming or live broadcasting.
- Waterproof / ruggedized enclosure (v1 scope is indoor / light outdoor use).
- Multi-user or social sharing platform features beyond simple export.

---

## System Architecture

```
[Pendant Hardware]
  RPi Zero 2W + Camera Module 3 Wide + PiJuice pHAT + 3000mAh LiPo
  Records H.264 720p/30fps in 5-minute rolling segments to SD card.
  LED indicates recording / uploading state.

      | home WiFi, continuous background upload
      v

[Cloud Storage + AI Pipeline]
  Raw segments land in Supabase Storage (S3-compatible).
  A serverless pipeline scores each segment for interestingness
  (speech via Whisper, scene novelty via Claude Haiku vision, motion via FFmpeg diff),
  extracts top moments, and stitches a 60-120 sec highlight reel.
  Optional vintage FFmpeg filter applied at assembly stage.

      | push notification
      v

[Mobile Companion App]
  React Native (Expo) app for iOS and Android.
  View daily reel, scrub raw footage, flag clips, adjust preferences,
  monitor device battery and storage, onboard device to home WiFi.
```

---

## Hardware Layer

### Bill of Materials

| Component | Part | Est. Cost |
|---|---|---|
| Compute | Raspberry Pi Zero 2W | $15 |
| Camera (standard) | Raspberry Pi Camera Module 3 Wide (120° FOV) | $25 |
| Camera (vintage alt) | OV5647 wide-angle module | $8 |
| Power management | PiJuice Zero pHAT | $18 |
| Battery | 3000mAh LiPo (PiJuice compatible) | $12 |
| Storage | 128GB microSD (Class 10) | $12 |
| Status indicator | WS2812B single NeoPixel | $1 |
| User input | 6mm tactile SMD button | $0.50 |
| Enclosure | 3D printed pendant shell + lanyard loop | ~$3 filament |
| Misc | Ribbon cable, headers, JST connectors | $5 |
| **Total (standard camera)** | | **~$91.50** |
| **Total (vintage camera)** | | **~$74.50** |

### Form Factor

- Dimensions: approximately 65mm x 45mm x 18mm (thick credit card).
- Camera lens centered on the face of the pendant.
- PiJuice pHAT and LiPo sandwiched behind the Pi board.
- Lanyard clip on top edge.

### Power Budget

- Pi Zero 2W active draw: ~1.2W.
- Camera Module 3 Wide: ~0.3W.
- Total continuous draw: ~1.5W.
- 3000mAh @ 3.7V = 11.1Wh → ~7.4 hours real-world.

### Vintage Camera Note

The OV5647 module has a natural warm color response and slight optical vignetting that produces an organic film-like aesthetic without any software filter.
It is still capable of 720p/30fps output.
The Camera Module 3 Wide with a post-process FFmpeg filter is the recommended default path because it produces a cleaner feed that is also usable without the filter.

---

## Firmware / Device Software

### OS

Raspberry Pi OS Lite (Bookworm, 64-bit headless).

### Recording Daemon (`visio-recorder`)

A single Python systemd service manages the full device lifecycle.

**Startup sequence (as-built, Epic 5 daemon glue):**

1. PiJuice powers on Pi; systemd starts `visio-recorder` with `EnvironmentFile=/etc/visio-recorder.env` supplying Supabase credentials and the data-dir/segment/framerate tunables.
2. Check battery level via PiJuice API. If below the halt threshold (<10%), flash LED red and exit 0 without recording; below the 20% low-battery threshold, proceed but show the low-battery LED throughout.
3. First boot only (no session file on disk yet): scan a QR code via `rpicam-still` + zbar to onboard WiFi (writes the NetworkManager keyfile) and Supabase auth, up to 60 attempts; exit 1 if onboarding never succeeds.
   Then activate the connection (`nmcli connection reload` + `nmcli connection up visio`); on activation failure, flash the Critical red LED and exit 1 so systemd retries.
   On restart boots (session file already on disk), the same reload/up runs again as a best-effort retry whenever the keyfile exists, so a first boot whose activation failed is not stranded offline; a failed retry is logged but never fatal, since the device may already be online via autoconnect.
4. Load or create the device's local `device_id`; register the device with Supabase the first time this ever succeeds, tracked by a separate `device_registered` marker file so a crash between steps 4 and 5 retries registration on the next boot rather than skipping it forever.
5. Flush any pending upload queue left over from a previous run.
6. Register the GPIO 17 flag-button listener, then begin rolling H.264 recording via `rpicam-vid`.

**Recording loop (as-built):**

- `rpicam-vid` is invoked once per 5-minute segment (not one long-running `--segment` process), named `YYYYMMDD_HHMMSS.h264` - this keeps filename/timing control at capture start and lets each segment fail independently, at the cost of a ~1-2s gap between segments.
- On segment completion the daemon wraps it into MP4 (`ffmpeg -c copy`), moves it to the upload queue, and hands it to a single background worker thread.
- The worker thread is the sole owner of upload state (segment count, consecutive-failure count) and every upload-related LED transition; the main thread only records and enqueues.
- The worker attempts the upload once per segment as it arrives; there is no continuous while-running retry loop - a failed upload's file stays queued and the *next process startup's* flush (step 5 above) is the retry mechanism.
- After 3 consecutive upload failures the LED escalates to Critical red flash even though recording continues uninterrupted; any subsequent successful upload resets the counter.
- On successful upload, local file is deleted to free SD space.

**Manual flag button:**

A single press inserts a `FLAG_YYYYMMDD_HHMMSS.marker` file into the upload queue.
The cloud pipeline reads this marker and boosts the composite score for the surrounding timestamp.
Flagged moments are always included in the highlight reel.

**As-built wiring:** `main()` registers a gpiozero button listener on GPIO 17 (pull-up, 50ms bounce time) whose press handler only debounces, writes the marker, and hands the path to a dedicated flag-upload worker thread, keeping gpiozero's callback thread off the network so later presses are never delayed by a slow upload.
The worker uploads each marker immediately as it arrives, so it reaches the pipeline before that night's run.
Presses within a 2-second cooldown of the last accepted press are dropped (switch bounce and accidental double-taps).
If the immediate upload fails, the marker stays in the upload queue and the next boot's flush retries it - the same retry contract as segments.

**LED state machine:**

| State | Color / Pattern |
|---|---|
| Recording | Solid green |
| Uploading | Pulsing blue |
| Low battery (<20%) | Pulsing yellow |
| Critical battery / error | Red flash |

### WiFi Onboarding

On first boot, the device runs a short Python script that activates the Pi camera and decodes a QR code displayed by the mobile app.
The QR code encodes the home WiFi SSID and password.
On successful decode, credentials are written to `wpa_supplicant.conf` and the device reboots into normal recording mode.

This avoids needing to configure a captive portal or AP mode (`hostapd`).

**As-built deviation:** the daemon writes a NetworkManager keyfile (`/etc/NetworkManager/system-connections/visio.nmconnection`) instead of `wpa_supplicant.conf`, because stock Raspberry Pi OS Bookworm uses NetworkManager and does not honor `wpa_supplicant.conf`.

**As-built activation:** after onboarding writes the keyfile, `main()` runs `nmcli connection reload` then `nmcli connection up visio` (behind a `ConnectionActivator` protocol with a subprocess-backed real implementation).
On activation failure the daemon flashes the Critical red LED and exits 1; the keyfile and session are already on disk, so systemd's `Restart=on-failure` plus NetworkManager autoconnect retry from there rather than re-running onboarding.
Because NetworkManager does not reliably load dropped-in keyfiles on its own, restart boots also retry the reload/up best-effort whenever the keyfile exists; that retry never fails the boot.

### Device Status Reporting

After each segment is processed, whether the upload succeeds or fails, the daemon performs a Supabase `UPSERT` on a `device_status` table row:

```
battery_pct, storage_used_gb, storage_free_gb,
segments_pending, segments_uploaded_today, recording_active, updated_at
```

The mobile app subscribes to this row via Supabase Realtime for live status without polling.

---

## Cloud AI Pipeline

### Trigger

A Supabase Edge Function (or AWS Lambda) is invoked nightly via a cron schedule, or when the pending segment count for a device exceeds a configurable threshold.

**As-built deviation:** the pipeline runs as the `nightly-reel` GitHub Actions workflow (`.github/workflows/nightly-reel.yml`): a nightly cron at 19:00 UTC (02:00 device-local ICT), plus `workflow_dispatch` with an optional `day` input for manual runs.
Supabase Edge Functions were ruled out because the pipeline is Python + FFmpeg and Edge Functions run Deno.
The pending-segment-count threshold trigger is not implemented.

**As-built addition:** storage keys the nightly run cannot process (unparseable/out-of-prefix keys, flag markers matching no segment window) are persisted to a `pipeline_dlq` table and retried each night with an attempt count; after 5 attempts the key is escalated and the device owner is notified via push.

### Pipeline Stages

**Stage 1 - Audio Analysis (Whisper API)**

Each segment is transcribed via OpenAI Whisper.
Scores extracted: speech presence ratio, laughter/exclamation detection, silence ratio.

**Stage 2 - Motion Analysis (FFmpeg, no API cost)**

FFmpeg extracts 1 frame per second as JPEG.
Inter-frame pixel diff produces a motion intensity score per segment.
Segments below a minimum motion threshold skip Stage 3 (cost gating).

**Stage 3 - Scene Novelty Scoring (Claude Haiku Vision)**

3 frames sampled from each motion-active segment are sent to `claude-haiku-4-5`.

Prompt:
```
Rate the visual interest of this moment on a scale of 1-10.
Consider: Is this a new location? Are people present and engaged?
Is there an interesting activity? Is this indoors or outdoors?
Reply with JSON: {"score": N, "location": "indoor|outdoor", "people": true|false}
```

**Stage 4 - Composite Scoring**

```
base_score = 0.4 * scene_novelty
           + 0.3 * audio_activity
           + 0.2 * motion_intensity

score = base_score * 1.5  (if manually flagged)
      | base_score         (otherwise)
```

These weights are stored per-user in Supabase and are tunable via the mobile app.

**Stage 5 - Highlight Selection**

Top-scoring segments are selected to fill a target duration (default 90 seconds).
A diversity constraint prevents two consecutive segments from the same location cluster (indoor/outdoor classification from Stage 3).

**Stage 6 - FFmpeg Assembly**

Selected segments are concatenated and encoded as 720p H.264 MP4.

Standard output: clean 720p feed.

Vintage filter (user opt-in):
```
-vf "curves=vintage,noise=alls=8:allf=t+u,vignette=PI/4"
```

**Stage 7 - Delivery**

Final reel uploaded to Supabase Storage.
Push notification sent to mobile app via Expo Push Notifications: "Your [Date] highlight reel is ready."

### Cost Estimate

Per day of full 8-hour recording (~16GB raw footage, ~96 segments):

| Service | Est. Cost |
|---|---|
| Whisper transcription (~8 hrs audio) | ~$0.20 |
| Claude Haiku vision (motion-gated, ~30% of segments) | ~$0.05-0.10 |
| Supabase Storage egress | ~$0.01 |
| **Total** | **<$0.30/day** |

---

## Mobile Companion App

### Stack

- React Native with Expo.
- Supabase JS client for auth, data, storage, and realtime.
- Expo AV for video playback.
- Expo Notifications for push notification handling.

**As-built deviation (Epic 5 screen wiring, issue #8):** playback uses `expo-video`, not Expo AV - `expo-av` is deprecated in current Expo SDKs and was only ever installed because this spec predates that deprecation. Timeline thumbnails are generated per segment via `expo-video-thumbnails`.

### Screens

**Tab 1 - Today's Reel**

- Full-screen video player for the daily highlight reel.
- Vintage toggle (applies client-side filter if cloud version was rendered without it).
- Share button - exports to camera roll or native share sheet.
- Regenerate button - opens a preferences bottom sheet: target length (30s / 60s / 90s / 2min), style (clean / vintage), mood weighting (conversation-heavy / action-heavy / balanced).

**As-built gap:** the regenerate bottom sheet is not wired in Epic 5 - `validateRegenerateRequest` (Epic 3) has no backend consumer yet, so there is nothing for it to trigger. Out of scope for issue #8; tracked as follow-up work, not silently dropped.

**Tab 2 - Raw Footage**

- Timeline scrubber showing the full day with thumbnail previews every 5 minutes.
- Tap a segment to open a preview player.
- Long-press a segment for: "Always include", "Never include" (persisted as user feedback to the scorer).
- Manual-flagged segments show a flag icon on the timeline.

**Tab 3 - Device**

- Live battery percentage and estimated recording time remaining.
- SD card storage used / free.
- Upload progress: segments pending and uploaded today.
- Recording active / paused indicator.
- Settings: WiFi re-onboarding (show QR code), resolution, segment length, score weights.

**As-built gap:** WiFi re-onboarding is not wired in Epic 5 - the Re-onboard button shows a "not available yet" alert instead of the QR display screen, since it has no backend consumer yet either (separate issue, out of scope for #8).

**Tab 4 - Archive**

- Calendar heat-map view of past reels by day (days with reels highlighted).
- Tap a day to open that reel.

### Auth

Supabase Auth with email/password.
Each device is linked to a user account via a `device_id` UUID generated on first boot.

---

## Data Model (Supabase Postgres)

```sql
users            -- Supabase Auth managed
devices          -- device_id, user_id, name, created_at
device_status    -- device_id, battery_pct, storage_used_gb, storage_free_gb,
                 --   segments_pending, segments_uploaded_today, recording_active, updated_at
segments         -- id, device_id, recorded_at, duration_sec, s3_key,
                 --   motion_score, audio_score, scene_score, composite_score,
                 --   manually_flagged, user_feedback (include|exclude|null)
reels            -- id, device_id, date, s3_key, duration_sec, style, created_at
                 --   unique (device_id, date) so same-day pipeline re-runs upsert
score_weights    -- user_id, scene_weight, audio_weight, motion_weight (defaults 0.4/0.3/0.2)
pipeline_dlq     -- device_id, key, kind (rejected|unmatched), attempts, escalated, updated_at
                 --   service_role-only (RLS enabled, no policies) nightly-pipeline DLQ
```

---

## Repository Structure

```
vision/
  supabase/          -- Postgres migrations + pgTAP tests (shared schema, RLS, storage buckets)
  firmware/          -- Python daemon + systemd unit + onboarding scripts
  pipeline/          -- Nightly AI pipeline (Python, run by the nightly-reel GitHub Actions workflow)
  app/               -- React Native / Expo mobile app
  hardware/          -- STL files for 3D printed enclosure, wiring diagrams
  docs/
    superpowers/
      specs/         -- This file
      plans/         -- Executable implementation plans (one per epic)
```

---

## Open Questions / Future Work

- v2: replace manual flag button with accelerometer-based gesture (sharp tap pattern) to avoid fishing for the button.
- v2: on-device wake-word detection (Porcupine) to auto-flag audio moments without cloud round-trip.
- v2: Bluetooth Low Energy status broadcast so the mobile app gets real-time device status without Supabase round-trip when on the same local network.
- Enclosure: explore off-the-shelf plastic pendant shells (AliExpress) to avoid 3D printing requirement.
