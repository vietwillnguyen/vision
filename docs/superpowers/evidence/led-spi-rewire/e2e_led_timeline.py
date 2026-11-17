"""End-to-end LED timeline: the daemon's recording loop driving an SPI WS2812B.

Only two pieces of hardware are simulated. The camera is ffmpeg writing a
real H.264 stream wherever rpicam-vid would (then muxed by the firmware's own
ffmpeg runner), and /dev/spidev0.0 is ``SimulatedSpiDevice``, which records
the exact bytes the driver clocks out of MOSI. Everything else is firmware
code: ``run_startup_sequence``, ``run_recording_loop``, ``led.py``, the real
``Ws2812LedDriver``, the flag-button press handler and its upload worker.

Each SPI frame is decoded into the colour a WS2812B would latch by the
independent datasheet-timing receiver in ``tests/ws2812_waveform.py``, at
6.25 MHz (what the BCM2835 actually clocks for the 6.4 MHz request).

Scenario: boot healthy, record, press the flag button mid-segment, lose the
network for three uploads, recover, drop to a low battery, then hit the halt
threshold.

Run from firmware/:

    uv run --locked --extra dev python \
        ../docs/superpowers/evidence/led-spi-rewire/e2e_led_timeline.py
"""

import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

FIRMWARE = Path(__file__).resolve().parents[4] / "firmware"
sys.path.insert(0, str(FIRMWARE))

from tests.fakes import FakeClock, SimulatedSpiDevice  # noqa: E402
from tests.ws2812_waveform import decode_frame  # noqa: E402
from visio_recorder.daemon import (  # noqa: E402
    LoopDeps,
    run_recording_loop,
    run_startup_sequence,
)
from visio_recorder.drivers import Ws2812LedDriver  # noqa: E402
from visio_recorder.flag_button import (  # noqa: E402
    FlagUploadWorker,
    make_flag_press_handler,
)
from visio_recorder.muxer import SubprocessCommandRunner  # noqa: E402
from visio_recorder.recording_loop import ShutilDiskStatsReader  # noqa: E402

EFFECTIVE_SPI_HZ = 6_250_000
NAMES = {
    (0, 255, 0): "green",
    (0, 0, 255): "blue",
    (255, 255, 0): "yellow",
    (255, 0, 0): "red",
}

T0 = time.monotonic()
events: list[tuple[float, str]] = []
events_lock = threading.Lock()


def log(event: str) -> None:
    with events_lock:
        events.append((time.monotonic() - T0, event))


class TimedSpi(SimulatedSpiDevice):
    def __init__(self) -> None:
        super().__init__()
        self.times: list[float] = []

    def writebytes2(self, values: bytes) -> None:
        super().writebytes2(values)
        self.times.append(time.monotonic() - T0)


class Battery:
    def __init__(self) -> None:
        self.pct = 85

    def get_charge_pct(self) -> int:
        return self.pct


class SimulatedCamera:
    """rpicam-vid becomes ffmpeg writing H.264 to the -o path."""

    def __init__(self, on_capture) -> None:
        self.captures = 0
        self._mux = SubprocessCommandRunner()
        self._on_capture = on_capture

    def run(self, args: list[str]) -> None:
        if args[0] != "rpicam-vid":
            self._mux.run(args[:1] + ["-loglevel", "error"] + args[1:])
            return
        self.captures += 1
        output = args[args.index("-o") + 1]
        seconds = int(args[args.index("-t") + 1]) / 1000
        log(f"capture {self.captures} starts ({Path(output).name})")
        subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc=size=640x360:rate=30:duration={seconds}",
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
        self._on_capture(self.captures)


class Storage:
    """A local bucket with a scripted network outage."""

    def __init__(self, root: Path, battery: Battery) -> None:
        self.root = root
        self.attempts = 0
        self.battery = battery

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        if local_path.suffix == ".marker":
            log(f"flag marker uploaded ({local_path.name})")
            return
        self.attempts += 1
        n = self.attempts
        if n in (2, 3, 4):
            log(f"segment upload {n} FAILS (network down)")
            raise ConnectionError("network down")
        dest = self.root / object_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(local_path.read_bytes())
        log(f"segment upload {n} ok ({dest.stat().st_size} bytes)")
        if n == 5:
            self.battery.pct = 15
            log("battery drops to 15% (low)")


class Status:
    def upsert_device_status(self, status: dict) -> None:
        pass


def main() -> None:
    spi = TimedSpi()
    led = Ws2812LedDriver(spi=spi)
    battery = Battery()
    stop = threading.Event()

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "data"
        queue_dir = data_dir / "queue"
        data_dir.mkdir()
        storage = Storage(Path(tmp) / "bucket", battery)
        flag_worker = FlagUploadWorker(storage, "device-e2e")
        press = make_flag_press_handler(queue_dir, flag_worker.submit)

        def on_capture(n: int) -> None:
            if n == 2:
                log("flag button pressed")
                press()
            if n == 7:
                battery.pct = 5
                log("battery drops to 5% (halt threshold)")

        log("boot: startup sequence")
        assert run_startup_sequence(battery, led).proceed
        deps = LoopDeps(
            command_runner=SimulatedCamera(on_capture),
            storage_client=storage,
            status_client=Status(),
            led_driver=led,
            battery_reader=battery,
            disk_stats_reader=ShutilDiskStatsReader(),
            data_dir=data_dir,
            queue_dir=queue_dir,
            device_id="device-e2e",
            segment_duration_ms=1000,
            framerate=30,
        )
        # One-second segments would otherwise share second-resolution names;
        # step the segment clock by the production 5 minutes instead.
        clock = FakeClock(datetime(2026, 11, 16, 9, 0, 0), step_seconds=300)
        run_recording_loop(deps, stop, clock=clock)
        flag_worker.stop()
        log("recording loop exited (battery halt)")

    for frame, at in zip(spi.writes, spi.times, strict=True):
        (colour,) = decode_frame(frame, EFFECTIVE_SPI_HZ)
        with events_lock:
            events.append((at, f"LED -> {NAMES[colour]} {colour} [{len(frame)} B]"))

    print(
        f"SPI frames written: {len(spi.writes)}, overlapping: {spi.overlapping_writes}"
    )
    print()
    print("| t (s) | event |")
    print("|---|---|")
    for at, event in sorted(events, key=lambda e: e[0]):
        print(f"| {at:6.3f} | {event} |")


if __name__ == "__main__":
    main()
