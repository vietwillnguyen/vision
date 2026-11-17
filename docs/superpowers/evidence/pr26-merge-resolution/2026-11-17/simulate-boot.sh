# shellcheck shell=bash
# Usage: bash simulate-boot.sh <repo-root>
# Needs local Supabase running (supabase start) and firmware/.venv synced.
# Mirrors firmware/systemd/visio-recorder.service on a non-Pi host:
# ExecStartPre (preflight) must exit 0 before ExecStart (the daemon) runs.
# Hardware is simulated: stub rpicam-*/zbarimg/nmcli on PATH (rpicam-vid
# writes a short H.264 test pattern with ffmpeg), stub rpi_ws281x/gpiozero
# modules so preflight's import checks pass; see simulate-boot.py.
set -u
REPO=$1
FW=$REPO/firmware
HERE=$(cd "$(dirname "$0")" && pwd)
SIM=$(mktemp -d)
mkdir -p "$SIM/bin" "$SIM/py"
for b in rpicam-still zbarimg; do printf '#!/bin/sh\nexit 0\n' > "$SIM/bin/$b"; done
printf '#!/bin/sh\necho "Available cameras"\necho "0 : imx708 [4608x2592 10-bit RGGB] (/base/soc/i2c0mux/i2c@1/imx708@1a)"\n' > "$SIM/bin/rpicam-hello"
printf '#!/bin/sh\necho "STATE      CONNECTIVITY  WIFI-HW  WIFI     WWAN-HW  WWAN"\necho "connected  full          enabled  enabled  missing  enabled"\n' > "$SIM/bin/nmcli"
# rpicam-vid -t <ms> --framerate <fps> --codec h264 -n -o <path>
cat > "$SIM/bin/rpicam-vid" <<'STUB'
#!/bin/sh
ms=5000; out=
while [ $# -gt 0 ]; do
  case $1 in -t) ms=$2; shift ;; -o) out=$2; shift ;; esac
  shift
done
secs=$(( ms / 1000 )); [ "$secs" -lt 1 ] && secs=1
echo "rpicam-vid (simulated): ${secs}s test pattern -> $out" >&2
exec ffmpeg -loglevel error -y -re -f lavfi -i "testsrc=size=320x240:rate=30:duration=$secs" \
  -c:v libx264 -pix_fmt yuv420p -f h264 "$out"
STUB
chmod +x "$SIM"/bin/*
touch "$SIM/py/rpi_ws281x.py" "$SIM/py/gpiozero.py"

eval "$(supabase status -o env 2>/dev/null)"
export PATH="$SIM/bin:$PATH" PYTHONPATH="$SIM/py"
export SUPABASE_URL=$API_URL SUPABASE_ANON_KEY=$ANON_KEY SUPABASE_SERVICE_ROLE_KEY=$SERVICE_ROLE_KEY
export VISIO_BATTERY_SOURCE=none VISIO_DATA_DIR=$SIM/data VISIO_SEGMENT_DURATION_MS=2000
mkdir -p "$VISIO_DATA_DIR"
cd "$FW" || exit 1

echo "### ExecStartPre: python -m visio_recorder.preflight"
"$FW/.venv/bin/python3" -m visio_recorder.preflight
pre=$?
echo "ExecStartPre exit=$pre"
if [ "$pre" -ne 0 ]; then echo "unit would not start"; rm -rf "$SIM"; exit "$pre"; fi
echo
echo "### ExecStart: visio_recorder.daemon main() (simulated hardware)"
"$FW/.venv/bin/python3" "$HERE/simulate-boot.py"
rc=$?
echo "ExecStart exit=$rc"
rm -rf "$SIM"
exit "$rc"
