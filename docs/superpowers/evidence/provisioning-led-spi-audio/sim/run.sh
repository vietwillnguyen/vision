#!/usr/bin/env bash
# End-to-end simulation of firmware/scripts/setup-device.sh on a stand-in for
# a fresh Raspberry Pi OS trixie Pi Zero 2W, three runs in a row exactly as an
# operator does them: template checkpoint, full provision, re-run after the
# reboot. Then visio-preflight from the provisioned venv, as the operator and
# as the daemon's uid. Usage: run.sh <repo-worktree> <work-dir>
set -euo pipefail
repo="$(cd "$1" && pwd)"
work="$2"
here="$(cd "$(dirname "$0")" && pwd)"
image="visio-setup-sim:trixie"
name="visio-setup-sim-$$"
rm -rf "${work}" && mkdir -p "${work}"

docker build -q -t "${image}" "${here}" >/dev/null
sha="$(git -C "${repo}" rev-parse HEAD)"
git clone -q --bare "${repo}" "${work}/vision.git"
# Before the reboot: trixie's default zram+file swap on a 512 MB board.
printf 'MemTotal:         427880 kB\nSwapTotal:        213940 kB\nSwapFree:         213940 kB\n' > "${work}/meminfo"

docker run -d --name "${name}" \
  -v "${work}/vision.git:/mirror:ro" \
  -v "$(command -v uv):/usr/local/bin/uv:ro" \
  -v "${work}/meminfo:/proc/meminfo:ro" \
  "${image}" sleep infinity >/dev/null
trap 'docker rm -f "${name}" >/dev/null' EXIT
dx() { docker exec -i "${name}" bash -c "$1"; }

# The device's own state before provisioning.
dx "mkdir -p /boot/firmware /etc/rpi && printf '[Main]\nMechanism=zram+file\n' > /etc/rpi/swap.conf"
docker cp "${here}/stock-config.txt" "${name}:/boot/firmware/config.txt"
# Clone plumbing only: REPO_URL resolves to the local mirror, not GitHub.
dx "git config --system url./mirror.insteadOf https://github.com/vietwillnguyen/vision.git && git config --system safe.directory '*'"
dx "su pi -c 'git clone -q /mirror ~/vision && git -C ~/vision checkout -q --detach ${sha}'"

snapshot() {
  dx "cd / && sha256sum boot/firmware/config.txt etc/asound.conf etc/rpi/swap.conf.d/10-visio.conf \
      etc/systemd/system/visio-recorder.service etc/visio-recorder.env home/pi/.bashrc \
      home/pi/.config/nvim/init.lua 2>&1; readlink /usr/local/bin/visio-preflight"
}
provision() {
  dx "cd /home/pi && SUDO_USER=pi bash /home/pi/vision/firmware/scripts/setup-device.sh 2>&1"
}

echo "=== provisioning revision ${sha}"
echo "=== run 1: fresh device (stops at the env file template)"
rc=0; provision || rc=$?; echo "exit=${rc}"
echo "=== env file template written by run 1"
dx "cat /etc/visio-recorder.env"
dx "sed -i 's|^SUPABASE_URL=\$|SUPABASE_URL=https://example.supabase.co|; s|^SUPABASE_ANON_KEY=\$|SUPABASE_ANON_KEY=\"sim-anon-key\"|' /etc/visio-recorder.env"

echo "=== run 2: full provision"
rc=0; provision || rc=$?; echo "exit=${rc}"
echo "=== /boot/firmware/config.txt changes (diff against the stock file)"
docker cp "${name}:/boot/firmware/config.txt" "${work}/config.txt.after"
diff -u "${here}/stock-config.txt" "${work}/config.txt.after" --label stock --label provisioned || true
echo "=== /etc/asound.conf matches firmware/alsa/visio-mic.conf"
dx "cmp /etc/asound.conf /opt/visio-recorder/firmware/alsa/visio-mic.conf && echo identical"
echo "=== swap drop-in"
dx "cat /etc/rpi/swap.conf.d/10-visio.conf"
snapshot > "${work}/after-run-2.sha256"

echo "=== simulated reboot: rpi-swap applies the 1024 MiB swap file, which reports 1048572 kB (issue #49)"
printf 'MemTotal:         427880 kB\nSwapTotal:       1048572 kB\nSwapFree:        1048572 kB\n' > "${work}/meminfo"
# /run is a tmpfs on the device, so a reboot empties it.
dx "rm -f /run/visio-setup-reboot-required"
dx "grep SwapTotal /proc/meminfo"

echo "=== run 3: re-run on the provisioned, rebooted device"
rc=0; provision || rc=$?; echo "exit=${rc}"
snapshot > "${work}/after-run-3.sha256"
echo "=== managed files after run 2 vs run 3"
if diff "${work}/after-run-2.sha256" "${work}/after-run-3.sha256"; then
  echo "byte-identical: run 3 changed nothing"
  cat "${work}/after-run-3.sha256"
fi

echo "=== stubbed apt-get install lines (what a real device would install)"
dx "grep 'STUB apt-get install' /var/log/visio-sim-stubs.log | sort -u"

echo "=== venv imports the apt-installed hardware libraries, and no rpi_ws281x"
dx "/opt/visio-recorder/firmware/.venv/bin/python3 -c 'import spidev, gpiozero; print(\"spidev\", spidev.__file__); print(\"gpiozero\", gpiozero.__file__)'"
dx "/opt/visio-recorder/firmware/.venv/bin/python3 -c 'import rpi_ws281x' 2>&1 | tail -1"

echo "=== visio-preflight as the operator (pi), SPI not yet on (before the reboot)"
dx "su pi -c 'cd /tmp && visio-preflight'; echo exit=\$?"
echo "=== after the reboot: /dev/spidev0.0 appears root:spi 0660 (simulated node)"
dx "touch /dev/spidev0.0 && chgrp spi /dev/spidev0.0 && chmod 0660 /dev/spidev0.0 && ls -l /dev/spidev0.0"
echo "=== visio-preflight as the operator (pi, in the spi group)"
dx "su pi -c 'cd /tmp && visio-preflight'; echo exit=\$?"
echo "=== visio-preflight as an operator outside the spi group"
dx "useradd -m guest 2>/dev/null; su guest -c 'cd /tmp && visio-preflight' | grep -E 'spidev0.0|audio'"
echo "=== the unit's ExecStartPre (uid 0, env from the EnvironmentFile), audio set to off"
dx "set -a; . /etc/visio-recorder.env; VISIO_AUDIO=off; set +a; cd /opt/visio-recorder/firmware && .venv/bin/python3 -m visio_recorder.preflight | grep -E 'spidev|audio|data_dir'"
