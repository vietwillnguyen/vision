"""Runtime environment verification for visio-recorder.

Checks are grouped by severity: BLOCK means the daemon cannot function
without it - the check's failure should stop the daemon from starting.
WARN means the daemon can still run, degraded - currently only the
PiJuice/I2C checks, since a missing battery HAT is an accepted, expected
state while VISIO_BATTERY_SOURCE=none (see
docs/superpowers/specs/2026-07-21-visio-device-provisioning-design.md).

Every check takes an injectable seam so it can be unit tested without
touching real hardware, matching the rest of firmware/'s Protocol/fake
convention.
"""

import importlib.util
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

BLOCK = "block"
WARN = "warn"

_DATA_DIR = Path("/var/lib/visio-recorder")
_I2C_DEVICE = Path("/dev/i2c-1")


@dataclass
class CheckResult:
    name: str
    severity: str
    ok: bool
    detail: str


def check_binary_on_path(
    name: str, which: Callable[[str], str | None] = shutil.which
) -> CheckResult:
    found = which(name) is not None
    return CheckResult(
        name=name,
        severity=BLOCK,
        ok=found,
        detail="found on PATH" if found else f"{name} not found on PATH - check the apt install step",
    )


def check_importable(
    module: str,
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
) -> CheckResult:
    found = find_spec(module) is not None
    return CheckResult(
        name=module,
        severity=BLOCK,
        ok=found,
        detail="importable" if found else f"{module} not importable - check the uv sync step",
    )


def check_pijuice_importable(
    find_spec: Callable[[str], object | None] = importlib.util.find_spec,
) -> CheckResult:
    found = find_spec("pijuice") is not None
    return CheckResult(
        name="pijuice",
        severity=WARN,
        ok=found,
        detail="pijuice importable"
        if found
        else "pijuice not installed - expected when VISIO_BATTERY_SOURCE=none",
    )


def check_camera_detected(
    run: Callable[[list[str]], subprocess.CompletedProcess] = lambda args: subprocess.run(
        args, capture_output=True, text=True, timeout=5
    ),
) -> CheckResult:
    try:
        result = run(["rpicam-hello", "--list-cameras"])
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            name="camera",
            severity=BLOCK,
            ok=False,
            detail=f"rpicam-hello unavailable - {type(exc).__name__}: check apt install step" if isinstance(exc, FileNotFoundError) else f"rpicam-hello timeout - camera may be unresponsive",
        )
    detected = result.returncode == 0 and re.search(r"^\d+\s*:", result.stdout, re.MULTILINE) is not None
    return CheckResult(
        name="camera",
        severity=BLOCK,
        ok=detected,
        detail="camera detected" if detected else "no camera detected - check ribbon cable connection",
    )


def check_networkmanager_running(
    run: Callable[[list[str]], subprocess.CompletedProcess] = lambda args: subprocess.run(
        args, capture_output=True, text=True, timeout=5
    ),
) -> CheckResult:
    try:
        result = run(["nmcli", "general", "status"])
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return CheckResult(
            name="networkmanager",
            severity=BLOCK,
            ok=False,
            detail=f"nmcli unavailable - check apt install step" if isinstance(exc, FileNotFoundError) else f"nmcli timeout - NetworkManager may be unresponsive",
        )
    ok = result.returncode == 0
    return CheckResult(
        name="networkmanager",
        severity=BLOCK,
        ok=ok,
        detail="running" if ok else "nmcli general status failed - is NetworkManager active?",
    )


def check_data_dir_writable(path: Path = _DATA_DIR) -> CheckResult:
    ok = path.is_dir() and os.access(path, os.W_OK)
    return CheckResult(
        name="data_dir",
        severity=BLOCK,
        ok=ok,
        detail=f"{path} writable" if ok else f"{path} missing or not writable - run setup-device.sh",
    )


def check_i2c_enabled(path: Path = _I2C_DEVICE) -> CheckResult:
    ok = path.exists()
    return CheckResult(
        name="i2c",
        severity=WARN,
        ok=ok,
        detail="I2C bus enabled" if ok else "I2C not enabled - run setup-device.sh, may need a reboot",
    )
