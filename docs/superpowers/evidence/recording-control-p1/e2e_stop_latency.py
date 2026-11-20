"""End-to-end measurement of how long a SIGTERM takes to stop a recording.

Simulated hardware only; nothing here needs the Pi or Supabase. What is real
is everything the latency depends on: a child process standing in for
``rpicam-vid``, real POSIX signals, the daemon's own SIGTERM handler shape,
``run_recording_loop`` on the main thread, and the real ffmpeg mux of whatever
the cut-short capture left behind.

The camera stand-in is a script that behaves the way ``rpicam-vid -s`` is
documented to: it encodes real H.264 in real time for its ``-t`` duration and,
on SIGUSR2, finalises the file and exits 0. It encodes through ffmpeg and
finalises by passing ffmpeg a SIGINT, which is ffmpeg's own "stop and close the
file" signal.

Both runners are then measured against it:

- ``BlockingCaptureRunner`` is what the daemon used before this change - a
  blocking ``subprocess.run(check=True)`` that cannot observe a stop, so the
  stop waits for the segment to end.
- ``SubprocessInterruptibleRunner`` is firmware's own new runner.

Run from firmware/:

    uv run --locked --extra dev python \
        ../docs/superpowers/evidence/recording-control-p1/e2e_stop_latency.py
"""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from visio_recorder.battery import BatteryState
from visio_recorder.capture import (
    CaptureExit,
    SubprocessInterruptibleRunner,
    VideoSettings,
)
from visio_recorder.daemon import LoopDeps, run_recording_loop
from visio_recorder.led import LedPattern
from visio_recorder.muxer import SubprocessCommandRunner
from visio_recorder.recording_loop import ShutilDiskStatsReader
from visio_recorder.upload_limit import FfprobeDurationProbe, UploadLimit


class NeverPowerOff:
    """A battery that never halts never asks; this would fail loudly if it did."""

    def power_off(self, *, wake_on_charge_pct: int) -> None:
        raise AssertionError("a steady battery must never power the pendant off")


SEGMENT_MS = 8000
SIGTERM_AFTER_SEC = 1.5
# Well past the segment, so a stop that waits for one is measured, not timed out.
PATIENCE_SEC = 60.0

CAMERA_SHIM = """
import signal, subprocess, sys

args = sys.argv[1:]
output = args[args.index("-o") + 1]
seconds = int(args[args.index("-t") + 1]) / 1000
width = args[args.index("--width") + 1]
height = args[args.index("--height") + 1]
framerate = args[args.index("--framerate") + 1]
bitrate = args[args.index("--bitrate") + 1]
assert "-s" in args, "the daemon must ask for signal handling"

# -re paces the encode at the frame rate, so a capture cut off after N
# seconds holds N seconds of video, exactly like a camera.
ffmpeg = subprocess.Popen([
    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
    "-re", "-f", "lavfi",
    "-i", f"testsrc2=size={width}x{height}:rate={framerate}",
    "-t", str(seconds),
    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    "-b:v", bitrate, "-f", "h264", output,
])


finalising = False


def finalise(*_):
    # SIGUSR2 is rpicam-vid's documented "quit"; SIGINT is ffmpeg's. The
    # handler only sends it: waiting for ffmpeg in here would block the main
    # thread inside its own wait() and the shim would never exit - the same
    # trap the daemon's runner avoids by polling instead of blocking.
    global finalising
    finalising = True
    ffmpeg.send_signal(signal.SIGINT)


signal.signal(signal.SIGUSR2, finalise)
while True:
    try:
        returncode = ffmpeg.wait(timeout=0.1)
        break
    except subprocess.TimeoutExpired:
        continue
sys.exit(0 if finalising else returncode)
"""


class BlockingCaptureRunner:
    """The runner the daemon had before this change: a stop cannot reach it."""

    def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
        subprocess.run(args, check=True)
        return CaptureExit(returncode=0, cancelled=False)


class QuietFfmpegRunner(SubprocessCommandRunner):
    """The real mux, minus ffmpeg's banner on this script's output."""

    def run(self, args: list[str]) -> None:
        super().run([args[0], "-hide_banner", "-loglevel", "error", *args[1:]])


class CollectingStorage:
    """Keeps a copy of every uploaded segment so it can be probed afterwards."""

    def __init__(self, kept_dir: Path) -> None:
        self.kept_dir = kept_dir
        self.kept_dir.mkdir(parents=True, exist_ok=True)
        self.uploaded: list[Path] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        kept = self.kept_dir / Path(object_path).name
        shutil.copyfile(local_path, kept)
        self.uploaded.append(kept)


class SteadyBattery:
    def get_charge_pct(self) -> int:
        return 85


class SilentLed:
    def set(self, color: tuple[int, int, int], pattern: LedPattern) -> None:
        pass


class SilentStatus:
    def upsert_device_status(self, status: dict) -> None:
        pass


def _probe_duration_sec(path: Path) -> float:
    probed = subprocess.run(
        [
            "ffprobe",
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_format",
            "-print_format",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(json.loads(probed.stdout)["format"]["duration"])


def _camera_shim(workdir: Path) -> Path:
    shim = workdir / "rpicam-vid"
    shim.write_text(CAMERA_SHIM)
    return shim


def measure(runner, label: str) -> dict:
    """One stop, measured from the SIGTERM to the loop returning."""
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        shim = _camera_shim(workdir)
        data_dir = workdir / "data"
        # main() creates the data dir before recording; the loop assumes it.
        data_dir.mkdir(parents=True)
        storage = CollectingStorage(workdir / "uploads")
        stop = threading.Event()
        capture_running = threading.Event()

        class ShimmedCapture:
            """Rewrites the capture command to run the camera stand-in."""

            def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
                capture_running.set()
                return runner.run(["python3", str(shim), *args[1:]], cancel)

        deps = LoopDeps(
            command_runner=QuietFfmpegRunner(),
            capture_runner=ShimmedCapture(),
            storage_client=storage,
            status_client=SilentStatus(),
            led_driver=SilentLed(),
            battery=BatteryState(SteadyBattery()),
            power_controller=NeverPowerOff(),
            disk_stats_reader=ShutilDiskStatsReader(),
            data_dir=data_dir,
            queue_dir=data_dir / "queue",
            device_id="e2e-device",
            segment_duration_ms=SEGMENT_MS,
            video=VideoSettings(),
            upload_limit=UploadLimit(
                max_bytes=2**62,
                runner=QuietFfmpegRunner(),
                probe=FfprobeDurationProbe(),
            ),
        )

        signalled_at: list[float] = []

        def sigterm_mid_capture() -> None:
            assert capture_running.wait(timeout=PATIENCE_SEC)
            time.sleep(SIGTERM_AFTER_SEC)
            signalled_at.append(time.monotonic())
            os.kill(os.getpid(), signal.SIGTERM)

        # The daemon's handler: SIGTERM sets stop, which is also the
        # in-flight capture's cancel event.
        previous = signal.signal(signal.SIGTERM, lambda *_: stop.set())
        killer = threading.Thread(target=sigterm_mid_capture)
        try:
            killer.start()
            run_recording_loop(deps, stop)
        finally:
            killer.join(timeout=PATIENCE_SEC)
            signal.signal(signal.SIGTERM, previous)

        stop_latency_sec = time.monotonic() - signalled_at[0]
        segment = storage.uploaded[0] if storage.uploaded else None
        return {
            "label": label,
            "stop_latency_sec": stop_latency_sec,
            "segment": segment.name if segment else None,
            "segment_duration_sec": (_probe_duration_sec(segment) if segment else None),
            "leftovers": sorted(
                p.name for p in data_dir.iterdir() if p.name != "queue"
            ),
        }


def main() -> int:
    print(
        f"segment length {SEGMENT_MS / 1000:.0f}s; SIGTERM sent "
        f"{SIGTERM_AFTER_SEC:.1f}s into the first capture\n"
    )
    results = [
        measure(BlockingCaptureRunner(), "before: blocking subprocess.run"),
        measure(SubprocessInterruptibleRunner(), "after: interruptible runner"),
    ]
    print(f"{'runner':<34} {'stop latency':>13} {'segment kept':>13}")
    for result in results:
        duration = result["segment_duration_sec"]
        print(
            f"{result['label']:<34} "
            f"{result['stop_latency_sec']:>12.2f}s "
            f"{(f'{duration:.2f}s' if duration else 'none'):>13}"
        )
    print()
    for result in results:
        print(
            f"{result['label']}: uploaded {result['segment']}, "
            f"nothing left in the data dir: {not result['leftovers']}"
        )
    before, after = results
    print(
        f"\nstop latency fell from {before['stop_latency_sec']:.2f}s to "
        f"{after['stop_latency_sec']:.2f}s, and the footage recorded before "
        f"the stop ({after['segment_duration_sec']:.2f}s) still muxed, "
        f"queued and uploaded."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
