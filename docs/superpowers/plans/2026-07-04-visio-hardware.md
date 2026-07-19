# Visio Pendant - Hardware Assembly Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to work through this plan task-by-task with the human doing the physical assembly. This is a physical-build checklist, not source code - there is no subagent-driven-development flow here since each step requires hands-on hardware work. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Assemble a working Visio pendant: Pi Zero 2W + Camera Module 3 Wide + PiJuice pHAT + battery, in a wearable enclosure, running Raspberry Pi OS Lite and ready for the firmware from [`2026-07-04-visio-firmware.md`](2026-07-04-visio-firmware.md).

**Approach:** Every task ends in a physical or measured verification step (continuity check, multimeter reading, visual inspection, timed test) instead of an automated test - there is no code under test here.

**Tech Stack:** Raspberry Pi Zero 2W, Raspberry Pi Camera Module 3 Wide, PiJuice Zero pHAT, 3000mAh LiPo, WS2812B NeoPixel, 6mm tactile button, 3D-printed enclosure.

## Global Constraints

- Total BOM under $110 (spec target: ~$91.50 with the standard camera, ~$74.50 with the vintage OV5647 alternative).
- Enclosure target dimensions: ~65mm x 45mm x 18mm.
- Target continuous draw: ~1.5W (Pi Zero 2W ~1.2W + Camera Module 3 Wide ~0.3W); target runtime ~7.4 hours on the 3000mAh/3.7V battery.
- OS: Raspberry Pi OS Lite (Bookworm, 64-bit headless).
- Camera choice: Camera Module 3 Wide is the default (cleaner feed, usable without the vintage filter); OV5647 is the optional vintage-look alternative - decide before ordering, since it changes the BOM total and the mounting footprint.

---

### Task 1: Source the bill of materials

**Verification artifact:** a parts list with vendor/order confirmation for each line item.

- [ ] [human] **Step 1: Choose camera path**

Decide standard (Camera Module 3 Wide, ~$25) vs. vintage (OV5647 wide-angle, ~$8) per the Global Constraints note. This decision changes Task 3's mounting steps below - if vintage is chosen, skip the vintage FFmpeg filter's necessity but not its optionality (the [pipeline plan's](2026-07-04-visio-pipeline.md) Task 8 vintage filter still applies to firmware footage either way, since the spec treats the filter and the vintage camera as independent options).

- [ ] [human] **Step 2: Order all BOM line items**

Order each line from the spec's Bill of Materials table: Raspberry Pi Zero 2W, chosen camera module, PiJuice Zero pHAT, 3000mAh PiJuice-compatible LiPo, 128GB microSD (Class 10), WS2812B NeoPixel, 6mm tactile SMD button, 3D print filament (or outsourced print), ribbon cable/headers/JST connectors.

- [ ] [human] **Step 3: Verify total spend against budget**

Sum actual receipts. Confirm total is under $110 (Global Constraint). If over, note which line item drove the overage before proceeding - do not silently exceed budget.

- [ ] [human] **Step 4: Inventory check on arrival**

Lay out all parts and confirm every BOM line item physically arrived and is undamaged (camera ribbon connector intact, PiJuice pins straight, battery JST connector present, no bulging on the LiPo cell).

---

### Task 2: Flash and boot-test the Pi in isolation (before any wiring)

**Verification artifact:** a Pi Zero 2W that boots headless and is SSH-reachable, confirmed before it's built into the enclosure where visual/serial debugging gets harder.

- [ ] [human] **Step 1: Flash Raspberry Pi OS Lite**

Use Raspberry Pi Imager to flash Raspberry Pi OS Lite (Bookworm, 64-bit) to the microSD card. In the Imager's advanced options, enable SSH and set a hostname (e.g. `visio-pendant`) and default user credentials.

- [ ] [human] **Step 2: First boot with USB power only (no PiJuice yet)**

Insert the microSD, power the Pi Zero 2W via a USB power adapter (not the LiPo/PiJuice yet - isolate variables), and wait ~90 seconds for first boot.

- [ ] [human] **Step 3: Verify SSH reachability**

From a machine on the same network:
```bash
ssh <user>@visio-pendant.local
```
Expected: successful login prompt. If `.local` mDNS resolution fails, find the IP via router admin page and SSH by IP instead.

- [ ] [human] **Step 4: Verify camera module is detected**

With the camera ribbon connected to the Pi's camera port:
```bash
rpicam-hello --list-cameras
```
Expected: the connected camera model is listed (Camera Module 3 Wide or OV5647, depending on Task 1's choice).

---

### Task 3: Wire power management and peripherals

**Verification artifact:** all peripherals responding correctly with the Pi running on PiJuice/battery power instead of USB.

- [ ] [human] **Step 1: Mount the PiJuice Zero pHAT**

Attach the PiJuice Zero pHAT to the Pi Zero 2W's GPIO header per the PiJuice hardware guide. Connect the LiPo battery's JST connector to the PiJuice.

- [ ] [human] **Step 2: Verify PiJuice is powering the Pi**

Disconnect USB power entirely. Confirm the Pi boots from battery power alone (SSH reachable per Task 2 Step 3, now over battery power).

- [ ] [human] **Step 3: Verify PiJuice reports battery charge via API**

```bash
ssh <user>@visio-pendant.local
pip install pijuice
python3 -c "from pijuice import PiJuice; pj = PiJuice(1, 0x14); print(pj.status.GetChargeLevel())"
```
Expected: a dict like `{'data': 87, 'error': 'NO_ERROR'}` - this is the exact call the firmware's `BatteryReader` implementation (from the firmware plan's Task 2) will wrap.

- [ ] [human] **Step 4: Wire the WS2812B NeoPixel**

Connect the NeoPixel's data line to a PWM-capable GPIO pin (per `rpi_ws281x` wiring requirements - typically GPIO18), plus power and ground. Run a one-off test script to set it solid green, then off, confirming visible color response.

- [ ] [human] **Step 5: Wire the tactile button**

Connect the 6mm tactile button between a spare GPIO pin and ground, with the internal pull-up enabled in software. Verify presses register:
```bash
python3 -c "
import RPi.GPIO as GPIO
import time
GPIO.setmode(GPIO.BCM)
GPIO.setup(17, GPIO.IN, pull_up_down=GPIO.PUD_UP)
print('press the button within 10 seconds...')
start = time.time()
while time.time() - start < 10:
    if GPIO.input(17) == GPIO.LOW:
        print('button press detected')
        break
"
```
Expected: `button press detected` printed after a physical press.

---

### Task 4: Measure the power budget

**Verification artifact:** a measured runtime figure to compare against the spec's ~7.4 hour estimate.

- [ ] [human] **Step 1: Fully charge the battery**

Charge via the PiJuice's micro-USB input until `GetChargeLevel()` reports 100.

- [ ] [human] **Step 2: Start a realistic recording load**

SSH in and start a continuous `rpicam-vid` recording loop (the same command the firmware will use) to simulate real draw, not idle draw:
```bash
rpicam-vid -t 0 --width 1280 --height 720 --framerate 30 -o /dev/null &
```

- [ ] [human] **Step 3: Measure current draw**

Use a USB power meter inline on the PiJuice's charge input (disconnected during this test) or a multimeter across the battery leads to confirm draw is in the ~1.5W range the spec estimates. Note the actual reading.

- [ ] [human] **Step 4: Run to depletion or extrapolate**

Either let the device run until PiJuice reports `should_halt` territory (<10%, per the firmware plan's `LOW_BATTERY_HALT_PCT`) and record elapsed time, or take two charge-level readings 30 minutes apart and extrapolate: `runtime_hours = 100 / (pct_drop_per_30min * 2)`.

- [ ] [human] **Step 5: Compare against spec estimate**

Expected: within a reasonable margin of the spec's ~7.4 hour estimate (11.1Wh / 1.5W). If materially short, note the gap - it affects whether the "full-day (7-8 hr) passive recording" goal from the spec is actually met and may need a larger battery or duty-cycled recording.

---

### Task 5: Enclosure assembly

**Verification artifact:** a fully assembled, wearable pendant.

- [ ] [human] **Step 1: Obtain the enclosure**

Either 3D print the pendant shell STL files (once created under `hardware/enclosure/`) or source an off-the-shelf pendant shell per the spec's open question about avoiding the 3D printing requirement - whichever path is chosen, confirm the shell's internal cavity fits the Pi Zero 2W + PiJuice + battery stack at ~65mm x 45mm x 18mm.

- [ ] [human] **Step 2: Assemble the internal stack**

Sandwich the PiJuice pHAT and LiPo battery behind the Pi Zero 2W board per the spec's Form Factor section, dressing the camera ribbon cable so the lens sits centered on the enclosure face without kinking the ribbon.

- [ ] [human] **Step 3: Mount the lanyard clip**

Attach the lanyard clip to the top edge of the enclosure. Confirm it holds the assembled weight (Pi + PiJuice + battery + shell) without deforming when the pendant hangs freely for 60 seconds.

- [ ] [human] **Step 4: Close the enclosure and re-verify boot**

Close the shell and re-run Task 2 Step 3 (SSH reachability) and Task 3 Step 2 (battery power boot) with the device fully enclosed - confirm nothing shifted or disconnected during assembly.

---

### Task 6: Fit and usability check

**Verification artifact:** sign-off that the pendant is physically usable as a wearable, not just electrically functional.

- [ ] [human] **Step 1: Lens visibility check**

With the enclosure closed, confirm the camera lens is unobstructed and centered - no shell material intrudes into the frame. Take a test photo and visually inspect for vignetting beyond what the spec expects from the optional OV5647 path.

- [ ] [human] **Step 2: LED visibility check**

Confirm the NeoPixel's solid green (recording), pulsing blue (uploading), pulsing yellow (low battery), and red flash (critical) states from the firmware plan's LED state machine are all visible from a normal wearing distance/angle, not hidden by the lanyard or shell edge.

- [ ] [human] **Step 3: Button accessibility check**

Confirm the tactile button is reachable by touch while worn (no need to remove the pendant to look at it) and has enough tactile distinction that it won't be confused with an edge of the shell.

- [ ] [human] **Step 4: Wear comfort check**

Wear the assembled pendant on the lanyard for several minutes. Confirm weight distribution doesn't cause obvious discomfort or the pendant flipping to show its back consistently (camera facing away from the wearer's intended subject).

---

## Handoff

Once Tasks 1-6 pass, the device is ready for Epic 5's integration checklist in [`2026-07-04-visio-epics-overview.md`](2026-07-04-visio-epics-overview.md): flashing it with the real firmware build, onboarding it to WiFi via the mobile app's QR flow, and running a full end-to-end recording-to-reel cycle.
