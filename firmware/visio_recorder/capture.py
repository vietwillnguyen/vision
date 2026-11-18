import logging
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from visio_recorder.muxer import CommandRunner

_logger = logging.getLogger(__name__)

# How often a running capture is checked on, which is also how long a stop
# can wait before it is acted on. Short enough to be invisible next to the
# shutdown budget, long enough not to spin on a battery.
CAPTURE_POLL_SEC = 0.2

# How long a cancelled capture is given to finalise its file after each
# signal before the next, harsher one. The whole escalation is bounded by
# twice this plus the poll, which has to fit inside the unit's
# TimeoutStopSec=30 alongside the mux, the enqueue and the status push.
CAPTURE_SIGNAL_GRACE_SEC = 5.0


@dataclass(frozen=True)
class VideoSettings:
    """What ``rpicam-vid`` is told to record; nothing is left to its defaults.

    rpicam-apps defaults to 640x480 when no size is given, and with no
    ``--bitrate`` the Pi's V4L2 H.264 encoder uses its own 10 Mbps default.
    Segment size is bitrate x duration, so the bitrate is pinned here and the
    segment length is derived from it (see ``visio_recorder.segment_size``).
    The defaults match the design spec's 720p/30fps; 4 Mbps is ample for
    hardware H.264 at 720p30 (about 0.15 bits per pixel per frame).
    """

    width: int = 1280
    height: int = 720
    framerate: int = 30
    bitrate_bps: int = 4_000_000


@dataclass(frozen=True)
class AudioSettings:
    """The microphone track recorded alongside the video.

    ``device`` is an ALSA PCM name; the default ``visio_mic`` is defined by
    ``firmware/alsa/visio-mic.conf`` (an INMP441 on the Pi's I2S pins, left
    channel only). 64 kbps mono AAC at 48 kHz is ample for speech, which is
    what the pipeline transcribes; the segment-size budget reserves 128 kbps.
    """

    device: str = "visio_mic"
    bitrate_bps: int = 64_000
    samplerate: int = 48_000
    channels: int = 1


def segment_filename(started_at: datetime, suffix: str = ".h264") -> str:
    return f"{started_at.strftime('%Y%m%d_%H%M%S')}{suffix}"


def build_rpicam_command(
    output_path: Path,
    duration_ms: int,
    video: VideoSettings,
    audio: AudioSettings | None = None,
) -> list[str]:
    """The ``rpicam-vid`` invocation for one segment.

    ``-s`` (``--signal``) is what makes a capture interruptible: with it,
    SIGUSR1 toggles the output and SIGUSR2 quits, and the documented starting
    state is recording, so a segment left alone behaves exactly as it did
    before. ``SubprocessInterruptibleRunner`` is the other half.

    Video only: the hardware H.264 encoder writes a raw elementary stream.
    With audio: the libav backend (rpicam-apps' ``--codec libav``, which still
    encodes video in hardware via ``h264_v4l2m2m``) muxes the ALSA microphone
    and the video in one pass, so the tracks share one clock and need no sync
    correction. It writes MPEG-TS rather than MP4 because a transport stream
    is still readable when the capture is killed mid-segment (MP4 writes its
    index last), so orphan recovery can remux it like a raw ``.h264``.
    """
    command = [
        "rpicam-vid",
        "-t",
        str(duration_ms),
        "--width",
        str(video.width),
        "--height",
        str(video.height),
        "--framerate",
        str(video.framerate),
        "--bitrate",
        str(video.bitrate_bps),
    ]
    if audio is None:
        command += ["--codec", "h264"]
    else:
        command += [
            "--codec",
            "libav",
            "--libav-format",
            "mpegts",
            "--libav-audio",
            "--audio-source",
            "alsa",
            "--audio-device",
            audio.device,
            "--audio-channels",
            str(audio.channels),
            "--audio-samplerate",
            str(audio.samplerate),
            "--audio-codec",
            "aac",
            "--audio-bitrate",
            str(audio.bitrate_bps),
        ]
    return command + ["-s", "-n", "-o", str(output_path)]


def probe_audio_device(runner: CommandRunner, audio: AudioSettings) -> bool:
    """Whether the ALSA capture device opens, the way rpicam-vid will open it.

    rpicam-vid's libav backend reads the microphone through ffmpeg's ALSA
    input, so a short ffmpeg capture from the same device with the same
    channel count and rate is the closest check short of recording. A missing
    or misconfigured device (no I2S overlay, mic not wired) fails here.
    """
    try:
        runner.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "alsa",
                "-channels",
                str(audio.channels),
                "-sample_rate",
                str(audio.samplerate),
                "-i",
                audio.device,
                "-t",
                "0.2",
                "-f",
                "null",
                "-",
            ]
        )
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


@dataclass(frozen=True)
class CaptureExit:
    """How one ``rpicam-vid`` run ended.

    ``cancelled`` is what separates a capture the daemon cut short from one
    that ran out its ``-t``. A runner raises for a failure nobody asked for,
    so a non-zero ``returncode`` reaching a caller always belongs to a
    cancelled capture: whether ``rpicam-vid -s`` exits 0 on SIGUSR2 is still
    unverified on the device (BENCH-1 in the recording-control design spec),
    and dropping a segment over its exit code would be worse than tolerating
    one.
    """

    returncode: int
    cancelled: bool


class InterruptibleCommandRunner(Protocol):
    """A command the caller can cut short; used only for the capture.

    It sits beside ``CommandRunner`` rather than replacing it because only
    the capture needs it. The mux (``ffmpeg -c copy``) and the ALSA probe are
    both short and end on their own, while one capture runs for a whole
    segment and is the thing a stop has to interrupt.
    """

    def run(self, args: list[str], cancel: threading.Event) -> CaptureExit: ...


class SubprocessInterruptibleRunner:
    """Runs ``rpicam-vid`` so that a stop ends the capture in seconds.

    ``--signal`` documents SIGUSR2 as "quit", which finalises the output
    file, so a cancel sends that first and escalates only if the process does
    not go away: SIGTERM after ``grace_sec``, then SIGKILL. A killed capture
    still leaves a usable file - a raw ``.h264`` has no trailer and MPEG-TS
    stays readable when truncated - but that is the last resort, not the plan.

    ``wait`` is polled instead of being waited on outright for two reasons:
    ``cancel`` is observed between polls, and a Python signal handler runs
    only on the main thread between bytecodes, so returning there regularly
    is what lets the daemon's own SIGTERM handler run while a capture is in
    flight.

    A capture that was not cancelled keeps ``subprocess.run(check=True)``'s
    contract and raises ``CalledProcessError`` on a non-zero exit, which is
    what the recording loop's audio fallback detects. A cancelled one never
    raises, however it exited.
    """

    def __init__(
        self,
        poll_sec: float = CAPTURE_POLL_SEC,
        grace_sec: float = CAPTURE_SIGNAL_GRACE_SEC,
    ) -> None:
        self._poll_sec = poll_sec
        self._grace_sec = grace_sec

    def run(self, args: list[str], cancel: threading.Event) -> CaptureExit:
        with subprocess.Popen(args) as process:
            while not cancel.is_set():
                try:
                    returncode = process.wait(timeout=self._poll_sec)
                except subprocess.TimeoutExpired:
                    continue
                if returncode != 0:
                    raise subprocess.CalledProcessError(returncode, args)
                return CaptureExit(returncode=returncode, cancelled=False)
            return CaptureExit(returncode=self._cut_short(process), cancelled=True)

    def _cut_short(self, process: "subprocess.Popen[bytes]") -> int:
        for sig in (signal.SIGUSR2, signal.SIGTERM):
            process.send_signal(sig)
            try:
                return process.wait(timeout=self._grace_sec)
            except subprocess.TimeoutExpired:
                _logger.warning(
                    "the capture is still running %.0fs after %s; escalating",
                    self._grace_sec,
                    sig.name,
                )
        process.kill()
        return process.wait()


def segment_path(
    output_dir: Path, started_at: "datetime", audio: AudioSettings | None
) -> Path:
    suffix = ".h264" if audio is None else ".ts"
    return output_dir / segment_filename(started_at, suffix)


def record_segment(
    runner: InterruptibleCommandRunner,
    output_dir: Path,
    started_at: "datetime",
    duration_ms: int,
    video: VideoSettings,
    audio: AudioSettings | None = None,
    *,
    cancel: threading.Event,
) -> Path:
    """Record one segment, returning its path whether or not it was cut short.

    Setting ``cancel`` ends the capture in seconds instead of letting it run
    out ``duration_ms``, and what it leaves behind is an ordinary, shorter
    segment: the caller muxes, queues and uploads it with no special case.
    The file can still be empty when the cancel lands before the first frame,
    which is the one case a caller has to handle.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = segment_path(output_dir, started_at, audio)
    capture_exit = runner.run(
        build_rpicam_command(output_path, duration_ms, video, audio), cancel
    )
    if capture_exit.cancelled:
        _logger.info(
            "cut the capture of %s short; rpicam-vid exited %d",
            output_path.name,
            capture_exit.returncode,
        )
    return output_path
