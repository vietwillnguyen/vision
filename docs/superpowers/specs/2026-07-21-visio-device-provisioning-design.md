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
    daemon.py               # modified: calls preflight.run_checks() first in main()
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
6. **Env file** — if `/etc/visio-recorder.env` doesn't exist, write a template with `SUPABASE_URL=`/`SUPABASE_ANON_KEY=` placeholders and exit with an actionable message. If it exists, leave it untouched (never overwrite real secrets) and just verify the required keys are present and non-placeholder.
7. **Data dir** — `mkdir -p /var/lib/visio-recorder`.
8. **Summary** — report what changed; if step 2 changed anything, explicitly say a reboot is required before the daemon will work.

### `firmware/visio_recorder/preflight.py`

Fast (sub-few-seconds), side-effect-free checks. Collects every failure rather than stopping at the first, so a single run tells you everything that's wrong instead of one problem at a time across repeated runs.

| Check | Method |
|---|---|
| `rpicam-vid`, `rpicam-still`, `zbarimg`, `ffmpeg`, `nmcli` on PATH | `shutil.which()` |
| `pijuice`, `rpi_ws281x`, `gpiozero`, `supabase` importable | `importlib.util.find_spec()` |
| I2C enabled | `/dev/i2c-1` exists |
| Camera detected | `rpicam-hello --list-cameras` reports at least one camera |
| NetworkManager running | `nmcli general status` succeeds |
| Data dir writable | `/var/lib/visio-recorder` exists and is writable |

The I2C check deliberately stops at "is the bus enabled," not "does the PiJuice respond" — that deeper check already happens in `run_startup_sequence`'s real battery read, with its own halt/critical-LED handling. Preflight answers "can this environment work at all," not "is today's hardware reading good."

Each check is a small function behind an injectable seam (e.g. a `which` callable), following the existing `Protocol`-based fake-in-tests convention used throughout `firmware/`, so this gets real unit test coverage without touching hardware in CI.

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
- **Physical Pi Zero 2W integration testing** (now available): run `setup-device.sh` on the real device end to end, confirm `preflight.py` passes cleanly afterward, then exercise the existing risk list from the earlier assessment - camera detection, PiJuice I2C read, LED driver under root, QR onboarding round-trip, NM keyfile write + `nmcli` activation, flag-button debounce, a full capture/mux/upload cycle, restart/crash recovery, and power-loss resilience.

## Open Questions / Future Work

- Whether `rpi_ws281x` genuinely requires root on this specific board/kernel combo, or would work via `spidev` under a non-root user, is being deferred rather than investigated — running as root (this spec's decision) makes the question moot for now, but worth revisiting if the device's threat model ever changes (e.g., if it grows a network-facing control surface).
- The setup script does not handle the reboot itself; a follow-up manual step (or a documented `sudo reboot` in the script's final summary output) is still needed the first time interfaces are enabled.
