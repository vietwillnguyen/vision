# Visio Device Provisioning & Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An idempotent device-provisioning script and a firmware-level preflight check for the Visio pendant's Raspberry Pi Zero 2W, both aware that the PiJuice battery HAT is currently unavailable and must not block on its absence.

**Architecture:** `firmware/scripts/setup-device.sh` (bash) takes a freshly flashed Pi to a runnable state - packages, interfaces, code deploy, systemd install. `firmware/visio_recorder/preflight.py` (Python) verifies the runtime environment at every boot via `ExecStartPre=`, with two severities: `block` (daemon cannot function, exits non-zero) and `warn` (degraded but runnable - this is where the missing PiJuice HAT lives). `daemon.py` gains a `VISIO_BATTERY_SOURCE` config switch (`pijuice` default, `none` for now) selecting between the real `PiJuiceBatteryReader` and a new `UnmeteredPowerReader` that reports a fixed 100% so `battery.py`'s halt/low-battery thresholds never false-trigger while running from a USB power bank. The systemd unit drops `User=pi` for root, closing the NM-keyfile-write and `rpi_ws281x` DMA permission gaps in one change.

**Tech Stack:** Bash (provisioning script), Python 3.11 + `uv` (firmware, existing), `pytest` (existing), systemd.

**Source spec:** [`docs/superpowers/specs/2026-07-21-visio-device-provisioning-design.md`](../specs/2026-07-21-visio-device-provisioning-design.md) - read it for full rationale; this plan implements it task-by-task.

## Global Constraints

- Python 3.11+, `uv`-locked deps - the committed `uv.lock` is the reproducibility contract for firmware deployed to devices.
- Run all Python test commands from `firmware/`: `cd firmware && uv run pytest`.
- TDD per task: failing test first, then minimal implementation, then commit.
- Existing hardware-boundary convention: `Protocol`/injectable-callable seams with fakes in tests, never mocks; real hardware wrapped in `drivers.py` behind deferred imports and deliberately not unit tested - except `UnmeteredPowerReader` (Task 1), which has no hardware dependency and is unit tested like any other pure `BatteryReader`.
- Battery thresholds (`battery.py`, unchanged): halt below 10%, low-battery warning below 20%. Any battery-reader fallback value must clear both.
- OS target: Raspberry Pi OS Lite (Bookworm, 64-bit headless).
- `setup-device.sh` is idempotent: every step checks current state before acting. It never auto-reboots (detects and reports only) and never overwrites an existing `/etc/visio-recorder.env` (real secrets).
- `VISIO_BATTERY_SOURCE` is `"pijuice"` (default, existing behavior) or `"none"` (current bring-up state, no PiJuice HAT present). An unrecognized value is a startup config error, not a silent default.
- Missing PiJuice hardware is an accepted, expected state right now - nothing in this plan may treat it as fatal.

---

### Task 1: `UnmeteredPowerReader` battery driver

**Files:**
- Modify: `firmware/visio_recorder/drivers.py`
- Test: Create `firmware/tests/test_drivers.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `UnmeteredPowerReader` class in `drivers.py`, implementing `battery.BatteryReader` (`get_charge_pct() -> int`).

- [ ] **Step 1: Write the failing test**

`firmware/tests/test_drivers.py`:

```python
"""Unit tests for the one class in drivers.py with no hardware dependency.

Every other class here (PiJuiceBatteryReader, Ws2812LedDriver,
GpioZeroFlagButton) wraps a real device library behind a deferred import
and is deliberately not unit tested - see drivers.py's module docstring.
UnmeteredPowerReader is different: it touches no hardware at all, so it
gets the same test treatment as any other pure BatteryReader (compare
tests/fakes.py's FakeBatteryReader).
"""

from visio_recorder.drivers import UnmeteredPowerReader


def test_unmetered_power_reader_always_reports_full_charge():
    reader = UnmeteredPowerReader()

    assert reader.get_charge_pct() == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd firmware && uv run pytest tests/test_drivers.py -v`
Expected: FAIL - `ImportError: cannot import name 'UnmeteredPowerReader'`

- [ ] **Step 3: Write minimal implementation**

Append to `firmware/visio_recorder/drivers.py` (after `GpioZeroFlagButton`):

```python
class UnmeteredPowerReader:
    """Battery source for VISIO_BATTERY_SOURCE=none: no battery HAT present.

    Always reports full charge. battery.py's read_battery_status is purely
    numeric (should_halt = pct < 10, is_low = pct < 20), so any fallback
    value has to clear both thresholds or it would falsely halt the device,
    or show a low-battery LED, while running from a mains-equivalent USB
    power bank. There is no meaningful percentage to report and "unknown"
    isn't a valid int for BatteryReader.get_charge_pct(), so 100 - not a
    sentinel - is the only value that's actually correct here.
    """

    def get_charge_pct(self) -> int:
        return 100
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd firmware && uv run pytest tests/test_drivers.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/drivers.py firmware/tests/test_drivers.py
git commit -m "feat(firmware): add UnmeteredPowerReader for VISIO_BATTERY_SOURCE=none"
```

---

### Task 2: Battery source config + driver selection

**Files:**
- Modify: `firmware/visio_recorder/daemon.py`
- Test: Modify `firmware/tests/test_daemon.py`

**Interfaces:**
- Consumes: `UnmeteredPowerReader` (Task 1), `PiJuiceBatteryReader` (existing) from `drivers.py`; `BatteryReader` from `battery.py` (already imported in `daemon.py`).
- Produces: `DaemonConfig.battery_source: str`; `resolve_battery_reader_factory(source: str) -> Callable[[], BatteryReader]`.

- [ ] **Step 1: Write the failing tests**

In `firmware/tests/test_daemon.py`, update the existing top-of-file import block:

```python
from visio_recorder.daemon import (
    LoopDeps,
    load_config,
    resolve_battery_reader_factory,
    run_recording_loop,
    run_startup_sequence,
)
```

Add a new import line just below it:

```python
from visio_recorder.drivers import PiJuiceBatteryReader, UnmeteredPowerReader
```

Add these tests near the existing `load_config` tests:

```python
def test_load_config_defaults_battery_source_to_pijuice():
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key-123",
    }

    config = load_config(env)

    assert config.battery_source == "pijuice"


def test_load_config_reads_battery_source_override():
    env = {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "anon-key-123",
        "VISIO_BATTERY_SOURCE": "none",
    }

    config = load_config(env)

    assert config.battery_source == "none"


def test_resolve_battery_reader_factory_returns_pijuice_reader_class():
    assert resolve_battery_reader_factory("pijuice") is PiJuiceBatteryReader


def test_resolve_battery_reader_factory_returns_unmetered_reader_class():
    assert resolve_battery_reader_factory("none") is UnmeteredPowerReader


def test_resolve_battery_reader_factory_rejects_unrecognized_source():
    with pytest.raises(ValueError, match="bogus"):
        resolve_battery_reader_factory("bogus")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run pytest tests/test_daemon.py -v`
Expected: FAIL - `ImportError: cannot import name 'resolve_battery_reader_factory'`, then (once that's fixed) `AttributeError: 'DaemonConfig' object has no attribute 'battery_source'`

- [ ] **Step 3: Write minimal implementation**

In `firmware/visio_recorder/daemon.py`, update the drivers import:

```python
from visio_recorder.drivers import (
    GpioZeroFlagButton,
    PiJuiceBatteryReader,
    UnmeteredPowerReader,
    Ws2812LedDriver,
)
```

Add `battery_source: str` to the `DaemonConfig` dataclass, after `framerate`:

```python
@dataclass
class DaemonConfig:
    supabase_url: str
    supabase_anon_key: str
    data_dir: Path
    segment_duration_ms: int
    framerate: int
    battery_source: str
```

Update `load_config` to populate it:

```python
def load_config(env: Mapping[str, str]) -> DaemonConfig:
    for key in ("SUPABASE_URL", "SUPABASE_ANON_KEY"):
        if key not in env:
            raise ValueError(f"missing required environment variable: {key}")
    return DaemonConfig(
        supabase_url=env["SUPABASE_URL"],
        supabase_anon_key=env["SUPABASE_ANON_KEY"],
        data_dir=Path(env.get("VISIO_DATA_DIR", _DEFAULT_DATA_DIR)),
        segment_duration_ms=int(
            env.get("VISIO_SEGMENT_DURATION_MS", _DEFAULT_SEGMENT_DURATION_MS)
        ),
        framerate=int(env.get("VISIO_FRAMERATE", _DEFAULT_FRAMERATE)),
        battery_source=env.get("VISIO_BATTERY_SOURCE", "pijuice"),
    )
```

Add the factory map and resolver function near the other module-level constants (after `_DEVICE_NAME`):

```python
_BATTERY_READER_FACTORIES: dict[str, Callable[[], BatteryReader]] = {
    "pijuice": PiJuiceBatteryReader,
    "none": UnmeteredPowerReader,
}


def resolve_battery_reader_factory(source: str) -> Callable[[], BatteryReader]:
    try:
        return _BATTERY_READER_FACTORIES[source]
    except KeyError:
        valid = ", ".join(sorted(_BATTERY_READER_FACTORIES))
        raise ValueError(
            f"unrecognized VISIO_BATTERY_SOURCE {source!r}; must be one of: {valid}"
        ) from None
```

In `main()`, replace the hardcoded battery reader construction:

```python
battery_reader = PiJuiceBatteryReader()
```

with:

```python
battery_reader = resolve_battery_reader_factory(config.battery_source)()
```

Note this line in `main()` now runs *after* `config = load_config(os.environ)` - no reordering needed, `battery_reader` construction already happens after config loading in the current code.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd firmware && uv run pytest tests/test_daemon.py -v`
Expected: PASS (all tests, including the pre-existing ones - confirms no regression)

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/daemon.py firmware/tests/test_daemon.py
git commit -m "feat(firmware): add VISIO_BATTERY_SOURCE config and battery driver selection"
```

---

### Task 3: Preflight check functions

**Files:**
- Create: `firmware/visio_recorder/preflight.py`
- Test: Create `firmware/tests/test_preflight.py`

**Interfaces:**
- Consumes: nothing (each function injects its own real default: `shutil.which`, `importlib.util.find_spec`, a `subprocess.run`-shaped callable, or a `Path`).
- Produces: `CheckResult` dataclass (`name: str, severity: str, ok: bool, detail: str`); `BLOCK = "block"`, `WARN = "warn"`; `check_binary_on_path`, `check_importable`, `check_pijuice_importable`, `check_camera_detected`, `check_networkmanager_running`, `check_data_dir_writable`, `check_i2c_enabled` - each `-> CheckResult`.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_preflight.py`:

```python
"""Unit tests for visio_recorder.preflight's individual check functions.

Every check takes an injectable seam (a callable or a Path) defaulting to
the real implementation, so these run without touching real hardware -
the same fake-injection convention used throughout firmware/.
"""

import subprocess

from visio_recorder.preflight import (
    BLOCK,
    WARN,
    check_binary_on_path,
    check_camera_detected,
    check_data_dir_writable,
    check_i2c_enabled,
    check_importable,
    check_networkmanager_running,
    check_pijuice_importable,
)


def test_check_binary_on_path_ok_when_found():
    result = check_binary_on_path("ffmpeg", which=lambda name: "/usr/bin/ffmpeg")

    assert result.name == "ffmpeg"
    assert result.severity == BLOCK
    assert result.ok is True


def test_check_binary_on_path_fails_when_missing():
    result = check_binary_on_path("ffmpeg", which=lambda name: None)

    assert result.ok is False
    assert result.severity == BLOCK
    assert "ffmpeg" in result.detail


def test_check_importable_ok_when_found():
    result = check_importable("supabase", find_spec=lambda mod: object())

    assert result.ok is True
    assert result.severity == BLOCK


def test_check_importable_fails_when_missing():
    result = check_importable("supabase", find_spec=lambda mod: None)

    assert result.ok is False


def test_check_pijuice_importable_is_warn_severity():
    result = check_pijuice_importable(find_spec=lambda mod: None)

    assert result.severity == WARN
    assert result.ok is False


def _completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_check_camera_detected_ok_when_camera_listed():
    result = check_camera_detected(
        run=lambda args: _completed(
            0, "Available cameras\n-----------------\n0 : imx708_wide [4608x2592]\n"
        )
    )

    assert result.ok is True
    assert result.severity == BLOCK


def test_check_camera_detected_fails_when_none_listed():
    result = check_camera_detected(
        run=lambda args: _completed(0, "Available cameras\n-----------------\n")
    )

    assert result.ok is False


def test_check_camera_detected_fails_when_command_errors():
    result = check_camera_detected(run=lambda args: _completed(1, ""))

    assert result.ok is False


def test_check_networkmanager_running_ok_on_zero_exit():
    result = check_networkmanager_running(run=lambda args: _completed(0, "connected"))

    assert result.ok is True
    assert result.severity == BLOCK


def test_check_networkmanager_running_fails_on_nonzero_exit():
    result = check_networkmanager_running(run=lambda args: _completed(1, ""))

    assert result.ok is False


def test_check_data_dir_writable_ok_for_existing_writable_dir(tmp_path):
    result = check_data_dir_writable(path=tmp_path)

    assert result.ok is True
    assert result.severity == BLOCK


def test_check_data_dir_writable_fails_when_missing(tmp_path):
    result = check_data_dir_writable(path=tmp_path / "does-not-exist")

    assert result.ok is False


def test_check_i2c_enabled_ok_when_device_exists(tmp_path):
    fake_i2c = tmp_path / "i2c-1"
    fake_i2c.touch()

    result = check_i2c_enabled(path=fake_i2c)

    assert result.ok is True
    assert result.severity == WARN


def test_check_i2c_enabled_fails_when_device_missing(tmp_path):
    result = check_i2c_enabled(path=tmp_path / "i2c-1")

    assert result.ok is False
    assert result.severity == WARN
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run pytest tests/test_preflight.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.preflight'`

- [ ] **Step 3: Write minimal implementation**

`firmware/visio_recorder/preflight.py`:

```python
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
    result = run(["rpicam-hello", "--list-cameras"])
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
    result = run(["nmcli", "general", "status"])
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd firmware && uv run pytest tests/test_preflight.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/preflight.py firmware/tests/test_preflight.py
git commit -m "feat(firmware): add preflight check functions with block/warn severity"
```

---

### Task 4: Preflight composition (run_checks, severity aggregation, report, CLI)

**Files:**
- Modify: `firmware/visio_recorder/preflight.py` (append)
- Test: Modify `firmware/tests/test_preflight.py` (append)

**Interfaces:**
- Consumes: `CheckResult`, `BLOCK`, `WARN`, and all `check_*` functions from Task 3.
- Produces: `run_checks(battery_source: str) -> list[CheckResult]`; `has_blocking_failure(results: list[CheckResult]) -> bool`; `format_report(results: list[CheckResult]) -> str`; `main() -> int` (CLI entrypoint, not unit tested - same convention as `daemon.py`'s `main()`).

- [ ] **Step 1: Write the failing tests**

Append to `firmware/tests/test_preflight.py`, updating the import block to add `CheckResult`, `format_report`, `has_blocking_failure`, `run_checks`:

```python
from visio_recorder.preflight import (
    BLOCK,
    WARN,
    CheckResult,
    check_binary_on_path,
    check_camera_detected,
    check_data_dir_writable,
    check_i2c_enabled,
    check_importable,
    check_networkmanager_running,
    check_pijuice_importable,
    format_report,
    has_blocking_failure,
    run_checks,
)
```

Add these tests at the end of the file:

```python
def test_run_checks_includes_pijuice_and_i2c_when_battery_source_is_pijuice():
    names = {r.name for r in run_checks("pijuice")}

    assert "pijuice" in names
    assert "i2c" in names


def test_run_checks_excludes_pijuice_and_i2c_when_battery_source_is_none():
    names = {r.name for r in run_checks("none")}

    assert "pijuice" not in names
    assert "i2c" not in names


def test_has_blocking_failure_true_when_a_block_check_fails():
    results = [
        CheckResult(name="a", severity=BLOCK, ok=False, detail="x"),
        CheckResult(name="b", severity=WARN, ok=True, detail="y"),
    ]

    assert has_blocking_failure(results) is True


def test_has_blocking_failure_false_when_only_warn_checks_fail():
    results = [
        CheckResult(name="a", severity=BLOCK, ok=True, detail="x"),
        CheckResult(name="b", severity=WARN, ok=False, detail="y"),
    ]

    assert has_blocking_failure(results) is False


def test_format_report_marks_failures_with_severity_and_passes_as_ok():
    results = [
        CheckResult(name="a", severity=BLOCK, ok=True, detail="fine"),
        CheckResult(name="b", severity=WARN, ok=False, detail="missing"),
    ]

    report = format_report(results)

    assert "[OK] a: fine" in report
    assert "[WARN] b: missing" in report
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run pytest tests/test_preflight.py -v`
Expected: FAIL - `ImportError: cannot import name 'run_checks'`

- [ ] **Step 3: Write minimal implementation**

Append to `firmware/visio_recorder/preflight.py`:

```python
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


def format_report(results: list[CheckResult]) -> str:
    lines = [
        f"[{'OK' if r.ok else r.severity.upper()}] {r.name}: {r.detail}" for r in results
    ]
    return "\n".join(lines)


def main() -> int:
    """CLI entrypoint: python3 -m visio_recorder.preflight.

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd firmware && uv run pytest tests/test_preflight.py -v`
Expected: PASS (all tests in the file, including Task 3's)

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/preflight.py firmware/tests/test_preflight.py
git commit -m "feat(firmware): compose preflight checks into a CLI with block/warn exit behavior"
```

---

### Task 5: Wire preflight into `daemon.py`'s `main()`

**Files:**
- Modify: `firmware/visio_recorder/daemon.py`

**Interfaces:**
- Consumes: `visio_recorder.preflight` module (Tasks 3-4).
- Produces: no new public interface - `main()` gains a preflight gate at its very top.

- [ ] **Step 1: Add the preflight import and call**

In `firmware/visio_recorder/daemon.py`, add near the top of the file (with the other `visio_recorder` imports):

```python
from visio_recorder import preflight
```

At the very top of `main()`, before `config = load_config(os.environ)`, add:

```python
def main() -> int:
    """Compose the daemon from its already-tested parts.

    Thin on purpose: no branching logic of its own beyond the boot-sequence
    gates the pieces it calls already own. Validated on real hardware in
    Epic 5 rather than through additional unit tests (see the plan's
    Handoff section).
    """
    preflight_battery_source = os.environ.get("VISIO_BATTERY_SOURCE", "pijuice")
    preflight_results = preflight.run_checks(preflight_battery_source)
    print(preflight.format_report(preflight_results))
    if preflight.has_blocking_failure(preflight_results):
        return 1

    config = load_config(os.environ)
    ...
```

Note: `preflight_battery_source` is read from `os.environ` directly here, duplicating what `load_config` will do a few lines later. This is intentional, not an oversight - the whole point of running preflight *before* `load_config` is to diagnose a broken environment even when `SUPABASE_URL`/`SUPABASE_ANON_KEY` are also missing, so preflight can't depend on `load_config` having already succeeded.

Leave the rest of `main()` unchanged (the existing `session_path`, `state_path`, battery/LED startup, onboarding, etc. all stay exactly as they are, just now running after the preflight gate).

- [ ] **Step 2: Run the full test suite to confirm no regressions**

Run: `cd firmware && uv run pytest -v`
Expected: PASS (all tests across every file - `main()` itself has no new tests per the existing convention, but this confirms nothing else broke)

- [ ] **Step 3: Commit**

```bash
git add firmware/visio_recorder/daemon.py
git commit -m "feat(firmware): run preflight checks before daemon startup"
```

---

### Task 6: systemd unit - root, `ExecStartPre`, venv-aware paths

**Files:**
- Modify: `firmware/systemd/visio-recorder.service`

**Interfaces:**
- Produces: updated systemd unit. Not unit tested (no branching logic); verified with `systemd-analyze verify`, same as when the unit was first created.

- [ ] **Step 1: Rewrite the unit file**

`firmware/systemd/visio-recorder.service`:

```ini
[Unit]
Description=Visio Pendant Recording Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/visio-recorder.env
ExecStartPre=/opt/visio-recorder/.venv/bin/python3 -m visio_recorder.preflight
ExecStart=/opt/visio-recorder/.venv/bin/python3 -m visio_recorder.daemon
WorkingDirectory=/opt/visio-recorder
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Two changes from the previous version: `User=pi` is removed (runs as root - see the spec's "Permission model change"), and both `ExecStartPre`/`ExecStart` point at `/opt/visio-recorder/.venv/bin/python3` (the `uv sync`-created project venv) instead of `/usr/bin/python3` (system Python, which never had `visio_recorder` installed into it).

- [ ] **Step 2: Verify the unit file syntax**

Run: `systemd-analyze verify firmware/systemd/visio-recorder.service`
Expected: no output, or only warnings about `ExecStart`/`ExecStartPre` binaries not existing on this dev machine - both are fine (same caveat as when this unit was first created); full validation happens on the real Pi in Task 9.

- [ ] **Step 3: Commit**

```bash
git add firmware/systemd/visio-recorder.service
git commit -m "feat(firmware): run visio-recorder as root with preflight ExecStartPre and venv paths"
```

---

### Task 7: `setup-device.sh` provisioning script

**Files:**
- Create: `firmware/scripts/setup-device.sh`

**Interfaces:**
- Produces: an idempotent, root-run bash script. No automated test (OS provisioning); syntax-checked with `bash -n`, functionally verified on real hardware in Task 9.

- [ ] **Step 1: Write the script**

`firmware/scripts/setup-device.sh`:

```bash
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

log "Step 1/8: apt packages"
apt-get update -y
apt-get install -y rpicam-apps zbar-tools ffmpeg network-manager git pijuice-base

log "Step 2/8: interfaces"
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

log "Step 3/8: uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="${HOME}/.local/bin:${PATH}"
else
  log "uv already installed"
fi

log "Step 4/8: code"
if [[ -d "${INSTALL_DIR}/.git" ]]; then
  git -C "${INSTALL_DIR}" pull
else
  git clone "${REPO_URL}" "${INSTALL_DIR}"
fi
(cd "${INSTALL_DIR}/firmware" && uv sync --locked)

log "Step 5/8: systemd"
cp "${INSTALL_DIR}/${SYSTEMD_UNIT_SRC}" "${SYSTEMD_UNIT_DST}"
systemctl daemon-reload
systemctl enable visio-recorder

log "Step 6/8: env file"
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

log "Step 7/8: data dir"
mkdir -p "${DATA_DIR}"

log "Step 8/8: summary"
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
```

Note on Step 2's camera toggle: `raspi-config nonint`'s exact camera subcommand varies across Raspberry Pi OS releases (older images use `do_camera`/`get_camera`; Bookworm's libcamera stack often auto-detects without one). The `>/dev/null 2>&1` guard means this degrades safely either way, but confirm the actual behavior against the real OS image in Task 9 and adjust if `get_camera` doesn't exist at all on this Bookworm build.

- [ ] **Step 2: Syntax-check the script**

Run: `bash -n firmware/scripts/setup-device.sh`
Expected: no output (clean syntax)

- [ ] **Step 3: Make it executable and commit**

```bash
chmod +x firmware/scripts/setup-device.sh
git add firmware/scripts/setup-device.sh
git commit -m "feat(firmware): add idempotent setup-device.sh provisioning script"
```

---

### Task 8: Update README prerequisites

**Files:**
- Modify: `README.md` (repo root)

**Interfaces:**
- Produces: updated prose in the "Firmware (Epic 1)" section - no code.

- [ ] **Step 1: Replace the stale prerequisites line**

In `README.md`, find this line (in the "Firmware (Epic 1)" section):

```
Device prerequisites beyond this project's `uv.lock`: `apt install zbar-tools` for QR decoding, plus the `pijuice` and `rpi_ws281x` system packages for the battery and LED drivers on the Pi.
```

Replace it with:

```
Device setup: run `sudo firmware/scripts/setup-device.sh` on the target Pi (idempotent - safe to re-run after an SD re-flash or to repair drift). It installs `rpicam-apps`, `zbar-tools`, `ffmpeg`, `network-manager`, `git`, and `pijuice-base`, enables the I2C/camera interfaces, deploys the code via `uv sync --locked` into `/opt/visio-recorder`, and installs the systemd unit. `visio_recorder.preflight` runs automatically before every start (`ExecStartPre=`) and standalone via `python3 -m visio_recorder.preflight` for manual diagnostics; missing PiJuice hardware only warns (`VISIO_BATTERY_SOURCE=none` is the current bring-up default - see [`2026-07-21-visio-device-provisioning-design.md`](docs/superpowers/specs/2026-07-21-visio-device-provisioning-design.md)), everything else blocks startup with a clear reason in `journalctl`.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: point firmware prerequisites at setup-device.sh and the provisioning spec"
```

---

### Task 9: Physical Pi Zero 2W integration test

**Files:** none (verification only - no code changes).

**Verification artifact:** a Pi Zero 2W running `visio-recorder` under the new root/preflight/battery-source setup, confirmed via SSH, on a USB power bank.

This task requires the physical device. Steps needing direct visual/physical confirmation (LED color, camera framing, button feel) are marked **[human]**; the rest are plain SSH commands either the human or an agent with SSH access to the device can run and report output for.

- [ ] **Step 1: Run the setup script end to end**

SSH to the Pi, clone/pull isn't needed manually - the script does it:

```bash
ssh <user>@visio-pendant.local
curl -fsSL https://raw.githubusercontent.com/vietwillnguyen/vision/main/firmware/scripts/setup-device.sh -o setup-device.sh
sudo bash setup-device.sh
```

A script fetched by `curl` is not in a git checkout, so step 5/10 prints a loud WARNING that it is falling back to the moving `main` - expected here, and also why this form cannot verify a pre-merge branch.
To provision an exact revision, clone the repo on the Pi and run `firmware/scripts/setup-device.sh` from that clone (it defaults to the clone's own commit), or set `VISIO_GIT_REF=<branch|tag|sha>` in the root environment the script runs in.

Expected first run (env file doesn't exist yet): script exits 0 after writing `/etc/visio-recorder.env`, printing "fill in SUPABASE_URL and SUPABASE_ANON_KEY, then re-run this script".

- [ ] **Step 2: Fill in secrets and re-run**

```bash
sudo nano /etc/visio-recorder.env   # fill in SUPABASE_URL, SUPABASE_ANON_KEY; leave VISIO_BATTERY_SOURCE=none
sudo bash setup-device.sh
```

Expected: completes through all 10 steps; summary reports the provisioned commit SHA and `VISIO_BATTERY_SOURCE=none` with the bring-up reminder, and states whether a reboot is required.

- [ ] **Step 3: Reboot if the summary said to**

```bash
sudo reboot
```

Wait ~30s, then reconnect: `ssh <user>@visio-pendant.local`

- [ ] **Step 4: Run preflight standalone and confirm no battery warnings**

```bash
visio-preflight; echo "exit: $?"
```

No `cd`, no venv path and no `sudo`: the setup script symlinks the console script into `/usr/local/bin` (see the README's firmware section).

Expected: an opening `[INFO] install:` line naming `/opt/visio-recorder/firmware/visio_recorder`, then every check `[OK]`, no `pijuice`/`i2c` lines at all (skipped under `VISIO_BATTERY_SOURCE=none`), `exit: 0`. If anything shows `[BLOCK]`, stop here and fix it before proceeding - that's exactly the failure this task exists to catch before it becomes an opaque daemon crash.

- [ ] **Step 5: Start the daemon and confirm it runs as root with no permission errors**

```bash
sudo systemctl start visio-recorder
sudo systemctl status visio-recorder
sudo journalctl -u visio-recorder -f
```

Expected: `Active: active (running)`, no `PermissionError` writing the NM keyfile, no LED driver DMA/permission errors.

- [ ] **Step 6: [human] Confirm the QR onboarding flow end to end**

Generate an onboarding QR (WiFi SSID/password + Supabase session tokens per `wifi_onboard.py`'s payload format) and present it to the camera. Confirm: the LED shows the onboarding state, `nmcli connection reload && nmcli connection up visio` succeeds, and the Pi comes up on the target WiFi network.

- [ ] **Step 7: [human] Confirm the flag button and LED**

Press the physical flag button while a segment is recording. Confirm: a `FLAG_YYYYMMDD_HHMMSS.marker` is created and uploaded (check Supabase Storage), and the LED responds as expected (no visible lag or wrong color).

- [ ] **Step 8: Confirm a full capture/mux/upload cycle**

Let the daemon run for at least one full segment duration (default 5 minutes, or set `VISIO_SEGMENT_DURATION_MS` lower for a faster test loop):

```bash
sudo journalctl -u visio-recorder --since "5 minutes ago" | grep -i "segment\|upload"
```

Expected: a `.h264` segment recorded, muxed to `.mp4` via `ffmpeg`, uploaded to Supabase Storage, and a `device_status` row updated with real disk stats.

- [ ] **Step 9: Confirm restart recovery**

```bash
sudo systemctl restart visio-recorder
sudo journalctl -u visio-recorder --since "1 minute ago"
```

Expected: no re-onboarding (session already persisted), no re-registration (device already registered), any queued-but-unsent segment from before the restart gets flushed first.

- [ ] **Step 10: Confirm power-loss resilience**

**[human]** Physically disconnect the USB power bank mid-recording (not a clean `systemctl stop`), then reconnect. Confirm the Pi boots cleanly and the daemon starts without a corrupted `session.json` or NM keyfile blocking the next boot.

- [ ] **Step 11: Record the outcome**

If every step above passes, this plan is fully verified end to end. If anything fails, file it as a GitHub issue against the `vision` repo referencing this plan and the specific step, rather than patching ad hoc - the fix should go through its own TDD cycle like everything else in this codebase.
