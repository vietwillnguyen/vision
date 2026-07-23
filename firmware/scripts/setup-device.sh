#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/vietwillnguyen/vision.git"
INSTALL_DIR="/opt/visio-recorder"
ENV_FILE="/etc/visio-recorder.env"
DATA_DIR="/var/lib/visio-recorder"
SYSTEMD_UNIT_SRC="firmware/systemd/visio-recorder.service"
SYSTEMD_UNIT_DST="/etc/systemd/system/visio-recorder.service"

reboot_needed=false

log() {
  echo "[setup-device] $*"
}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Re-running as root..." >&2
  exec sudo -E bash "$0" "$@"
fi

log "Step 1/9: swap size"
SWAP_CONF="/etc/dphys-swapfile"
SWAP_SIZE_MB=1024
if [[ -f "${SWAP_CONF}" ]]; then
  current_swap="$(grep -E '^CONF_SWAPSIZE=' "${SWAP_CONF}" | cut -d= -f2- || echo 0)"
  if [[ "${current_swap:-0}" -lt "${SWAP_SIZE_MB}" ]]; then
    sed -i "s/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=${SWAP_SIZE_MB}/" "${SWAP_CONF}"
    systemctl restart dphys-swapfile
    log "Swap was ${current_swap:-0}MB - increased to ${SWAP_SIZE_MB}MB (Pi Zero 2W's 512MB RAM needs this for apt/dpkg operations, or package configuration can thrash swap badly enough to look like a hung/dropped SSH session)"
  else
    log "Swap already ${current_swap}MB (>= ${SWAP_SIZE_MB}MB) - no change needed"
  fi
else
  log "No ${SWAP_CONF} found - skipping swap size check (non-standard OS image?)"
fi

log "Step 2/9: apt packages"
# Bookworm's needrestart hook can pop an interactive "which services to
# restart" prompt on packages like network-manager that touch running
# daemons; over a non-interactive SSH pipe that hangs the script forever
# instead of failing loudly. Force fully-automatic mode.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a
# Defensive: if a prior run was killed mid dpkg-configure (e.g. the
# swap-thrashing hang Step 1 now prevents), finish that before proceeding
# rather than letting apt-get fail confusingly on a half-configured package.
dpkg --configure -a
apt-get update -y
apt-get install -y rpicam-apps zbar-tools ffmpeg network-manager git pijuice-base

log "Step 3/9: interfaces"
current_i2c="$(raspi-config nonint get_i2c || echo 1)"
if [[ "${current_i2c}" != "0" ]]; then
  raspi-config nonint do_i2c 0
  reboot_needed=true
  log "I2C was disabled - enabled now, reboot required"
else
  log "I2C already enabled"
fi

if raspi-config nonint get_camera >/dev/null 2>&1; then
  current_camera="$(raspi-config nonint get_camera || echo 1)"
  if [[ "${current_camera}" != "0" ]]; then
    raspi-config nonint do_camera 0
    reboot_needed=true
    log "Camera was disabled - enabled now, reboot required"
  else
    log "Camera already enabled"
  fi
else
  log "No raspi-config camera toggle on this OS image - assuming auto-detected (Bookworm default)"
fi

log "Step 4/9: uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
else
  log "uv already installed"
fi

log "Step 5/9: code"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  git -C "${INSTALL_DIR}" pull
else
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi
(cd "${INSTALL_DIR}/firmware" && uv sync --locked)

log "Step 6/9: systemd"
cp "${INSTALL_DIR}/${SYSTEMD_UNIT_SRC}" "${SYSTEMD_UNIT_DST}"
systemctl daemon-reload
systemctl enable visio-recorder

log "Step 7/9: env file"
if [[ ! -f "${ENV_FILE}" ]]; then
  cat > "${ENV_FILE}" <<'EOF'
SUPABASE_URL=
SUPABASE_ANON_KEY=
# Bring-up default: no PiJuice HAT installed yet (availability blocked as of
# 2026-07-21), running from a USB power bank instead. Switch to "pijuice"
# once real battery hardware is wired - see
# docs/superpowers/specs/2026-07-21-visio-device-provisioning-design.md
VISIO_BATTERY_SOURCE=none
EOF
  chmod 600 "${ENV_FILE}"
  log "Wrote template ${ENV_FILE} - fill in SUPABASE_URL and SUPABASE_ANON_KEY, then re-run this script"
  exit 0
else
  missing=()
  for key in SUPABASE_URL SUPABASE_ANON_KEY; do
    value="$(grep -E "^${key}=" "${ENV_FILE}" | cut -d= -f2- || true)"
    if [[ -z "${value}" ]]; then
      missing+=("${key}")
    fi
  done
  if [[ "${#missing[@]}" -gt 0 ]]; then
    log "ERROR: ${ENV_FILE} exists but is missing values for: ${missing[*]}"
    exit 1
  fi
  log "${ENV_FILE} already configured"
fi

log "Step 8/9: data dir"
mkdir -p "${DATA_DIR}"

log "Step 9/9: summary"
battery_source="$(grep -E '^VISIO_BATTERY_SOURCE=' "${ENV_FILE}" | cut -d= -f2- || echo pijuice)"
battery_source="${battery_source:-pijuice}"
log "VISIO_BATTERY_SOURCE=${battery_source}"
if [[ "${battery_source}" == "none" ]]; then
  log "Bring-up mode: running without a PiJuice HAT (USB power bank). Not a failure - expected for now."
fi
if [[ "${reboot_needed}" == "true" ]]; then
  log "REBOOT REQUIRED: interface changes need a reboot to take effect before the daemon will work."
else
  log "No reboot required."
fi
log "Setup complete."
