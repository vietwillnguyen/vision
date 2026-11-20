"""End-to-end proof that a flag survives the network being gone.

Two pieces of hardware are simulated and nothing else. The camera is ffmpeg
writing a real H.264 stream wherever ``rpicam-vid`` would, which the
firmware's own ffmpeg runner then muxes; and ``/dev/spidev0.0`` is
``SimulatedSpiDevice``, whose bytes are decoded back into the colours a
WS2812B would latch by ``tests/ws2812_waveform.py``, an independent receiver
written from the datasheet timing windows. Everything else is firmware:
``run_recording_loop``, ``flush_pending``, ``FlagUploadWorker``,
``make_flag_press_handler``, ``LedController`` and the real
``Ws2812LedDriver``.

Storage is the fake client ``Network`` below, which refuses uploads while
the simulated WiFi is down and records, in order, everything that got
through.

The retry schedule is scaled 1500:1 (30 s -> 20 ms, 480 s -> 320 ms) so a
two-hour outage plays out in a few seconds of real time. Nothing else is
scaled, and scenario 1 prints the unscaled production schedule straight out
of ``RetryBackoff`` beside the scaled one.

Run from firmware/:

    uv run --locked --extra dev python \
        ../docs/superpowers/evidence/flag-marker-offline-first/e2e_flag_offline.py
"""

import logging
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parents[4] / "firmware"
sys.path.insert(0, str(FIRMWARE))

from tests.fakes import FakeBatteryReader, FakeClock, SimulatedSpiDevice  # noqa: E402
from tests.ws2812_waveform import decode_frame  # noqa: E402
from visio_recorder.capture import CaptureExit, VideoSettings  # noqa: E402
from visio_recorder.daemon import (  # noqa: E402
    LoopDeps,
    run_recording_loop,
    run_startup_sequence,
)
from visio_recorder.drivers import Ws2812LedDriver  # noqa: E402
from visio_recorder.flag_button import (  # noqa: E402
    FLAG_UPLOAD_THREAD_NAME,
    FlagUploadWorker,
    make_flag_press_handler,
    write_flag_marker,
)
from visio_recorder.led import LedController  # noqa: E402
from visio_recorder.muxer import SubprocessCommandRunner  # noqa: E402
from visio_recorder.recording_loop import (  # noqa: E402
    RESTART_GAP_SEC,
    RetryBackoff,
    ShutilDiskStatsReader,
    flush_pending,
)
from visio_recorder.upload_limit import UploadLimit  # noqa: E402
from visio_recorder.upload_queue import list_pending, pending_markers  # noqa: E402

DEVICE = "device-sim"
EFFECTIVE_SPI_HZ = 6_250_000
SCALE = 1500.0  # production seconds per simulated second
SCALED_BACKOFF = RetryBackoff(base_sec=30.0 / SCALE, max_sec=480.0 / SCALE)
SCALED_GAP_SEC = RESTART_GAP_SEC / SCALE
COLOUR_NAMES = {
    (0, 255, 0): "green (recording)",
    (0, 0, 255): "blue (uploading)",
    (255, 255, 0): "yellow (low battery)",
    (255, 0, 0): "red (critical)",
    (255, 0, 255): "MAGENTA (flag acknowledged)",
}


class _NoTraceback(logging.Formatter):
    """Firmware logs every failed upload with its traceback; here the message
    is the evidence and fifteen lines of stack is noise."""

    def formatException(self, exc_info: object) -> str:
        return ""

    def format(self, record: logging.LogRecord) -> str:
        return "    firmware: " + record.getMessage()


def _quiet_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_NoTraceback())
    logging.basicConfig(level=logging.WARNING, handlers=[handler], force=True)


failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(': ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def heading(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


class Network:
    """A fake storage client with a switchable network.

    Implements the ``StorageClient`` protocol. Records every attempt in the
    order it was made, which is what the marker-before-segment ordering is
    read off, and which thread made it, which is what the single-uploader
    invariant is read off.
    """

    def __init__(self, online: bool = True) -> None:
        self.online = online
        self._lock = threading.Lock()
        self.attempts: list[tuple[str, bool, str]] = []
        self.stored: list[str] = []
        self.changed = threading.Condition()

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        with self._lock:
            online = self.online
            self.attempts.append((object_path, online, threading.current_thread().name))
            if online:
                self.stored.append(object_path)
        with self.changed:
            self.changed.notify_all()
        if not online:
            raise ConnectionError("network down")

    def marker_attempts(self) -> list[tuple[str, bool, str]]:
        with self._lock:
            return [a for a in self.attempts if a[0].endswith(".marker")]

    def wait_for_marker_attempts(self, count: int, timeout: float = 30.0) -> bool:
        with self.changed:
            return self.changed.wait_for(
                lambda: len(self.marker_attempts()) >= count, timeout
            )


class SimulatedCamera:
    """rpicam-vid becomes ffmpeg writing H.264 to the -o path.

    One object serves as both runners the loop takes: called with a cancel
    event it is the interruptible capture runner, and without one it is the
    command runner that muxes. The simulated capture is short enough that it
    always runs to completion, so it never reports a cancel.
    """

    def __init__(self, on_capture=None) -> None:
        self.captures = 0
        self._mux = SubprocessCommandRunner()
        self._on_capture = on_capture

    def run(
        self, args: list[str], cancel: threading.Event | None = None
    ) -> CaptureExit | None:
        if args[0] != "rpicam-vid":
            self._mux.run(args[:1] + ["-loglevel", "error"] + args[1:])
            return None
        self.captures += 1
        output = args[args.index("-o") + 1]
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x180:rate=15:duration=0.3",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "h264",
                output,
            ],
            check=True,
        )
        if self._on_capture is not None:
            self._on_capture(self.captures)
        return CaptureExit(returncode=0, cancelled=False)


class Status:
    def upsert_device_status(self, status: dict) -> None:
        pass


def _deps(
    tmp: Path,
    runner,
    storage,
    led,
    flag_worker,
    *,
    gap_sec: float = SCALED_GAP_SEC,
    sleep=time.sleep,
) -> LoopDeps:
    data_dir = tmp / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return LoopDeps(
        command_runner=runner,
        capture_runner=runner,
        storage_client=storage,
        status_client=Status(),
        led_driver=led,
        battery_reader=FakeBatteryReader(85),
        disk_stats_reader=ShutilDiskStatsReader(),
        data_dir=data_dir,
        queue_dir=tmp / "data" / "queue",
        device_id=DEVICE,
        segment_duration_ms=300,
        video=VideoSettings(),
        upload_limit=UploadLimit(max_bytes=2**40, runner=runner, probe=_NoProbe()),
        flush_markers=flag_worker.flush_markers,
        retry_backoff=SCALED_BACKOFF,
        restart_gap_sec=gap_sec,
        sleep=sleep,
    )


class _NoProbe:
    def duration_sec(self, path: Path) -> float:
        raise AssertionError(f"nothing should need splitting: {path}")


def _ordering_holds(stored: list[str]) -> tuple[bool, str]:
    """No segment may reach storage before a marker that is still to come."""
    seen_segment = False
    for key in stored:
        if key.endswith(".marker"):
            if seen_segment:
                return False, f"{key} landed after a segment"
        else:
            seen_segment = True
    return True, "every marker preceded every segment"


def scenario_1_long_outage() -> None:
    heading("1. WiFi down for hours, then restored")
    print("Production retry schedule, straight out of RetryBackoff():")
    production = RetryBackoff()
    print(
        "  consecutive failures 1..6 -> "
        + ", ".join(f"{production.delay_sec(n):.0f}s" for n in range(1, 7))
    )
    print(
        f"  simulated here at 1/{SCALE:.0f}: "
        + ", ".join(f"{SCALED_BACKOFF.delay_sec(n) * 1000:.0f}ms" for n in range(1, 7))
    )

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        queue_dir = tmp / "data" / "queue"
        storage = Network(online=False)
        spi = SimulatedSpiDevice()
        led = LedController(Ws2812LedDriver(spi=spi))
        flag_worker = FlagUploadWorker(
            storage, DEVICE, queue_dir, retry_backoff=SCALED_BACKOFF
        )
        press = make_flag_press_handler(
            queue_dir,
            flag_worker.submit,
            ack=led.ack_flag,
            clock=FakeClock(datetime(2026, 7, 4, 12, 0, 30), step_seconds=0),
        )
        stop = threading.Event()
        battery = FakeBatteryReader(85)
        assert run_startup_sequence(battery, led).proceed  # LED: recording green

        def on_capture(n: int) -> None:
            if n == 1:
                press()  # flagged 30 s into the first segment
            if n >= 4:
                stop.set()

        runner = SimulatedCamera(on_capture)
        deps = _deps(tmp, runner, storage, led, flag_worker)
        run_recording_loop(
            deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0), step_seconds=60)
        )

        # Two device-hours of outage: 30 + 60 + 120 + 240 + 480 * n seconds.
        rounds, device_seconds = 0, 0.0
        while device_seconds < 2 * 3600:
            rounds += 1
            device_seconds += production.delay_sec(rounds)
        print(
            f"\nHolding the outage for {rounds} retry rounds "
            f"= {device_seconds / 3600:.1f} device-hours "
            f"({rounds * SCALED_BACKOFF.max_sec:.1f}s real at worst)"
        )
        got_rounds = storage.wait_for_marker_attempts(rounds)
        attempts = storage.marker_attempts()
        check(
            "the marker is retried in process while the network is down",
            got_rounds and len(attempts) >= rounds,
            f"{len(attempts)} attempts over {device_seconds / 3600:.1f} device-hours",
        )
        check(
            "every one of those attempts was the flag worker's",
            {a[2] for a in attempts} == {FLAG_UPLOAD_THREAD_NAME},
            str({a[2] for a in attempts}),
        )
        check(
            "nothing reached storage while the network was down",
            storage.stored == [],
        )
        check(
            "the marker is still on the card",
            [p.name for p in pending_markers(queue_dir)]
            == ["FLAG_20260704_120030.marker"],
        )
        colours = [decode_frame(f, EFFECTIVE_SPI_HZ)[0] for f in spi.writes]
        check(
            "the acknowledgement blink reached the wire with no network at all",
            (255, 0, 255) in colours,
            f"{colours.count((255, 0, 255))} magenta frame(s)",
        )
        check(
            "the upload failures still latched Critical red",
            (255, 0, 0) in colours,
        )
        check("no two LED frames interleaved", spi.overlapping_writes == 0)

        print("\nWiFi comes back.")
        storage.online = True
        stop2 = threading.Event()

        def drained(n: int) -> None:
            # Record until the backlog is gone, or give up after a day's worth.
            if not list_pending(queue_dir) or n >= 20:
                stop2.set()

        runner2 = SimulatedCamera(drained)
        deps2 = _deps(tmp, runner2, storage, led, flag_worker)
        run_recording_loop(
            deps2,
            stop2,
            clock=FakeClock(datetime(2026, 7, 4, 12, 4, 0), step_seconds=60),
        )
        flag_worker.stop()
        led.close()

        print("  storage, in the order it received things:")
        for key in storage.stored:
            print(f"    {key}")
        ok, detail = _ordering_holds(storage.stored)
        check("the flag reached storage before any of its day's footage", ok, detail)
        check("the queue drained completely", list_pending(queue_dir) == [], "")


def scenario_2_flaky_wifi() -> None:
    heading("2. Flaky WiFi (every other attempt fails)")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        queue_dir = tmp / "data" / "queue"

        class Flaky(Network):
            stable = False

            def upload(self, bucket, object_path, local_path):
                with self._lock:
                    if not self.stable:
                        self.online = not self.online
                super().upload(bucket, object_path, local_path)

        storage = Flaky(online=False)  # flips to True on the first attempt
        spi = SimulatedSpiDevice()
        led = LedController(Ws2812LedDriver(spi=spi))
        assert run_startup_sequence(FakeBatteryReader(85), led).proceed
        flag_worker = FlagUploadWorker(
            storage, DEVICE, queue_dir, retry_backoff=SCALED_BACKOFF
        )
        pressed_at = iter(
            [
                datetime(2026, 7, 4, 12, 0, 30),
                datetime(2026, 7, 4, 12, 2, 15),
                datetime(2026, 7, 4, 12, 4, 1),  # in the restart gap
            ]
        )
        press = make_flag_press_handler(
            queue_dir,
            flag_worker.submit,
            ack=led.ack_flag,
            clock=lambda: next(pressed_at),
        )
        stop = threading.Event()

        def on_capture(n: int) -> None:
            if n in (1, 3, 5):
                press()
            if n >= 6:
                stop.set()

        runner = SimulatedCamera(on_capture)
        deps = _deps(tmp, runner, storage, led, flag_worker)
        run_recording_loop(
            deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0), step_seconds=60)
        )

        # Settle it the way a boot would: WiFi steady, one startup flush.
        storage.stable = True
        storage.online = True
        flag_worker.flush_markers()
        flag_worker.stop()
        flush_pending(queue_dir, storage, DEVICE, deps.upload_limit)
        led.close()

        markers = [k for k in storage.stored if k.endswith(".marker")]
        check("all three presses reached storage", len(set(markers)) == 3, str(markers))
        check("the queue drained completely", list_pending(queue_dir) == [])
        colours = [decode_frame(f, EFFECTIVE_SPI_HZ)[0] for f in spi.writes]
        check(
            "each press was acknowledged on the wire",
            colours.count((255, 0, 255)) == 3,
            f"{colours.count((255, 0, 255))} magenta frames",
        )
        check("no two LED frames interleaved", spi.overlapping_writes == 0)


def scenario_3_reboot_mid_backlog() -> None:
    heading("3. Reboot in the middle of a backlog")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        queue_dir = tmp / "data" / "queue"
        queue_dir.mkdir(parents=True)
        # A day's worth of outage left on the card by the previous run.
        for minute in range(4):
            (queue_dir / f"2026070412{minute:02d}00.mp4").write_bytes(b"segment")
        for stamp in ("20260704120030", "20260704120401"):
            write_flag_marker(queue_dir, datetime.strptime(stamp, "%Y%m%d%H%M%S"))
        print("  on the card at boot (name order, which is what the queue lists):")
        for path in list_pending(queue_dir):
            print(f"    {path.name}")

        class DiesAfter(Network):
            """The battery gives out three uploads into the boot flush."""

            def __init__(self, budget: int) -> None:
                super().__init__(online=True)
                self.budget = budget

            def upload(self, bucket, object_path, local_path):
                with self._lock:
                    if self.budget <= 0:
                        self.online = False
                    self.budget -= 1
                super().upload(bucket, object_path, local_path)

        storage = DiesAfter(budget=3)
        limit = UploadLimit(max_bytes=2**40, runner=SimulatedCamera(), probe=_NoProbe())
        flush_pending(queue_dir, storage, DEVICE, limit)

        print("  reached storage before the power went:")
        for key in storage.stored:
            print(f"    {key}")
        markers_up = [k for k in storage.stored if k.endswith(".marker")]
        check(
            "the partial flush sent the flags, not three segments and no flags",
            len(markers_up) == 2,
            f"{len(markers_up)} of 2 markers, "
            f"{len(storage.stored) - len(markers_up)} segment(s)",
        )
        check(
            "no flag is left behind a segment that already reached storage",
            _ordering_holds(storage.stored)[0],
        )
        print("  still on the card for the next boot:")
        for path in list_pending(queue_dir):
            print(f"    {path.name}")

        second_boot = Network(online=True)
        flush_pending(queue_dir, second_boot, DEVICE, limit)
        check(
            "the next boot clears the rest",
            list_pending(queue_dir) == [],
            f"{len(second_boot.stored)} more uploaded",
        )


def scenario_4_press_during_upload() -> None:
    heading("4. A press while a segment upload is in flight")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        queue_dir = tmp / "data" / "queue"
        in_upload = threading.Event()
        let_go = threading.Event()

        class Blocking(Network):
            def upload(self, bucket, object_path, local_path):
                if object_path.endswith(".mp4"):
                    in_upload.set()
                    assert let_go.wait(timeout=20), "the upload never finished"
                super().upload(bucket, object_path, local_path)

        storage = Blocking(online=True)
        spi = SimulatedSpiDevice()
        led = LedController(Ws2812LedDriver(spi=spi))
        flag_worker = FlagUploadWorker(
            storage, DEVICE, queue_dir, retry_backoff=SCALED_BACKOFF
        )
        durable_before_handover: list[bool] = []

        def submit(path: Path) -> None:
            # The marker must already be on the card by the time the uploader
            # is even told about it; that is what "local-first" buys.
            durable_before_handover.append(path.exists())
            flag_worker.submit(path)

        press = make_flag_press_handler(
            queue_dir,
            submit,
            ack=led.ack_flag,
            clock=FakeClock(datetime(2026, 7, 4, 12, 0, 20), step_seconds=0),
        )
        stop = threading.Event()
        runner = SimulatedCamera(lambda n: stop.set() if n >= 1 else None)
        assert run_startup_sequence(FakeBatteryReader(85), led).proceed
        deps = _deps(tmp, runner, storage, led, flag_worker)
        loop = threading.Thread(
            target=run_recording_loop,
            args=(deps, stop),
            kwargs={
                "clock": FakeClock(datetime(2026, 7, 4, 12, 0, 0), step_seconds=60)
            },
        )
        loop.start()

        assert in_upload.wait(timeout=20), "the segment upload never started"
        started = time.monotonic()
        press()
        press_returned = time.monotonic() - started
        check(
            "the press handler returned while the segment upload was still blocked",
            press_returned < 0.05 and not let_go.is_set(),
            f"{press_returned * 1000:.1f} ms",
        )
        check(
            "the marker was on the card before the uploader was told about it",
            durable_before_handover == [True],
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            colours = [decode_frame(f, EFFECTIVE_SPI_HZ)[0] for f in spi.writes]
            if (255, 0, 255) in colours:
                break
            time.sleep(0.01)
        check(
            "the wearer was acknowledged before the upload even finished",
            (255, 0, 255) in colours and not let_go.is_set(),
        )
        let_go.set()
        loop.join(timeout=20)
        flag_worker.stop()
        led.close()

        colours = [decode_frame(f, EFFECTIVE_SPI_HZ)[0] for f in spi.writes]
        print("  LED timeline on the wire:")
        for colour in colours:
            print(f"    {COLOUR_NAMES[colour]}")
        check(
            "the blink did not leave magenta on",
            colours[-1] != (255, 0, 255),
            COLOUR_NAMES[colours[-1]]
            + " - the upload finished mid-blink, so that state change won"
            + " and the restore stood down",
        )
        check(
            "only the flag worker ever uploaded a marker",
            {a[2] for a in storage.marker_attempts()} == {FLAG_UPLOAD_THREAD_NAME},
        )
        check("no two LED frames interleaved", spi.overlapping_writes == 0)


def scenario_5_press_in_the_restart_gap() -> None:
    heading("5. A press in the restart gap, after the segment has been muxed")
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        queue_dir = tmp / "data" / "queue"
        storage = Network(online=True)
        led = LedController(Ws2812LedDriver(spi=SimulatedSpiDevice()))
        flag_worker = FlagUploadWorker(
            storage, DEVICE, queue_dir, retry_backoff=SCALED_BACKOFF
        )
        # 12:00:00 + 60 s segment, pressed 2.5 s into the gap that follows.
        press = make_flag_press_handler(
            queue_dir,
            flag_worker.submit,
            ack=led.ack_flag,
            clock=FakeClock(datetime(2026, 7, 4, 12, 1, 2, 500000), step_seconds=0),
        )
        waits: list[float] = []

        def gap_sleep(seconds: float) -> None:
            # The worker is waiting out the gap: the segment is already muxed
            # and would have been on its way to storage without this wait.
            waits.append(seconds)
            press()
            time.sleep(seconds)

        stop = threading.Event()

        def stop_once_the_first_segment_is_stored(captures: int) -> None:
            # Recording carries on past the gap: a stop ends the run without
            # waiting out a gap, so the stop lands on the next capture, once
            # the segment under test has reached storage.
            if captures < 2:
                return
            with storage.changed:
                storage.changed.wait_for(
                    lambda: f"{DEVICE}/20260704_120000.mp4" in storage.stored, 20
                )
            stop.set()

        runner = SimulatedCamera(stop_once_the_first_segment_is_stored)
        assert run_startup_sequence(FakeBatteryReader(85), led).proceed
        deps = _deps(
            tmp,
            runner,
            storage,
            led,
            flag_worker,
            gap_sec=RESTART_GAP_SEC,
            sleep=gap_sleep,
        )
        run_recording_loop(
            deps, stop, clock=FakeClock(datetime(2026, 7, 4, 12, 0, 0), step_seconds=60)
        )
        flag_worker.stop()
        led.close()

        print("  storage, in the order it received things:")
        for key in storage.stored:
            print(f"    {key}")
        check(
            "the segment waited for its gap, and never longer than the gap",
            len(waits) == 1 and 0 < waits[0] <= RESTART_GAP_SEC,
            f"waited {waits[0]:.2f} s of the unscaled {RESTART_GAP_SEC:.0f} s gap"
            if len(waits) == 1
            else f"{len(waits)} waits",
        )
        check(
            "the gap press reached storage before the segment it flags",
            storage.stored
            == [
                f"{DEVICE}/FLAG_20260704_120102.marker",
                f"{DEVICE}/20260704_120000.mp4",
                f"{DEVICE}/20260704_120100.mp4",
            ],
            str(storage.stored),
        )
        check("the queue drained completely", list_pending(queue_dir) == [])


def main() -> int:
    _quiet_logging()
    print(
        f"visio flag-marker offline-first simulation, {datetime.now():%Y-%m-%d %H:%M}"
    )
    print(f"firmware: {FIRMWARE}")
    scenario_1_long_outage()
    scenario_2_flaky_wifi()
    scenario_3_reboot_mid_backlog()
    scenario_4_press_during_upload()
    scenario_5_press_in_the_restart_gap()
    heading("Result")
    if failures:
        for name in failures:
            print(f"  FAILED: {name}")
        return 1
    print("  every check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
