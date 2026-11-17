# Visio Device Provisioning & Preflight - Design Spec

## Overview

`firmware/`'s device-setup story today is one prose sentence in the README (`apt install zbar-tools`, `pijuice`, `rpi_ws281x`) plus ad-hoc manual commands buried in the hardware-assembly plan.
It has already drifted from reality: `ffmpeg` (required by `muxer.py` for every segment mux) is undocumented as a prerequisite, and the hardware plan's Task 3 Step 3 has a human run `pip install pijuice` globally, which installs outside the project's `uv`-managed venv entirely.
Separately, nothing in the codebase verifies its own runtime environment before recording starts: a missing binary or unconfigured interface currently surfaces as an opaque crash mid-daemon rather than a clear diagnostic.
This spec covers two components that close both gaps: an idempotent device-provisioning script, and a firmware-level preflight check.

## Goals

- One idempotent script that takes a freshly flashed Raspberry Pi OS Lite image (trixie / Debian 13, the OS verified on the device during bring-up; Bookworm remains supported by the script) to a state where `visio-recorder` can run, safe to re-run after an SD re-flash, OS update, or to repair drift.
- A firmware-level preflight check that fails fast with a clear diagnostic when the runtime environment is missing something the daemon needs, instead of failing opaquely partway through startup.
- Resolve the NetworkManager-keyfile-write permission gap (`wifi_onboard.py`'s `write_nm_keyfile` writes into a root-owned directory) and the likely-related `rpi_ws281x` root/DMA requirement, both currently unaddressed by the existing `User=pi` unit.
  (The LED SPI rewire later retired `rpi_ws281x`; see "LED SPI rewire follow-ups for PR #26".)
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
  pyproject.toml            # modified: [build-system], without which uv treats the
                             # project as virtual and never installs visio_recorder;
                             # plus the visio-preflight [project.scripts] entry point
```

### `firmware/scripts/setup-device.sh`

Bash, run as root (or self-elevates via `sudo`) over SSH on the target Pi. Idempotent: every step checks current state before acting, so re-running after the first successful run is a fast no-op pass with a clean summary.

Steps, in order (the numbering matches the script's own `Step N/10` log lines):

1. **Swap size** — guarantee a 1024MB swap file before any apt work, since the Pi Zero 2W's 512MB of RAM makes dpkg thrash badly enough to look like a dropped SSH session. Handles both the bookworm (`/etc/dphys-swapfile`) and trixie (`rpi-swap` drop-in) mechanisms; the trixie path needs a reboot to apply.
   The trixie path converges on the drop-in's contents and compares `SwapTotal` in KiB with a one-MiB allowance, because the 1024 MiB file it pins reports as 1048572 kB (mkswap keeps a 4 KiB header page): compared in whole MiB, that read 1023 < 1024 and every re-run rewrote the drop-in and asked for a reboot ([#49](https://github.com/vietwillnguyen/vision/issues/49)).
   A drop-in already in place but not yet active still reports the reboot as owed, without rewriting anything.
2. **apt packages** — required: `rpicam-apps`, `zbar-tools`, `ffmpeg`, `network-manager`, `git`, `python3-gpiozero`, `python3-spidev`, `alsa-utils`. `python3-gpiozero` and `python3-spidev` are required rather than best-effort because preflight blocks on `gpiozero` and `spidev` being importable, so a release that cannot supply them should fail here rather than hand back a device whose daemon never starts; Debian ships both, `spidev` prebuilt. `alsa-utils` supplies `arecord` for checking the microphone by hand. Best-effort: `pijuice-base`, which replaces the hardware plan's stray `pip install pijuice` (it installs into system Python, which matters once the daemon runs as root against system-level hardware libraries rather than a project venv for those specific libraries — see `drivers.py`'s deferred-import convention). `pijuice-base` is installed only when it is present in the running release's package index: the Raspberry Pi archive ships it for bookworm but dropped it in trixie, and per the non-negotiable above a missing battery driver must never fail setup. The script probes `apt-cache policy` rather than ignoring the install's exit code, so a genuine install failure still aborts.
3. **Interfaces** - SPI (the status LED), I2C (the PiJuice or a replacement fuel gauge) and the camera are enabled through Raspberry Pi OS's own `raspi-config nonint`, never by our own `config.txt` edit, so they are set the way the OS sets them (see "Interfaces through raspi-config" below for the exact upstream semantics this relies on).
   For each, `raspi-config nonint get_spi` / `get_i2c` / `get_camera` is asked first and prints `0` when the interface is enabled; only otherwise does the script run `do_spi 0` / `do_i2c 0` / `do_camera 0` (`0` means enable), so a re-run logs `already enabled` and changes nothing.
   Because `do_*` exits 0 even when its write failed, success is judged by asking `get_*` again; an interface still off afterwards, or a `get_spi` / `get_i2c` that fails outright, is carried to the summary as `INCOMPLETE`.
   A `raspi-config` with no `get_camera` skips the camera toggle instead.
   If `raspi-config` is not installed at all (an image other than Raspberry Pi OS), the step logs an `ERROR`, enables nothing, and the summary says `INCOMPLETE`; there is deliberately no fallback edit, because a script that quietly does something other than what the OS does is how the two drift apart.
   Then ensure the two lines in `/boot/firmware/config.txt` that `raspi-config` has no option for: `core_freq_fixed=1` for the SPI status LED and `dtoverlay=adau7002-simple` for the I2S microphone (pendant spec, "GPIO Pin Map", "LED driver" and "Audio Capture").
   Each is idempotent: a line already applying to the Zero 2W (global, or under `[all]`) is left alone, a conflicting single-valued setting (`core_freq_fixed=0`) is rewritten in place rather than shadowed, and a missing line is appended, under a fresh `[all]` when the file ends inside another board's filter such as `[cm5]`.
   Lines under another board's filter never count, since they never reach this board.
   The file is replaced atomically via a temp file, so a full or read-only boot partition leaves it untouched; that failure is carried to the summary as `INCOMPLETE` rather than aborting.
   Any change, by `raspi-config` or by our edit, marks a reboot as required.
4. **`uv`** — check `command -v uv`; if missing, download the astral.sh installer **at a pinned version**, verify it against a recorded SHA-256, and only then execute it. The device holds Supabase credentials and the installer runs as root, so an unpinned `curl | sh` is not acceptable; the version and digest are constants at the top of the script and are bumped together.
5. **Code** — `/opt/visio-recorder`, checked out at an **explicit revision** rather than `git pull`. The revision is `VISIO_GIT_REF` if set, else the exact commit of the checkout the script itself is running from, else `main` with a loud warning that this is the one non-reproducible path. The update path fetches all branches and tags, resolves the ref to a SHA, then `checkout --force --detach` + `reset --hard`, so it converges from a diverged branch or local edits instead of assuming a clean fast-forward; untracked paths are left alone because `firmware/.venv` lives here. Then `cd firmware && uv sync --locked` (prod deps only, exact), and symlink the `visio-preflight` console script from the venv into `/usr/local/bin`. The venv itself is created up front with `uv venv --python /usr/bin/python3 --system-site-packages`, not left to `uv sync`: a venv uv builds on its own is isolated from the system site-packages *and* is built from an interpreter uv downloaded for itself, so it can import none of `gpiozero`, `spidev` or `pijuice` — the hardware libraries that arrive by apt (step 2), the first two of which preflight blocks on. It is recreated, not repaired, when it fails either condition, since the base interpreter is fixed at creation; the check is repeated after the sync so a venv that silently lost the setting fails the run rather than the daemon. Nothing is compiled on the device: until the LED SPI rewire, `rpi_ws281x` was built from source into the venv (with `build-essential` and `python3-dev`) and kept by `--inexact`; the exact sync now also removes that wheel from a device provisioned before the rewire.
6. **systemd and ALSA** — copy `firmware/systemd/visio-recorder.service` into `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable visio-recorder`. Both the copy and `enable` are naturally idempotent.
   Then install `firmware/alsa/visio-mic.conf` as `/etc/asound.conf`, which defines the `visio_mic` PCM: an identical file is left alone, and any other file already there is kept once as `/etc/asound.conf.pre-visio` before being replaced, since it may be an operator's own.
   Installed whether or not a microphone is wired, because the daemon records video only when `visio_mic` does not open.
7. **Env file** — if `/etc/visio-recorder.env` doesn't exist, write a template with `SUPABASE_URL=`/`SUPABASE_ANON_KEY=` placeholders plus `VISIO_BATTERY_SOURCE=none` and `VISIO_DEV_SHELL=1` (both commented, explaining the bring-up defaults), and every other key the daemon reads - `VISIO_DATA_DIR`, the video and segment-size keys (`VISIO_VIDEO_WIDTH`, `VISIO_VIDEO_HEIGHT`, `VISIO_FRAMERATE`, `VISIO_VIDEO_BITRATE`, `VISIO_MAX_UPLOAD_BYTES`, `VISIO_SEGMENT_DURATION_MS`) and the audio keys (`VISIO_AUDIO`, `VISIO_AUDIO_DEVICE`, `VISIO_AUDIO_BITRATE`) - commented out at their defaults, since the segment length is derived only while it stays unset; a test pins that list against the keys `daemon.py` and `preflight.py` actually read. Then exit with an actionable message. If it exists, leave it untouched (never overwrite real secrets) and just verify the required keys are present and non-placeholder. This runs *before* the developer shell so the gate below is honest: a fresh device stops here with the switch already visible, and nothing has been installed yet.
8. **Developer shell** — editor and shell conveniences for bring-up over SSH: `neovim` (also set as the `editor` alternative and as `EDITOR`/`VISUAL`), `ripgrep`, `fd-find`, `tmux`, `htop`, `tree`, `jq`, `less`, `bash-completion`, plus a managed block in the *invoking* user's `~/.bashrc` (resolved from `SUDO_USER`, not root's `$HOME`) carrying the standard `ls` aliases, colour, history settings, and a git-branch-aware prompt. Gated on `VISIO_DEV_SHELL` in the env file, defaulting to enabled; set it to `0` for a shipped appliance image. The switch covers these editor and interactive-shell conveniences and nothing else. The `~/.bashrc` block is delimited and rewritten in place, so re-running is byte-identical rather than appending, and disabling the switch removes the block rather than merely stopping future updates - packages already installed are deliberately left alone, so pre-seeding the env file is the only path to a device that never carries these conveniences. Packages are probed against the release's index with the same pattern as `pijuice-base`. Resolving the invoking user's home and group is guarded: an unresolvable account skips this optional step rather than aborting provisioning before the data dir and summary.
9. **Data dir** — `mkdir -p` the directory the daemon will actually record into, which is `VISIO_DATA_DIR` from the env file when set and `/var/lib/visio-recorder` otherwise. Provisioning the configured one rather than the default matters because preflight checks the same value: creating only the default would leave preflight blocking on a directory nothing ever creates, and advising a re-run that would not create it either. The value is operator input rather than a constant this script owns, so it is validated - a non-absolute path is rejected outright, since it would resolve against the script's cwd here but against the unit's `WorkingDirectory=` for the daemon - and a failed `mkdir` is reported and carried to the summary rather than aborting, because step 10 is where the operator finds out what happened.
10. **Summary** - report what changed, including the exact provisioned commit SHA, the data dir, and the `visio-preflight` command; if step 3 changed anything, explicitly say a reboot is required before the daemon will work. Also echoes the configured `VISIO_BATTERY_SOURCE` and, when it's `none`, a one-line reminder that this is a bring-up-only configuration - never a hard failure, since a missing PiJuice HAT is an accepted, current-known state, not a setup error. A step-3 interface `raspi-config` could not enable (or a missing `raspi-config`), a step-3 `config.txt` edit that failed, and a step-9 data dir that could not be created are each repeated here as an explicit `INCOMPLETE` line, carried in a flag the way the reboot notice is: the mid-run error is thousands of lines of apt output away by the time an operator reads the summary.
    An owed reboot is also remembered across runs in `/run/visio-setup-reboot-required`, which only an actual reboot clears (`/run` is a tmpfs) and which an `EXIT` trap writes however the run ends - checkpoint, error exit or summary: a fresh device's first run makes its reboot-needing changes and then stops at the step-7 checkpoint, so without the marker the second run would find every edit already made and report no reboot on a device whose SPI bus does not exist yet.
    The checkpoint itself also prints the reboot notice. The last line and the exit status then follow those flags - `Setup incomplete` and exit 1 when any of them is set, `Setup complete.` and exit 0 otherwise - so neither the operator's last line nor a caller testing `$?` reports success for a device that cannot record or cannot start. This is distinct from the env-file step's exit 0, which is a deliberate re-run checkpoint rather than a failure: "not finished yet, by design" and "finished but broken" are different answers and get different statuses.
    The one exception is a step-3 failure already known by the time the checkpoint is reached: the checkpoint then prints the same step-3 `INCOMPLETE` lines the summary would (one shared helper prints both), followed by `Setup incomplete`, and exits 1, so a fresh non-Raspberry-Pi OS device's first run does not end on success. The reboot notice and the owed-reboot marker are kept either way.

### Interfaces through raspi-config

Verified against [`raspi-config` on RPi-Distro's `trixie` branch at `22cea75`](https://github.com/RPi-Distro/raspi-config/blob/22cea75ecbcb8e6f6e2e80a7f565d6cfe154bc02/raspi-config) (package `20261026+2`, the branch's head on 2026-11-17), not from memory:

- **Which file.** `CONFIG` is `/boot/firmware/config.txt` when that file exists and `/boot/config.txt` otherwise, the same file step 3's own edits target on bookworm and later.
- **Dispatch and exit codes.** `raspi-config nonint <function> [args]` sets `INTERACTIVE=False`, calls the shell function of that name, and exits with its status; an unknown name exits 127.
  `get_spi`, `get_i2c` and `get_camera` report on **stdout** (`0` enabled, `1` not) and always exit 0 - a missing `config.txt` also reads `1`.
  `do_spi`, `do_i2c` and `do_camera` end on an `if [ "$INTERACTIVE" = True ]` with no branch taken, so with a valid argument they exit 0 whether or not their write succeeded; only a value other than `0` or `1` is returned as an error.
  That is why the script asks `get_*` again after `do_*` rather than trusting its exit status.
- **What `get_spi` / `get_i2c` read.** An active line matching `^(device_tree_param|dtparam)=([^,]*,)*spi(=(on|true|yes|1))?(,.*)?$` (with `i2c(_arm)?` for I2C), anywhere in the file.
  It is not section-aware: a `dtparam=spi=on` under another board's filter such as `[pi4]` also reads as enabled.
  The pendant's `config.txt` has no such line, and preflight's `/dev/spidev0.0` check catches the case after the reboot.
- **What `do_spi 0` writes.** `set_config_var dtparam=spi on`, an `awk` pass that rewrites every line matching `^#?[[:space:]]*dtparam=spi=` - commented or not, in any section - to `dtparam=spi=on`, or appends that line at the end of the file if none matched, via a `.bak` temp file renamed over the original.
  On a stock Raspberry Pi OS trixie image that uncomments the stock `#dtparam=spi=on` in place, in the global section above the board filters.
  It then un-blacklists `spi-bcm2708` in `/etc/modprobe.d/raspi-blacklist.conf` (creating the file) and runs `dtparam spi=on`, which applies the parameter to the running kernel's device tree, so `/dev/spidev0.0` can appear before any reboot.
- **What `do_i2c 0` writes.** The same for `dtparam=i2c_arm=on`, then it un-blacklists `i2c-bcm2708`, makes sure `/etc/modules` lists `i2c-dev`, runs `dtparam i2c_arm=on` and `modprobe i2c-dev`, so `/dev/i2c-1` can also appear immediately.
- **The camera.** On Debian 11 and later `get_camera` reads `camera_auto_detect` (absent counts as enabled, which trixie's stock `camera_auto_detect=1` is anyway) and `do_camera 0` sets `camera_auto_detect=1`.
- **Why a reboot is still required.** The live `dtparam` loads `spi-bcm2835` against a core clock that `core_freq_fixed=1` only pins from the next boot, and `spi-bcm2835` caches the clock rate at probe, so the LED's bit timing is only trustworthy after a reboot; the summary therefore reports `REBOOT REQUIRED` for any interface change.

`firmware/tests/fixtures/raspi-config` carries those upstream functions byte-for-byte (only `/etc/modules`, `dtparam` and `modprobe` redirected), and a test compares the copy against that pinned revision; the end-to-end simulation runs the real upstream script at the same commit.

### `firmware/visio_recorder/preflight.py`

Fast (sub-few-seconds), side-effect-free checks. Collects every failure rather than stopping at the first, so a single run tells you everything that's wrong instead of one problem at a time across repeated runs.

Each check carries a severity. `block` means the daemon cannot function without it and preflight exits non-zero; `warn` means the daemon can still run, degraded, and preflight logs clearly but exits 0.

| Check | Method | Severity |
|---|---|---|
| `rpicam-vid`, `rpicam-still`, `zbarimg`, `ffmpeg`, `nmcli` on PATH | `shutil.which()` | block |
| `spidev`, `gpiozero`, `supabase` importable | `importlib.util.find_spec()` | block |
| `/dev/spidev0.0` opens read-write (the status LED's bus) | `os.open(..., O_RDWR)` | block when missing (SPI off, or not rebooted since setup) or when the daemon's uid cannot open it; **warn** for another caller refused by permissions (not in the `spi` group) |
| Core clock held fixed | `vcgencmd get_config core_freq_fixed` reads `core_freq_fixed=1` | **warn** - a scaling core clock skews the LED's SPI timing, it does not stop recording |
| Microphone opens | the daemon's own `capture.probe_audio_device` (a 0.2 s `ffmpeg -f alsa` capture from `VISIO_AUDIO_DEVICE`, resolved like the data dir); on failure, checks `/proc/asound/cards` for the `adau7002` card, then `/etc/asound.conf` for the PCM, so the detail names the missing layer (overlay, ALSA config, or wiring / device in use) | **warn** always - the daemon records video only without it; reported as OK and not probed under `VISIO_AUDIO=off` |
| Camera detected | `rpicam-hello --list-cameras` reports at least one camera | block |
| NetworkManager running | `nmcli general status` succeeds | block |
| Data dir writable | the daemon's own data dir - `VISIO_DATA_DIR` from the environment, else from `/etc/visio-recorder.env`, else `/var/lib/visio-recorder`; `daemon.load_config` calls the same resolver, so there is no second default to keep in step - exists and is writable | block, except **exists-but-unwritable for a non-daemon caller**, which is warn - see below |
| `pijuice` importable, I2C enabled (`/dev/i2c-1` exists) | `importlib.util.find_spec()`, path check | **warn** - and only run at all when `VISIO_BATTERY_SOURCE=pijuice`; skipped entirely under `=none` since there's nothing to warn about in the intended bring-up state |

A failed import names the step that actually installs that module rather than a single generic "check the uv sync step": only `supabase` comes from `uv.lock`, `gpiozero` and `spidev` are apt's `python3-gpiozero` and `python3-spidev` reached through the venv's `--system-site-packages`.

Which directory gets checked is caller-relative too, and for the same reason. `ExecStartPre` inherits `VISIO_DATA_DIR` from the unit's `EnvironmentFile=`; the standalone `visio-preflight` inherits nothing, so it reads `/etc/visio-recorder.env` itself. That file is mode 600 root-owned and stays that way - it is not relaxed for a diagnostic's benefit - so an unprivileged run cannot see a configured value. It checks the default and says so on the `data_dir` line rather than reporting a guess as a verified fact, which would show a blocking failure for a directory that is correctly absent on a device recording to a USB mount. Severity is untouched by the caveat: which directory was checked, and how certain that choice is, are separate facts from whether the directory that was checked is usable.

The data-dir check splits along which half of it is caller-relative, because the same module is both the daemon's startup gate and an operator diagnostic.
Existence is not caller-relative: `/var/lib` is 0755 root:root, so a missing data dir looks missing to every uid, and a device without one is simply not provisioned - `setup-device.sh` exits at the env-file step before it ever reaches the data-dir step, both on a fresh device and whenever the Supabase keys are unfilled.
That case stays `block` for every caller, with the actionable "run setup-device.sh" advice.
Writability is caller-relative, and the directory is deliberately root-owned - it holds the captain's recorded video, so it is not widened for a diagnostic's benefit.
An existing-but-unwritable directory is therefore still `block` for the uid the unit starts the daemon as, and `warn` for an operator over SSH, saying plainly that this is expected rather than a fault - otherwise every healthy device reports a spurious blocking failure during the physical checklist.
`_DAEMON_UID` is pinned to the unit by a test asserting `visio-recorder.service` carries no `User=` directive, so the deferred de-root task has to update it deliberately rather than silently degrading the daemon's own gate.

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
- Standalone: `visio-preflight` for manual SSH diagnostics.
  This is a `[project.scripts]` console script, symlinked into `/usr/local/bin` by the setup script, so it runs from any directory as a plain non-root SSH user - no venv path, no `sudo`, no `cd`.
  `python3 -m visio_recorder.preflight` remains equivalent when invoked with the venv interpreter.
  Its report opens with an `[INFO] install:` line naming the directory the running package was imported from, so a transcript is unambiguous about which of the device's two clones produced it.

### Permission model change

`visio-recorder.service` drops `User=pi` (runs as root). This resolves two gaps at once:

- `write_nm_keyfile` (`wifi_onboard.py`) writes into `/etc/NetworkManager/system-connections/`, root:root mode 700 by default — currently would hit a `PermissionError` under `User=pi` the first time onboarding actually runs on hardware.
- `rpi_ws281x`'s PWM/DMA access for the WS2812 LED typically needs root.
  (No longer true: the LED now runs over `spidev`, so this reason is gone; see "Now possible: drop root" below.)

Given the device's threat model (offline pendant, no other users, no exposed network services), full-root for a single-purpose daemon is a reasonable trade against maintaining two separate narrow-privilege mechanisms.

This also surfaces and fixes a latent bug: the current unit's `ExecStart=/usr/bin/python3 -m visio_recorder.daemon` assumes a system-wide install, but `uv sync` (step 5 of the setup script) creates a project-local `.venv` at the uv project root, which is `firmware/` - not the repo root. Both `ExecStart` and the new `ExecStartPre` need to point at `/opt/visio-recorder/firmware/.venv/bin/python3` instead.

## Testing

- **Unit tests** (no hardware): `preflight.py`'s individual check functions, following the existing fake-injection pattern — same treatment as every other hardware boundary in this codebase.
- **`setup-device.sh`**: the OS-provisioning steps (apt, raspi-config, systemd) have no meaningful unit test and are validated by the physical integration testing below, run twice to confirm the idempotency claim. The pure-bash parts are testable off-device and are covered in `firmware/tests/test_setup_device.py`, which extracts the real shell functions out of the script and runs them: the `~/.bashrc` managed block (a second run must be byte-identical, never an append; a block whose end marker was hand-deleted is refused rather than truncating the file), the `VISIO_DEV_SHELL` gate and the `EnvironmentFile` parsing it reads through, the step ordering that gate depends on, the pinned-and-checksum-verified `uv` install, and revision resolution and convergence exercised against a real local git repo. `shellcheck` runs on the script in CI (`.github/workflows/lint.yml`).
  The same file covers the trixie swap step's convergence (#49), step 3's interfaces against a copy of upstream `raspi-config`'s own functions (SPI and I2C enabled once and verified, a second pass changing nothing, a `do_spi` that silently changes nothing and a missing `raspi-config` both reported `INCOMPLETE`, at the summary and at the step-7 checkpoint with exit 1), the remaining `config.txt` edits against Raspberry Pi OS's stock file (added once, conflicting values replaced, other boards' filters ignored, a second pass byte-identical, an unwritable boot partition left untouched), the ALSA config install, the owed-reboot marker, and the env template against the keys the daemon reads.
- **End-to-end simulation**: `docs/superpowers/evidence/provisioning-led-spi-audio/sim/run.sh` runs the whole script three times on a Debian trixie container standing in for a fresh device - template checkpoint, full provision, re-run after a simulated reboot - then a fourth time with `raspi-config` removed, with the real upstream `raspi-config` at the pinned commit and only apt, systemctl, `dtparam`, `modprobe` and the boot partition stubbed or seeded, then runs `visio-preflight` from the provisioned venv as the operator, as a user outside the `spi` group, and as the unit's `ExecStartPre` would.
- **Physical Pi Zero 2W integration testing** (now available, running on a USB power bank per the battery-hardware deferral above): run `setup-device.sh` on the real device end to end, confirm `preflight.py` passes cleanly afterward with `VISIO_BATTERY_SOURCE=none` and no battery-related warnings printed, then exercise the risk list from the earlier assessment - camera detection, LED driver under root, QR onboarding round-trip, NM keyfile write + `nmcli` activation, flag-button debounce, a full capture/mux/upload cycle, restart/crash recovery, and power-loss resilience. The PiJuice-specific checks (I2C read, low-battery/critical LED thresholds) are deferred until real battery hardware is sourced.

## Open Questions / Future Work

- ~~Whether `rpi_ws281x` genuinely requires root on this specific board/kernel combo, or would work via `spidev` under a non-root user~~ - resolved by the LED SPI rewire: the firmware no longer uses `rpi_ws281x`; see "LED SPI rewire follow-ups for PR #26" below.
- The setup script does not handle the reboot itself; a follow-up manual step (or a documented `sudo reboot` in the script's final summary output) is still needed the first time interfaces are enabled.
- Selecting and wiring the real battery hardware (PiSugar 2/3, a generic I2C-fuel-gauge UPS HAT, or a hand-built TP4056/IP5306 + MAX17048 combo - see "Battery hardware: deferred" above) is separate future work: a new `BatteryReader` driver plus a new `VISIO_BATTERY_SOURCE` value. Not scoped here.
- The hardware BOM documents (`2026-07-04-visio-pendant-design.md`, `2026-07-04-visio-hardware.md`) still specify PiJuice as the power management line item; they carry a pointer note to this spec but haven't been rewritten, since the eventual replacement isn't chosen yet.

## LED SPI rewire follow-ups for PR #26

The firmware now drives the WS2812B status LED over SPI0 MOSI (GPIO 10) with py-spidev instead of `rpi_ws281x` PWM/DMA on GPIO 18 (pendant spec, "GPIO Pin Map" and "LED driver").
PR #26 provisioned and checked for `rpi_ws281x`, so it needed the changes below; they were deliberately not made in the rewire PR, which left the unit and `setup-device.sh` to #26.
Status as of the 2026-11-17 update of #26: items 1-6 are done and covered by `firmware/tests/test_setup_device.py` and `firmware/tests/test_preflight.py`, and by an end-to-end run of the real script on a Debian trixie stand-in (`docs/superpowers/evidence/provisioning-led-spi-audio/`); items 7-10 remain open; the last list needs the physical device.

### Required for the LED to work

1. [x] **`setup-device.sh` apt step:** add `python3-spidev` to the `apt-get install` line (Debian trixie ships it, 3.6-1, arm64; prebuilt, nothing compiles on the device).
2. [x] **`setup-device.sh` code step:** delete `RPI_WS281X_SPEC`, the `rpi_ws281x` import probe, the build-dependency install, the `uv pip install` of `rpi-ws281x`, the `ws281x_missing` summary flag and its comments.
   With it goes the only on-device compile, and with that the build toolchain the step installs.
   The venv keeps `--system-site-packages`, which is how the daemon already imports apt-installed `gpiozero` and will import `spidev`.
3. [x] **`setup-device.sh` interfaces step:** enable SPI, marking a reboot as required when it was off.
   Done through `raspi-config nonint do_spi 0`, checked first with `raspi-config nonint get_spi` and verified with it afterwards, exactly as I2C already was (see "Interfaces through raspi-config").
   It was first built as our own `dtparam=spi=on` edit of `config.txt`; that was replaced so the interfaces are set the way Raspberry Pi OS sets them, and our own edit is now kept only for the two settings `raspi-config` has no option for.
4. [x] **`setup-device.sh` interfaces step:** ensure `core_freq_fixed=1` is present in `/boot/firmware/config.txt` (idempotent append under `[all]`), marking a reboot as required when it was added.
   Without it the Zero 2W's core clock scales between 250 and 400 MHz while `spi-bcm2835` keeps the rate it cached at probe, which can shift the SPI clock by up to 1.6x and corrupt colours.
   Raspberry Pi recommends this flag over pinning `core_freq` directly; it holds the higher 400 MHz rate, so it does not cost H.264 encode headroom.
5. [x] **`preflight.py`:** replace the `rpi_ws281x` importable check with `spidev` importable (block), and add `/dev/spidev0.0` exists and is writable by the daemon's user (block - it fails when SPI is off or the user lacks the `spi` group).
   Optionally warn when `vcgencmd get_config core_freq_fixed` is not `1` (done, as a warn).
   As built, the device check opens the node read-write rather than testing writability, and a permission refusal blocks only the daemon's own uid; an operator outside the `spi` group gets a warning, the same caller-relative split as the data dir.
6. [x] **Docs in #26:** replace `rpi_ws281x` with `python3-spidev` wherever the prerequisites are listed.

### Now possible: drop root

The "Permission model change" above ran the daemon as root for two reasons, and the rewire removes one of them (`/dev/mem`).
The other, NetworkManager, needs a firmware change as well as configuration, so de-rooting is a separate follow-up rather than part of #26's LED changes.

**Status: still open, deliberately (2026-11-17).**
De-rooting was reconsidered for #26 and left out, because its correctness hinges on what only the device can show.
Items 8-10 change who opens the camera (`/dev/video*`, `/dev/media*`, `/dev/dma_heap/*` for libcamera), the GPIO chip, the I2C bus and the SPI node, and who talks to NetworkManager; a simulation can check the unit file, the group memberships and the polkit rule's syntax, but not that `rpicam-vid` captures, the flag button reads, or `nmcli connection up` succeeds as `visio` on Raspberry Pi OS's real udev rules and polkit.
A mistake there produces a pendant that silently does not record, which is worse than the current root daemon on a single-purpose, offline device.
It should land in its own PR together with its physical test (run onboarding, a capture, an LED colour, a flag press and an upload as `visio`, plus `journalctl` free of permission errors), and that PR must also move `preflight._DAEMON_UID`, which a test pins to the unit's lack of `User=`.

7. [ ] **Service user:** create a system user (for example `visio`, `useradd --system --home-dir /var/lib/visio-recorder --shell /usr/sbin/nologin`) in groups `video` (camera and `rpicam-*`), `gpio` (flag button via `/dev/gpiochip*`), `i2c` (`/dev/i2c-1` for the PiJuice or replacement fuel gauge) and `spi` (`/dev/spidev0.0`).
   Raspberry Pi OS's udev rules (`raspberrypi-sys-mods`, `99-com.rules`) give `spidev`, `i2c-dev` and `*gpiomem*` devices to those groups with mode 0660.
   `chown -R visio: /var/lib/visio-recorder`, and make `/etc/visio-recorder.env` `root:visio` mode 0640.
8. [ ] **Unit:** add `User=visio` and `Group=visio` (plus `SupplementaryGroups=video gpio i2c spi` so the groups apply even if the account's membership drifts), keep `ExecStartPre` and `ExecStart` as they are.
9. [ ] **NetworkManager - firmware change first:** a polkit rule alone cannot de-root the current onboarding code.
   `wifi_onboard.write_nm_keyfile` writes into the root-only `/etc/NetworkManager/system-connections/`, and NetworkManager refuses keyfiles that are not owned by root ("File owner ... is insecure", `nms-keyfile-utils.c`), so a non-root daemon cannot author a keyfile NetworkManager will load.
   `nmcli connection reload` (D-Bus `ReloadConnections`) is denied to every non-root caller by NetworkManager's own D-Bus policy (`org.freedesktop.NetworkManager.conf`), which polkit never sees.
   So onboarding has to create the profile through NetworkManager instead: `nmcli connection add type wifi con-name visio ifname wlan0 ssid <ssid> wifi-sec.key-mgmt wpa-psk wifi-sec.psk <psk>` (or `connection modify` when `visio` exists), which NetworkManager writes as a root-owned keyfile itself, then `nmcli connection up visio`.
   The "keyfile exists" re-onboarding check becomes "connection `visio` exists".
10. [ ] **Polkit rule** for the new calls, in `/etc/polkit-1/rules.d/50-visio-recorder.rules`:

    ```js
    polkit.addRule(function (action, subject) {
      if (subject.user === "visio" &&
          (action.id === "org.freedesktop.NetworkManager.settings.modify.system" ||
           action.id === "org.freedesktop.NetworkManager.network-control")) {
        return polkit.Result.YES;
      }
    });
    ```

    `settings.modify.system` covers adding and modifying the system connection, `network-control` covers `connection up`.

### Needs the physical device

- Move the LED data wire from GPIO 18 (header pin 12) to GPIO 10 (header pin 19).
- After provisioning and the reboot it asks for, confirm `ls -l /dev/spidev0.0` shows `root spi` mode `0660` and `visio-preflight` reports `[OK] spidev0.0` and `[OK] core_freq_fixed`.
- Visually check each colour after provisioning: green (recording), blue (uploading), yellow (low battery), red (critical).
- Confirm the WS2812B latches 3.3 V SPI data reliably.
  The WS2812B-V5 input-high threshold is 0.63 x VDD, 3.15 V at a 5 V supply, so a 3.3 V GPIO meets the datasheet with only 150 mV of margin (legacy pre-V5 parts needed 0.7 x VDD, 3.5 V); this was equally true of the PWM wiring, so it is not new, but it has never been bench-tested.
  If colours flicker or come out wrong, add a 74AHCT125 (or SN74LVC1T45) level shifter on the data line.
  Powering the LED from 3.3 V is not an option, because the V5 minimum supply is 3.7 V.
- Confirm which WS2812B revision is fitted.
  The SPI encoding targets the WS2812B-V5 timing windows; a legacy pre-V5 part sees its T1H 10 ns short and its T1L 40 ns long, so check its colours on the bench or swap it for a V5 part.
- Confirm `vcgencmd measure_clock core` reads 400 MHz both idle and while `rpicam-vid` is encoding, with `core_freq_fixed=1` set.
