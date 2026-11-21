"""The optional ``demo.toml`` that sits beside the demo's source clips.

It says when each clip was recorded and where its flag presses were, so a
directory of ordinary video files can become a believable pendant day::

    start = "09:00"        # first clip's wall-clock start (UTC), default 09:00
    gap_minutes = 7        # between clips that carry no start, default 7

    [clips."coffee.mp4"]
    start = "09:12:30"     # optional; must not overlap the clip before it
    flags = [4.5, 30]      # seconds into the clip where the button was pressed

Clips not listed are placed in name order with the default gap and no flags.
"""

import tomllib
from dataclasses import dataclass, field
from datetime import time
from typing import Any

MANIFEST_NAME = "demo.toml"
DEFAULT_GAP_SEC = 7 * 60


@dataclass(frozen=True)
class ClipSettings:
    start: time | None = None
    flags: tuple[float, ...] = ()


@dataclass(frozen=True)
class Manifest:
    start: time | None = None
    gap_sec: int = DEFAULT_GAP_SEC
    clips: dict[str, ClipSettings] = field(default_factory=dict)


def parse_manifest(text: str) -> Manifest:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{MANIFEST_NAME}: {exc}") from exc
    _reject_unknown(data, {"start", "gap_minutes", "clips"}, MANIFEST_NAME)

    gap_minutes = data.get("gap_minutes", DEFAULT_GAP_SEC / 60)
    if not isinstance(gap_minutes, int | float) or gap_minutes < 0:
        raise ValueError(f"{MANIFEST_NAME}: gap_minutes must be a non-negative number")

    clips: dict[str, ClipSettings] = {}
    for name, settings in data.get("clips", {}).items():
        where = f"{MANIFEST_NAME} [clips.{name!r}]"
        if not isinstance(settings, dict):
            raise ValueError(f"{where}: expected a table")
        _reject_unknown(settings, {"start", "flags"}, where)
        flags = settings.get("flags", [])
        if not isinstance(flags, list) or not all(
            isinstance(f, int | float) and not isinstance(f, bool) for f in flags
        ):
            raise ValueError(f"{where}: flags must be a list of seconds")
        clips[name] = ClipSettings(
            start=_parse_time(settings["start"], where)
            if "start" in settings
            else None,
            flags=tuple(float(f) for f in flags),
        )

    return Manifest(
        start=_parse_time(data["start"], MANIFEST_NAME) if "start" in data else None,
        gap_sec=round(gap_minutes * 60),
        clips=clips,
    )


def _parse_time(value: Any, where: str) -> time:
    if not isinstance(value, str):
        raise ValueError(f'{where}: start must be a string like "09:12"')
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{where}: start {value!r} is not HH:MM or HH:MM:SS") from exc


def _reject_unknown(table: dict, known: set[str], where: str) -> None:
    unknown = sorted(set(table) - known)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}")
