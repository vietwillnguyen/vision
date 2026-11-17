"""Shared test fakes for the visio_recorder firmware suite."""

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from visio_recorder.battery import BatteryReader
from visio_recorder.led import LedDriver, LedPattern
from visio_recorder.muxer import CommandRunner
from visio_recorder.recording_loop import DiskStats
from visio_recorder.upload_limit import UploadLimit
from visio_recorder.uploader import StorageClient


class FakeBatteryReader(BatteryReader):
    def __init__(self, pct: int) -> None:
        self._pct = pct

    def get_charge_pct(self) -> int:
        return self._pct


class TriggerableBatteryReader(BatteryReader):
    """Reports a healthy charge until ``trigger_halt`` flips it to a halt level.

    The trigger is a ``threading.Event`` so it can be set from the main loop
    thread (via a command runner hook) and observed from the worker thread
    without a race.
    """

    def __init__(self, healthy_pct: int = 85, halt_pct: int = 5) -> None:
        self._healthy_pct = healthy_pct
        self._halt_pct = halt_pct
        self._halt = threading.Event()

    def trigger_halt(self) -> None:
        self._halt.set()

    def get_charge_pct(self) -> int:
        return self._halt_pct if self._halt.is_set() else self._healthy_pct


class FakeLedDriver(LedDriver):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[tuple[tuple[int, int, int], LedPattern]] = []

    def set(self, color, pattern):
        with self._lock:
            self.calls.append((color, pattern))


class SimulatedSpiDevice:
    """Stands in for py-spidev's ``SpiDev`` bound to ``/dev/spidev0.0``.

    Enforces what the real device enforces: ``mode`` and ``max_speed_hz`` can
    only be set on an open device, and one write may not exceed the kernel's
    default ``spidev.bufsiz`` of 4096 bytes. Records every write together with
    the clock it was sent at, and detects two threads inside ``writebytes2``
    at once (on hardware that would interleave two frames on one wire).
    """

    BUFSIZ = 4096

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_write = False
        self.opened: tuple[int, int] | None = None
        self._mode = 0
        self._max_speed_hz = 0
        self.writes: list[bytes] = []
        self.overlapping_writes = 0

    def open(self, bus: int, device: int) -> None:
        self.opened = (bus, device)

    def _require_open(self) -> None:
        if self.opened is None:
            raise OSError(9, "Bad file descriptor")

    @property
    def mode(self) -> int:
        return self._mode

    @mode.setter
    def mode(self, value: int) -> None:
        self._require_open()
        self._mode = value

    @property
    def max_speed_hz(self) -> int:
        return self._max_speed_hz

    @max_speed_hz.setter
    def max_speed_hz(self, value: int) -> None:
        self._require_open()
        self._max_speed_hz = value

    def writebytes2(self, values: bytes) -> None:
        self._require_open()
        if len(values) > self.BUFSIZ:
            raise OSError(90, "Message too long")
        with self._lock:
            if self._in_write:
                self.overlapping_writes += 1
            self._in_write = True
        try:
            # Widen the window a racing writer would have to hit.
            time.sleep(0.0005)
            with self._lock:
                self.writes.append(bytes(values))
        finally:
            with self._lock:
                self._in_write = False


class FakeCommandRunner(CommandRunner):
    """Fake runner for both ``rpicam-vid`` (record) and ``ffmpeg`` (mux).

    - ``rpicam-vid`` writes an empty file at its ``-o`` output path so the
      recording loop produces a real ``.h264`` for the worker to mux.
    - ``ffmpeg`` copies the ``-i`` input into the output path, standing in for
      the mux step.

    ``on_record`` is invoked (with the running record count) after each
    ``rpicam-vid`` call, letting tests drive cycle counts deterministically
    from the main loop thread.

    ``mux_gate``, when given, blocks each ``ffmpeg`` call until the event is
    set. The mux is the first thing the upload worker does per segment, so a
    gate holds the entire worker back until the main loop reaches a known
    point (e.g. to guarantee segments are still queued when a battery halt is
    observed).
    """

    def __init__(
        self,
        on_record: Callable[[int], None] | None = None,
        mux_gate: threading.Event | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._on_record = on_record
        self._mux_gate = mux_gate
        self.calls: list[list[str]] = []
        self.record_count = 0

    def run(self, args: list[str]) -> None:
        with self._lock:
            self.calls.append(args)
        if args and args[0] == "rpicam-vid":
            output_path = Path(args[args.index("-o") + 1])
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"")
            self.record_count += 1
            if self._on_record is not None:
                self._on_record(self.record_count)
        elif "ffmpeg" in args:
            if self._mux_gate is not None:
                assert self._mux_gate.wait(timeout=5), "mux_gate never opened"
            input_path = Path(args[args.index("-i") + 1])
            output_path = Path(args[args.index("copy") + 1])
            if input_path.exists():
                output_path.write_bytes(input_path.read_bytes())


class FakeStorageClient(StorageClient):
    def __init__(self) -> None:
        self.uploaded: list[str] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        self.uploaded.append(object_path)


class FailingStorageClient(StorageClient):
    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        raise RuntimeError("network down")


class FakeStorageClientFailingOn(StorageClient):
    def __init__(self, failing_filename: str) -> None:
        self.failing_filename = failing_filename
        self.uploaded: list[str] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        if local_path.name == self.failing_filename:
            raise RuntimeError(f"upload failed for {self.failing_filename}")
        self.uploaded.append(object_path)


class ScriptedStorageClient(StorageClient):
    """Fails on a scripted set of 1-based upload attempt numbers, else succeeds.

    Only the single upload worker calls ``upload``, so attempt ``k`` maps to the
    ``k``-th completed segment in order.
    """

    def __init__(self, fail_on_attempts: set[int]) -> None:
        self.fail_on_attempts = set(fail_on_attempts)
        self.attempts = 0
        self.uploaded: list[str] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        self.attempts += 1
        if self.attempts in self.fail_on_attempts:
            raise RuntimeError(f"upload failed on attempt {self.attempts}")
        self.uploaded.append(object_path)


class FakeStatusClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.upserts: list[dict] = []

    def upsert_device_status(self, status: dict) -> None:
        with self._lock:
            self.upserts.append(status)


class ScriptedStatusClient:
    """Fails on a scripted set of 1-based upsert attempt numbers, else records.

    Only the single upload worker calls ``upsert_device_status``, so attempt
    ``k`` maps to the ``k``-th completed segment in order.
    """

    def __init__(self, fail_on_attempts: set[int]) -> None:
        self.fail_on_attempts = set(fail_on_attempts)
        self.attempts = 0
        self.upserts: list[dict] = []

    def upsert_device_status(self, status: dict) -> None:
        self.attempts += 1
        if self.attempts in self.fail_on_attempts:
            raise RuntimeError(f"status upsert failed on attempt {self.attempts}")
        self.upserts.append(status)


class FakeDiskStatsReader:
    def __init__(self, stats: DiskStats):
        self._stats = stats

    def usage(self, path):
        return self._stats


class FakeClock:
    """Returns strictly increasing datetimes, one per call.

    Guarantees each recording cycle gets a unique second-resolution timestamp
    so segment filenames never collide.
    """

    def __init__(self, start: datetime, step_seconds: int = 1) -> None:
        self._next = start
        self._step = timedelta(seconds=step_seconds)

    def __call__(self) -> datetime:
        value = self._next
        self._next = self._next + self._step
        return value


class UnusedProbe:
    def duration_sec(self, path: Path) -> float:
        raise AssertionError(f"nothing should be probed: {path}")


def unlimited_upload_limit() -> UploadLimit:
    """An upload limit no test segment reaches, so nothing is ever split."""
    return UploadLimit(max_bytes=2**62, runner=FakeCommandRunner(), probe=UnusedProbe())
