# Visio Device Provisioning & Preflight - Design Spec

## Overview

`firmware/`'s device-setup story today is one prose sentence in the README (`apt install zbar-tools`, `pijuice`, `rpi_ws281x`) plus ad-hoc manual commands buried in the hardware-assembly plan.
It has already drifted from reality: `ffmpeg` (required by `muxer.py` for every segment mux) is undocumented as a prerequisite, and the hardware plan's Task 3 Step 3 has a human run `pip install pijuice` globally, which installs outside the project's `uv`-managed venv entirely.
Separately, nothing in the codebase verifies its own runtime environment before recording starts: a missing binary or unconfigured interface currently surfaces as an opaque crash mid-daemon rather than a clear diagnostic.
This spec covers two components that close both gaps: an idempotent device-provisioning script, and a firmware-level preflight check.

## Goals

- One idempotent script that takes a freshly flashed Raspberry Pi OS Lite (Bookworm) image to a state where `visio-recorder` can run, safe to re-run after an SD re-flash, OS update, or to repair drift.
- A firmware-level preflight check that fails fast with a clear diagnostic when the runtime environment is missing something the daemon needs, instead of failing opaquely partway through startup.
- Resolve the NetworkManager-keyfile-write permission gap (`wifi_onboard.py`'s `write_nm_keyfile` writes into a root-owned directory) and the likely-related `rpi_ws281x` root/DMA requirement, both currently unaddressed by the existing `User=pi` unit.
- Run without a PiJuice HAT physically present: the PiJuice Zero pHAT specified in the hardware BOM is currently blocked on availability. Bring-up proceeds on a generic USB power bank instead, so neither the setup script nor the preflight check may treat missing battery hardware as fatal.

## Battery hardware: deferred (2026-07-21)

The hardware BOM (`2026-07-04-visio-pendant-design.md`, `2026-07-04-visio-hardware.md`) specifies a PiJuice Zero pHAT, currently unavailable.
For now, the device runs from a generic USB power bank into the Pi's power input instead - no HAT, no I2C battery telemetry, no low-battery LED behavior.
Real alternatives (PiSugar 2/3, a generic I2C-fuel-gauge UPS HAT, or a hand-built TP4056/IP5306 + MAX17048 combo) were researched and are viable drop-in replacements later, since `BatteryReader` is already a `Protocol` (`get_charge_pct() -> int`) - swapping hardware means a new driver class in `drivers.py` plus one line in `daemon.py`'s wiring, not a firmware rewrite.
Picking and wiring one of those is out of scope here and tracked as future work below.
This spec's job is narrower: make the *current* firmware and provisioning tooling work correctly with no battery HAT at all, and make that state visible rather than silently assumed.

## Non-Goals

- Physical assembly, wiring, or enclosure work (covered by `2026-07-04-visio-hardware.md`, entirely `[human]` steps).
- Fleet provisioning / multiple-device management. This is a single-pendant hobby device; the script targets one Pi at a time over SSH.
- Changing the recording/upload/onboarding business logic in `daemon.py` beyond the preflight call and the `User=pi` removal.
- Auto-reboot handling — the script detects and reports when a reboot is needed; it never reboots the device itself.

## Architecture

Two new pieces, plus one permission-model change to the existing systemd unit:

```
firmware/
  scripts/
    setup-device.sh       # new: idempotent OS-level provisioning
  visio_recorder/
    preflight.py           # new: runtime environment verification
    drivers.py               # modified: adds UnmeteredPowerReader (no HAT present)
    daemon.py               # modified: calls preflight.run_checks() first in main();
                             # selects battery driver from VISIO_BATTERY_SOURCE
  systemd/
    visio-recorder.service  # modified: root instead of User=pi, ExecStartPre= added,
                             # ExecStart/ExecStartPre point at the venv's python
```

### `firmware/scripts/setup-device.sh`

Bash, run as root (or self-elevates via `sudo`) over SSH on the target Pi. Idempotent: every step checks current state before acting, so re-running after the first successful run is a fast no-op pass with a clean summary.

Steps, in order:

1. **apt packages** — `rpicam-apps`, `zbar-tools`, `ffmpeg`, `network-manager`, `git`, `pijuice-base`. `pijuice-base` replaces the hardware plan's stray `pip install pijuice`: it installs into system Python, which matters once the daemon runs as root against system-level hardware libraries rather than a project venv for those specific libraries (see `drivers.py`'s deferred-import convention).
2. **Interfaces** — check `raspi-config nonint get_i2c` and the camera overlay state first; only run `raspi-config nonint do_i2c 0` / enable the camera if not already on. Track whether either changed.
3. **`uv`** — check `command -v uv`; install via the official astral.sh installer if missing (that installer is itself safe to re-run).
4. **Code** — `/opt/visio-recorder`: `git pull` if a `.git` dir already exists there, else `git clone` the `vision` repo. Then `cd firmware && uv sync --locked` (prod deps only).
5. **systemd** — copy `firmware/systemd/visio-recorder.service` into `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable visio-recorder`. Both the copy and `enable` are naturally idempotent.
6. **Env file** — if `/etc/visio-recorder.env` doesn't exist, write a template with `SUPABASE_URL=`/`SUPABASE_ANON_KEY=` placeholders plus `VISIO_BATTERY_SOURCE=none` (commented, explaining it's the current bring-up default pending PiJuice/alternative hardware) and exit with an actionable message. If it exists, leave it untouched (never overwrite real secrets) and just verify the required keys are present and non-placeholder.
7. **Data dir** — `mkdir -p /var/lib/visio-recorder`.
8. **Summary** — report what changed; if step 2 changed anything, explicitly say a reboot is required before the daemon will work. Also echoes the configured `VISIO_BATTERY_SOURCE` and, when it's `none`, a one-line reminder that this is a bring-up-only configuration - never a hard failure, since a missing PiJuice HAT is an accepted, current-known state, not a setup error.

### `firmware/visio_recorder/preflight.py`

Fast (sub-few-seconds), side-effect-free checks. Collects every failure rather than stopping at the first, so a single run tells you everything that's wrong instead of one problem at a time across repeated runs.

Each check carries a severity. `block` means the daemon cannot function without it and preflight exits non-zero; `warn` means the daemon can still run, degraded, and preflight logs clearly but exits 0.

| Check | Method | Severity |
|---|---|---|
| `rpicam-vid`, `rpicam-still`, `zbarimg`, `ffmpeg`, `nmcli` on PATH | `shutil.which()` | block |
| `rpi_ws281x`, `gpiozero`, `supabase` importable | `importlib.util.find_spec()` | block |
| Camera detected | `rpicam-hello --list-cameras` reports at least one camera | block |
| NetworkManager running | `nmcli general status` succeeds | block |
| Data dir writable | `/var/lib/visio-recorder` exists and is writable | block |
| `pijuice` importable, I2C enabled (`/dev/i2c-1` exists) | `importlib.util.find_spec()`, path check | **warn** - and only run at all when `VISIO_BATTERY_SOURCE=pijuice`; skipped entirely under `=none` since there's nothing to warn about in the intended bring-up state |

The I2C check deliberately stops at "is the bus enabled," not "does the PiJuice respond" — that deeper check already happens in `run_startup_sequence`'s real battery read, with its own halt/critical-LED handling, when a PiJuice is actually configured as the battery source. Preflight answers "can this environment work at all," not "is today's hardware reading good."

Each check is a small function behind an injectable seam (e.g. a `which` callable), following the existing `Protocol`-based fake-in-tests convention used throughout `firmware/`, so this gets real unit test coverage without touching hardware in CI.

### Battery source selection

New `DaemonConfig.battery_source: str` field, read from `VISIO_BATTERY_SOURCE` (default `"pijuice"`, the existing behavior). `daemon.py`'s `main()` selects the driver accordingly:

- `"pijuice"` → `PiJuiceBatteryReader()` (existing, unchanged)
- `"none"` → `UnmeteredPowerReader()` (new, in `drivers.py`) - `get_charge_pct()` always returns `100`

`100` specifically, not some "unknown" sentinel: `battery.py`'s `read_battery_status` is purely numeric (`should_halt = pct < 10`, `is_low = pct < 20`), so any fallback value has to clear both thresholds or it would falsely halt the device or show a low-battery LED while running from mains-equivalent USB power. An unrecognized `VISIO_BATTERY_SOURCE` value is a startup config error (same treatment as a missing `SUPABASE_URL` in `load_config` today) - fails loudly rather than silently picking a default, so a typo doesn't quietly disable battery protection later once real hardware is wired up.

This is deliberately explicit config, not auto-detection with a silent fallback: auto-detecting "PiJuice not responding, fall back to unmetered" would also mask a real wiring fault once a PiJuice HAT is actually installed. The operator states which mode they're in.

Wired in three places:
- `ExecStartPre=` in the systemd unit — blocks the daemon from starting at all on a broken environment; ExecStartPre failures are visually distinct from runtime crashes in `journalctl`.
- First call inside `daemon.py`'s `main()`, before `load_config` — belt-and-suspenders if ever run outside systemd.
- Standalone: `python3 -m visio_recorder.preflight` for manual SSH diagnostics.

### Permission model change

`visio-recorder.service` drops `User=pi` (runs as root). This resolves two gaps at once:

- `write_nm_keyfile` (`wifi_onboard.py`) writes into `/etc/NetworkManager/system-connections/`, root:root mode 700 by default — currently would hit a `PermissionError` under `User=pi` the first time onboarding actually runs on hardware.
- `rpi_ws281x`'s PWM/DMA access for the WS2812 LED typically needs root.

Given the device's threat model (offline pendant, no other users, no exposed network services), full-root for a single-purpose daemon is a reasonable trade against maintaining two separate narrow-privilege mechanisms.

This also surfaces and fixes a latent bug: the current unit's `ExecStart=/usr/bin/python3 -m visio_recorder.daemon` assumes a system-wide install, but `uv sync` (step 4 of the setup script) creates a project-local `.venv` under `/opt/visio-recorder`. Both `ExecStart` and the new `ExecStartPre` need to point at `/opt/visio-recorder/.venv/bin/python3` instead.

## Testing

- **Unit tests** (no hardware): `preflight.py`'s individual check functions, following the existing fake-injection pattern — same treatment as every other hardware boundary in this codebase.
- **`setup-device.sh`**: no meaningful unit test (it's OS provisioning); validated by physical integration testing below, run twice to confirm the idempotency claim (second run is a clean no-op pass).
- **Physical Pi Zero 2W integration testing** (now available, running on a USB power bank per the battery-hardware deferral above): run `setup-device.sh` on the real device end to end, confirm `preflight.py` passes cleanly afterward with `VISIO_BATTERY_SOURCE=none` and no battery-related warnings printed, then exercise the risk list from the earlier assessment - camera detection, LED driver under root, QR onboarding round-trip, NM keyfile write + `nmcli` activation, flag-button debounce, a full capture/mux/upload cycle, restart/crash recovery, and power-loss resilience. The PiJuice-specific checks (I2C read, low-battery/critical LED thresholds) are deferred until real battery hardware is sourced.

## Open Questions / Future Work

- Whether `rpi_ws281x` genuinely requires root on this specific board/kernel combo, or would work via `spidev` under a non-root user, is being deferred rather than investigated — running as root (this spec's decision) makes the question moot for now, but worth revisiting if the device's threat model ever changes (e.g., if it grows a network-facing control surface).
- The setup script does not handle the reboot itself; a follow-up manual step (or a documented `sudo reboot` in the script's final summary output) is still needed the first time interfaces are enabled.
- Selecting and wiring the real battery hardware (PiSugar 2/3, a generic I2C-fuel-gauge UPS HAT, or a hand-built TP4056/IP5306 + MAX17048 combo - see "Battery hardware: deferred" above) is separate future work: a new `BatteryReader` driver plus a new `VISIO_BATTERY_SOURCE` value. Not scoped here.
- The hardware BOM documents (`2026-07-04-visio-pendant-design.md`, `2026-07-04-visio-hardware.md`) still specify PiJuice as the power management line item; they carry a pointer note to this spec but haven't been rewritten, since the eventual replacement isn't chosen yet.
