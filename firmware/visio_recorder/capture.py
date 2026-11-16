import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from visio_recorder.muxer import CommandRunner


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
    return command + ["-n", "-o", str(output_path)]


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


def record_segment(
    runner: CommandRunner,
    output_dir: Path,
    started_at: "datetime",
    duration_ms: int,
    video: VideoSettings,
    audio: AudioSettings | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".h264" if audio is None else ".ts"
    output_path = output_dir / segment_filename(started_at, suffix)
    runner.run(build_rpicam_command(output_path, duration_ms, video, audio))
    return output_path
