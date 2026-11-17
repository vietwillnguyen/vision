#!/usr/bin/env bash
# End-to-end simulation of the root -> visio migration on a stand-in for a
# Raspberry Pi OS trixie Pi Zero 2W already provisioned by the root-era
# setup-device.sh, with a real D-Bus, polkitd and NetworkManager.
# Usage: run.sh <repo-worktree> <work-dir> <root-era-ref>
set -euo pipefail
repo="$(cd "$1" && pwd)"
work="$2"
old_ref="$3"
here="$(cd "$(dirname "$0")" && pwd)"
image="visio-derooted-sim:trixie"
name="visio-derooted-sim-$$"
rm -rf "${work}" && mkdir -p "${work}"

docker build -q -t "${image}" "${here}" >/dev/null
sha="$(git -C "${repo}" rev-parse HEAD)"
old_sha="$(git -C "${repo}" rev-parse "${old_ref}")"
git clone -q --bare "${repo}" "${work}/vision.git"
# Swap already sized: this is a device that has been through bring-up.
printf 'MemTotal:         427880 kB\nSwapTotal:       1048572 kB\nSwapFree:        1048572 kB\n' > "${work}/meminfo"

docker run -d --name "${name}" \
  -v "${work}/vision.git:/mirror:ro" \
  -v "$(command -v uv):/usr/local/bin/uv:ro" \
  -v "${work}/meminfo:/proc/meminfo:ro" \
  "${image}" sleep infinity >/dev/null
trap 'docker rm -f "${name}" >/dev/null' EXIT
dx() { docker exec -i "${name}" bash -c "$1"; }
section() { printf '\n=== %s\n' "$*"; }

dx "mkdir -p /boot/firmware /etc/rpi && printf '[Main]\nMechanism=zram+file\n' > /etc/rpi/swap.conf"
docker cp "${here}/stock-config.txt" "${name}:/boot/firmware/config.txt"
dx "git config --system url./mirror.insteadOf https://github.com/vietwillnguyen/vision.git && git config --system safe.directory '*'"
dx "su pi -c 'git clone -q /mirror ~/vision'"

section "system bus, polkitd and NetworkManager (real daemons, systemd is not PID 1)"
dx "mkdir -p /run/dbus && dbus-daemon --system --fork && sleep 0.5
    (/usr/lib/polkit-1/polkitd --no-debug >/var/log/polkitd.log 2>&1 &)
    sleep 1; (NetworkManager --no-daemon >/var/log/nm.log 2>&1 &); sleep 3
    nmcli general status; dpkg-query -W network-manager polkitd"

# The operator runs the script from their own clone, at the revision being
# provisioned, exactly as the hardware checklist does.
provision() {
  dx "su pi -c 'git -C ~/vision checkout -q --detach $1' && cd /home/pi && SUDO_USER=pi bash /home/pi/vision/firmware/scripts/setup-device.sh 2>&1"
}

section "root era: provision with ${old_sha} (the daemon runs as root)"
rc=0; provision "${old_sha}" > "${work}/root-era-template.log" || rc=$?; echo "template run exit=${rc}"; tail -3 "${work}/root-era-template.log"
dx "sed -i 's|^SUPABASE_URL=\$|SUPABASE_URL=https://example.supabase.co|; s|^SUPABASE_ANON_KEY=\$|SUPABASE_ANON_KEY=\"sim-anon-key\"|' /etc/visio-recorder.env"
rc=0; provision "${old_sha}" | tail -4 || rc=$?; echo "full run exit=${rc}"
section "root era: what the root daemon left behind (session, identity, queued segment, keyfile)"
dx "d=/var/lib/visio-recorder; mkdir -p \$d/queue
    printf '{\"access_token\":\"at\",\"refresh_token\":\"rt\"}' > \$d/session.json; chmod 600 \$d/session.json
    echo '{\"device_id\":\"sim\"}' > \$d/device.json; touch \$d/device_registered
    head -c 1024 /dev/urandom > \$d/queue/20261117T070000.mp4
    printf '[connection]\nid=visio\ntype=wifi\nautoconnect=true\n\n[wifi]\nmode=infrastructure\nssid=HomeNet\n\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=hunter2secret\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n' > /etc/NetworkManager/system-connections/visio.nmconnection
    chmod 600 /etc/NetworkManager/system-connections/visio.nmconnection; nmcli connection reload
    grep -E '^(User|Group)=' /etc/systemd/system/visio-recorder.service || echo '(unit has no User= - runs as root)'
    ls -ln /etc/visio-recorder.env; ls -lnR \$d; nmcli -g NAME connection show | grep -x visio"

section "migration: provision with ${sha} on the same device"
rc=0; provision "${sha}" || rc=$?; echo "exit=${rc}"

snapshot() {
  dx "cd / && sha256sum etc/systemd/system/visio-recorder.service etc/polkit-1/rules.d/50-visio-recorder.rules \
      etc/visio-recorder.env boot/firmware/config.txt etc/asound.conf home/pi/.bashrc
      stat -c '%n %U:%G %a' etc/visio-recorder.env var/lib/visio-recorder var/lib/visio-recorder/session.json
      id visio; ls /etc/systemd/system/visio-recorder.service.d 2>/dev/null || true"
}
snapshot > "${work}/after-migration.txt"
section "re-run: a migrated device converges to no change"
rc=0; provision "${sha}" > "${work}/rerun.log" || rc=$?; echo "exit=${rc}"
grep -E 'visio|0640|0750|Installed|Created|Added|Daemon user|Setup' "${work}/rerun.log" || true
snapshot > "${work}/after-rerun.txt"
if diff "${work}/after-migration.txt" "${work}/after-rerun.txt"; then
  echo "byte-identical: the re-run changed nothing"
fi

section "after migration: account, env file, data dir, unit, polkit rule"
dx "id visio; getent passwd visio
    ls -ln /etc/visio-recorder.env; stat -c '%n %U:%G %a' /etc/visio-recorder.env
    find /var/lib/visio-recorder -printf '%p %u:%g %m\n'
    grep -E '^(User|Group|SupplementaryGroups|StateDirectory|RuntimeDirectory|Environment|ProtectSystem|NoNewPrivileges)=' /etc/systemd/system/visio-recorder.service
    stat -c '%n %U:%G %a' /etc/polkit-1/rules.d/50-visio-recorder.rules"

section "systemd-analyze verify on the installed unit (real systemd $(docker exec "${name}" systemd-analyze --version | head -1))"
dx "systemd-analyze verify /etc/systemd/system/visio-recorder.service 2>&1 && echo 'verify: no complaints'"

section "fake device nodes, owned the way Raspberry Pi OS's udev rules own them"
dx "mkdir -p /dev/dma_heap /dev/snd
    for n in media0 media1 video0 video10 video11 video12 v4l-subdev0 dma_heap/linux,cma dma_heap/system; do
      : > /dev/\$n; chown root:video /dev/\$n; chmod 0660 /dev/\$n; done
    : > /dev/spidev0.0; chown root:spi /dev/spidev0.0; chmod 0660 /dev/spidev0.0
    : > /dev/gpiochip0; chown root:gpio /dev/gpiochip0; chmod 0660 /dev/gpiochip0
    : > /dev/i2c-1; chown root:i2c /dev/i2c-1; chmod 0660 /dev/i2c-1
    for n in controlC0 pcmC0D0c timer; do : > /dev/snd/\$n; chown root:audio /dev/snd/\$n; chmod 0660 /dev/snd/\$n; done
    ls -l /dev/video* /dev/media* /dev/v4l-subdev* /dev/dma_heap/* /dev/spidev0.0 /dev/gpiochip0 /dev/i2c-1 /dev/snd/*"

# The unit's ExecStartPre as systemd would run it: as visio, with the
# account's groups plus SupplementaryGroups=, the env file loaded, from the
# unit's WorkingDirectory. VISIO_BATTERY_SOURCE=pijuice so the i2c path runs.
as_daemon() {
  dx "set -a; . /etc/visio-recorder.env; VISIO_BATTERY_SOURCE=pijuice; LG_WD=/tmp; set +a
      cd /opt/visio-recorder/firmware
      setpriv --reuid visio --regid visio --groups $1 -- .venv/bin/python3 -m visio_recorder.preflight; echo exit=\$?"
}
section "ExecStartPre as visio with every unit group (video gpio i2c spi audio)"
as_daemon video,gpio,i2c,spi,audio
section "ExecStartPre as visio without gpio: the flag button's group is missing"
as_daemon video,i2c,spi,audio | grep -E 'gpio_access|daemon_user|exit='
section "ExecStartPre as visio without video and i2c"
as_daemon gpio,spi,audio | grep -E 'camera_access|i2c_access|exit='
section "a node not owned the way the udev rules make it (/dev/spidev0.0 root:root 0600)"
dx "chown root:root /dev/spidev0.0 && chmod 0600 /dev/spidev0.0"
as_daemon video,gpio,i2c,spi,audio | grep -E 'spi|exit='
dx "chown root:spi /dev/spidev0.0 && chmod 0660 /dev/spidev0.0"

section "operator view: visio-preflight as pi and under sudo answers for visio"
dx "su pi -c 'cd /tmp && visio-preflight' | grep -E 'daemon_user|_access|data_dir'; echo exit=\$?"
dx "cd /tmp && visio-preflight | grep -E 'daemon_user|_access|data_dir'"
section "operator view: visio dropped from gpio in /etc/group - the unit still grants it"
dx "gpasswd -d visio gpio >/dev/null && id visio && su pi -c 'cd /tmp && visio-preflight' | grep -E 'gpio_access'"
section "operator view: and with SupplementaryGroups= dropping gpio too (drop-in)"
dx "mkdir -p /etc/systemd/system/visio-recorder.service.d && printf '[Service]\nSupplementaryGroups=video i2c spi audio\n' > /etc/systemd/system/visio-recorder.service.d/50-sim.conf
    su pi -c 'cd /tmp && visio-preflight' | grep -E 'gpio_access'
    rm /etc/systemd/system/visio-recorder.service.d/50-sim.conf; usermod -aG gpio visio"

section "the daemon's files as visio: env file, session, queue, a new segment"
dx "cd /opt/visio-recorder/firmware && setpriv --reuid visio --regid visio --groups video,gpio,i2c,spi,audio -- .venv/bin/python3 - <<'PY'
from pathlib import Path
from visio_recorder.preflight import read_env_file
d = Path('/var/lib/visio-recorder')
print('env file keys:', sorted(read_env_file() or {}))
print('session.json readable:', bool((d / 'session.json').read_text()))
print('queued segment readable:', len((d / 'queue/20261117T070000.mp4').read_bytes()), 'bytes')
(d / 'queue/20261117T070100.mp4').write_bytes(b'new')
print('new segment written as visio')
PY
    ls -ln /var/lib/visio-recorder/queue"

section "wifi onboarding as visio through the real NetworkManager and polkitd"
onboard() {
  dx "cd /opt/visio-recorder/firmware && setpriv --reuid visio --regid visio --init-groups -- env HOME=/var/lib/visio-recorder .venv/bin/python3 - <<'PY'
from visio_recorder.onboarding import NmcliConnectionActivator, NmcliWifiConfigurator, WifiConfigurationError
wifi = NmcliWifiConfigurator()
print('profile visio exists before onboarding:', wifi.exists())
try:
    wifi.configure('$1', '$2')
    print('configure ok')
except WifiConfigurationError as exc:
    print('configure refused:', exc)
try:
    NmcliConnectionActivator().activate('visio')
except Exception as exc:
    print('activate:', type(exc).__name__, exc.returncode)
PY
    nmcli -g NAME connection show | grep -cx visio | sed 's/^/profiles named visio: /'
    ls -ln /etc/NetworkManager/system-connections/
    grep -E '^(ssid|psk)=' /etc/NetworkManager/system-connections/visio.nmconnection"
}
echo "--- re-onboarding over the root-era keyfile (modify path)"
onboard OtherNet newsecret99 2>&1
echo "--- connection up as visio: authorized, fails only for want of a wifi device"
dx "setpriv --reuid visio --regid visio --init-groups -- nmcli connection up visio; echo exit=\$?"
echo "--- fresh device: no profile yet (add path)"
dx "nmcli connection delete visio >/dev/null"
onboard HomeNet hunter2secret 2>&1
echo "--- what the rule does not grant"
dx "setpriv --reuid visio --regid visio --init-groups -- nmcli networking off; echo exit=\$?
    setpriv --reuid visio --regid visio --init-groups -- nmcli connection reload; echo exit=\$?"
echo "--- without the polkit rule: refused, the error carries no password, the stored profile is unchanged"
dx "mv /etc/polkit-1/rules.d/50-visio-recorder.rules /root/ && sleep 2"
onboard HomeNet2 another-secret-1 2>&1 | grep -E 'configure|psk='
dx "mv /root/50-visio-recorder.rules /etc/polkit-1/rules.d/ && sleep 2"

section "rollback: a drop-in running the daemon as root, which setup leaves alone"
dx "printf '[Service]\nUser=root\nGroup=root\n' > /etc/systemd/system/visio-recorder.service.d/90-run-as-root.conf"
provision "${sha}" | grep -E 'WARNING|runs as root|Setup'
dx "cat /etc/systemd/system/visio-recorder.service.d/90-run-as-root.conf
    su pi -c 'cd /tmp && visio-preflight' | grep -E 'daemon_user'"
dx "rm /etc/systemd/system/visio-recorder.service.d/90-run-as-root.conf"

section "stubbed apt-get install lines"
dx "grep 'STUB apt-get install' /var/log/visio-sim-stubs.log | sort -u"
