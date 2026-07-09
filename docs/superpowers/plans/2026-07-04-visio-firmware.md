# Visio Pendant - Firmware Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `visio-recorder` Python systemd daemon: boot-time battery check, WiFi + auth onboarding, device registration, rolling segment capture and upload, manual flag button, LED state machine, and device status reporting.

**Architecture:** Every component that touches real hardware or a network API (PiJuice, GPIO, WS2812B LEDs, `ffmpeg`/`rpicam-vid` subprocesses, Supabase) is defined as a small `Protocol`/abstract class. Business logic is pure functions that take an instance of that interface as an argument, so unit tests inject a fake and never touch real hardware or network. `daemon.py` is the only module that wires real implementations together; it is not unit tested itself beyond the startup sequence (Task 10), since the remaining wiring (process supervision for `rpicam-vid`, threading, real disk-usage stats) has no branching logic of its own - see Handoff.

**Tech Stack:** Python 3.11, pytest, `pigpio`/`rpi_ws281x` (real LED driver, not exercised in tests), `pijuice` (real battery driver, not exercised in tests), `supabase-py` client, `ffmpeg`/`rpicam-vid` CLI tools.

**As-built note:** a later coordinated rename (issue #9, ahead of Epic 5, per this plan's Handoff) changed flag marker filenames from `FLAG_HHMMSS.marker` to dated `FLAG_YYYYMMDD_HHMMSS.marker` names.
Inline task snippets showing the undated format are historical; the `firmware/` source is authoritative where they differ.

## Global Constraints

- Segments are 5-minute rolling H.264 files named `YYYYMMDD_HHMMSS.h264`, muxed to MP4 with `ffmpeg -i seg.h264 -c copy seg.mp4`.
- Battery halt threshold: `< 10%`. Battery low-warning threshold: `< 20%`.
- LED states: Recording = solid green, Uploading = pulsing blue, Low battery (<20%) = pulsing yellow, Critical/error = red flash. Low battery takes priority over whatever else is happening (uploading on a low battery still shows yellow, not blue).
- Manual flag creates `FLAG_YYYYMMDD_HHMMSS.marker` in the upload queue, which is uploaded to Supabase Storage the same way a segment is - the cloud pipeline (see [`2026-07-04-visio-pipeline.md`](2026-07-04-visio-pipeline.md) Task 11) is what turns it into a `manually_flagged` update on the matching `segments` row.
- On successful upload, the local file is deleted to free SD space.
- Device status upsert happens after each successful segment upload, targeting the `device_status` table from [`2026-07-04-visio-supabase-foundation.md`](2026-07-04-visio-supabase-foundation.md).
- Every device is linked to a user account via a `device_id` UUID generated on first boot (spec, Mobile Companion App > Auth). Since every table is RLS-scoped to `auth.uid()` ([`2026-07-04-visio-supabase-foundation.md`](2026-07-04-visio-supabase-foundation.md) Task 7), the device cannot write to Supabase as an anonymous client - it needs an authenticated session. This plan's onboarding flow (Task 8) carries a Supabase user session (access + refresh token) inside the same QR payload that carries the WiFi credentials, captured during pairing while the user's own phone is already signed in. This is the simplest mechanism that satisfies RLS without provisioning a separate device-credential system, and should be confirmed with the spec owner before Epic 5 if a more formal device-credential flow is wanted later.
- OS target: Raspberry Pi OS Lite (Bookworm, 64-bit headless) - code must run under Python 3.11.

---

### Task 1: Project scaffolding

**Files:**
- Create: `firmware/pyproject.toml`
- Create: `firmware/visio_recorder/__init__.py`
- Create: `firmware/tests/__init__.py`

**Interfaces:**
- Produces: a `pytest` command runnable from `firmware/` that discovers `tests/`.

- [ ] **Step 1: Create the package layout**

```bash
mkdir -p firmware/visio_recorder firmware/tests firmware/systemd
touch firmware/visio_recorder/__init__.py firmware/tests/__init__.py
```

- [ ] **Step 2: Write `pyproject.toml`**

`firmware/pyproject.toml`:
```toml
[project]
name = "visio-recorder"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "supabase>=2.4.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0"]

[tool.setuptools]
packages = ["visio_recorder"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: Install and verify pytest runs with zero tests**

Dependencies are managed with [`uv`](https://docs.astral.sh/uv/) rather than a hand-rolled venv, so the lockfile (`firmware/uv.lock`) stays reproducible across dev machines and deployed devices:

```bash
cd firmware
uv sync --extra dev
uv run pytest
```
Expected: `no tests ran` (exit 0/5 depending on pytest version - either is fine at this stage).

- [ ] **Step 4: Commit**

```bash
git add firmware/pyproject.toml firmware/visio_recorder/__init__.py firmware/tests/__init__.py
git commit -m "chore: scaffold firmware python package"
```

---

### Task 2: Battery monitor

**Files:**
- Create: `firmware/visio_recorder/battery.py`
- Test: `firmware/tests/test_battery.py`

**Interfaces:**
- Produces: `BatteryReader` (Protocol with `get_charge_pct() -> int`), `BatteryStatus` dataclass (`pct: int`, `should_halt: bool`, `is_low: bool`), `read_battery_status(reader: BatteryReader) -> BatteryStatus`, constants `LOW_BATTERY_HALT_PCT = 10`, `LOW_BATTERY_WARN_PCT = 20`.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_battery.py`:
```python
from visio_recorder.battery import BatteryReader, read_battery_status


class FakeBatteryReader(BatteryReader):
    def __init__(self, pct: int) -> None:
        self._pct = pct

    def get_charge_pct(self) -> int:
        return self._pct


def test_battery_above_thresholds_is_normal():
    status = read_battery_status(FakeBatteryReader(85))
    assert status.pct == 85
    assert status.should_halt is False
    assert status.is_low is False


def test_battery_below_warn_threshold_is_low():
    status = read_battery_status(FakeBatteryReader(15))
    assert status.is_low is True
    assert status.should_halt is False


def test_battery_below_halt_threshold_should_halt():
    status = read_battery_status(FakeBatteryReader(5))
    assert status.should_halt is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_battery.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.battery'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/battery.py`:
```python
from dataclasses import dataclass
from typing import Protocol

LOW_BATTERY_HALT_PCT = 10
LOW_BATTERY_WARN_PCT = 20


class BatteryReader(Protocol):
    def get_charge_pct(self) -> int:
        ...


@dataclass
class BatteryStatus:
    pct: int
    should_halt: bool
    is_low: bool


def read_battery_status(reader: BatteryReader) -> BatteryStatus:
    pct = reader.get_charge_pct()
    return BatteryStatus(
        pct=pct,
        should_halt=pct < LOW_BATTERY_HALT_PCT,
        is_low=pct < LOW_BATTERY_WARN_PCT,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_battery.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/battery.py firmware/tests/test_battery.py
git commit -m "feat: add battery monitor"
```

---

### Task 3: LED state machine

**Files:**
- Create: `firmware/visio_recorder/led.py`
- Test: `firmware/tests/test_led.py`

**Interfaces:**
- Produces: `LedState` enum (`RECORDING`, `UPLOADING`, `LOW_BATTERY`, `CRITICAL`), `LedPattern` enum (`SOLID`, `PULSING`, `FLASHING`), `LedDriver` Protocol (single method `set(color: tuple[int, int, int], pattern: LedPattern) -> None`), `apply_led_state(driver: LedDriver, state: LedState) -> None`.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_led.py`:
```python
from visio_recorder.led import LedDriver, LedPattern, LedState, apply_led_state


class FakeLedDriver(LedDriver):
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[int, int, int], LedPattern]] = []

    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        self.calls.append((color, pattern))


def test_recording_state_is_solid_green():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.RECORDING)
    assert driver.calls == [((0, 255, 0), LedPattern.SOLID)]


def test_uploading_state_is_pulsing_blue():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.UPLOADING)
    assert driver.calls == [((0, 0, 255), LedPattern.PULSING)]


def test_low_battery_state_is_pulsing_yellow():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.LOW_BATTERY)
    assert driver.calls == [((255, 255, 0), LedPattern.PULSING)]


def test_critical_state_is_flashing_red():
    driver = FakeLedDriver()
    apply_led_state(driver, LedState.CRITICAL)
    assert driver.calls == [((255, 0, 0), LedPattern.FLASHING)]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_led.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.led'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/led.py`:
```python
from enum import Enum
from typing import Protocol


class LedState(Enum):
    RECORDING = "recording"
    UPLOADING = "uploading"
    LOW_BATTERY = "low_battery"
    CRITICAL = "critical"


class LedPattern(Enum):
    SOLID = "solid"
    PULSING = "pulsing"
    FLASHING = "flashing"


class LedDriver(Protocol):
    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        ...


GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)

_STATE_TO_COLOR_PATTERN: dict[LedState, tuple[tuple[int, int, int], LedPattern]] = {
    LedState.RECORDING: (GREEN, LedPattern.SOLID),
    LedState.UPLOADING: (BLUE, LedPattern.PULSING),
    LedState.LOW_BATTERY: (YELLOW, LedPattern.PULSING),
    LedState.CRITICAL: (RED, LedPattern.FLASHING),
}


def apply_led_state(driver: LedDriver, state: LedState) -> None:
    color, pattern = _STATE_TO_COLOR_PATTERN[state]
    driver.set(color, pattern)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_led.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/led.py firmware/tests/test_led.py
git commit -m "feat: add LED state machine"
```

---

### Task 4: Segment muxer (H.264 → MP4)

**Files:**
- Create: `firmware/visio_recorder/muxer.py`
- Test: `firmware/tests/test_muxer.py`

**Interfaces:**
- Produces: `CommandRunner` Protocol (`run(args: list[str]) -> None`), `SubprocessCommandRunner` (real implementation, not unit tested), `mux_segment(runner: CommandRunner, h264_path: Path, framerate: int) -> Path`. The capture framerate is threaded through explicitly (`ffmpeg -framerate <fps> -i ...`) rather than assumed, since `rpicam-vid` can be configured to capture at rates other than the nominal default and a mismatched `-framerate` produces a corrupted-looking MP4 without `ffmpeg` erroring.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_muxer.py`:
```python
from pathlib import Path

from visio_recorder.muxer import CommandRunner, mux_segment


class FakeCommandRunner(CommandRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> None:
        self.calls.append(args)


def test_mux_segment_returns_mp4_path():
    runner = FakeCommandRunner()
    result = mux_segment(runner, Path("/data/20260704_120000.h264"), framerate=30)
    assert result == Path("/data/20260704_120000.mp4")


def test_mux_segment_invokes_ffmpeg_with_input_framerate_and_copy_codec():
    runner = FakeCommandRunner()
    mux_segment(runner, Path("/data/20260704_120000.h264"), framerate=30)
    assert runner.calls == [
        [
            "ffmpeg", "-y",
            "-framerate", "30",
            "-i", "/data/20260704_120000.h264",
            "-c", "copy",
            "/data/20260704_120000.mp4",
        ]
    ]


def test_mux_segment_threads_through_the_given_framerate():
    runner = FakeCommandRunner()
    mux_segment(runner, Path("/data/20260704_120000.h264"), framerate=25)
    framerate_idx = runner.calls[0].index("-framerate") + 1
    assert runner.calls[0][framerate_idx] == "25"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_muxer.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.muxer'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/muxer.py`:
```python
import subprocess
from pathlib import Path
from typing import Protocol


class CommandRunner(Protocol):
    def run(self, args: list[str]) -> None:
        ...


class SubprocessCommandRunner:
    def run(self, args: list[str]) -> None:
        subprocess.run(args, check=True)


def mux_segment(runner: CommandRunner, h264_path: Path, framerate: int) -> Path:
    mp4_path = h264_path.with_suffix(".mp4")
    runner.run(
        [
            "ffmpeg",
            "-y",
            "-framerate",
            str(framerate),
            "-i",
            str(h264_path),
            "-c",
            "copy",
            str(mp4_path),
        ]
    )
    return mp4_path
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_muxer.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/muxer.py firmware/tests/test_muxer.py
git commit -m "feat: add h264 to mp4 muxer"
```

---

### Task 5: Upload queue

**Files:**
- Create: `firmware/visio_recorder/upload_queue.py`
- Test: `firmware/tests/test_upload_queue.py`

**Interfaces:**
- Produces: `enqueue(queue_dir: Path, source_path: Path) -> Path`, `list_pending(queue_dir: Path) -> list[Path]`, `mark_uploaded(path: Path) -> None`.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_upload_queue.py`:
```python
from visio_recorder.upload_queue import enqueue, list_pending, mark_uploaded


def test_enqueue_moves_file_into_queue_dir(tmp_path):
    source = tmp_path / "source" / "seg.mp4"
    source.parent.mkdir()
    source.write_bytes(b"data")
    queue_dir = tmp_path / "queue"

    dest = enqueue(queue_dir, source)

    assert dest == queue_dir / "seg.mp4"
    assert dest.exists()
    assert not source.exists()


def test_list_pending_returns_sorted_queue_contents(tmp_path):
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "20260704_120500.mp4").write_bytes(b"")
    (queue_dir / "20260704_120000.mp4").write_bytes(b"")
    (queue_dir / "FLAG_120300.marker").touch()

    pending = list_pending(queue_dir)

    assert [p.name for p in pending] == [
        "20260704_120000.mp4",
        "20260704_120500.mp4",
        "FLAG_120300.marker",
    ]


def test_list_pending_on_missing_dir_returns_empty():
    from pathlib import Path
    assert list_pending(Path("/nonexistent/queue")) == []


def test_mark_uploaded_deletes_file(tmp_path):
    path = tmp_path / "seg.mp4"
    path.write_bytes(b"data")

    mark_uploaded(path)

    assert not path.exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_upload_queue.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.upload_queue'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/upload_queue.py`:
```python
import shutil
from pathlib import Path


def enqueue(queue_dir: Path, source_path: Path) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    dest = queue_dir / source_path.name
    shutil.move(str(source_path), str(dest))
    return dest


def list_pending(queue_dir: Path) -> list[Path]:
    if not queue_dir.exists():
        return []
    return sorted(queue_dir.iterdir(), key=lambda p: p.name)


def mark_uploaded(path: Path) -> None:
    path.unlink()
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_upload_queue.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/upload_queue.py firmware/tests/test_upload_queue.py
git commit -m "feat: add upload queue"
```

---

### Task 6: Supabase uploader

**Files:**
- Create: `firmware/visio_recorder/uploader.py`
- Test: `firmware/tests/test_uploader.py`

**Interfaces:**
- Produces: `StorageClient` Protocol (`upload(bucket: str, object_path: str, local_path: Path) -> None`), `upload_segment(client: StorageClient, device_id: str, local_path: Path) -> str` returning the object path `{device_id}/{filename}`. Used for both `.mp4` segments and `.marker` flag files - the function only cares about the filename, so the same call uploads either.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_uploader.py`:
```python
from pathlib import Path

from visio_recorder.uploader import StorageClient, upload_segment


class FakeStorageClient(StorageClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        self.calls.append((bucket, object_path, local_path))


def test_upload_segment_uses_segments_bucket_and_device_prefixed_path():
    client = FakeStorageClient()
    local_path = Path("/queue/20260704_120000.mp4")

    object_path = upload_segment(client, "device-abc", local_path)

    assert object_path == "device-abc/20260704_120000.mp4"
    assert client.calls == [("segments", "device-abc/20260704_120000.mp4", local_path)]


def test_upload_segment_also_uploads_flag_marker_files():
    client = FakeStorageClient()
    local_path = Path("/queue/FLAG_120300.marker")

    object_path = upload_segment(client, "device-abc", local_path)

    assert object_path == "device-abc/FLAG_120300.marker"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_uploader.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.uploader'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/uploader.py`:
```python
from pathlib import Path
from typing import Protocol


class StorageClient(Protocol):
    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        ...


def upload_segment(client: StorageClient, device_id: str, local_path: Path) -> str:
    object_path = f"{device_id}/{local_path.name}"
    client.upload("segments", object_path, local_path)
    return object_path
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_uploader.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/uploader.py firmware/tests/test_uploader.py
git commit -m "feat: add supabase segment uploader"
```

---

### Task 7: Flag button marker writer

**Files:**
- Create: `firmware/visio_recorder/flag_button.py`
- Test: `firmware/tests/test_flag_button.py`

**Interfaces:**
- Produces: `write_flag_marker(queue_dir: Path, pressed_at: datetime) -> Path`, writing an empty `FLAG_HHMMSS.marker` file and returning its path.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_flag_button.py`:
```python
from datetime import datetime

from visio_recorder.flag_button import write_flag_marker


def test_write_flag_marker_creates_timestamped_file(tmp_path):
    queue_dir = tmp_path / "queue"
    pressed_at = datetime(2026, 7, 4, 12, 3, 45)

    marker_path = write_flag_marker(queue_dir, pressed_at)

    assert marker_path == queue_dir / "FLAG_120345.marker"
    assert marker_path.exists()


def test_write_flag_marker_creates_queue_dir_if_missing(tmp_path):
    queue_dir = tmp_path / "does" / "not" / "exist"
    write_flag_marker(queue_dir, datetime(2026, 7, 4, 8, 0, 0))
    assert queue_dir.exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_flag_button.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.flag_button'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/flag_button.py`:
```python
from datetime import datetime
from pathlib import Path


def write_flag_marker(queue_dir: Path, pressed_at: datetime) -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    marker_path = queue_dir / f"FLAG_{pressed_at.strftime('%H%M%S')}.marker"
    marker_path.touch()
    return marker_path
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_flag_button.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/flag_button.py firmware/tests/test_flag_button.py
git commit -m "feat: add manual flag marker writer"
```

---

### Task 8: WiFi and auth onboarding

**Files:**
- Create: `firmware/visio_recorder/wifi_onboard.py`
- Test: `firmware/tests/test_wifi_onboard.py`

**Interfaces:**
- Produces: `QrDecodeError` exception, `OnboardingPayload` dataclass (`ssid: str`, `password: str`, `user_access_token: str`, `user_refresh_token: str`), `parse_onboarding_qr_payload(payload: str) -> OnboardingPayload`, `render_wpa_supplicant(ssid: str, password: str) -> str`, `write_wpa_supplicant(path: Path, ssid: str, password: str) -> None`, `write_session_credentials(path: Path, access_token: str, refresh_token: str) -> None`.

The QR payload the mobile app displays during pairing carries the user's own (already-authenticated) Supabase session alongside the WiFi credentials, per this plan's Global Constraints note on device auth. `write_session_credentials` persists that session so the daemon can authenticate its Supabase client on every subsequent boot without re-pairing.

Hardening applied after the initial implementation (see review commits `ea25bae`, `c812901`): `QrDecodeError` messages never embed the raw payload (only whether JSON parsing failed, or which field was missing/non-string, plus payload length) since the payload contains the WiFi password and Supabase tokens and would otherwise leak into journald; `render_wpa_supplicant` rejects `ssid`/`password` containing a quote, newline, carriage return, or backslash instead of interpolating them unescaped into `wpa_supplicant.conf`, and rejects passwords outside the 8-63 character range required for a WPA-PSK passphrase; `write_wpa_supplicant` and `write_session_credentials` create their target files atomically at `0o600` via `os.open`, and tighten permissions via `fchmod` even when the target file already existed with looser permissions.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_wifi_onboard.py`:
```python
import json
import stat

import pytest

from visio_recorder.wifi_onboard import (
    QrDecodeError,
    parse_onboarding_qr_payload,
    render_wpa_supplicant,
    write_session_credentials,
    write_wpa_supplicant,
)

VALID_PAYLOAD = json.dumps(
    {
        "ssid": "HomeNet",
        "password": "hunter2",
        "user_access_token": "access-abc",
        "user_refresh_token": "refresh-xyz",
    }
)


def test_parse_valid_qr_payload():
    result = parse_onboarding_qr_payload(VALID_PAYLOAD)
    assert result.ssid == "HomeNet"
    assert result.password == "hunter2"
    assert result.user_access_token == "access-abc"
    assert result.user_refresh_token == "refresh-xyz"


def test_parse_invalid_qr_payload_raises():
    bad_payload = "not json but contains hunter2 and refresh-xyz"
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(bad_payload)
    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "refresh-xyz" not in message
    assert "not valid JSON" in message


def test_parse_qr_payload_missing_fields_raises():
    payload = json.dumps(
        {
            "ssid": "HomeNet",
            "password": "hunter2",
            "user_refresh_token": "refresh-xyz",
        }
    )
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(payload)
    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "refresh-xyz" not in message
    assert "missing required field" in message
    assert "user_access_token" in message


@pytest.mark.parametrize("field", ["ssid", "password", "user_access_token", "user_refresh_token"])
def test_parse_qr_payload_non_string_field_raises(field):
    data = json.loads(VALID_PAYLOAD)
    data[field] = 123
    with pytest.raises(QrDecodeError) as exc_info:
        parse_onboarding_qr_payload(json.dumps(data))
    message = str(exc_info.value)
    assert "hunter2" not in message
    assert "refresh-xyz" not in message
    assert field in message


def test_render_wpa_supplicant_includes_ssid_and_password():
    content = render_wpa_supplicant("HomeNet", "hunter2pass")
    assert 'ssid="HomeNet"' in content
    assert 'psk="hunter2pass"' in content
    assert "network={" in content


def test_write_wpa_supplicant_writes_file(tmp_path):
    path = tmp_path / "wpa_supplicant.conf"
    write_wpa_supplicant(path, "HomeNet", "hunter2pass")
    assert 'ssid="HomeNet"' in path.read_text()


def test_write_wpa_supplicant_sets_restrictive_permissions(tmp_path):
    path = tmp_path / "wpa_supplicant.conf"
    write_wpa_supplicant(path, "HomeNet", "hunter2pass")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_session_credentials_writes_json(tmp_path):
    path = tmp_path / "session.json"
    write_session_credentials(path, "access-abc", "refresh-xyz")
    saved = json.loads(path.read_text())
    assert saved == {"access_token": "access-abc", "refresh_token": "refresh-xyz"}


def test_write_session_credentials_sets_restrictive_permissions(tmp_path):
    path = tmp_path / "session.json"
    write_session_credentials(path, "access-abc", "refresh-xyz")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("writer", ["wpa_supplicant", "session_credentials"])
def test_writers_tighten_permissions_on_pre_existing_loose_file(tmp_path, writer):
    path = tmp_path / "target"
    path.touch(mode=0o644)
    path.chmod(0o644)
    if writer == "wpa_supplicant":
        write_wpa_supplicant(path, "HomeNet", "hunter2pass")
    else:
        write_session_credentials(path, "access-abc", "refresh-xyz")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "ssid,password",
    [
        ('Home"Net', "hunter2pass"),
        ("HomeNet", 'hunter"2pass'),
        ("Home\nNet", "hunter2pass"),
        ("HomeNet", "hunter\r2pass"),
        ("Home\nNet", "hunter\r2pass"),
        ("Home\\Net", "hunter2pass"),
        ("HomeNet", "hunter\\2pass"),
        ("HomeNet", "hunter2pass\\"),
    ],
)
def test_render_wpa_supplicant_rejects_unsafe_characters(ssid, password):
    with pytest.raises(ValueError):
        render_wpa_supplicant(ssid, password)


def test_write_wpa_supplicant_rejects_unsafe_characters(tmp_path):
    path = tmp_path / "wpa_supplicant.conf"
    with pytest.raises(ValueError):
        write_wpa_supplicant(path, 'Home"Net', "hunter2pass")


@pytest.mark.parametrize("password", ["", "short77", "a" * 64])
def test_render_wpa_supplicant_rejects_out_of_range_psk_length(password):
    with pytest.raises(ValueError) as exc_info:
        render_wpa_supplicant("HomeNet", password)
    message = str(exc_info.value)
    assert "8-63" in message
    if password:
        assert password not in message


@pytest.mark.parametrize("password", ["a" * 8, "a" * 63])
def test_render_wpa_supplicant_accepts_boundary_psk_lengths(password):
    content = render_wpa_supplicant("HomeNet", password)
    assert f'psk="{password}"' in content


def test_write_wpa_supplicant_rejects_out_of_range_psk_length(tmp_path):
    path = tmp_path / "wpa_supplicant.conf"
    with pytest.raises(ValueError):
        write_wpa_supplicant(path, "HomeNet", "short77")
    assert not path.exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_wifi_onboard.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.wifi_onboard'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/wifi_onboard.py`:
```python
import json
import os
from dataclasses import dataclass
from pathlib import Path

_UNSAFE_CHARS = ('"', "\n", "\r", "\\")
_PSK_MIN_LENGTH = 8
_PSK_MAX_LENGTH = 63


class QrDecodeError(ValueError):
    pass


@dataclass
class OnboardingPayload:
    ssid: str
    password: str
    user_access_token: str
    user_refresh_token: str


def parse_onboarding_qr_payload(payload: str) -> OnboardingPayload:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise QrDecodeError(
            f"invalid onboarding QR payload: not valid JSON (length={len(payload)})"
        ) from exc

    fields = {}
    for field in ("ssid", "password", "user_access_token", "user_refresh_token"):
        try:
            value = data[field]
        except (KeyError, TypeError) as exc:
            raise QrDecodeError(
                f"invalid onboarding QR payload: missing required field {field!r} "
                f"(length={len(payload)})"
            ) from exc
        if not isinstance(value, str):
            raise QrDecodeError(
                f"invalid onboarding QR payload: field {field!r} is not a string "
                f"(length={len(payload)})"
            )
        fields[field] = value
    return OnboardingPayload(**fields)


def render_wpa_supplicant(ssid: str, password: str) -> str:
    for name, value in (("ssid", ssid), ("password", password)):
        if any(ch in value for ch in _UNSAFE_CHARS):
            raise ValueError(
                f"{name} contains an unsafe character (quote, newline, or backslash); "
                "rejecting rather than attempting to escape it for wpa_supplicant.conf"
            )
    if not _PSK_MIN_LENGTH <= len(password) <= _PSK_MAX_LENGTH:
        raise ValueError(
            f"password length {len(password)} is outside the 8-63 character range "
            "required for a WPA-PSK passphrase; wpa_supplicant would reject the config"
        )
    return (
        "country=US\n"
        "ctrl_interface=DIR=/var/run/wpa_supplicant GROUP=netdev\n"
        "update_config=1\n\n"
        "network={\n"
        f'    ssid="{ssid}"\n'
        f'    psk="{password}"\n'
        "}\n"
    )


def _write_private_text(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        # The mode passed to os.open only applies when the file is newly
        # created; a pre-existing file (e.g. the stock wpa_supplicant.conf
        # shipped at 0o644) keeps its old permissions, so tighten via the fd.
        os.fchmod(f.fileno(), 0o600)
        f.write(content)


def write_wpa_supplicant(path: Path, ssid: str, password: str) -> None:
    _write_private_text(path, render_wpa_supplicant(ssid, password))


def write_session_credentials(path: Path, access_token: str, refresh_token: str) -> None:
    _write_private_text(
        path, json.dumps({"access_token": access_token, "refresh_token": refresh_token})
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_wifi_onboard.py -v`
Expected: PASS (29 passed).

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/wifi_onboard.py firmware/tests/test_wifi_onboard.py
git commit -m "feat: add wifi and auth onboarding via qr code"
```

---

### Task 9: Device identity and registration

**Files:**
- Create: `firmware/visio_recorder/device_identity.py`
- Test: `firmware/tests/test_device_identity.py`

**Interfaces:**
- Produces: `load_or_create_device_id(state_path: Path) -> str` (returns the persisted UUID if `state_path` exists, otherwise generates a new UUID4, persists it, and returns it), `DeviceRegistrationClient` Protocol (`upsert_device(device_id: str, name: str) -> None`), `register_device(client: DeviceRegistrationClient, device_id: str, name: str) -> None`.

Hardening applied after the initial implementation (see review commit `1a5cd23`): the write is atomic - the new UUID is written to a `.tmp` sibling file, `fsync`'d, then moved into place with `os.replace`, so a power loss mid-write (common on a headless Pi that loses power without a clean shutdown) can never leave `state_path` truncated or partially written. A pre-existing state file that fails to parse (empty, truncated JSON, or missing the `device_id` key) is treated the same as a missing file - a fresh UUID is generated and persisted - rather than raising, since a corrupt identity file should self-heal rather than crash the daemon on every boot.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_device_identity.py`:
```python
import json
import uuid

import pytest

from visio_recorder.device_identity import (
    DeviceRegistrationClient,
    load_or_create_device_id,
    register_device,
)


def test_load_or_create_device_id_generates_a_valid_uuid_when_missing(tmp_path):
    state_path = tmp_path / "device_id.json"

    device_id = load_or_create_device_id(state_path)

    assert uuid.UUID(device_id)
    assert state_path.exists()


def test_load_or_create_device_id_returns_the_same_id_on_subsequent_calls(tmp_path):
    state_path = tmp_path / "device_id.json"

    first = load_or_create_device_id(state_path)
    second = load_or_create_device_id(state_path)

    assert first == second


def test_load_or_create_device_id_reads_a_pre_existing_file(tmp_path):
    state_path = tmp_path / "device_id.json"
    state_path.write_text(json.dumps({"device_id": "11111111-1111-1111-1111-111111111111"}))

    assert load_or_create_device_id(state_path) == "11111111-1111-1111-1111-111111111111"


@pytest.mark.parametrize("corrupt_content", ["", '{"device_id', '{"other": 1}'])
def test_load_or_create_device_id_recovers_from_a_corrupt_file(tmp_path, corrupt_content):
    state_path = tmp_path / "device_id.json"
    state_path.write_text(corrupt_content)

    device_id = load_or_create_device_id(state_path)

    assert uuid.UUID(device_id)
    assert json.loads(state_path.read_text()) == {"device_id": device_id}


def test_load_or_create_device_id_leaves_no_temp_files(tmp_path):
    state_path = tmp_path / "device_id.json"

    load_or_create_device_id(state_path)

    assert [p.name for p in tmp_path.iterdir()] == ["device_id.json"]


class FakeDeviceRegistrationClient(DeviceRegistrationClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def upsert_device(self, device_id: str, name: str) -> None:
        self.calls.append((device_id, name))


def test_register_device_calls_client_with_id_and_name():
    client = FakeDeviceRegistrationClient()

    register_device(client, "11111111-1111-1111-1111-111111111111", "visio-pendant")

    assert client.calls == [("11111111-1111-1111-1111-111111111111", "visio-pendant")]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_device_identity.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.device_identity'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/device_identity.py`:
```python
import json
import os
import uuid
from pathlib import Path
from typing import Optional, Protocol


def _read_stored_device_id(state_path: Path) -> Optional[str]:
    try:
        device_id = json.loads(state_path.read_text())["device_id"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return device_id if isinstance(device_id, str) else None


def load_or_create_device_id(state_path: Path) -> str:
    if state_path.exists():
        stored = _read_stored_device_id(state_path)
        if stored is not None:
            return stored

    device_id = str(uuid.uuid4())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"device_id": device_id}))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, state_path)
    return device_id


class DeviceRegistrationClient(Protocol):
    def upsert_device(self, device_id: str, name: str) -> None:
        ...


def register_device(client: DeviceRegistrationClient, device_id: str, name: str) -> None:
    client.upsert_device(device_id, name)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_device_identity.py -v`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/device_identity.py firmware/tests/test_device_identity.py
git commit -m "feat: add device identity and registration"
```

---

### Task 10: Startup sequence orchestration

**Files:**
- Create: `firmware/visio_recorder/daemon.py`
- Test: `firmware/tests/test_daemon.py`

**Interfaces:**
- Consumes: `BatteryReader`, `read_battery_status` from Task 2; `LedDriver`, `LedPattern`, `LedState`, `apply_led_state` from Task 3.
- Produces: `StartupResult` dataclass (`proceed: bool`, `battery_pct: int`), `run_startup_sequence(battery_reader: BatteryReader, led_driver: LedDriver) -> StartupResult`.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_daemon.py`:
```python
from visio_recorder.daemon import run_startup_sequence
from visio_recorder.led import LedDriver, LedPattern


class FakeBatteryReader:
    def __init__(self, pct: int) -> None:
        self._pct = pct

    def get_charge_pct(self) -> int:
        return self._pct


class FakeLedDriver(LedDriver):
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[int, int, int], LedPattern]] = []

    def set(self, color, pattern):
        self.calls.append((color, pattern))


def test_healthy_battery_proceeds_and_shows_recording_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(85), led)

    assert result.proceed is True
    assert result.battery_pct == 85
    assert led.calls == [((0, 255, 0), LedPattern.SOLID)]


def test_low_battery_proceeds_and_shows_low_battery_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(15), led)

    assert result.proceed is True
    assert result.battery_pct == 15
    assert led.calls == [((255, 255, 0), LedPattern.PULSING)]


def test_battery_at_warn_threshold_shows_recording_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(20), led)

    assert result.proceed is True
    assert result.battery_pct == 20
    assert led.calls == [((0, 255, 0), LedPattern.SOLID)]


def test_critical_battery_halts_and_shows_critical_led():
    led = FakeLedDriver()
    result = run_startup_sequence(FakeBatteryReader(5), led)

    assert result.proceed is False
    assert result.battery_pct == 5
    assert led.calls == [((255, 0, 0), LedPattern.FLASHING)]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_daemon.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.daemon'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/daemon.py`:
```python
from dataclasses import dataclass

from visio_recorder.battery import BatteryReader, read_battery_status
from visio_recorder.led import LedDriver, LedState, apply_led_state


@dataclass
class StartupResult:
    proceed: bool
    battery_pct: int


def run_startup_sequence(battery_reader: BatteryReader, led_driver: LedDriver) -> StartupResult:
    status = read_battery_status(battery_reader)
    if status.should_halt:
        apply_led_state(led_driver, LedState.CRITICAL)
        return StartupResult(proceed=False, battery_pct=status.pct)
    if status.is_low:
        apply_led_state(led_driver, LedState.LOW_BATTERY)
    else:
        apply_led_state(led_driver, LedState.RECORDING)
    return StartupResult(proceed=True, battery_pct=status.pct)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_daemon.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/daemon.py firmware/tests/test_daemon.py
git commit -m "feat: add startup sequence orchestration"
```

---

### Task 11: Recording loop tick and status reporting

**Files:**
- Create: `firmware/visio_recorder/recording_loop.py`
- Test: `firmware/tests/test_recording_loop.py`

**Interfaces:**
- Consumes: `BatteryReader`, `read_battery_status` from Task 2; `LedDriver`, `LedState`, `apply_led_state` from Task 3; `CommandRunner`, `mux_segment` from Task 4; `enqueue`, `list_pending`, `mark_uploaded` from Task 5; `StorageClient`, `upload_segment` from Task 6.
- Produces: `DeviceStatus` dataclass (`device_id: str`, `battery_pct: int`, `storage_used_gb: float`, `storage_free_gb: float`, `segments_pending: int`, `segments_uploaded_today: int`, `recording_active: bool`), `StatusClient` Protocol (`upsert_device_status(status: dict) -> None`), `next_led_state(battery_is_low: bool, is_uploading: bool) -> LedState` (battery takes priority: low battery always wins over the uploading indicator), `on_segment_complete(h264_path: Path, queue_dir: Path, command_runner: CommandRunner, storage_client: StorageClient, status_client: StatusClient, led_driver: LedDriver, battery_reader: BatteryReader, device_id: str, segments_uploaded_today: int, framerate: int) -> int` (returns the new `segments_uploaded_today` count) - this is what runs once per completed 5-minute segment, per the spec's Recording Loop section. `framerate` is forwarded to `mux_segment` (Task 4), and the raw `.h264` is deleted once the mux succeeds so raw capture footage doesn't accumulate on the SD card alongside the muxed `.mp4`.

- [ ] **Step 1: Write the failing tests**

`firmware/tests/test_recording_loop.py`:
```python
from pathlib import Path

from visio_recorder.led import LedDriver, LedPattern, LedState
from visio_recorder.muxer import CommandRunner
from visio_recorder.recording_loop import next_led_state, on_segment_complete
from visio_recorder.uploader import StorageClient


class FakeBatteryReader:
    def __init__(self, pct: int) -> None:
        self._pct = pct

    def get_charge_pct(self) -> int:
        return self._pct


class FakeLedDriver(LedDriver):
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[int, int, int], LedPattern]] = []

    def set(self, color, pattern):
        self.calls.append((color, pattern))


class FakeCommandRunner(CommandRunner):
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> None:
        self.calls.append(args)
        # For mux_segment, create the mp4 file from the h264 input
        if "ffmpeg" in args:
            # args format: ["ffmpeg", "-y", "-framerate", fps, "-i", input_path, "-c", "copy", output_path]
            input_idx = args.index("-i") + 1
            output_idx = args.index("copy") + 1
            input_path = Path(args[input_idx])
            output_path = Path(args[output_idx])
            # Copy the h264 file as mp4
            if input_path.exists():
                output_path.write_bytes(input_path.read_bytes())


class FakeStorageClient(StorageClient):
    def __init__(self) -> None:
        self.uploaded: list[str] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        self.uploaded.append(object_path)


class FakeStatusClient:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    def upsert_device_status(self, status: dict) -> None:
        self.upserts.append(status)


def test_next_led_state_prioritizes_low_battery_over_uploading():
    assert next_led_state(battery_is_low=True, is_uploading=True) == LedState.LOW_BATTERY
    assert next_led_state(battery_is_low=True, is_uploading=False) == LedState.LOW_BATTERY


def test_next_led_state_shows_uploading_when_battery_is_healthy():
    assert next_led_state(battery_is_low=False, is_uploading=True) == LedState.UPLOADING


def test_next_led_state_shows_recording_when_idle_and_healthy():
    assert next_led_state(battery_is_low=False, is_uploading=False) == LedState.RECORDING


def test_on_segment_complete_happy_path(tmp_path):
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    led = FakeLedDriver()
    storage = FakeStorageClient()
    status_client = FakeStatusClient()
    command_runner = FakeCommandRunner()

    new_count = on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=command_runner,
        storage_client=storage,
        status_client=status_client,
        led_driver=led,
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=41,
        framerate=30,
    )

    assert new_count == 42
    framerate_idx = command_runner.calls[0].index("-framerate") + 1
    assert command_runner.calls[0][framerate_idx] == "30"
    assert storage.uploaded == ["device-abc/20260704_120000.mp4"]
    assert not h264_path.exists()
    assert led.calls == [
        ((0, 0, 255), LedPattern.PULSING),
        ((0, 255, 0), LedPattern.SOLID),
    ]
    assert status_client.upserts == [
        {
            "device_id": "device-abc",
            "battery_pct": 85,
            "storage_used_gb": 0.0,
            "storage_free_gb": 0.0,
            "segments_pending": 0,
            "segments_uploaded_today": 42,
            "recording_active": True,
        }
    ]


def test_on_segment_complete_with_low_battery_uses_low_battery_led(tmp_path):
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    led = FakeLedDriver()

    on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=FakeCommandRunner(),
        storage_client=FakeStorageClient(),
        status_client=FakeStatusClient(),
        led_driver=led,
        battery_reader=FakeBatteryReader(15),
        device_id="device-abc",
        segments_uploaded_today=0,
        framerate=30,
    )

    assert led.calls == [
        ((255, 255, 0), LedPattern.PULSING),
        ((255, 255, 0), LedPattern.PULSING),
    ]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd firmware && uv run pytest tests/test_recording_loop.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'visio_recorder.recording_loop'`.

- [ ] **Step 3: Write the implementation**

`firmware/visio_recorder/recording_loop.py`:
```python
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from visio_recorder.battery import BatteryReader, read_battery_status
from visio_recorder.led import LedDriver, LedState, apply_led_state
from visio_recorder.muxer import CommandRunner, mux_segment
from visio_recorder.upload_queue import enqueue, list_pending, mark_uploaded
from visio_recorder.uploader import StorageClient, upload_segment


@dataclass
class DeviceStatus:
    device_id: str
    battery_pct: int
    storage_used_gb: float
    storage_free_gb: float
    segments_pending: int
    segments_uploaded_today: int
    recording_active: bool


class StatusClient(Protocol):
    def upsert_device_status(self, status: dict) -> None:
        ...


def next_led_state(battery_is_low: bool, is_uploading: bool) -> LedState:
    if battery_is_low:
        return LedState.LOW_BATTERY
    if is_uploading:
        return LedState.UPLOADING
    return LedState.RECORDING


def on_segment_complete(
    h264_path: Path,
    queue_dir: Path,
    command_runner: CommandRunner,
    storage_client: StorageClient,
    status_client: StatusClient,
    led_driver: LedDriver,
    battery_reader: BatteryReader,
    device_id: str,
    segments_uploaded_today: int,
    framerate: int,
) -> int:
    mp4_path = mux_segment(command_runner, h264_path, framerate)
    h264_path.unlink()
    queued_path = enqueue(queue_dir, mp4_path)

    battery_status = read_battery_status(battery_reader)
    apply_led_state(led_driver, next_led_state(battery_status.is_low, is_uploading=True))

    upload_segment(storage_client, device_id, queued_path)
    mark_uploaded(queued_path)
    segments_uploaded_today += 1

    apply_led_state(led_driver, next_led_state(battery_status.is_low, is_uploading=False))

    # Real disk-usage stats (shutil.disk_usage) are wired in Epic 5 against
    # the actual SD card; there is no meaningful fake for a syscall here.
    status = DeviceStatus(
        device_id=device_id,
        battery_pct=battery_status.pct,
        storage_used_gb=0.0,
        storage_free_gb=0.0,
        segments_pending=len(list_pending(queue_dir)),
        segments_uploaded_today=segments_uploaded_today,
        recording_active=True,
    )
    status_client.upsert_device_status(asdict(status))

    return segments_uploaded_today
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd firmware && uv run pytest tests/test_recording_loop.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the full firmware test suite**

Run: `cd firmware && uv run pytest -v`
Expected: all tests across Tasks 2-11 pass (64 passed).

- [ ] **Step 6: Commit**

```bash
git add firmware/visio_recorder/recording_loop.py firmware/tests/test_recording_loop.py
git commit -m "feat: add recording loop tick and status reporting"
```

---

### Task 12: systemd unit

**Files:**
- Create: `firmware/systemd/visio-recorder.service`

**Interfaces:**
- Produces: an installable systemd unit that starts `visio_recorder.daemon` on boot. Not unit tested (no branching logic); verified with `systemd-analyze verify`.

- [ ] **Step 1: Write the unit file**

`firmware/systemd/visio-recorder.service`:
```ini
[Unit]
Description=Visio Pendant Recording Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m visio_recorder.daemon
WorkingDirectory=/opt/visio-recorder
Restart=on-failure
RestartSec=5
User=pi

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Verify the unit file syntax**

Run: `systemd-analyze verify firmware/systemd/visio-recorder.service`
Expected: no output (clean unit) - warnings about the referenced `ExecStart` binary not existing on the dev machine are expected and fine; this is only validated for real on the Pi during Epic 4/5 integration.

- [ ] **Step 3: Commit**

```bash
git add firmware/systemd/visio-recorder.service
git commit -m "feat: add visio-recorder systemd unit"
```

---

## Handoff

`daemon.py` currently only implements the startup battery/LED check from Task 10. The remaining wiring for Epic 5 integration testing - composing already-tested units, not adding new decision logic - is:

- **Onboarding sequence:** decode the QR payload (Task 8), write `wpa_supplicant.conf` and the session credentials file, then on every boot load the session file and call the real Supabase client's `auth.set_session(access_token, refresh_token)` before constructing the `StorageClient`/`StatusClient`/`DeviceRegistrationClient` wrappers used elsewhere - this is what makes writes pass RLS.
- **First-boot registration:** call `load_or_create_device_id` (Task 9); if the state file didn't already exist, also call `register_device`.
- **Startup queue flush:** per the spec's Recording Daemon startup sequence ("flush any pending upload queue before recording starts"), call `list_pending` (Task 5) and drive each pending item through the upload half of `on_segment_complete` (Task 11) before starting a new `rpicam-vid` capture.
- **Recording loop:** supervise `rpicam-vid` as a subprocess, and call `on_segment_complete` (Task 11) once per completed 5-minute segment, on a background thread per the spec.
- **Real disk-usage stats:** replace the `0.0` placeholders in `on_segment_complete`'s `DeviceStatus` with real `shutil.disk_usage()` readings against the SD card mount.
- **Upload error handling:** wrap the upload half of `on_segment_complete` in try/except.
  On failure restore the LED via `next_led_state(..., is_uploading=False)` and leave the queued file in place - the startup queue flush is the retry mechanism.
  Define when repeated failures escalate to the CRITICAL LED.
- **Bookworm networking:** stock Raspberry Pi OS Bookworm uses NetworkManager, which does not honor `/etc/wpa_supplicant/wpa_supplicant.conf`.
  Epic 5 wiring may need nmcli/NM keyfiles instead of `write_wpa_supplicant`'s output path.
- **FLAG marker naming:** resolved (issue #9) - `write_flag_marker` now writes dated `FLAG_YYYYMMDD_HHMMSS.marker` names matching the pipeline's `parse_flag_marker_filename` contract (Task 11 there), so same-second presses on different days no longer collide at the storage object path.

None of this has branching logic beyond what Tasks 8-11 already test in isolation - it is glue, validated on real hardware in Epic 5 rather than through additional unit tests.
