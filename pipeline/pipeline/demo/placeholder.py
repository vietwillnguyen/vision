"""Clearly synthetic stand-in footage, generated with FFmpeg at seed time.

The public demo will run on clips the owner shoots himself. Until then these
moving test patterns let the whole seed path run end to end, and every frame
says what it is: a "SAMPLE FOOTAGE - synthetic placeholder" caption and a
running timecode. Nothing is committed; the clips are rebuilt on each seed.

Most patterns move enough to pass the pipeline's motion gate, two stay calm,
and one carries a tone so the audio path (transcription, the reel's mixed
track) runs too.
"""

from dataclasses import dataclass
from pathlib import Path

from pipeline.demo.footage import CommandRunner
from pipeline.demo.manifest import MANIFEST_NAME

SOURCE_SIZE = "640x360"
SOURCE_RATE = 30


@dataclass(frozen=True)
class PlaceholderClip:
    name: str
    seconds: float
    pattern: str
    sound: bool = False
    flags: tuple[float, ...] = ()


# Lengths chosen so the day has clips that split into two segments, clips
# that stay whole, and a short stop-press-shaped one; about six minutes in all.
PLACEHOLDER_CLIPS = (
    PlaceholderClip("01-test-pattern.mp4", 75, "testsrc2", flags=(20,)),
    PlaceholderClip("02-gradients.mp4", 45, "gradients", sound=True),
    PlaceholderClip("03-life.mp4", 60, "life"),
    PlaceholderClip("04-mandelbrot.mp4", 35, "mandelbrot", flags=(12,)),
    PlaceholderClip("05-cellular.mp4", 80, "cellauto"),
    PlaceholderClip("06-bars.mp4", 25, "bars"),
)

# The pipeline measures motion on 160x90 grayscale frames a second apart, so
# fine detail averages out to grey: the busy patterns move large shapes (slow
# scrolls, chunky cells) to clear its motion gate, and the two calm ones stay
# under it, as a quiet moment would. Nothing strobes.
PATTERNS = {
    "testsrc2": f"testsrc2=size={SOURCE_SIZE}:rate={SOURCE_RATE},scroll=h=0.01",
    "gradients": f"gradients=size={SOURCE_SIZE}:rate={SOURCE_RATE}:speed=0.03:n=4",
    "life": (
        "life=size=80x45:rate=8:mold=10:ratio=0.3:death_color=#1a1e26"
        ":life_color=#4f8ef7:mold_color=#3dd68c,"
        f"scale={SOURCE_SIZE.replace('x', ':')}:flags=neighbor,fps={SOURCE_RATE}"
    ),
    "mandelbrot": f"mandelbrot=size={SOURCE_SIZE}:rate={SOURCE_RATE}:end_scale=0.05",
    "cellauto": (
        "cellauto=size=80x45:rate=8:rule=30:scroll=1,"
        f"scale={SOURCE_SIZE.replace('x', ':')}:flags=neighbor,fps={SOURCE_RATE}"
    ),
    "bars": f"smptehdbars=size={SOURCE_SIZE}:rate={SOURCE_RATE},scroll=h=0.004",
}

CAPTION = "SAMPLE FOOTAGE - synthetic placeholder"


def _label(name: str) -> str:
    # drawtext's own escaping: ':' separates options and '\' escapes.
    caption = CAPTION.replace(":", r"\:")
    title = name.removesuffix(".mp4").replace(":", r"\:")
    box = "box=1:boxcolor=black@0.6:boxborderw=10:fontcolor=white"
    return (
        f"drawtext=text='{caption}':x=(w-tw)/2:y=24:fontsize=26:{box},"
        f"drawtext=text='{title}':x=(w-tw)/2:y=h-96:fontsize=20:{box},"
        f"drawtext=text='%{{pts\\:hms}}':x=(w-tw)/2:y=h-52:fontsize=20:{box}"
    )


def placeholder_command(clip: PlaceholderClip, out: Path) -> list[str]:
    video = f"{PATTERNS[clip.pattern]},format=yuv420p,{_label(clip.name)}"
    command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", video]
    if clip.sound:
        command += [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:beep_factor=3:sample_rate=48000",
        ]
        command += ["-c:a", "aac", "-b:a", "96k"]
    command += [
        "-t", f"{clip.seconds:g}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
        "-movflags", "+faststart", str(out),
    ]  # fmt: skip
    return command


def manifest_text(clips: tuple[PlaceholderClip, ...]) -> str:
    lines = [
        "# Generated with the placeholder clips; real clips get a hand-written one.",
        'start = "09:05"',
        "gap_minutes = 6",
    ]
    for clip in clips:
        if clip.flags:
            flags = ", ".join(f"{f:g}" for f in clip.flags)
            lines += ["", f'[clips."{clip.name}"]', f"flags = [{flags}]"]
    return "\n".join(lines) + "\n"


def generate_placeholder_clips(
    runner: CommandRunner,
    out_dir: Path,
    clips: tuple[PlaceholderClip, ...] = PLACEHOLDER_CLIPS,
) -> Path:
    """Write the clips and their ``demo.toml`` to ``out_dir``; returns it."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for clip in clips:
        runner.run(placeholder_command(clip, out_dir / clip.name))
    (out_dir / MANIFEST_NAME).write_text(manifest_text(clips))
    return out_dir
