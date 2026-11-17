#!/usr/bin/env bash
# The step 3 failure paths, live, on the same trixie sim image as run.sh.
# Usage: failure-paths.sh <repo-worktree> <work-dir>
#  A) fresh device, raspi-config missing -> run 1 stops at the env checkpoint
#     and must print ERROR + INCOMPLETE and exit 1, keeping the reboot notice/marker.
#  B) fresh device, config.txt on a read-only mount -> real upstream do_spi
#     "succeeds" (exit 0) but writes nothing; get_spi re-check must catch it.
#  C) control: fresh device, everything fine -> checkpoint still exits 0.
set -uo pipefail
repo="$(cd "$1" && pwd)"; work="$2"; sim="${repo}/docs/superpowers/evidence/provisioning-led-spi-audio/sim"
image="visio-setup-sim:trixie"
docker build -q -t "${image}" "${sim}" >/dev/null || exit 1
rm -rf "${work}" && mkdir -p "${work}"
sha="$(git -C "${repo}" rev-parse HEAD)"
git clone -q --bare "${repo}" "${work}/vision.git"
printf 'MemTotal:         427880 kB\nSwapTotal:        213940 kB\nSwapFree:         213940 kB\n' > "${work}/meminfo"
cp "${sim}/stock-config.txt" "${work}/ro-config.txt"
names=()
simA="visio-fp-a-$$"; simB="visio-fp-b-$$"; simC="visio-fp-c-$$"
trap 'for n in "${names[@]}"; do docker rm -f "$n" >/dev/null; done' EXIT
start() { # $1 name, extra docker args...
  local n="$1"; shift; names+=("$n")
  docker run -d --name "$n" -v "${work}/vision.git:/mirror:ro" -v "$(command -v uv):/usr/local/bin/uv:ro" \
    -v "${work}/meminfo:/proc/meminfo:ro" "$@" "${image}" sleep infinity >/dev/null || exit 1
  docker exec "$n" bash -c "mkdir -p /boot/firmware /etc/rpi && printf '[Main]\nMechanism=zram+file\n' > /etc/rpi/swap.conf" || exit 1
  docker exec "$n" bash -c "git config --system url./mirror.insteadOf https://github.com/vietwillnguyen/vision.git && git config --system safe.directory '*'" || exit 1
  docker exec "$n" bash -c "su pi -c 'git clone -q /mirror ~/vision && git -C ~/vision checkout -q --detach ${sha}'" || exit 1
}
provision() { docker exec "$1" bash -c "cd /home/pi && SUDO_USER=pi bash /home/pi/vision/firmware/scripts/setup-device.sh 2>&1"; }
show() { grep -E 'Step 3/10|raspi-config|SPI|I2C|Camera|ERROR|INCOMPLETE|REBOOT|Setup (in)?complete|No reboot|template|fill in' <<<"$1"; }
echo "=== provisioning revision ${sha}"

echo "=== A) fresh device, raspi-config removed: run 1 (env-file checkpoint)"
start "${simA}"; docker cp "${sim}/stock-config.txt" "${simA}":/boot/firmware/config.txt || exit 1
docker exec "${simA}" mv /usr/bin/raspi-config /usr/bin/raspi-config.hidden || exit 1
rc=0; out="$(provision "${simA}")" || rc=$?; show "$out"; echo "exit=${rc}"
echo "--- /etc/visio-recorder.env template written? / owed-reboot marker:"
docker exec "${simA}" bash -c 'ls -l /etc/visio-recorder.env; ls -l /run/visio-setup-reboot-required && echo marker-present || echo marker-absent'
echo "--- config.txt spi/i2c lines (must still be commented: nothing enabled them)"
docker exec "${simA}" grep -nE 'dtparam=(spi|i2c_arm)' /boot/firmware/config.txt
echo "--- A2) operator fills the env file and re-runs, still without raspi-config"
docker exec "${simA}" sed -i 's|^SUPABASE_URL=$|SUPABASE_URL=https://example.supabase.co|; s|^SUPABASE_ANON_KEY=$|SUPABASE_ANON_KEY="sim-anon-key"|' /etc/visio-recorder.env
rc=0; out="$(provision "${simA}")" || rc=$?; show "$out"; echo "exit=${rc}"
echo "--- A3) raspi-config installed again, re-run converges"
docker exec "${simA}" mv /usr/bin/raspi-config.hidden /usr/bin/raspi-config
rc=0; out="$(provision "${simA}")" || rc=$?; show "$out"; echo "exit=${rc}"
docker exec "${simA}" bash -c 'for f in get_spi get_i2c; do echo "$f=$(raspi-config nonint $f)"; done'

echo "=== B) fresh device, /boot/firmware/config.txt on a read-only mount (real upstream raspi-config)"
start "${simB}" -v "${work}/ro-config.txt:/boot/firmware/config.txt:ro"
echo "--- upstream do_spi 0 exit status on the read-only file:"
docker exec "${simB}" bash -c 'raspi-config nonint do_spi 0 >/dev/null 2>&1; echo do_spi_exit=$?; echo get_spi=$(raspi-config nonint get_spi)'
rc=0; out="$(provision "${simB}")" || rc=$?; show "$out"; echo "exit=${rc}"
docker exec "${simB}" bash -c 'ls /run/visio-setup-reboot-required >/dev/null 2>&1 && echo marker-present || echo marker-absent'

echo "=== C) control: fresh healthy device, run 1 checkpoint"
start "${simC}"; docker cp "${sim}/stock-config.txt" "${simC}":/boot/firmware/config.txt || exit 1
rc=0; out="$(provision "${simC}")" || rc=$?; show "$out"; echo "exit=${rc}"
