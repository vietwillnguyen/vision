from enum import Enum
from pathlib import Path

VINTAGE_FILTER = "curves=vintage,noise=alls=8:allf=t+u,vignette=PI/4"


def compute_clip_duration_sec(
    segment_duration_sec: float, clip_duration_sec: float
) -> float:
    """A segment shorter than a clip contributes all of itself."""
    return min(clip_duration_sec, segment_duration_sec)


def compute_clip_offset_sec(
    segment_duration_sec: float, clip_duration_sec: float
) -> float:
    return max((segment_duration_sec - clip_duration_sec) / 2, 0.0)


class ClipAudio(Enum):
    """What audio a reel clip carries.

    The concat demuxer takes a reel's stream layout from its first clip, so
    either every clip has an audio stream in the same format or none does: a
    video-only first clip drops the whole reel's audio, and a video-only clip
    later leaves a gap that players close up, playing the next clip's sound
    over this one's pictures.
    """

    NONE = "none"  # a reel with no audio anywhere: video only, as before
    SOURCE = "source"  # the segment's own microphone track
    SILENCE = "silence"  # a video-only segment in a reel that has audio


# One audio format for every clip, so concatenated clips line up.
_REEL_AUDIO = ["-c:a", "aac", "-ar", "48000", "-ac", "1"]


def build_trim_command(
    input_path: Path,
    output_path: Path,
    offset_sec: float,
    clip_duration_sec: float,
    audio: ClipAudio = ClipAudio.NONE,
) -> list[str]:
    head = ["ffmpeg", "-y", "-ss", str(offset_sec), "-i", str(input_path)]
    duration = ["-t", str(clip_duration_sec)]
    if audio is ClipAudio.NONE:
        return [*head, *duration, "-c:v", "libx264", str(output_path)]
    if audio is ClipAudio.SOURCE:
        return [
            *head,
            *duration,
            "-map", "0:v:0", "-map", "0:a:0",
            "-c:v", "libx264", *_REEL_AUDIO,
            str(output_path),
        ]  # fmt: skip
    return [
        *head,
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
        *duration,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", *_REEL_AUDIO,
        str(output_path),
    ]  # fmt: skip


def render_concat_file_content(segment_paths: list[Path]) -> str:
    return "".join(
        "file '{}'\n".format(str(p).replace("'", "'\\''")) for p in segment_paths
    )


def build_assembly_command(
    concat_file: Path, output_path: Path, vintage: bool
) -> list[str]:
    args = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-s",
        "1280x720",
        "-c:v",
        "libx264",
    ]
    if vintage:
        args += ["-vf", VINTAGE_FILTER]
    args.append(str(output_path))
    return args
