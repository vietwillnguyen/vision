# Provisioning for the SPI status LED and the I2S microphone (PR #26 update)

Evidence for bringing `firmware/scripts/setup-device.sh` and `visio-preflight` up to date with the SPI LED rewire (#85), the 50 MB segment contract (#86) and the I2S microphone (#87), and for the swap re-run fix (#49).
No physical device was used; everything below is simulated, and the hardware checklist in the PR body covers what only the Pi can show.

## Files

- `01-issue-49-before-fix.txt` - the new trixie swap tests run against the unfixed step 1.
  `test_an_applied_1024mb_swap_file_is_a_no_op_on_re_run` reproduces the exact line from issue #49 (`Swap is 1023MB - wrote ... 10-visio.conf ...` and a reboot flag) from a `/proc/meminfo` reporting the 1048572 kB a 1024 MiB swap file really shows.
- `02-issue-49-after-fix.txt` - the same tests, plus the existing swap tests, passing after the fix.
- `03-e2e-simulation.log` - `sim/run.sh` output: the real script, three runs in a row, on a Debian trixie container standing in for a fresh Raspberry Pi OS trixie Pi Zero 2W, then `visio-preflight` from the provisioned venv.
- `04-firmware-suite.txt` - the whole firmware suite after the change.
- `sim/` - the harness: `Dockerfile`, `stubs/` (apt-get, apt-cache, dpkg, raspi-config, systemctl, update-alternatives), `stock-config.txt` (Raspberry Pi OS trixie's stock `/boot/firmware/config.txt`) and `run.sh`.

## What the simulation is and is not

Real: the whole of `setup-device.sh` as committed, bash, git checkout of the exact commit, `uv venv --system-site-packages` and `uv sync --locked`, Debian's `python3-spidev` and `python3-gpiozero` imported from the provisioned venv, `config.txt`, `/etc/asound.conf`, the rpi-swap drop-in, `~/.bashrc`, and `visio-preflight` itself.
Stubbed: apt and dpkg (logged, not run - the packages preflight imports are baked into the image instead), `raspi-config`, `systemctl`, the clone URL (rewritten to a local mirror of this branch), `/proc/meminfo` (bind-mounted so the swap reading can change across a simulated reboot), and `/dev/spidev0.0` (a regular file with the udev rule's `root:spi 0660`, so the open check sees the real permission split).
Not simulated: the camera, NetworkManager, `vcgencmd` and an actual I2S card, which is why those preflight lines read BLOCK or WARN in the log.

## Findings

- Run 1 stops at the env-file checkpoint but has already edited swap, I2C and `config.txt`; it now says `REBOOT REQUIRED once the re-run finishes`.
- Run 2 finds every `config.txt` line `already set` and still reports `REBOOT REQUIRED`, via the `/run/visio-setup-reboot-required` marker.
  Before that marker existed the simulation showed run 2 relying on the swap drop-in alone for its reboot notice; with swap already sized it would have printed `No reboot required.` on a device whose SPI bus did not exist yet.
  This was found by the simulation and fixed test-first in its own commit.
- Run 3, after a simulated reboot with `SwapTotal: 1048572 kB`, prints `No reboot required.` and `Setup complete.`, and every managed file is byte-identical to run 2 (checksums in the log): config.txt, asound.conf, swap drop-in, unit, env file, `~/.bashrc`, nvim config.
- `config.txt` gains exactly three lines under the stock file's final `[all]`: `dtparam=spi=on`, `core_freq_fixed=1`, `dtoverlay=adau7002-simple`.
- The provisioned venv imports `spidev` and `gpiozero` from `/usr/lib/python3/dist-packages` and has no `rpi_ws281x`.
- `visio-preflight`: `[BLOCK] spidev0.0` naming `dtparam=spi=on` and the reboot while the node is absent; `[OK] spidev0.0` once it exists for a user in the `spi` group; `[WARN]` (not BLOCK) for a user outside it; and `[OK] audio: VISIO_AUDIO=off` with no probe when audio is disabled.
  The microphone line is `[WARN] audio: no adau7002 sound card ...` with no I2S card present, never a block.

## Reproduce

```bash
docs/superpowers/evidence/provisioning-led-spi-audio/sim/run.sh . "$(mktemp -d)"
```

Needs Docker, network access for the image build and `uv sync`, and `uv` on the host's PATH (it is bind-mounted into the container).
