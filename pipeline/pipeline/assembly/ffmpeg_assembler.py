from pathlib import Path

VINTAGE_FILTER = "curves=vintage,noise=alls=8:allf=t+u,vignette=PI/4"


def compute_clip_offset_sec(segment_duration_sec: int, clip_duration_sec: int) -> float:
    return max((segment_duration_sec - clip_duration_sec) / 2, 0.0)


def build_trim_command(
    input_path: Path, output_path: Path, offset_sec: float, clip_duration_sec: int
) -> list[str]:
    return [
        "ffmpeg", "-y",
        "-ss", str(offset_sec),
        "-i", str(input_path),
        "-t", str(clip_duration_sec),
        "-c:v", "libx264",
        str(output_path),
    ]


def render_concat_file_content(segment_paths: list[Path]) -> str:
    return "".join("file '{}'\n".format(str(p).replace("'", "'\\''")) for p in segment_paths)


def build_assembly_command(concat_file: Path, output_path: Path, vintage: bool) -> list[str]:
    args = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-s", "1280x720", "-c:v", "libx264",
    ]
    if vintage:
        args += ["-vf", VINTAGE_FILTER]
    args.append(str(output_path))
    return args
