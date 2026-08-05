#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/vietwillnguyen/vision.git"
INSTALL_DIR="/opt/visio-recorder"
FIRMWARE_DIR="${INSTALL_DIR}/firmware"
VENV_DIR="${FIRMWARE_DIR}/.venv"
ENV_FILE="/etc/visio-recorder.env"
DATA_DIR="/var/lib/visio-recorder"
SYSTEMD_UNIT_SRC="firmware/systemd/visio-recorder.service"
SYSTEMD_UNIT_DST="/etc/systemd/system/visio-recorder.service"
BIN_DIR="/usr/local/bin"
PREFLIGHT_BIN="visio-preflight"

# ${VENV_DIR}/bin/python3 is the interpreter behind both ${PREFLIGHT_BIN} and
# the systemd unit's ExecStart/ExecStartPre, and the hardware libraries they
# import - gpiozero, pijuice, rpi_ws281x - live outside this project (see
# visio_recorder/drivers.py). Two of the three are apt packages, so the venv
# has to be built from the system interpreter rather than one uv downloads for
# itself, whose site-packages hold nothing apt has ever installed.
SYSTEM_PYTHON="/usr/bin/python3"

# The exception: no Debian or Raspberry Pi archive ships rpi_ws281x at all, and
# upstream publishes a source distribution only, so it is compiled into the
# venv rather than apt-installed. Pinned for the same reason the uv installer
# is - two devices provisioned a week apart must get the same firmware.
RPI_WS281X_SPEC="rpi-ws281x==5.0.0"

# Fallback revision, used only when neither VISIO_GIT_REF nor the script's own
# checkout can supply one (see resolve_requested_ref). A branch name is a
# moving target, so reaching this path is reported loudly.
DEFAULT_GIT_REF="main"

# uv is fetched over the network and executed as root on a device that holds
# Supabase credentials, so the version is pinned and the payload verified.
# Astral publishes no .sha256 for the installer; this digest was recorded from
# the immutable versioned URL below, which is byte-identical to the
# uv-installer.sh asset on the matching GitHub release. Bump both together.
UV_VERSION="0.9.24"
UV_INSTALLER_SHA256="f6e468855afb4e653fa96ed68a7cad0b2534794ece25ec202f6543c589eb04dc"

# Delimiters for the block this script owns inside the operator's ~/.bashrc.
# Everything between them is regenerated on every run; anything outside is left
# untouched, so re-running is a byte-for-byte no-op.
BASHRC_BEGIN="# >>> visio-recorder dev shell (managed by setup-device.sh) >>>"
BASHRC_END="# <<< visio-recorder dev shell (managed by setup-device.sh) <<<"

reboot_needed=false
ws281x_missing=false
data_dir_failed=false

log() {
  echo "[setup-device] $*"
}

# Resolved before the re-exec below so it survives it: "$0" is passed through
# unchanged, but the caller's working directory is not guaranteed to be.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Re-running as root..." >&2
  exec sudo -E bash "$0" "$@"
fi

# Which revision to provision, most specific first:
#   1. VISIO_GIT_REF - an explicit branch, tag, or commit SHA.
#   2. The exact commit of the checkout this script is running from. Two
#      devices provisioned from the same clone therefore get byte-identical
#      firmware regardless of what has since landed upstream, and re-running
#      the installed copy under /opt is a no-op rather than a silent upgrade.
#   3. DEFAULT_GIT_REF, with a warning - this is the only non-reproducible path.
resolve_requested_ref() {
  if [[ -n "${VISIO_GIT_REF:-}" ]]; then
    printf '%s\n' "${VISIO_GIT_REF}"
    return
  fi
  local self_sha
  # By this point we are root, while the operator's clone is owned by them, so
  # git's dubious-ownership check applies. Git does exempt a repo owned by
  # $SUDO_UID, but that only helps when the script was reached via sudo - not
  # via `su -` or a root login - and a silent failure here would downgrade a
  # reproducible install to the moving DEFAULT_GIT_REF. `safe.directory` is
  # scoped to this one read-only rev-parse, which runs no hooks and reads no
  # repo-supplied config.
  if self_sha="$(git -c safe.directory='*' -C "${SCRIPT_DIR}" rev-parse --verify HEAD 2>/dev/null)"; then
    printf '%s\n' "${self_sha}"
    return
  fi
  printf '%s\n' "${DEFAULT_GIT_REF}"
}

log "Step 1/10: swap size"
SWAP_CONF="/etc/dphys-swapfile"
RPI_SWAP_CONF="/etc/rpi/swap.conf"
RPI_SWAP_DROPIN="/etc/rpi/swap.conf.d/10-visio.conf"
SWAP_SIZE_MB=1024
# The Pi Zero 2W's 512MB RAM needs a real swap file for apt/dpkg operations, or
# package configuration can thrash swap badly enough to look like a hung or
# dropped SSH session.
active_swap_mb="$(awk '/^SwapTotal:/ {print int($2 / 1024)}' /proc/meminfo)"
active_swap_mb="${active_swap_mb:-0}"
if [[ -f "${SWAP_CONF}" ]]; then
  # A hand-edited CONF_SWAPSIZE can hold anything at all ("auto", "1024M", a
  # trailing comment), and the `|| true` guard - which pipefail on line 2
  # makes load-bearing when grep matches nothing - turns a missing key into an
  # empty string. Either value would make the arithmetic comparison below
  # abort the script, so normalise to a plain integer first.
  current_swap="$(grep -E '^CONF_SWAPSIZE=' "${SWAP_CONF}" | cut -d= -f2- || true)"
  if [[ ! "${current_swap}" =~ ^[0-9]+$ ]]; then
    log "CONF_SWAPSIZE in ${SWAP_CONF} is missing or not a plain integer ('${current_swap}') - treating it as 0"
    current_swap=0
  fi
  if [[ "${current_swap}" -lt "${SWAP_SIZE_MB}" ]]; then
    sed -i "s/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=${SWAP_SIZE_MB}/" "${SWAP_CONF}"
    systemctl restart dphys-swapfile
    log "Swap was ${current_swap}MB - increased to ${SWAP_SIZE_MB}MB"
  else
    log "Swap already ${current_swap}MB (>= ${SWAP_SIZE_MB}MB) - no change needed"
  fi
elif [[ -f "${RPI_SWAP_CONF}" ]]; then
  # Trixie onwards: rpi-swap Replaces/Provides dphys-swapfile, so /etc/dphys-swapfile
  # no longer exists. Its default "zram+file" mechanism sizes zram off RAM and uses
  # the file only as writeback storage, not as swap - on 512MB that leaves SwapTotal
  # well under what dpkg needs. Pin a plain swap file at the same size this script
  # guaranteed on bookworm, via a drop-in rather than editing the vendor file
  # (see swap.conf(5)). rpi-swap applies this from a boot-time systemd generator,
  # so it takes a reboot - Step 2's apt still runs on current swap this pass.
  if [[ "${active_swap_mb}" -lt "${SWAP_SIZE_MB}" ]]; then
    mkdir -p "$(dirname "${RPI_SWAP_DROPIN}")"
    cat > "${RPI_SWAP_DROPIN}" <<EOF
# Managed by visio setup-device.sh - do not edit by hand.
[Main]
Mechanism=swapfile

[File]
FixedSizeMiB=${SWAP_SIZE_MB}
EOF
    systemctl daemon-reload
    reboot_needed=true
    log "Swap is ${active_swap_mb}MB - wrote ${RPI_SWAP_DROPIN} pinning a ${SWAP_SIZE_MB}MB swap file, applied on the reboot reported at the end"
  else
    log "Swap already ${active_swap_mb}MB (>= ${SWAP_SIZE_MB}MB) - no change needed"
  fi
else
  log "Neither ${SWAP_CONF} nor ${RPI_SWAP_CONF} found - skipping swap sizing (SwapTotal is ${active_swap_mb}MB)"
fi

log "Step 2/10: apt packages"
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
# python3-gpiozero is in the required list, not probed like pijuice-base below:
# the flag-button driver imports gpiozero and preflight blocks on it, so a
# release that cannot supply it should fail here rather than hand back a device
# whose daemon never starts. Both Debian and the Raspberry Pi archive ship it.
apt-get install -y rpicam-apps zbar-tools ffmpeg network-manager git python3-gpiozero

# Whether this release's index offers a package at all. Probing beats
# swallowing an install's exit code: a genuine failure (dependency conflict, no
# disk) still aborts loudly, while a package the archive simply does not ship
# for this release is reported and skipped.
apt_candidate() {
  apt-cache policy "$1" 2>/dev/null | awk -F ': ' '/^  Candidate:/ {print $2}'
}

apt_has_candidate() {
  local candidate
  candidate="$(apt_candidate "$1")"
  [[ -n "${candidate}" && "${candidate}" != "(none)" ]]
}

# pijuice-base is deliberately not in the required list above. The Raspberry Pi
# archive ships it for bookworm but dropped it in trixie, and a missing PiJuice
# HAT is an accepted bring-up state (VISIO_BATTERY_SOURCE=none) - the same rule
# preflight.py applies when it downgrades the pijuice check to a warning.
if apt_has_candidate pijuice-base; then
  apt-get install -y pijuice-base
  log "pijuice-base installed ($(apt_candidate pijuice-base))"
else
  log "pijuice-base is not in this release's package index - skipping (expected on Raspberry Pi OS trixie, where the Raspberry Pi archive no longer ships it). Keep VISIO_BATTERY_SOURCE=none until a battery driver is installed another way."
fi

log "Step 3/10: interfaces"
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

log "Step 4/10: uv"
# The installer's own default is ${HOME}/.local/bin, which is root's home after
# the re-exec above and is on no later run's PATH: sudo hands this script
# `secure_path`, so `command -v uv` would miss it and re-download, re-verify and
# re-run the installer on every pass - and hard-fail at curl on a device with no
# network, which is exactly when an operator re-runs this to repair drift.
# ${BIN_DIR} is on secure_path and on a login shell's PATH, so the install is
# found again by this script, by the operator, and by anything else on the box.
export UV_INSTALL_DIR="${BIN_DIR}"
if command -v uv >/dev/null 2>&1; then
  log "uv already installed at $(command -v uv) ($(uv --version 2>/dev/null || echo 'version unknown')) - reusing it, nothing downloaded"
elif [[ -x "${UV_INSTALL_DIR}/uv" ]]; then
  export PATH="${UV_INSTALL_DIR}:${PATH}"
  log "uv already installed at ${UV_INSTALL_DIR}/uv ($(uv --version 2>/dev/null || echo 'version unknown')) - reusing it, nothing downloaded"
else
  uv_installer="$(mktemp)"
  curl --proto '=https' --tlsv1.2 -LsSf "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "${uv_installer}"
  if ! printf '%s  %s\n' "${UV_INSTALLER_SHA256}" "${uv_installer}" | sha256sum --check --status; then
    log "ERROR: uv ${UV_VERSION} installer failed checksum verification - refusing to execute it"
    log "  expected ${UV_INSTALLER_SHA256}"
    log "  actual   $(sha256sum "${uv_installer}" | cut -d' ' -f1)"
    rm -f "${uv_installer}"
    exit 1
  fi
  # No PATH edits in root's shell rc files; this script sets PATH itself below.
  INSTALLER_NO_MODIFY_PATH=1 sh "${uv_installer}"
  rm -f "${uv_installer}"
  export PATH="${UV_INSTALL_DIR}:${PATH}"
  log "uv ${UV_VERSION} installed to ${UV_INSTALL_DIR} (checksum verified)"
fi

# Bring an install dir to the exact commit a ref names, cloning it first if
# needed, and print the resolved SHA. Detach-and-hard-reset rather than pull:
# provisioning has to converge on one revision from any prior state - a
# diverged branch, local edits, an interrupted merge - not merely fast-forward
# from a clean one. Untracked paths are deliberately left alone, because
# firmware/.venv lives inside the checkout.
#   $1 install dir   $2 clone URL   $3 ref (branch, tag, or SHA)
# Resolution happens after the clone and fetch, so an unresolvable ref returns
# non-zero with the checkout left at whatever revision it already had - on a
# fresh device, that means the clone exists at origin's default branch. Nothing
# is installed from it either way, and the next run converges.
#
# Every git command is checked by hand rather than left to `set -e`: the caller
# below tests this function's exit status, which suppresses errexit for its
# whole body. Unchecked, a failed fetch would fall through to rev-parse, resolve
# the STALE remote-tracking ref left by the last successful fetch, print that
# SHA and return 0 - reporting a successful provision of firmware the device
# does not have. Diagnostics go to stderr; stdout carries only the SHA.
#
# Exit status: 0 with the SHA on stdout, 1 when the ref cannot be resolved and
# the caller should say so, or 2 when a git command failed and this function has
# already printed the only advice that fits. The two need opposite instructions
# - fix your network versus push your branch - so they are not merged.
checkout_revision() {
  local install_dir="$1" repo_url="$2" ref="$3" sha="" candidate
  if [[ -d "${install_dir}/.git" ]]; then
    if ! git -C "${install_dir}" remote set-url origin "${repo_url}"; then
      log "ERROR: cannot point ${install_dir}'s origin remote at ${repo_url}. Is ${install_dir} a healthy git checkout?" >&2
      return 2
    fi
  elif ! git clone "${repo_url}" "${install_dir}"; then
    log "ERROR: cloning ${repo_url} into ${install_dir} failed. Check network access, DNS and credentials, then re-run - nothing was installed." >&2
    return 2
  fi
  # Fetch every branch and tag explicitly rather than trusting the clone's
  # configured refspec, so a tag or SHA passed via VISIO_GIT_REF resolves too.
  if ! git -C "${install_dir}" fetch --prune --tags --force origin '+refs/heads/*:refs/remotes/origin/*'; then
    log "ERROR: fetching from ${repo_url} failed, so this run cannot know what '${ref}' points at now." >&2
    log "  ${install_dir} has been left on its current revision rather than provisioned from a stale copy of '${ref}'." >&2
    log "  This is a network, DNS or credentials problem, not a bad ref. Fix connectivity and re-run." >&2
    return 2
  fi
  # origin/<ref> first: a stale local branch of the same name must never win.
  for candidate in "refs/remotes/origin/${ref}" "${ref}"; do
    if sha="$(git -C "${install_dir}" rev-parse --verify --quiet "${candidate}^{commit}")"; then
      break
    fi
  done
  [[ -n "${sha}" ]] || return 1
  if ! git -C "${install_dir}" checkout --force --detach "${sha}"; then
    log "ERROR: checking out ${sha} in ${install_dir} failed - the checkout is NOT at the requested revision. Check free disk space and file permissions." >&2
    return 2
  fi
  if ! git -C "${install_dir}" reset --hard --quiet "${sha}"; then
    log "ERROR: resetting ${install_dir} to ${sha} failed - tracked files may still carry local edits. Check free disk space and file permissions." >&2
    return 2
  fi
  printf '%s\n' "${sha}"
}

# Whether $1 is a venv that can import what apt installed system-wide. Two
# conditions, because a venv uv creates on its own satisfies neither: it is
# isolated from the system site-packages, and uv resolves an interpreter it
# downloaded for itself in preference to /usr/bin/python3, whose dist-packages
# are the only place gpiozero and pijuice exist on the device.
venv_sees_system_packages() {
  local cfg="$1/pyvenv.cfg"
  [[ -f "${cfg}" ]] || return 1
  grep -qE '^[[:space:]]*include-system-site-packages[[:space:]]*=[[:space:]]*true[[:space:]]*$' "${cfg}" &&
    grep -qE "^[[:space:]]*home[[:space:]]*=[[:space:]]*$(dirname "${SYSTEM_PYTHON}")/?[[:space:]]*\$" "${cfg}"
}

# Recreated rather than repaired in place: the interpreter a venv is built from
# is fixed at creation, so a venv pointing at a downloaded CPython cannot be
# fixed by flipping the flag in pyvenv.cfg - its base site-packages would still
# be the downloaded interpreter's. `uv sync` repopulates whatever this leaves
# behind, and this only fires when the venv does not already satisfy both
# conditions, so a healthy device does not rebuild its venv on every run.
ensure_system_site_venv() {
  local venv_dir="$1"
  if venv_sees_system_packages "${venv_dir}"; then
    return 0
  fi
  if [[ -n "${venv_dir}" && -e "${venv_dir}" ]]; then
    log "${venv_dir} cannot see the system site-packages - recreating it from ${SYSTEM_PYTHON}"
    rm -rf "${venv_dir}"
  fi
  uv venv --python "${SYSTEM_PYTHON}" --system-site-packages "${venv_dir}"
}

log "Step 5/10: code"
requested_ref="$(resolve_requested_ref)"
if [[ "${requested_ref}" == "${DEFAULT_GIT_REF}" && -z "${VISIO_GIT_REF:-}" ]]; then
  log "WARNING: this script is not running from a git checkout and VISIO_GIT_REF is unset, so it is falling back to '${DEFAULT_GIT_REF}'. That is a moving target - two devices provisioned on different days will not match. Set VISIO_GIT_REF to a tag or commit SHA for a reproducible install."
fi

checkout_status=0
target_sha="$(checkout_revision "${INSTALL_DIR}" "${REPO_URL}" "${requested_ref}")" || checkout_status=$?
if [[ "${checkout_status}" -ne 0 ]]; then
  if [[ "${checkout_status}" -eq 1 ]]; then
    log "ERROR: cannot resolve '${requested_ref}' in ${INSTALL_DIR}."
    log "  If it is a commit on a local-only branch, push it first, or set VISIO_GIT_REF to a revision that exists on ${REPO_URL}."
  fi
  exit 1
fi
log "Provisioned revision ${target_sha} (requested '${requested_ref}')"

ensure_system_site_venv "${VENV_DIR}"
# --inexact: rpi_ws281x is installed into this venv below but is deliberately
# absent from uv.lock, and an exact sync would delete it on every re-run and
# recompile it, which is minutes of build time on a Pi Zero 2W.
(cd "${FIRMWARE_DIR}" && uv sync --locked --inexact)
if ! venv_sees_system_packages "${VENV_DIR}"; then
  log "ERROR: ${VENV_DIR} does not have access to the system site-packages after uv sync."
  log "  gpiozero and pijuice are apt packages, so the daemon and ${PREFLIGHT_BIN} could not import them from this venv."
  log "  Delete ${VENV_DIR} and re-run; if it recurs, check that ${SYSTEM_PYTHON} exists and satisfies firmware/pyproject.toml's requires-python."
  exit 1
fi

# rpi_ws281x is the one hardware library with no apt package anywhere, so it is
# built from source here, which is why a device that has to build it needs a C
# compiler and the Python headers. Non-fatal on purpose: the systemd unit, the
# env file and the data dir below are still worth provisioning on a device with
# no compiler or no network, and ${PREFLIGHT_BIN} reports the missing module
# itself rather than this script guessing on its behalf. A failure is carried to
# the summary in ${ws281x_missing} the way ${reboot_needed} is - thousands of
# lines of apt output scroll past between here and there.
if "${VENV_DIR}/bin/python3" -c 'import rpi_ws281x' >/dev/null 2>&1; then
  log "rpi_ws281x already importable from ${VENV_DIR} - nothing to build"
else
  ws281x_build_deps=()
  for pkg in build-essential python3-dev; do
    if apt_has_candidate "${pkg}"; then
      ws281x_build_deps+=("${pkg}")
    fi
  done
  if [[ "${#ws281x_build_deps[@]}" -gt 0 ]]; then
    if ! apt-get install -y "${ws281x_build_deps[@]}"; then
      log "WARNING: installing ${ws281x_build_deps[*]} failed - the ${RPI_WS281X_SPEC} build below needs them and will probably fail too"
    fi
  fi
  if uv pip install --python "${VENV_DIR}/bin/python3" "${RPI_WS281X_SPEC}"; then
    log "${RPI_WS281X_SPEC} built and installed into ${VENV_DIR}"
  else
    ws281x_missing=true
    log "WARNING: could not install ${RPI_WS281X_SPEC} into ${VENV_DIR}. It ships as a source distribution, so the build needs a C compiler, Python headers and network access."
    log "  ${PREFLIGHT_BIN} will report rpi_ws281x as a blocking failure, and the daemon will not start, until this succeeds. Fix the build prerequisites and re-run."
  fi
fi

# Put preflight on PATH for a plain SSH user. /usr/local/bin is on the default
# PATH for login shells and in sudo's secure_path on Raspberry Pi OS, so this
# works with and without sudo. `ln -sfn` makes it idempotent and lets it
# re-point if the venv is rebuilt.
preflight_src="${VENV_DIR}/bin/${PREFLIGHT_BIN}"
if [[ ! -x "${preflight_src}" ]]; then
  log "ERROR: ${preflight_src} is missing after uv sync."
  log "  firmware/pyproject.toml must declare both [build-system] and [project.scripts]; without the former uv treats the project as virtual and never installs it."
  exit 1
fi
ln -sfn "${preflight_src}" "${BIN_DIR}/${PREFLIGHT_BIN}"
log "${BIN_DIR}/${PREFLIGHT_BIN} -> ${preflight_src}"

log "Step 6/10: systemd"
cp "${INSTALL_DIR}/${SYSTEMD_UNIT_SRC}" "${SYSTEMD_UNIT_DST}"
systemctl daemon-reload
systemctl enable visio-recorder

# Read one key out of ${ENV_FILE} the way systemd reads it as an
# EnvironmentFile: the last assignment wins, and surrounding whitespace plus one
# layer of matching quotes are not part of the value. Quoting is idiomatic there
# and will be unavoidable once SUPABASE_ANON_KEY is a real JWT, so every reader
# below goes through this - otherwise this script and the daemon can disagree
# about what a key is set to, and the disagreement is silent.
#
# The key match tolerates leading whitespace because systemd does. Anchoring it
# at column 0 would make an indented key invisible here while the daemon and
# preflight both still saw it, so this script would provision one data dir and
# the unit would then block on another - silently, and permanently.
env_file_value() {
  local key="$1" value=""
  if [[ -f "${ENV_FILE}" ]]; then
    value="$(grep -E "^[[:space:]]*${key}=" "${ENV_FILE}" | tail -n1 | cut -d= -f2- || true)"
  fi
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  if [[ "${#value}" -ge 2 ]]; then
    case "${value}" in
      \"*\" | \'*\') value="${value:1:${#value}-2}" ;;
    esac
  fi
  printf '%s\n' "${value}"
}

log "Step 7/10: env file"
if [[ ! -f "${ENV_FILE}" ]]; then
  cat > "${ENV_FILE}" <<'EOF'
SUPABASE_URL=
SUPABASE_ANON_KEY=
# Bring-up default: no PiJuice HAT installed yet (availability blocked as of
# 2026-07-21), running from a USB power bank instead. Switch to "pijuice"
# once real battery hardware is wired - see
# docs/superpowers/specs/2026-07-21-visio-device-provisioning-design.md
VISIO_BATTERY_SOURCE=none
# Editor and interactive shell conveniences (neovim as EDITOR/VISUAL, tmux,
# ls/git aliases, history settings, a branch-aware prompt, and the managed
# block in the invoking user's ~/.bashrc) installed by setup-device.sh for
# whoever runs it. Useful during bring-up; set to 0 for a shipped appliance
# image to skip them. The daemon ignores this key.
#
# Scope, so this is not mistaken for a wider switch: it covers those editor
# and shell conveniences only. It does not control the LED driver's build
# dependencies - rpi_ws281x currently has no apt package in any archive, so a
# device that has to build it from source gets a C compiler and the Python
# headers in step 5 regardless of this setting.
#
# Setting it to 0 later removes the managed block from that user's ~/.bashrc
# on the next run, but deliberately does NOT uninstall the packages - purging
# neovim out from under an operator mid-bring-up is worse than leaving it
# there. Setting it to 0 here, on this first run, is the way to get a device
# that never installs the dev shell at all: this file is written before the
# developer-shell step runs, so nothing has been installed yet.
VISIO_DEV_SHELL=1
# Where recordings are written; must be an absolute path. setup-device.sh
# creates whatever is set here, and the daemon reads it from this file through
# the unit's EnvironmentFile=, so those two always agree.
#
# visio-preflight reads it too, but only when it can: this file is mode 600
# root-owned, so a plain SSH user cannot see it. An unprivileged run therefore
# checks the default below and says so on its data_dir line instead of
# claiming the configured path was verified. Run it under sudo to check the
# configured one.
# VISIO_DATA_DIR=/var/lib/visio-recorder
EOF
  chmod 600 "${ENV_FILE}"
  log "Wrote template ${ENV_FILE} - fill in SUPABASE_URL and SUPABASE_ANON_KEY (and set VISIO_DEV_SHELL=0 now if this is an appliance image), then re-run this script"
  exit 0
else
  missing=()
  for key in SUPABASE_URL SUPABASE_ANON_KEY; do
    if [[ -z "$(env_file_value "${key}")" ]]; then
      missing+=("${key}")
    fi
  done
  if [[ "${#missing[@]}" -gt 0 ]]; then
    log "ERROR: ${ENV_FILE} exists but is missing values for: ${missing[*]}"
    exit 1
  fi
  log "${ENV_FILE} already configured"
fi

log "Step 8/10: developer shell"
# Gated so a shipped appliance image need not carry a dev toolchain: set
# VISIO_DEV_SHELL=0 in ${ENV_FILE}. This runs after the env-file step above
# on purpose - a fresh device stops at the template with the switch already in
# it, so nothing is installed before the operator has had the chance to see
# and flip it. Defaults to enabled when the file or the key is absent.
dev_shell_enabled() {
  local value
  # Read through env_file_value and case-fold, so VISIO_DEV_SHELL="0" - valid,
  # and 0 as far as the daemon is concerned - cannot silently leave the dev
  # shell enabled on a device meant to ship without one.
  value="$(env_file_value VISIO_DEV_SHELL)"
  case "${value,,}" in
    "") return 0 ;;
    0 | no | off | false) return 1 ;;
    *) return 0 ;;
  esac
}

# Print $1 with this script's managed block removed, every other line
# reproduced byte-for-byte. A file with no block, or no file at all, yields
# the file's own contents unchanged. Shared by the write and remove paths so
# both agree on exactly what "the block" is.
#
# A begin marker with no matching end marker - a hand-edit that deleted or
# mangled the terminator - is refused rather than treated as "skip to EOF",
# which would silently delete every personal setting below it. Refusing to
# touch a file we cannot parse is the right failure mode when the alternative
# is unrecoverable data loss in someone's ~/.bashrc.
strip_managed_block() {
  local file="$1"
  [[ -f "${file}" ]] || return 0
  awk -v begin="${BASHRC_BEGIN}" -v end="${BASHRC_END}" '
    $0 == begin { skip = 1 }
    skip != 1   { print }
    $0 == end   { skip = 0 }
    END         { if (skip == 1) exit 3 }
  ' "${file}"
}

# Shared refusal message for the two callers below.
warn_unterminated_block() {
  local file="$1"
  log "ERROR: ${file} contains '${BASHRC_BEGIN}' with no matching end marker, so this script cannot tell where the managed block stops."
  log "  Leaving ${file} untouched. Repair the block by re-adding '${BASHRC_END}' after it, or delete the block by hand, then re-run."
}

# Put ${4} in the file ${1}, owned by ${3}, via the temp path ${2}. The write,
# its byte count, the ownership, the mode and the rename are each checked, and
# any failure removes the temp file and returns non-zero with ${1} untouched:
# the operator's ~/.bashrc must never end up partial or empty, and a full SD
# card is the realistic way that happens - step 2 and step 8 both apt-install
# into this same filesystem. Only a temp file known to be complete is renamed,
# and rename is atomic, so ${1} is either its old contents or the new ones.
replace_file_via_tmp() {
  local file="$1" tmp="$2" owner="$3" content="$4" expected written
  expected="$(printf '%s' "${content}" | wc -c)"
  if ! printf '%s' "${content}" > "${tmp}"; then
    log "ERROR: writing ${tmp} failed - ${file} is unchanged. Check free disk space and file permissions."
    rm -f "${tmp}"
    return 1
  fi
  written="$(wc -c < "${tmp}" 2>/dev/null || true)"
  if [[ "${written:-0}" -ne "${expected}" ]]; then
    log "ERROR: ${tmp} holds ${written:-0} bytes but should hold ${expected} - refusing to move a partial file over ${file}, which is unchanged. Check free disk space."
    rm -f "${tmp}"
    return 1
  fi
  if ! chown "${owner}" "${tmp}"; then
    log "ERROR: cannot give ${tmp} to ${owner} - ${file} is unchanged."
    rm -f "${tmp}"
    return 1
  fi
  if ! chmod 644 "${tmp}"; then
    log "ERROR: cannot set mode 644 on ${tmp} - ${file} is unchanged."
    rm -f "${tmp}"
    return 1
  fi
  if ! mv "${tmp}" "${file}"; then
    log "ERROR: cannot move ${tmp} onto ${file} - ${file} is unchanged."
    rm -f "${tmp}"
    return 1
  fi
}

# Replace this script's managed block in $1 with $2, leaving every other line
# byte-identical. Trailing blank lines are normalised so a second run produces
# exactly the same file as the first.
write_managed_block() {
  local file="$1" body="$2" owner="$3" stripped content
  # Command substitution strips every trailing newline; exactly one blank
  # separator line is then re-added, so the result is a fixed point.
  if ! stripped="$(strip_managed_block "${file}")"; then
    warn_unterminated_block "${file}"
    return 1
  fi
  content=""
  if [[ -n "${stripped}" ]]; then
    content="${stripped}"$'\n\n'
  fi
  content+="${BASHRC_BEGIN}"$'\n'"${body}"$'\n'"${BASHRC_END}"$'\n'
  replace_file_via_tmp "${file}" "${file}.visio-setup.tmp" "${owner}" "${content}"
}

# The inverse: drop the managed block from $1 and leave the rest of the file
# byte-identical. A no-op when the file does not exist or carries no block, so
# it is safe to run on every disabled pass.
remove_managed_block() {
  local file="$1" owner="$2" stripped content
  [[ -f "${file}" ]] || return 0
  grep -qxF "${BASHRC_BEGIN}" "${file}" || return 0
  if ! stripped="$(strip_managed_block "${file}")"; then
    warn_unterminated_block "${file}"
    return 1
  fi
  content=""
  if [[ -n "${stripped}" ]]; then
    content="${stripped}"$'\n'
  fi
  replace_file_via_tmp "${file}" "${file}.visio-setup.tmp" "${owner}" "${content}"
}

# This script re-execs itself as root, so $HOME is /root. The conveniences
# belong to whoever invoked it. Both lookups are guarded rather than left to
# abort under `set -o pipefail`: an account with no passwd entry, or one whose
# home is on an unmounted volume, must skip this optional convenience step and
# still reach the data dir and summary below.
dev_user="${SUDO_USER:-root}"
dev_home="$(getent passwd "${dev_user}" | cut -d: -f6 || true)"
dev_group="$(id -gn "${dev_user}" 2>/dev/null || true)"
if [[ -z "${dev_home}" || ! -d "${dev_home}" || -z "${dev_group}" ]]; then
  log "Cannot resolve a home directory for '${dev_user}' - skipping the developer shell step"
elif ! dev_shell_enabled; then
  # Turning the switch off has to actually reverse the shell change, not merely
  # stop refreshing it. Packages stay installed on purpose - purging neovim out
  # from under an operator mid-bring-up is worse than leaving it - which is why
  # ${ENV_FILE} documents pre-seeding the key as the way to get a device that
  # never has the dev toolchain at all.
  #
  # A refusal to touch a malformed ~/.bashrc is reported by the function itself
  # and must not abort the run: the data dir and summary below matter more than
  # an optional convenience, exactly as for the unresolvable-user guard above.
  if remove_managed_block "${dev_home}/.bashrc" "${dev_user}:${dev_group}"; then
    log "VISIO_DEV_SHELL is disabled in ${ENV_FILE} - skipped the editor install and removed any managed block from ${dev_home}/.bashrc. Packages installed by an earlier run are left in place."
  else
    log "VISIO_DEV_SHELL is disabled in ${ENV_FILE} - skipped the editor install. Packages installed by an earlier run are left in place."
  fi
else
  # tmux earns its place here specifically: bring-up over SSH on a 512MB board
  # has already produced apparent hangs and dropped sessions, and a detached
  # session survives them.
  dev_packages=(neovim ripgrep fd-find tmux htop tree jq less bash-completion)
  dev_install=()
  dev_skipped=()
  for pkg in "${dev_packages[@]}"; do
    if apt_has_candidate "${pkg}"; then
      dev_install+=("${pkg}")
    else
      dev_skipped+=("${pkg}")
    fi
  done
  # Every command in this branch is checked rather than left to `set -e`. The
  # step's own guards above promise that an account with no passwd entry, or a
  # ~/.bashrc this script refuses to parse, still reaches the data dir and the
  # summary; a transient apt mirror error on `tmux` has to be no different, or
  # optional convenience work silently produces an unprovisioned device.
  if [[ "${#dev_install[@]}" -gt 0 ]]; then
    if apt-get install -y "${dev_install[@]}"; then
      log "Installed: ${dev_install[*]}"
    else
      log "ERROR: installing ${dev_install[*]} failed - continuing without them. This is optional convenience tooling; re-run once apt is healthy."
    fi
  fi
  if [[ "${#dev_skipped[@]}" -gt 0 ]]; then
    log "Not in this release's package index, skipped: ${dev_skipped[*]}"
  fi

  # System-wide default for anything that consults `editor` (visudo, git's
  # fallback, crontab) rather than $EDITOR.
  if update-alternatives --list editor 2>/dev/null | grep -qx /usr/bin/nvim; then
    if update-alternatives --set editor /usr/bin/nvim >/dev/null; then
      log "Default 'editor' alternative set to nvim"
    else
      log "Could not set the 'editor' alternative to nvim - continuing"
    fi
  fi

  # Written once, then left alone: an operator who edits it keeps their edits
  # across re-runs. Unlike the ~/.bashrc block, this file is not managed.
  #
  # Every write is guarded for the same reason as the apt install above: a home
  # on a full or read-only filesystem, or a regular file sitting where
  # ~/.config should be, makes mkdir fail, and this step must skip rather than
  # abort. The file itself goes through replace_file_via_tmp so a half-written
  # init.lua is never left behind either.
  nvim_starter_config="$(cat <<'NVIMRC'
-- Starting point written once by setup-device.sh. Edit freely: the script
-- will not overwrite this file on a re-run.
vim.opt.number = true
vim.opt.expandtab = true
vim.opt.shiftwidth = 2
vim.opt.tabstop = 2
vim.opt.smartindent = true
vim.opt.ignorecase = true
vim.opt.smartcase = true
vim.opt.termguicolors = true
vim.opt.mouse = 'a'
vim.opt.undofile = true
vim.opt.signcolumn = 'yes'
vim.opt.scrolloff = 4
vim.opt.list = true
vim.opt.listchars = { tab = '> ', trail = '.' }
NVIMRC
)"
  nvim_dir="${dev_home}/.config/nvim"
  if [[ -e "${nvim_dir}/init.lua" || -e "${nvim_dir}/init.vim" ]]; then
    log "${nvim_dir} already has a config - left untouched"
  elif ! mkdir -p "${nvim_dir}"; then
    log "Cannot create ${nvim_dir} - skipping the starter neovim config"
  elif ! chown "${dev_user}:${dev_group}" "${dev_home}/.config" "${nvim_dir}"; then
    log "Cannot give ${nvim_dir} to ${dev_user}:${dev_group} - skipping the starter neovim config"
  elif replace_file_via_tmp "${nvim_dir}/init.lua" "${nvim_dir}/init.lua.visio-setup.tmp" \
    "${dev_user}:${dev_group}" "${nvim_starter_config}"$'\n'; then
    log "Wrote starter ${nvim_dir}/init.lua"
  fi

  # Quoted heredoc: every expansion below is evaluated by the operator's shell
  # at prompt time, not by this script.
  dev_shell_body="$(cat <<'BASHRC_BODY'
# Regenerated on every setup-device.sh run - put personal settings outside
# this block. Set VISIO_DEV_SHELL=0 in /etc/visio-recorder.env and re-run the
# script to delete this block; the packages it installed stay.

# Colour. Raspberry Pi OS ships these commented out on some images.
if [ -x /usr/bin/dircolors ]; then
    if [ -r "$HOME/.dircolors" ]; then
        eval "$(dircolors -b "$HOME/.dircolors")"
    else
        eval "$(dircolors -b)"
    fi
    alias ls='ls --color=auto'
    alias grep='grep --color=auto'
    alias fgrep='fgrep --color=auto'
    alias egrep='egrep --color=auto'
fi

alias ll='ls -la'
alias la='ls -A'
alias l='ls -CF'
alias ..='cd ..'
alias ...='cd ../..'

if command -v nvim >/dev/null 2>&1; then
    export EDITOR=nvim
    export VISUAL=nvim
    alias vi=nvim
    alias vim=nvim
fi

if command -v fdfind >/dev/null 2>&1 && ! command -v fd >/dev/null 2>&1; then
    alias fd=fdfind
fi

# History: append instead of clobbering (several SSH sessions at once during
# bring-up), drop duplicates and space-prefixed commands, keep plenty, and
# flush after every prompt so a dropped connection loses nothing.
shopt -s histappend
shopt -s checkwinsize
shopt -s globstar
HISTCONTROL=ignoreboth:erasedups
HISTSIZE=100000
HISTFILESIZE=200000
HISTTIMEFORMAT='%F %T '
case "${PROMPT_COMMAND:-}" in
    *'history -a'*) ;;
    *) PROMPT_COMMAND="history -a;${PROMPT_COMMAND:-}" ;;
esac

if ! shopt -oq posix; then
    if [ -f /usr/share/bash-completion/bash_completion ]; then
        . /usr/share/bash-completion/bash_completion
    elif [ -f /etc/bash_completion ]; then
        . /etc/bash_completion
    fi
fi

# Current git branch in the prompt. Two clones live on this device - the
# provisioned one under /opt/visio-recorder and your own - so the working
# directory and branch together tell you which one you are standing in.
__visio_git_branch() {
    local ref
    ref="$(git symbolic-ref --quiet --short HEAD 2>/dev/null)" ||
        ref="$(git rev-parse --short HEAD 2>/dev/null)" ||
        return 0
    printf ' (%s)' "$ref"
}
PS1='\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\[\033[01;33m\]$(__visio_git_branch)\[\033[00m\]\$ '
BASHRC_BODY
)"

  if write_managed_block "${dev_home}/.bashrc" "${dev_shell_body}" "${dev_user}:${dev_group}"; then
    log "Managed dev-shell block written to ${dev_home}/.bashrc (owner ${dev_user}:${dev_group})"
  fi
fi

log "Step 9/10: data dir"
# The daemon records into ${ENV_FILE}'s VISIO_DATA_DIR (daemon.py's
# load_config) and preflight checks that same directory, so provision the one
# that is actually configured rather than the default it usually is -
# otherwise preflight blocks on a directory nothing ever created and advises a
# re-run that would not create it either.
#
# The argument is operator input now rather than a constant this script owns,
# so both ways it can be wrong are checked rather than left to `set -e`: a
# relative path resolves against this script's invocation cwd but against the
# unit's WorkingDirectory= for the daemon, and mkdir fails outright on a
# read-only mount or a regular file sitting at the path. Neither aborts the
# run, because step 10 is where the operator learns what happened - including
# this - and dying here would take that report with it.
data_dir="$(env_file_value VISIO_DATA_DIR)"
data_dir="${data_dir:-${DATA_DIR}}"
if [[ "${data_dir}" != /* ]]; then
  data_dir_failed=true
  log "ERROR: VISIO_DATA_DIR in ${ENV_FILE} is '${data_dir}', which is not an absolute path."
  log "  A relative path means this script and the daemon would resolve it against different directories, so nothing was created. Set an absolute path and re-run."
elif ! mkdir -p "${data_dir}"; then
  data_dir_failed=true
  log "ERROR: cannot create ${data_dir}, the data dir named by VISIO_DATA_DIR in ${ENV_FILE}."
  log "  Check that the path is on a writable, mounted filesystem and that nothing else already occupies it, then re-run."
fi

log "Step 10/10: summary"
battery_source="$(env_file_value VISIO_BATTERY_SOURCE)"
battery_source="${battery_source:-pijuice}"
log "Provisioned revision: ${target_sha}"
log "VISIO_BATTERY_SOURCE=${battery_source}"
log "Data dir: ${data_dir}"
if [[ "${battery_source}" == "none" ]]; then
  log "Bring-up mode: running without a PiJuice HAT (USB power bank). Not a failure - expected for now."
fi
if [[ "${data_dir_failed}" == "true" ]]; then
  log "INCOMPLETE: ${data_dir} was NOT created (see the step 9 error above), so the daemon has nowhere to record and '${PREFLIGHT_BIN}' will report data_dir as a blocking failure."
fi
if [[ "${ws281x_missing}" == "true" ]]; then
  log "INCOMPLETE: ${RPI_WS281X_SPEC} could not be installed (see the step 5 warning above), so the daemon will not start until it is - '${PREFLIGHT_BIN}' will report rpi_ws281x as a blocking failure."
fi
log "Run '${PREFLIGHT_BIN}' from any directory to check the runtime environment."
if [[ "${reboot_needed}" == "true" ]]; then
  log "REBOOT REQUIRED: interface changes need a reboot to take effect before the daemon will work."
else
  log "No reboot required."
fi
# Two different outcomes, deliberately not conflated. The env-file step above
# exits 0 because it is a re-run checkpoint - nothing is wrong, the operator
# just has keys to fill in. Reaching here with an INCOMPLETE line means the
# run finished and the device is not usable, which neither the last line an
# operator reads nor an exit status a caller tests may report as success.
if [[ "${data_dir_failed}" == "true" || "${ws281x_missing}" == "true" ]]; then
  log "Setup incomplete - see the INCOMPLETE lines above. Fix what they name and re-run this script."
  exit 1
fi
log "Setup complete."
