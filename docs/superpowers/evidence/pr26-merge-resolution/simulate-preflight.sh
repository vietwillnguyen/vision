# shellcheck shell=bash
# Usage: bash simulate-preflight.sh <path-to-firmware-dir-with-synced-venv>
# Run the real visio-preflight console script four times on a non-Pi host:
# 1) bare host (no Pi hardware) - must block honestly, exit 1
# 2) hardware simulated: stub rpicam/zbarimg/nmcli binaries on PATH and stub
#    rpi_ws281x/gpiozero modules on PYTHONPATH - must pass, exit 0
# 3) simulated Pi with the camera ribbon unplugged (rpicam-hello lists no
#    cameras) - must block, exit 1
# 4) simulated Pi with VISIO_BATTERY_SOURCE=pijuice and no HAT - battery
#    checks warn only, exit 0
set -u
FW=$1; SIM=$(mktemp -d)
cd /  # prove the entry point resolves from any working directory
echo "### 1. bare host, VISIO_BATTERY_SOURCE=none, VISIO_DATA_DIR=$SIM/data (missing)"
VISIO_BATTERY_SOURCE=none VISIO_DATA_DIR="$SIM/data" "$FW/.venv/bin/visio-preflight"; echo "exit=$?"
mkdir -p "$SIM/bin" "$SIM/py" "$SIM/data"
for b in rpicam-vid rpicam-still zbarimg; do printf '#!/bin/sh\nexit 0\n' > "$SIM/bin/$b"; done
printf '#!/bin/sh\necho "Available cameras"\necho "-----------------"\necho "0 : imx708 [4608x2592 10-bit RGGB] (/base/soc/i2c0mux/i2c@1/imx708@1a)"\n' > "$SIM/bin/rpicam-hello"
printf '#!/bin/sh\necho "STATE      CONNECTIVITY  WIFI-HW  WIFI     WWAN-HW  WWAN"\necho "connected  full          enabled  enabled  missing  enabled"\n' > "$SIM/bin/nmcli"
chmod +x "$SIM"/bin/*
command -v ffmpeg >/dev/null || printf '#!/bin/sh\nexit 0\n' > "$SIM/bin/ffmpeg"; chmod +x "$SIM"/bin/*
touch "$SIM/py/rpi_ws281x.py" "$SIM/py/gpiozero.py"
echo
echo "### 2. simulated Pi: stub camera/NM/QR binaries, stub LED/GPIO modules, writable data dir"
PATH="$SIM/bin:$PATH" PYTHONPATH="$SIM/py" VISIO_BATTERY_SOURCE=none VISIO_DATA_DIR="$SIM/data" "$FW/.venv/bin/visio-preflight"; echo "exit=$?"
echo
echo "### 3. simulated Pi but camera ribbon unplugged (rpicam-hello lists no cameras)"
printf '#!/bin/sh\necho "No cameras available!"\n' > "$SIM/bin/rpicam-hello"
PATH="$SIM/bin:$PATH" PYTHONPATH="$SIM/py" VISIO_BATTERY_SOURCE=none VISIO_DATA_DIR="$SIM/data" "$FW/.venv/bin/visio-preflight"; echo "exit=$?"
echo
echo "### 4. simulated Pi with VISIO_BATTERY_SOURCE=pijuice and no HAT (battery checks warn only, exit 0)"
printf '#!/bin/sh\necho "0 : imx708 [4608x2592]"\n' > "$SIM/bin/rpicam-hello"
PATH="$SIM/bin:$PATH" PYTHONPATH="$SIM/py" VISIO_BATTERY_SOURCE=pijuice VISIO_DATA_DIR="$SIM/data" "$FW/.venv/bin/visio-preflight"; echo "exit=$?"
rm -rf "$SIM"
