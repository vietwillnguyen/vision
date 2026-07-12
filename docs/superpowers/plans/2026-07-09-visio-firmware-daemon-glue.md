# Visio Firmware Epic 5 Daemon Glue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the already-tested firmware units into a running `visio-recorder` daemon so the systemd unit records, uploads, and reports for real (closes issue #7).

**Architecture:** Every hardware and network boundary stays a `Protocol` with a fake in tests, exactly like the existing modules.
New glue lives in three new modules (`capture.py`, `onboarding.py`, `supabase_clients.py`) plus additions to `recording_loop.py` and `daemon.py`.
`rpicam-vid` is invoked once per 5-minute segment so the daemon controls the `YYYYMMDD_HHMMSS` filename at capture start and supervises the process by construction; uploads run on a single background worker thread so the next capture starts on time.

**Tech Stack:** Python 3.11, pytest, `uv` (locked), `supabase-py`, `rpicam-vid`/`rpicam-still`/`ffmpeg`/`zbarimg` CLI tools, systemd, NetworkManager keyfiles.

## Global Constraints

- Segments are 5-minute rolling H.264 files named `YYYYMMDD_HHMMSS.h264`, muxed to MP4 with `ffmpeg -c copy` (existing `mux_segment`).
- The pipeline's `parse_segment_filename` hard-requires MP4 names matching `strptime("%Y%m%d_%H%M%S")`; the pipeline's `parse_flag_marker_filename` hard-requires `FLAG_%Y%m%d_%H%M%S.marker` (issue #9, PR #13).
- Battery halt threshold `< 10%`, low-warning threshold `< 20%` (existing `battery.py` constants).
- LED states per spec: Recording solid green, Uploading pulsing blue, Low battery pulsing yellow, Critical red flash; low battery outranks uploading (existing `next_led_state`).
- On successful upload the local file is deleted (existing `mark_uploaded`); on failed upload the queued file stays and the startup flush is the retry mechanism.
- All tests run with `uv run --locked --extra dev pytest` from `firmware/`; never call real hardware, network, or `subprocess` in tests.
- Prose style for docs: plain hyphens, one sentence per line in new markdown.

**Decision (needs user sign-off, deviates from spec text):** WiFi credentials are written as a NetworkManager keyfile (`/etc/NetworkManager/system-connections/visio.nmconnection`, mode 0600), not `wpa_supplicant.conf`, because stock Raspberry Pi OS Bookworm runs NetworkManager and does not honor `/etc/wpa_supplicant/wpa_supplicant.conf` (recorded in the Epic 1 plan Handoff).
The existing `write_wpa_supplicant` stays for non-NM images, but the daemon wires the NM path.
Task 10 updates the spec and Epic 5 checklist wording accordingly.

**Decision:** upload failures escalate to the CRITICAL LED after 3 consecutive failures (`UPLOAD_FAILURES_TO_CRITICAL = 3`); recording continues either way, and any successful upload resets the counter.

---

### Task 1: Segment filename contract

**Files:**
- Create: `firmware/visio_recorder/capture.py`
- Test: `firmware/tests/test_capture.py`

**Interfaces:**
- Produces: `segment_filename(started_at: datetime) -> str` returning `"YYYYMMDD_HHMMSS.h264"`.

- [ ] **Step 1: Write the failing tests**

```python
# firmware/tests/test_capture.py
from datetime import datetime

from visio_recorder.capture import segment_filename


def test_segment_filename_uses_recording_start_time():
    assert segment_filename(datetime(2026, 7, 9, 8, 5, 30)) == "20260709_080530.h264"


def test_segment_filename_stem_matches_the_pipeline_parser_contract():
    # After muxing, the .mp4 keeps this stem; the pipeline's
    # parse_segment_filename hard-requires strptime("%Y%m%d_%H%M%S").
    started_at = datetime(2026, 7, 9, 8, 5, 30)
    stem = segment_filename(started_at).removesuffix(".h264")
    assert datetime.strptime(stem, "%Y%m%d_%H%M%S") == started_at
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_capture.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'visio_recorder.capture'`

- [ ] **Step 3: Write minimal implementation**

```python
# firmware/visio_recorder/capture.py
from datetime import datetime


def segment_filename(started_at: datetime) -> str:
    return f"{started_at.strftime('%Y%m%d_%H%M%S')}.h264"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_capture.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/capture.py firmware/tests/test_capture.py
git commit -m "feat(firmware): segment filename contract pinned to pipeline parser"
```

### Task 2: rpicam-vid single-segment capture

**Files:**
- Modify: `firmware/visio_recorder/capture.py`
- Test: `firmware/tests/test_capture.py`

**Interfaces:**
- Consumes: `CommandRunner` Protocol from `visio_recorder.muxer` (`run(args: list[str]) -> None`).
- Produces: `build_rpicam_command(output_path: Path, duration_ms: int, framerate: int) -> list[str]` and `record_segment(runner: CommandRunner, output_dir: Path, started_at: datetime, duration_ms: int, framerate: int) -> Path`.

- [ ] **Step 1: Write the failing tests**

```python
# append to firmware/tests/test_capture.py
from pathlib import Path

from visio_recorder.capture import build_rpicam_command, record_segment


class FakeRunner:
    def __init__(self):
        self.calls: list[list[str]] = []

    def run(self, args: list[str]) -> None:
        self.calls.append(args)


def test_build_rpicam_command_records_h264_for_the_given_duration():
    cmd = build_rpicam_command(Path("/data/20260709_080530.h264"), 300000, 30)
    assert cmd == [
        "rpicam-vid",
        "-t", "300000",
        "--framerate", "30",
        "--codec", "h264",
        "-n",
        "-o", "/data/20260709_080530.h264",
    ]


def test_record_segment_runs_rpicam_and_returns_the_timestamped_path(tmp_path):
    runner = FakeRunner()
    started_at = datetime(2026, 7, 9, 8, 5, 30)

    path = record_segment(runner, tmp_path, started_at, 300000, 30)

    assert path == tmp_path / "20260709_080530.h264"
    assert runner.calls == [build_rpicam_command(path, 300000, 30)]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_capture.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_rpicam_command'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to firmware/visio_recorder/capture.py
from pathlib import Path

from visio_recorder.muxer import CommandRunner


def build_rpicam_command(
    output_path: Path, duration_ms: int, framerate: int
) -> list[str]:
    return [
        "rpicam-vid",
        "-t", str(duration_ms),
        "--framerate", str(framerate),
        "--codec", "h264",
        "-n",
        "-o", str(output_path),
    ]


def record_segment(
    runner: CommandRunner,
    output_dir: Path,
    started_at: "datetime",
    duration_ms: int,
    framerate: int,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / segment_filename(started_at)
    runner.run(build_rpicam_command(output_path, duration_ms, framerate))
    return output_path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_capture.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/capture.py firmware/tests/test_capture.py
git commit -m "feat(firmware): rpicam-vid per-segment capture via CommandRunner"
```

### Task 3: Real disk stats replace the 0.0 placeholders

**Files:**
- Modify: `firmware/visio_recorder/recording_loop.py`
- Test: `firmware/tests/test_recording_loop.py`

**Interfaces:**
- Produces: `DiskStats` dataclass (`used_gb: float`, `free_gb: float`), `DiskStatsReader` Protocol (`usage(path: Path) -> DiskStats`), `ShutilDiskStatsReader` real implementation, and two new `on_segment_complete` parameters: `disk_stats_reader: DiskStatsReader, data_dir: Path`.
- Consumes: existing `on_segment_complete` signature and its tests' fakes.

- [ ] **Step 1: Write the failing tests**

Add a `FakeDiskStatsReader` to `firmware/tests/test_recording_loop.py` and assert real values flow into the status upsert.

```python
# append to firmware/tests/test_recording_loop.py
from visio_recorder.recording_loop import DiskStats, ShutilDiskStatsReader


class FakeDiskStatsReader:
    def __init__(self, stats: DiskStats):
        self._stats = stats

    def usage(self, path):
        return self._stats


def test_on_segment_complete_reports_real_disk_stats(tmp_path, monkeypatch):
    # Reuse the existing arrange helpers/fakes in this file for the other
    # collaborators; pass disk_stats_reader=FakeDiskStatsReader(
    #     DiskStats(used_gb=3.5, free_gb=25.1)) and data_dir=tmp_path.
    # Assert the upserted status dict has storage_used_gb == 3.5 and
    # storage_free_gb == 25.1 instead of 0.0.
    ...


def test_shutil_disk_stats_reader_converts_bytes_to_gb(tmp_path):
    stats = ShutilDiskStatsReader().usage(tmp_path)
    assert stats.used_gb >= 0.0
    assert stats.free_gb > 0.0
```

The first test's body must be written out fully by adapting this file's existing `on_segment_complete` test arrangement (same fakes, two new arguments); it is elided here only because it reuses that file's local helpers verbatim.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_recording_loop.py -v`
Expected: FAIL with `ImportError: cannot import name 'DiskStats'`

- [ ] **Step 3: Write minimal implementation**

```python
# in firmware/visio_recorder/recording_loop.py
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_BYTES_PER_GB = 1024**3


@dataclass
class DiskStats:
    used_gb: float
    free_gb: float


class DiskStatsReader(Protocol):
    def usage(self, path: Path) -> DiskStats: ...


class ShutilDiskStatsReader:
    def usage(self, path: Path) -> DiskStats:
        usage = shutil.disk_usage(path)
        return DiskStats(
            used_gb=usage.used / _BYTES_PER_GB,
            free_gb=usage.free / _BYTES_PER_GB,
        )
```

`on_segment_complete` gains `disk_stats_reader: DiskStatsReader` and `data_dir: Path` parameters, replaces the `0.0` placeholders with `stats = disk_stats_reader.usage(data_dir)` then `storage_used_gb=stats.used_gb, storage_free_gb=stats.free_gb`, and drops the placeholder comment.
Update every existing call in `firmware/tests/test_recording_loop.py` to pass the two new arguments.

- [ ] **Step 4: Run the full firmware suite**

Run: `cd firmware && uv run --locked --extra dev pytest -v`
Expected: all pass (existing `on_segment_complete` tests updated, no regressions)

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/recording_loop.py firmware/tests/test_recording_loop.py
git commit -m "feat(firmware): real disk usage stats in device status reporting"
```

### Task 4: Upload error handling with LED recovery

**Files:**
- Modify: `firmware/visio_recorder/recording_loop.py`
- Test: `firmware/tests/test_recording_loop.py`

**Interfaces:**
- Produces: `SegmentResult` dataclass (`segments_uploaded_today: int`, `upload_ok: bool`); `on_segment_complete` now returns `SegmentResult` instead of `int`; module constant `UPLOAD_FAILURES_TO_CRITICAL = 3` (consumed by Task 8's loop).

- [ ] **Step 1: Write the failing tests**

```python
# append to firmware/tests/test_recording_loop.py
def test_failed_upload_restores_led_keeps_queued_file_and_reports_failure(tmp_path):
    # Arrange with this file's existing fakes, but a storage client whose
    # upload() raises RuntimeError("network down").
    # Assert on the returned SegmentResult and side effects:
    result = on_segment_complete(...)  # full arrangement as in existing tests
    assert result.upload_ok is False
    assert result.segments_uploaded_today == 0  # unchanged count
    # queued file still present in queue_dir (the retry mechanism)
    # last LED call is next_led_state(battery_is_low, is_uploading=False)
    # status upsert still ran with segments_pending counting the stuck file


def test_successful_upload_returns_ok_result(tmp_path):
    result = on_segment_complete(...)  # existing happy-path arrangement
    assert result.upload_ok is True
    assert result.segments_uploaded_today == 1
```

Write both arrangements out fully by copying the file's existing happy-path test arrangement; only the storage client fake and the assertions differ.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_recording_loop.py -v`
Expected: FAIL with `ImportError: cannot import name 'SegmentResult'` (and the raising fake would previously have propagated)

- [ ] **Step 3: Write minimal implementation**

```python
# in firmware/visio_recorder/recording_loop.py
UPLOAD_FAILURES_TO_CRITICAL = 3


@dataclass
class SegmentResult:
    segments_uploaded_today: int
    upload_ok: bool
```

Inside `on_segment_complete`, wrap only the upload half:

```python
    upload_ok = True
    try:
        upload_segment(storage_client, device_id, queued_path)
        mark_uploaded(queued_path)
        segments_uploaded_today += 1
    except Exception:
        upload_ok = False

    apply_led_state(
        led_driver, next_led_state(battery_status.is_low, is_uploading=False)
    )
```

The status upsert still runs after the except (so `segments_pending` counts the stuck file), and the function returns `SegmentResult(segments_uploaded_today=segments_uploaded_today, upload_ok=upload_ok)`.
Update all existing callers/tests from `int` return to `.segments_uploaded_today`.

- [ ] **Step 4: Run the full firmware suite**

Run: `cd firmware && uv run --locked --extra dev pytest -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/recording_loop.py firmware/tests/test_recording_loop.py
git commit -m "feat(firmware): upload failures restore LED and keep the queued file"
```

### Task 5: Startup queue flush

**Files:**
- Modify: `firmware/visio_recorder/recording_loop.py`
- Test: `firmware/tests/test_recording_loop.py`

**Interfaces:**
- Consumes: `list_pending`, `upload_segment`, `mark_uploaded` (existing).
- Produces: `flush_pending(queue_dir: Path, storage_client: StorageClient, device_id: str) -> int` returning the number of files uploaded; files whose upload raises stay queued and stop neither the flush nor the daemon.

- [ ] **Step 1: Write the failing tests**

```python
# append to firmware/tests/test_recording_loop.py
from visio_recorder.recording_loop import flush_pending


def test_flush_pending_uploads_and_clears_all_queued_files(tmp_path):
    (tmp_path / "20260708_235500.mp4").touch()
    (tmp_path / "FLAG_20260708_235700.marker").touch()
    client = FakeStorageClient()  # this file's existing recording fake

    uploaded = flush_pending(tmp_path, client, "device-abc")

    assert uploaded == 2
    assert list(tmp_path.iterdir()) == []


def test_flush_pending_leaves_failing_files_queued_and_continues(tmp_path):
    (tmp_path / "20260708_235500.mp4").touch()
    (tmp_path / "20260708_235000.mp4").touch()
    client = FakeStorageClientFailingOn("20260708_235000.mp4")

    uploaded = flush_pending(tmp_path, client, "device-abc")

    assert uploaded == 1
    assert [p.name for p in tmp_path.iterdir()] == ["20260708_235000.mp4"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_recording_loop.py -v`
Expected: FAIL with `ImportError: cannot import name 'flush_pending'`

- [ ] **Step 3: Write minimal implementation**

```python
# append to firmware/visio_recorder/recording_loop.py
def flush_pending(
    queue_dir: Path, storage_client: StorageClient, device_id: str
) -> int:
    uploaded = 0
    for path in list_pending(queue_dir):
        try:
            upload_segment(storage_client, device_id, path)
        except Exception:
            continue
        mark_uploaded(path)
        uploaded += 1
    return uploaded
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_recording_loop.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/recording_loop.py firmware/tests/test_recording_loop.py
git commit -m "feat(firmware): startup queue flush uploads pending files"
```

**As-built note (commit fb331c2):** the `except Exception: continue` above was silent - a persistently failing queued file gave no operator-visible signal.
It now logs via `_logger.exception` before continuing; the retry-by-leaving-queued behavior is unchanged.

### Task 6: NetworkManager keyfile rendering

**Files:**
- Modify: `firmware/visio_recorder/wifi_onboard.py`
- Test: `firmware/tests/test_wifi_onboard.py`

**Interfaces:**
- Produces: `render_nm_keyfile(ssid: str, password: str) -> str` and `write_nm_keyfile(path: Path, ssid: str, password: str) -> None` (0600 via the existing `_write_private_text`).
- Reuses the module's existing `_UNSAFE_CHARS` and PSK length validation, extracted into `_validate_credentials(ssid, password)` shared by both renderers.

- [ ] **Step 1: Write the failing tests**

```python
# append to firmware/tests/test_wifi_onboard.py
from visio_recorder.wifi_onboard import render_nm_keyfile, write_nm_keyfile


def test_render_nm_keyfile_produces_a_wifi_psk_connection():
    keyfile = render_nm_keyfile("HomeNet", "hunter2secret")
    assert "[connection]" in keyfile
    assert "type=wifi" in keyfile
    assert "ssid=HomeNet" in keyfile
    assert "key-mgmt=wpa-psk" in keyfile
    assert "psk=hunter2secret" in keyfile


def test_render_nm_keyfile_rejects_unsafe_characters():
    with pytest.raises(ValueError):
        render_nm_keyfile('Home"Net', "hunter2secret")


def test_render_nm_keyfile_rejects_out_of_range_psk_length():
    with pytest.raises(ValueError):
        render_nm_keyfile("HomeNet", "short")


def test_write_nm_keyfile_is_owner_read_write_only(tmp_path):
    path = tmp_path / "visio.nmconnection"
    write_nm_keyfile(path, "HomeNet", "hunter2secret")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_wifi_onboard.py -v`
Expected: FAIL with `ImportError: cannot import name 'render_nm_keyfile'`

- [ ] **Step 3: Write minimal implementation**

Extract the validation currently at the top of `render_wpa_supplicant` into `_validate_credentials(ssid: str, password: str) -> None` and call it from both renderers.

```python
# append to firmware/visio_recorder/wifi_onboard.py
def render_nm_keyfile(ssid: str, password: str) -> str:
    _validate_credentials(ssid, password)
    return (
        "[connection]\n"
        "id=visio\n"
        "type=wifi\n"
        "autoconnect=true\n\n"
        "[wifi]\n"
        "mode=infrastructure\n"
        f"ssid={ssid}\n\n"
        "[wifi-security]\n"
        "key-mgmt=wpa-psk\n"
        f"psk={password}\n\n"
        "[ipv4]\n"
        "method=auto\n\n"
        "[ipv6]\n"
        "method=auto\n"
    )


def write_nm_keyfile(path: Path, ssid: str, password: str) -> None:
    _write_private_text(path, render_nm_keyfile(ssid, password))
```

NM keyfiles treat `;`, `=`, and leading/trailing whitespace specially; extend `_UNSAFE_CHARS` handling for the NM renderer only if a test shows it is needed (YAGNI otherwise, the existing unsafe set already blocks quotes, newlines, and backslashes).

- [ ] **Step 4: Run the full firmware suite**

Run: `cd firmware && uv run --locked --extra dev pytest -v`
Expected: all pass (existing wpa_supplicant tests untouched)

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/wifi_onboard.py firmware/tests/test_wifi_onboard.py
git commit -m "feat(firmware): NetworkManager keyfile rendering for Bookworm onboarding"
```

### Task 7: First-boot onboarding sequence (camera QR scan)

**Files:**
- Create: `firmware/visio_recorder/onboarding.py`
- Test: `firmware/tests/test_onboarding.py`

**Interfaces:**
- Consumes: `parse_onboarding_qr_payload`, `QrDecodeError`, `write_nm_keyfile`, `write_session_credentials` (existing plus Task 6).
- Produces: `QrScanner` Protocol (`scan() -> str | None`, one capture-and-decode attempt), `RpicamZbarScanner` real implementation (`rpicam-still` then `zbarimg --raw -q`), and `run_onboarding(scanner: QrScanner, nm_keyfile_path: Path, session_path: Path, max_attempts: int) -> OnboardingPayload | None`.

- [ ] **Step 1: Write the failing tests**

```python
# firmware/tests/test_onboarding.py
import json
from pathlib import Path

from visio_recorder.onboarding import run_onboarding

VALID_PAYLOAD = json.dumps(
    {
        "ssid": "HomeNet",
        "password": "hunter2secret",
        "user_access_token": "at-123",
        "user_refresh_token": "rt-456",
    }
)


class FakeScanner:
    def __init__(self, results):
        self._results = list(results)

    def scan(self):
        return self._results.pop(0) if self._results else None


def test_run_onboarding_writes_keyfile_and_session_on_first_good_scan(tmp_path):
    scanner = FakeScanner([None, "not json", VALID_PAYLOAD])
    keyfile = tmp_path / "visio.nmconnection"
    session = tmp_path / "session.json"

    payload = run_onboarding(scanner, keyfile, session, max_attempts=5)

    assert payload is not None and payload.ssid == "HomeNet"
    assert "psk=hunter2secret" in keyfile.read_text()
    assert json.loads(session.read_text()) == {
        "access_token": "at-123",
        "refresh_token": "rt-456",
    }


def test_run_onboarding_gives_up_after_max_attempts(tmp_path):
    scanner = FakeScanner([None, "garbage"])

    payload = run_onboarding(
        scanner, tmp_path / "k", tmp_path / "s", max_attempts=2
    )

    assert payload is None
    assert not (tmp_path / "k").exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_onboarding.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'visio_recorder.onboarding'`

- [ ] **Step 3: Write minimal implementation**

```python
# firmware/visio_recorder/onboarding.py
import subprocess
import tempfile
from pathlib import Path
from typing import Optional, Protocol

from visio_recorder.wifi_onboard import (
    OnboardingPayload,
    QrDecodeError,
    parse_onboarding_qr_payload,
    write_nm_keyfile,
    write_session_credentials,
)


class QrScanner(Protocol):
    def scan(self) -> Optional[str]: ...


class RpicamZbarScanner:
    def scan(self) -> Optional[str]:
        with tempfile.TemporaryDirectory() as tmp:
            still = Path(tmp) / "qr.jpg"
            capture = subprocess.run(
                ["rpicam-still", "-n", "-t", "1000", "-o", str(still)]
            )
            if capture.returncode != 0:
                return None
            decode = subprocess.run(
                ["zbarimg", "--raw", "-q", str(still)],
                capture_output=True,
                text=True,
            )
            payload = decode.stdout.strip()
            return payload or None


def run_onboarding(
    scanner: QrScanner,
    nm_keyfile_path: Path,
    session_path: Path,
    max_attempts: int,
) -> Optional[OnboardingPayload]:
    for _ in range(max_attempts):
        raw = scanner.scan()
        if raw is None:
            continue
        try:
            payload = parse_onboarding_qr_payload(raw)
        except QrDecodeError:
            continue
        write_nm_keyfile(nm_keyfile_path, payload.ssid, payload.password)
        write_session_credentials(
            session_path, payload.user_access_token, payload.user_refresh_token
        )
        return payload
    return None
```

`RpicamZbarScanner` is deliberately not unit tested (two `subprocess.run` calls, no branching beyond return-code checks), matching the codebase convention that real drivers are exercised on hardware in Epic 5.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_onboarding.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/onboarding.py firmware/tests/test_onboarding.py
git commit -m "feat(firmware): first-boot QR onboarding loop"
```

### Task 8: Supabase client adapters

**Files:**
- Create: `firmware/visio_recorder/supabase_clients.py`
- Test: `firmware/tests/test_supabase_clients.py`

**Interfaces:**
- Consumes: session file JSON written by `write_session_credentials` (`{"access_token", "refresh_token"}`); `supabase-py`'s `create_client(url, key)` object shape (`auth.set_session`, `storage.from_(bucket).upload(path, file, options)`, `table(name).upsert(row).execute()`).
- Produces: `SupabaseClients` dataclass with `storage: StorageClient`, `status: StatusClient`, `registration: DeviceRegistrationClient` (the three existing Protocols), built by `build_supabase_clients(url: str, key: str, session_path: Path, client_factory=create_client) -> SupabaseClients`.

- [ ] **Step 1: Write the failing tests**

```python
# firmware/tests/test_supabase_clients.py
import json
from pathlib import Path

from visio_recorder.supabase_clients import build_supabase_clients


class FakeSupabase:
    # Records auth.set_session, storage uploads, and table upserts, mimicking
    # the supabase-py client surface used by the adapters.
    ...


def _write_session(tmp_path: Path) -> Path:
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"access_token": "at", "refresh_token": "rt"}))
    return session


def test_build_sets_the_session_before_any_client_use(tmp_path):
    fake = FakeSupabase()
    build_supabase_clients("http://x", "anon", _write_session(tmp_path),
                           client_factory=lambda url, key: fake)
    assert fake.session_set_with == ("at", "rt")


def test_storage_adapter_uploads_to_bucket_and_path(tmp_path):
    fake = FakeSupabase()
    clients = build_supabase_clients("http://x", "anon", _write_session(tmp_path),
                                     client_factory=lambda url, key: fake)
    local = tmp_path / "20260709_080000.mp4"
    local.write_bytes(b"vid")

    clients.storage.upload("segments", "device-abc/20260709_080000.mp4", local)

    assert fake.uploads == [("segments", "device-abc/20260709_080000.mp4", b"vid")]


def test_status_adapter_upserts_device_status_row(tmp_path):
    ...  # clients.status.upsert_device_status({...}) lands in fake.upserts["device_status"]


def test_registration_adapter_upserts_device_row(tmp_path):
    ...  # clients.registration.upsert_device("device-abc", "Visio Pendant")
```

Flesh out `FakeSupabase` and the two elided tests fully when implementing; the fake records every call so each adapter method has one assertion-backed test.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_supabase_clients.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'visio_recorder.supabase_clients'`

- [ ] **Step 3: Write minimal implementation**

```python
# firmware/visio_recorder/supabase_clients.py
import json
from dataclasses import dataclass
from pathlib import Path

from supabase import create_client


class SupabaseStorage:
    def __init__(self, client):
        self._client = client

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        with open(local_path, "rb") as f:
            self._client.storage.from_(bucket).upload(object_path, f.read())


class SupabaseStatus:
    def __init__(self, client):
        self._client = client

    def upsert_device_status(self, status: dict) -> None:
        self._client.table("device_status").upsert(status).execute()


class SupabaseRegistration:
    def __init__(self, client):
        self._client = client

    def upsert_device(self, device_id: str, name: str) -> None:
        self._client.table("devices").upsert(
            {"id": device_id, "name": name}
        ).execute()


@dataclass
class SupabaseClients:
    storage: SupabaseStorage
    status: SupabaseStatus
    registration: SupabaseRegistration


def build_supabase_clients(
    url: str, key: str, session_path: Path, client_factory=create_client
) -> SupabaseClients:
    client = client_factory(url, key)
    session = json.loads(session_path.read_text())
    client.auth.set_session(session["access_token"], session["refresh_token"])
    return SupabaseClients(
        storage=SupabaseStorage(client),
        status=SupabaseStatus(client),
        registration=SupabaseRegistration(client),
    )
```

Add `supabase` to `firmware/pyproject.toml` dependencies if not already present and run `uv lock` (check first; the Epic 1 plan lists `supabase-py` in the stack).
Verify the exact `devices` upsert column names against `supabase/migrations` before implementing `SupabaseRegistration` (the schema is the contract; adjust the row dict to match).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd firmware && uv run --locked --extra dev pytest tests/test_supabase_clients.py -v`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add firmware/visio_recorder/supabase_clients.py firmware/tests/test_supabase_clients.py firmware/pyproject.toml firmware/uv.lock
git commit -m "feat(firmware): supabase adapters implementing the client protocols"
```

### Task 9: Recording loop with background uploads

**Files:**
- Modify: `firmware/visio_recorder/daemon.py`
- Test: `firmware/tests/test_daemon.py`

**Interfaces:**
- Consumes: `record_segment` and `segment_filename` (Tasks 1-2), `on_segment_complete`/`SegmentResult`/`UPLOAD_FAILURES_TO_CRITICAL` (Task 4), `read_battery_status`, `apply_led_state`.
- Produces: `run_recording_loop(deps: LoopDeps, stop: threading.Event, clock: Callable[[], datetime] = datetime.now) -> None` where `LoopDeps` is a dataclass bundling the existing protocol instances plus `data_dir: Path`, `queue_dir: Path`, `device_id: str`, `segment_duration_ms: int`, `framerate: int`.

Behavior to encode in tests, one behavior per test:

1. Each cycle records one segment named from the cycle's start time, then submits the completed h264 to a single background worker (a `queue.Queue` consumed by one `threading.Thread`) that calls `on_segment_complete`.
2. A battery reading below the halt threshold sets the CRITICAL LED, drains the worker, and exits the loop.
3. `UPLOAD_FAILURES_TO_CRITICAL` consecutive `upload_ok=False` results set the CRITICAL LED but recording continues; any success resets the counter and restores the normal LED on the next cycle.
4. Setting `stop` exits after the in-flight cycle and joins the worker thread.

Tests drive the loop with fakes (instant fake recorder, scripted battery readings, raising storage client) and a `stop` event set after N cycles; no sleeps and no real threads beyond the worker under test (join with a timeout and assert it finished).

- [ ] **Step 1: Write the failing tests** covering the four behaviors above (full arrangements using the existing fakes from `test_recording_loop.py`, moved into a shared `tests/fakes.py` if duplication gets noisy).
- [ ] **Step 2: Run to verify they fail** (`ImportError: cannot import name 'run_recording_loop'`).
- [ ] **Step 3: Implement `LoopDeps` and `run_recording_loop`** exactly to the behaviors; the worker thread is the only new concurrency and the queue is unbounded.
- [ ] **Step 4: Run the full firmware suite** - all pass, no hangs (worker joins with `timeout=5`).
- [ ] **Step 5: Commit** `feat(firmware): recording loop with background upload worker`.

**As-built notes:**

- (commit 51e8a0b) the worker only wrapped the upload half of `on_segment_complete`'s work in error handling; any other exception (e.g. a status-upsert failure) propagated and killed the thread, silently stalling all future uploads with no crash and no restart. The worker's `while True` loop now wraps the whole `on_segment_complete` call, logs via `_logger.exception`, and continues to the next queued segment. The same review added a missing `updated_at` field to `DeviceStatus` (`recording_loop.py`), which this plan's Task 3/4 steps omitted - without it the status row's timestamp never advanced past the first insert.
- (commit fb331c2) `worker_thread.join(timeout=5)` could return before the worker actually finished draining, letting `main`'s halt-path CRITICAL LED apply-and-return race the worker's own in-flight LED call. The join no longer takes a timeout, so halt/stop always waits for the worker to fully drain first.

### Task 10: main() entry, config, systemd, and docs

**Files:**
- Modify: `firmware/visio_recorder/daemon.py`, `firmware/systemd/visio-recorder.service`, `README.md`, `docs/superpowers/specs/2026-07-04-visio-pendant-design.md`, `docs/superpowers/plans/2026-07-04-visio-epics-overview.md`
- Test: `firmware/tests/test_daemon.py`

**Interfaces:**
- Produces: `DaemonConfig` dataclass and `load_config(env: Mapping[str, str]) -> DaemonConfig` (reads `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `VISIO_DATA_DIR` default `/var/lib/visio-recorder`, `VISIO_SEGMENT_DURATION_MS` default `300000`, `VISIO_FRAMERATE` default `30`; raises `ValueError` naming any missing required key), plus `main() -> int` and the `if __name__ == "__main__": raise SystemExit(main())` guard.

- [ ] **Step 1: Write failing tests for `load_config`** (happy path, defaults, missing-key error naming the key).
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement `load_config` and `main`.**

`main()` composes, in order: `load_config(os.environ)`; `run_startup_sequence` (exit 0 with CRITICAL LED if `proceed` is False); first-boot check (`session_path` missing -> `run_onboarding` with `RpicamZbarScanner`, exit 1 if it returns None); `load_or_create_device_id` (+ `register_device` when the state file was new); `build_supabase_clients`; `flush_pending`; `run_recording_loop` with a `stop` event wired to `signal.SIGTERM`/`SIGINT`.
`main` stays a thin composition function tested only via its parts, matching the codebase convention that `daemon.py` wiring is validated on hardware in Epic 5.

**As-built note (commit fb331c2):** gating `register_device` on "the state file was new" left a retry gap - if the daemon crashed after `load_or_create_device_id` persisted `device.json` but before `register_device` completed, the device would never retry registration on subsequent boots. `main()` now gates on a separate `device_registered` marker file, touched only after `register_device` succeeds, so registration retries independently of device-ID persistence.

Systemd unit gains:

```ini
[Service]
EnvironmentFile=/etc/visio-recorder.env
```

- [ ] **Step 4: Docs.**

README firmware section: replace the "currently only implements the startup battery/LED sequence" sentence with the as-built description (onboarding, queue flush, recording loop, background uploads, `EnvironmentFile` config, `apt install zbar-tools` prerequisite).
Spec WiFi Onboarding section: note the as-built NM keyfile deviation with one sentence and why.
Epic 5 checklist step 2: "confirm the NetworkManager keyfile is written and the device reconnects" replaces the `wpa_supplicant.conf` wording.
Firmware plan Handoff: mark the glue items as resolved by this plan (mirror the as-built note pattern used for issue #9).

- [ ] **Step 5: Run the full firmware suite, then commit** `feat(firmware): daemon main entry, config, and systemd wiring (closes #7)`.

---

## Execution notes

- Branch: `feat/firmware-daemon-glue` off `main` (after PR #13 merges; rebase if the FLAG rename is still in flight).
- Validate the finished branch with `/no-mistakes`, intent quoting issue #7 and the two decisions above.
- Hardware smoke test (rpicam-vid flags, zbarimg decode, NM keyfile pickup, `nmcli connection reload`) happens in Epic 5 on the assembled device; nothing in this plan requires hardware.
