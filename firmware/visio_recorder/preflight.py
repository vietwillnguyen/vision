"""Runtime environment verification for visio-recorder.

Checks are grouped by severity: BLOCK means the daemon cannot function
without it - the check's failure should stop the daemon from starting.
WARN means the daemon is unaffected: the PiJuice/I2C checks, since a
missing battery HAT is an accepted, expected state while
VISIO_BATTERY_SOURCE=none (see
docs/superpowers/specs/2026-07-21-visio-device-provisioning-design.md),
and the data-dir check when the directory exists but is not writable by
a caller other than the uid the daemon runs as.

Severity is therefore not always a property of the check alone - it can
depend on who is running it, because this module is both the daemon's
own startup gate and a diagnostic an operator runs over SSH. Only the
genuinely caller-relative part is downgraded: a data dir that does not
exist at all still blocks for every caller.

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

# The uid the systemd unit starts the daemon as (User= is unset, so root).
# Only that caller is gated on the data dir being writable.
_DAEMON_UID = 0


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
    except (FileNotFoundError, PermissionError) as exc:
        return CheckResult(
            name="camera",
            severity=BLOCK,
            ok=False,
            detail=f"rpicam-hello unavailable - {type(exc).__name__}: check apt install step",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="camera",
            severity=BLOCK,
            ok=False,
            detail="rpicam-hello timeout - camera may be unresponsive",
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
    except (FileNotFoundError, PermissionError) as exc:
        return CheckResult(
            name="networkmanager",
            severity=BLOCK,
            ok=False,
            detail=f"nmcli unavailable - {type(exc).__name__}: check apt install step",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="networkmanager",
            severity=BLOCK,
            ok=False,
            detail="nmcli timeout - NetworkManager may be unresponsive",
        )
    ok = result.returncode == 0
    return CheckResult(
        name="networkmanager",
        severity=BLOCK,
        ok=ok,
        detail="running" if ok else "nmcli general status failed - is NetworkManager active?",
    )


def check_data_dir_writable(
    path: Path = _DATA_DIR,
    getuid: Callable[[], int] = os.getuid,
    can_write: Callable[[Path], bool] = lambda path: os.access(path, os.W_OK),
) -> CheckResult:
    """Existence and writability answer different questions, so they differ.

    Whether the directory exists is caller-independent - /var/lib is 0755
    root:root, so a missing data dir looks missing to every uid - and a device
    without one is not provisioned: setup-device.sh exits at the env-file step
    before it ever reaches the data-dir step, both on a fresh device and
    whenever the Supabase keys are unfilled. That stays BLOCK for everyone.

    Writability is caller-relative, and only it. The daemon runs as
    ``_DAEMON_UID`` and cannot function without a writable data dir, so for
    that caller an existing-but-unwritable directory stays BLOCK - the same
    gate the unit's ExecStartPre has always applied. For an operator running
    the diagnostic over SSH it is *expected*: the directory stays root-owned
    on purpose because it holds recorded video, so reporting BLOCK made a
    healthy device look mis-provisioned and advised a re-run of
    setup-device.sh that would change nothing.
    """
    exists = path.is_dir()
    if exists and can_write(path):
        return CheckResult(name="data_dir", severity=BLOCK, ok=True, detail=f"{path} writable")
    uid = getuid()
    if not exists or uid == _DAEMON_UID:
        return CheckResult(
            name="data_dir",
            severity=BLOCK,
            ok=False,
            detail=f"{path} missing or not writable - run setup-device.sh",
        )
    return CheckResult(
        name="data_dir",
        severity=WARN,
        ok=False,
        detail=(
            f"{path} exists but is not writable by uid {uid} - expected for a "
            f"diagnostic run as a non-daemon user, not a fault; the daemon runs "
            f"as uid {_DAEMON_UID}"
        ),
    )


def check_i2c_enabled(path: Path = _I2C_DEVICE) -> CheckResult:
    ok = path.exists()
    return CheckResult(
        name="i2c",
        severity=WARN,
        ok=ok,
        detail="I2C bus enabled" if ok else "I2C not enabled - run setup-device.sh, may need a reboot",
    )


def run_checks(battery_source: str) -> list[CheckResult]:
    results = [
        check_binary_on_path("rpicam-vid"),
        check_binary_on_path("rpicam-still"),
        check_binary_on_path("zbarimg"),
        check_binary_on_path("ffmpeg"),
        check_binary_on_path("nmcli"),
        check_importable("rpi_ws281x"),
        check_importable("gpiozero"),
        check_importable("supabase"),
        check_camera_detected(),
        check_networkmanager_running(),
        check_data_dir_writable(),
    ]
    if battery_source == "pijuice":
        results.append(check_pijuice_importable())
        results.append(check_i2c_enabled())
    return results


def has_blocking_failure(results: list[CheckResult]) -> bool:
    return any(not r.ok and r.severity == BLOCK for r in results)


def install_root(module_file: str = __file__) -> Path:
    """Directory the running ``visio_recorder`` package was imported from.

    A device carries two clones - the provisioned one under
    /opt/visio-recorder and the operator's own working clone - and nothing at
    the shell prompt distinguishes them. Reporting the resolved path makes
    every preflight transcript self-identifying, whether it came from an SSH
    session or from the unit's ExecStartPre in ``journalctl``.
    """
    return Path(module_file).resolve().parent


def format_report(results: list[CheckResult], root: Path | None = None) -> str:
    lines = [f"[INFO] install: {root if root is not None else install_root()}"]
    lines += [
        f"[{'OK' if r.ok else r.severity.upper()}] {r.name}: {r.detail}" for r in results
    ]
    return "\n".join(lines)


def main() -> int:
    """CLI entrypoint, exposed as the ``visio-preflight`` console script.

    Also reachable as ``python3 -m visio_recorder.preflight``; both resolve
    from any working directory once the package is actually installed into
    the venv (see this package's pyproject.toml [build-system] note).

    Thin on purpose, same convention as daemon.py's main() - no branching
    logic beyond what run_checks/has_blocking_failure already own, so it's
    not unit tested itself.
    """
    battery_source = os.environ.get("VISIO_BATTERY_SOURCE", "pijuice")
    results = run_checks(battery_source)
    print(format_report(results))
    return 1 if has_blocking_failure(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
