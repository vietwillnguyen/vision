# De-rooted recorder: simulation evidence

Evidence for running `visio-recorder` as the unprivileged `visio` user (provisioning spec, "De-rooted recorder").
No physical device was used; the spec's "Needs the physical device" list and Task 9's Step 11 cover what only the Pi can show.

## Files

- `01-migration-simulation.log` - `sim/run.sh` output at this branch's merge with #26's `raspi-config` change.
- `02-unit-tests.txt` - the new and changed onboarding, preflight and setup-script tests, verbose.
- `03-firmware-suite.txt` - the whole firmware suite.
- `04-systemd-analyze-host.txt` - `systemd-analyze verify` on the repo's unit on a dev host (systemd 255), where the only complaint is the venv interpreter not existing; the log above has the clean run on the provisioned trixie stand-in (systemd 257).
- `sim/` - the harness: `Dockerfile`, `stubs/`, `stock-config.txt` and `run.sh`.

## What the simulation is and is not

Real: both `setup-device.sh` revisions (the root-era `a85ef42`, then this branch) run from the operator's clone, `useradd`/`usermod`/`groupadd`, Raspberry Pi OS's own `raspi-config` at the base harness's pinned commit, `uv sync`, the provisioned venv, `visio-preflight` and `python3 -m visio_recorder.preflight`, `systemd-analyze verify` (systemd 257), and a D-Bus system bus with polkitd 126 and NetworkManager 1.52.1, against which the firmware's own `NmcliWifiConfigurator` and `NmcliConnectionActivator` run as `visio`.
Stubbed: apt, `systemctl` (it logs, and answers `show` from the installed unit and drop-ins the way systemd would), `dtparam`, `modprobe`, the clone URL.
Simulated: device nodes are regular files with the owner, group and mode Raspberry Pi OS's udev rules give the real ones; the unit's process credentials are reproduced with `setpriv --reuid visio --regid visio --groups ...`, because systemd is not PID 1, so the sandboxing directives themselves are verified only by `systemd-analyze`.
Not simulated: the camera, the GPIO and SPI hardware, the microphone, WiFi radios, lgpio, and the hardened unit actually running.

## Findings

- A device provisioned by the root-era script (root daemon, `/etc/visio-recorder.env` 0600, a root-owned data dir with `session.json`, `device.json` and a queued segment, and a root-written `visio.nmconnection`) is migrated by one run: `visio` created in `video gpio i2c spi audio`, unit and polkit rule installed, env file `root:visio 0640`, data dir and everything in it `visio:visio`, dir 0750.
- The next run changes nothing: managed files, ownership and the account are byte-identical.
- `systemd-analyze verify` on the installed unit: no complaints.
- ExecStartPre as `visio` with the unit's groups: `[OK]` for `daemon_user` and every `*_access` line (camera nodes, `spidev0.0`, `gpiochip0`, `/dev/snd/*`, `i2c-1`) and for `data_dir`; the remaining BLOCKs are the absent camera binaries.
- Missing group: without `gpio`, `[BLOCK] gpio_access: visio cannot open /dev/gpiochip0 (root:gpio 0660) ... visio is not in the gpio group`; without `video` and `i2c`, a BLOCK listing all nine camera nodes and a WARN for `i2c-1`.
- Node owned wrongly (`/dev/spidev0.0 root:root 0600`): BLOCK pointing at the udev rules rather than at the account.
- Operator view (`pi`, and root under sudo) reports the daemon's access, not the caller's; the unit's `SupplementaryGroups=` keeps `gpio` granted after `visio` is removed from it in `/etc/group`, and dropping it from the unit too turns the line into a BLOCK.
- The daemon's files as `visio`: env file parsed, `session.json` and a root-era queued segment read, a new segment written.
- WiFi as `visio` through real polkitd and NetworkManager: `modify` over the root-era keyfile and `add` on a fresh device both succeed, NetworkManager writes `root:root 0600` keyfiles itself, and `connection up` is authorized (it fails only for want of a wifi device).
  `nmcli networking off` and `nmcli connection reload` are refused; without the polkit rule `configure` raises `nmcli connection modify exited 1` with no password in it and the stored profile is unchanged.
- Rollback: a `90-run-as-root.conf` drop-in is warned about and left alone by setup, and preflight then reports `daemon_user: root`.

## Reproduce

```bash
docs/superpowers/evidence/derooted-recorder/sim/run.sh . "$(mktemp -d)" a85ef42
```

Needs Docker, network access for the image build and `uv sync`, and `uv` on the host's PATH.
