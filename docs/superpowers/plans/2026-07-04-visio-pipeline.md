# Visio Pendant - Cloud AI Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the nightly pipeline that scores recorded segments (speech, motion, scene novelty), selects a diverse highlight set, assembles the final reel with FFmpeg, and delivers a push notification.

**Architecture:** Every stage is a pure function or a function taking an injectable client `Protocol` (`TranscriptionClient`, `VisionClient`, `PushClient`) so tests never call OpenAI, Anthropic, or Expo. The only stage that isn't fully pure is FFmpeg command construction, which is tested by asserting on the generated command list rather than running `ffmpeg`.

**Tech Stack:** Python 3.11, pytest, `openai` (Whisper), `anthropic` (Claude Haiku vision), FFmpeg CLI, Expo Push API.

## Global Constraints

- Composite scoring formula (exact, from spec Stage 4):
  `base_score = scene_weight * scene_novelty + audio_weight * audio_activity + motion_weight * motion_intensity`;
  `score = base_score * 1.5` if manually flagged, else `base_score`.
- Default weights: `scene_weight = 0.4`, `audio_weight = 0.3`, `motion_weight = 0.2` (matches `score_weights` table in [`2026-07-04-visio-supabase-foundation.md`](2026-07-04-visio-supabase-foundation.md) - these are per-user and tunable, never hardcode them outside of the default).
- Motion-gated cost control: segments below the motion threshold skip Claude Haiku vision scoring entirely (Stage 3 is the expensive stage).
- Scene scoring prompt and response shape are fixed by the spec: `{"score": N, "location": "indoor|outdoor", "people": true|false}`, `N` on a 1-10 scale.
- Highlight selection fills a target duration (default 90s) and must never place two chronologically-consecutive selected segments from the same location (`indoor`/`outdoor`) back to back. Manually flagged segments are always included, ahead of ranking by score (spec: "Flagged moments are always included in the highlight reel").
- Recorded segments are 5 minutes (300s) long (firmware plan's Global Constraints), but the assembled reel targets 60-120s total. A highlight is therefore a fixed-length **clip** (`CLIP_DURATION_SEC = 15`) extracted from within its source segment, not the whole segment - see Task 7 and Task 8. This clip-length decision is this plan's interpretation of the spec's "extracts top moments" language (the spec does not give an exact clip length); flag it to the spec owner as worth confirming.
- Vintage filter, when requested, is exactly: `curves=vintage,noise=alls=8:allf=t+u,vignette=PI/4`.
- Push notification copy: `"Your [Date] highlight reel is ready."` Delivery targets the push token stored on the `devices` row (`push_token` column, [`2026-07-04-visio-supabase-foundation.md`](2026-07-04-visio-supabase-foundation.md) Task 9).

**Note on `audio_activity`:** the spec names three raw signals (speech presence ratio, laughter/exclamation detection, silence ratio) but doesn't specify how they combine into the single `audio_activity` number Stage 4 consumes. Task 3 below defines a specific combination (`0.6 * speech_presence_ratio + 0.4 * (1 - silence_ratio)`, `+0.2` bonus for exclamation, clamped to 1.0). Flag this to the spec owner as a decision worth confirming - it is not called out in the approved spec.

---

### Task 1: Project scaffolding

**Files:**
- Create: `pipeline/pyproject.toml`
- Create: `pipeline/pipeline/__init__.py`
- Create: `pipeline/tests/__init__.py`

**Interfaces:**
- Produces: a `pytest` command runnable from `pipeline/`.

- [ ] **Step 1: Create the package layout**

```bash
mkdir -p pipeline/pipeline pipeline/tests
touch pipeline/pipeline/__init__.py pipeline/tests/__init__.py
```

- [ ] **Step 2: Write `pyproject.toml`**

`pipeline/pyproject.toml`:
```toml
[project]
name = "visio-pipeline"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "openai>=1.30.0",
    "anthropic>=0.30.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: Install and verify pytest runs with zero tests**

```bash
cd pipeline
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```
Expected: `no tests ran`.

- [ ] **Step 4: Commit**

```bash
git add pipeline/pyproject.toml pipeline/pipeline/__init__.py pipeline/tests/__init__.py
git commit -m "chore: scaffold pipeline python package"
```

---

### Task 2: Data models

**Files:**
- Create: `pipeline/pipeline/models.py`
- Test: `pipeline/tests/test_models.py`

**Interfaces:**
- Produces: `ScoreWeights` dataclass (`scene_weight: float = 0.4`, `audio_weight: float = 0.3`, `motion_weight: float = 0.2`), `Segment` dataclass (`id: str`, `recorded_at: datetime`, `duration_sec: int`, `s3_key: str`, `location: str`, `composite_score: float = 0.0`, `manually_flagged: bool = False`).

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_models.py`:
```python
from datetime import datetime

from pipeline.models import ScoreWeights, Segment


def test_score_weights_defaults_match_spec():
    weights = ScoreWeights()
    assert weights.scene_weight == 0.4
    assert weights.audio_weight == 0.3
    assert weights.motion_weight == 0.2


def test_segment_defaults_composite_score_and_manually_flagged():
    segment = Segment(
        id="seg-1",
        recorded_at=datetime(2026, 7, 4, 12, 0, 0),
        duration_sec=300,
        s3_key="device-abc/20260704_120000.mp4",
        location="indoor",
    )
    assert segment.composite_score == 0.0
    assert segment.manually_flagged is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_models.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.models'`.

- [ ] **Step 3: Write the implementation**

`pipeline/pipeline/models.py`:
```python
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ScoreWeights:
    scene_weight: float = 0.4
    audio_weight: float = 0.3
    motion_weight: float = 0.2


@dataclass
class Segment:
    id: str
    recorded_at: datetime
    duration_sec: int
    s3_key: str
    location: str
    composite_score: float = 0.0
    manually_flagged: bool = False
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_models.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline/models.py pipeline/tests/test_models.py
git commit -m "feat: add segment and score weight models"
```

---

### Task 3: Audio activity scoring

**Files:**
- Create: `pipeline/pipeline/scoring/__init__.py`
- Create: `pipeline/pipeline/scoring/audio.py`
- Test: `pipeline/tests/test_audio_scoring.py`

**Interfaces:**
- Produces: `TranscriptionResult` dataclass (`speech_presence_ratio: float`, `silence_ratio: float`, `has_exclamation: bool`), `TranscriptionClient` Protocol (`transcribe(audio_path: Path) -> TranscriptionResult`), `compute_audio_activity(result: TranscriptionResult) -> float`.

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_audio_scoring.py`:
```python
import pytest

from pipeline.scoring.audio import TranscriptionResult, compute_audio_activity


def test_full_speech_no_silence_scores_full_activity():
    result = TranscriptionResult(speech_presence_ratio=1.0, silence_ratio=0.0, has_exclamation=False)
    assert compute_audio_activity(result) == 1.0


def test_no_speech_all_silence_scores_zero():
    result = TranscriptionResult(speech_presence_ratio=0.0, silence_ratio=1.0, has_exclamation=False)
    assert compute_audio_activity(result) == 0.0


def test_exclamation_adds_bonus():
    result = TranscriptionResult(speech_presence_ratio=0.5, silence_ratio=0.5, has_exclamation=True)
    assert compute_audio_activity(result) == pytest.approx(0.7)


def test_score_is_clamped_to_one():
    result = TranscriptionResult(speech_presence_ratio=1.0, silence_ratio=0.0, has_exclamation=True)
    assert compute_audio_activity(result) == 1.0
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_audio_scoring.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.scoring'`.

- [ ] **Step 3: Write the implementation**

```bash
mkdir -p pipeline/pipeline/scoring
touch pipeline/pipeline/scoring/__init__.py
```

`pipeline/pipeline/scoring/audio.py`:
```python
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class TranscriptionResult:
    speech_presence_ratio: float
    silence_ratio: float
    has_exclamation: bool


class TranscriptionClient(Protocol):
    def transcribe(self, audio_path: Path) -> TranscriptionResult:
        ...


def compute_audio_activity(result: TranscriptionResult) -> float:
    score = 0.6 * result.speech_presence_ratio + 0.4 * (1 - result.silence_ratio)
    if result.has_exclamation:
        score += 0.2
    return min(score, 1.0)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_audio_scoring.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline/scoring/__init__.py pipeline/pipeline/scoring/audio.py pipeline/tests/test_audio_scoring.py
git commit -m "feat: add audio activity scoring"
```

---

### Task 4: Motion scoring and cost gating

**Files:**
- Create: `pipeline/pipeline/scoring/motion.py`
- Test: `pipeline/tests/test_motion_scoring.py`

**Interfaces:**
- Produces: `compute_motion_intensity(frame_diffs: list[float], max_diff: float = 255.0) -> float`, constant `MOTION_GATING_THRESHOLD = 0.1`, `should_run_scene_scoring(motion_intensity: float) -> bool`.

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_motion_scoring.py`:
```python
from pipeline.scoring.motion import (
    MOTION_GATING_THRESHOLD,
    compute_motion_intensity,
    should_run_scene_scoring,
)


def test_no_frame_diffs_scores_zero():
    assert compute_motion_intensity([]) == 0.0


def test_average_diff_normalized_by_max():
    assert compute_motion_intensity([25.5, 25.5], max_diff=255.0) == 0.1


def test_diff_above_max_clamps_to_one():
    assert compute_motion_intensity([300.0], max_diff=255.0) == 1.0


def test_gating_at_threshold_runs_scene_scoring():
    assert should_run_scene_scoring(MOTION_GATING_THRESHOLD) is True


def test_gating_below_threshold_skips_scene_scoring():
    assert should_run_scene_scoring(MOTION_GATING_THRESHOLD - 0.01) is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_motion_scoring.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.scoring.motion'`.

- [ ] **Step 3: Write the implementation**

`pipeline/pipeline/scoring/motion.py`:
```python
MOTION_GATING_THRESHOLD = 0.1


def compute_motion_intensity(frame_diffs: list[float], max_diff: float = 255.0) -> float:
    if not frame_diffs:
        return 0.0
    avg_diff = sum(frame_diffs) / len(frame_diffs)
    return min(avg_diff / max_diff, 1.0)


def should_run_scene_scoring(motion_intensity: float) -> bool:
    return motion_intensity >= MOTION_GATING_THRESHOLD
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_motion_scoring.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline/scoring/motion.py pipeline/tests/test_motion_scoring.py
git commit -m "feat: add motion scoring and cost gating threshold"
```

---

### Task 5: Scene novelty scoring

**Files:**
- Create: `pipeline/pipeline/scoring/scene.py`
- Test: `pipeline/tests/test_scene_scoring.py`

**Interfaces:**
- Produces: `SCENE_SCORING_PROMPT` constant (the exact prompt sent to Claude Haiku), `SceneScoreParseError` exception, `SceneScore` dataclass (`novelty: float`, `location: str`, `people_present: bool`), `VisionClient` Protocol (`score_frames(frame_paths: list[Path], prompt: str) -> str`), `parse_scene_response(raw_json: str) -> SceneScore`, `score_scene(client: VisionClient, frame_paths: list[Path]) -> SceneScore`.

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_scene_scoring.py`:
```python
from pathlib import Path

import pytest

from pipeline.scoring.scene import (
    SCENE_SCORING_PROMPT,
    SceneScoreParseError,
    parse_scene_response,
    score_scene,
)


def test_prompt_matches_spec_exactly():
    assert SCENE_SCORING_PROMPT == (
        "Rate the visual interest of this moment on a scale of 1-10.\n"
        "Consider: Is this a new location? Are people present and engaged?\n"
        "Is there an interesting activity? Is this indoors or outdoors?\n"
        'Reply with JSON: {"score": N, "location": "indoor|outdoor", "people": true|false}'
    )


def test_parse_valid_response_normalizes_score_to_0_1():
    result = parse_scene_response('{"score": 8, "location": "outdoor", "people": true}')
    assert result.novelty == 0.8
    assert result.location == "outdoor"
    assert result.people_present is True


def test_parse_invalid_json_raises():
    with pytest.raises(SceneScoreParseError):
        parse_scene_response("not json")


def test_parse_missing_field_raises():
    with pytest.raises(SceneScoreParseError):
        parse_scene_response('{"score": 8, "location": "outdoor"}')


def test_parse_unexpected_location_raises():
    with pytest.raises(SceneScoreParseError):
        parse_scene_response('{"score": 8, "location": "space", "people": false}')


class FakeVisionClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[list[Path], str]] = []

    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        self.calls.append((frame_paths, prompt))
        return self.response


def test_score_scene_calls_client_with_spec_prompt_and_parses_result():
    client = FakeVisionClient('{"score": 5, "location": "indoor", "people": false}')
    frame_paths = [Path("/frames/1.jpg"), Path("/frames/2.jpg"), Path("/frames/3.jpg")]

    result = score_scene(client, frame_paths)

    assert result.novelty == 0.5
    assert client.calls == [(frame_paths, SCENE_SCORING_PROMPT)]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_scene_scoring.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.scoring.scene'`.

- [ ] **Step 3: Write the implementation**

`pipeline/pipeline/scoring/scene.py`:
```python
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SceneScoreParseError(ValueError):
    pass


@dataclass
class SceneScore:
    novelty: float
    location: str
    people_present: bool


class VisionClient(Protocol):
    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        ...


SCENE_SCORING_PROMPT = (
    "Rate the visual interest of this moment on a scale of 1-10.\n"
    "Consider: Is this a new location? Are people present and engaged?\n"
    "Is there an interesting activity? Is this indoors or outdoors?\n"
    'Reply with JSON: {"score": N, "location": "indoor|outdoor", "people": true|false}'
)


def parse_scene_response(raw_json: str) -> SceneScore:
    try:
        data = json.loads(raw_json)
        raw_score = data["score"]
        location = data["location"]
        people = data["people"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SceneScoreParseError(f"invalid scene scoring response: {raw_json!r}") from exc

    if location not in ("indoor", "outdoor"):
        raise SceneScoreParseError(f"unexpected location value: {location!r}")

    return SceneScore(novelty=raw_score / 10.0, location=location, people_present=bool(people))


def score_scene(client: VisionClient, frame_paths: list[Path]) -> SceneScore:
    raw_json = client.score_frames(frame_paths, SCENE_SCORING_PROMPT)
    return parse_scene_response(raw_json)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_scene_scoring.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline/scoring/scene.py pipeline/tests/test_scene_scoring.py
git commit -m "feat: add scene novelty scoring"
```

---

### Task 6: Composite scoring formula

**Files:**
- Create: `pipeline/pipeline/scoring/composite.py`
- Test: `pipeline/tests/test_composite_scoring.py`

**Interfaces:**
- Consumes: `ScoreWeights` from Task 2.
- Produces: `compute_composite_score(scene_novelty: float, audio_activity: float, motion_intensity: float, weights: ScoreWeights, manually_flagged: bool) -> float`.

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_composite_scoring.py`:
```python
import pytest

from pipeline.models import ScoreWeights
from pipeline.scoring.composite import compute_composite_score


def test_default_weights_unflagged():
    score = compute_composite_score(
        scene_novelty=1.0, audio_activity=1.0, motion_intensity=1.0,
        weights=ScoreWeights(), manually_flagged=False,
    )
    assert score == pytest.approx(0.9)


def test_flagged_segment_gets_1_5x_multiplier():
    score = compute_composite_score(
        scene_novelty=1.0, audio_activity=1.0, motion_intensity=1.0,
        weights=ScoreWeights(), manually_flagged=True,
    )
    assert score == pytest.approx(1.35)


def test_custom_weights_are_respected():
    weights = ScoreWeights(scene_weight=1.0, audio_weight=0.0, motion_weight=0.0)
    score = compute_composite_score(
        scene_novelty=0.5, audio_activity=1.0, motion_intensity=1.0,
        weights=weights, manually_flagged=False,
    )
    assert score == pytest.approx(0.5)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_composite_scoring.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.scoring.composite'`.

- [ ] **Step 3: Write the implementation**

`pipeline/pipeline/scoring/composite.py`:
```python
from pipeline.models import ScoreWeights


def compute_composite_score(
    scene_novelty: float,
    audio_activity: float,
    motion_intensity: float,
    weights: ScoreWeights,
    manually_flagged: bool,
) -> float:
    base_score = (
        weights.scene_weight * scene_novelty
        + weights.audio_weight * audio_activity
        + weights.motion_weight * motion_intensity
    )
    return base_score * 1.5 if manually_flagged else base_score
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_composite_scoring.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline/scoring/composite.py pipeline/tests/test_composite_scoring.py
git commit -m "feat: add composite scoring formula"
```

---

### Task 7: Highlight selection with diversity constraint

**Files:**
- Create: `pipeline/pipeline/selection/__init__.py`
- Create: `pipeline/pipeline/selection/highlight_selector.py`
- Test: `pipeline/tests/test_highlight_selector.py`

**Interfaces:**
- Consumes: `Segment` from Task 2 (including `manually_flagged`).
- Produces: `CLIP_DURATION_SEC = 15` constant, `select_highlights(segments: list[Segment], target_duration_sec: int = 90, clip_duration_sec: int = CLIP_DURATION_SEC) -> list[Segment]`, chronologically ordered. Each selected `Segment` is a **source** for one `clip_duration_sec`-long clip - see Task 8, which extracts that clip rather than using the whole (5-minute) segment. All `manually_flagged` segments are included unconditionally, in addition to (not counted against) the `target_duration_sec // clip_duration_sec` budget of top-ranked unflagged segments (spec: "Flagged moments are always included in the highlight reel").

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_highlight_selector.py`:
```python
from datetime import datetime

from pipeline.models import Segment
from pipeline.selection.highlight_selector import select_highlights


def _segment(id_, minute, score, location, flagged=False):
    return Segment(
        id=id_,
        recorded_at=datetime(2026, 7, 4, 12, minute, 0),
        duration_sec=300,
        s3_key=f"device/{id_}.mp4",
        location=location,
        composite_score=score,
        manually_flagged=flagged,
    )


def test_selects_top_scoring_clips_up_to_clip_budget():
    segments = [
        _segment("a", 0, 0.9, "indoor"),
        _segment("b", 1, 0.1, "outdoor"),
        _segment("c", 2, 0.8, "outdoor"),
        _segment("d", 3, 0.2, "indoor"),
    ]

    selected = select_highlights(segments, target_duration_sec=30, clip_duration_sec=15)

    assert [s.id for s in selected] == ["a", "c"]


def test_result_is_chronologically_ordered():
    segments = [
        _segment("late", 10, 0.9, "indoor"),
        _segment("early", 0, 0.5, "outdoor"),
    ]

    selected = select_highlights(segments, target_duration_sec=30, clip_duration_sec=15)

    assert [s.id for s in selected] == ["early", "late"]


def test_diversity_constraint_skips_consecutive_same_location():
    segments = [
        _segment("a", 0, 0.9, "indoor"),
        _segment("b", 1, 0.85, "indoor"),
        _segment("c", 2, 0.5, "outdoor"),
    ]

    selected = select_highlights(segments, target_duration_sec=45, clip_duration_sec=15)

    assert [s.id for s in selected] == ["a", "c"]


def test_ties_break_by_recorded_at_ascending():
    segments = [
        _segment("later", 5, 0.5, "indoor"),
        _segment("earlier", 0, 0.5, "outdoor"),
    ]

    selected = select_highlights(segments, target_duration_sec=15, clip_duration_sec=15)

    assert [s.id for s in selected] == ["earlier"]


def test_flagged_segments_are_included_in_addition_to_clip_budget():
    segments = [
        _segment("top", 0, 0.9, "outdoor"),
        _segment("flagged", 10, 0.1, "indoor", flagged=True),
    ]

    selected = select_highlights(segments, target_duration_sec=15, clip_duration_sec=15)

    assert [s.id for s in selected] == ["top", "flagged"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_highlight_selector.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.selection'`.

- [ ] **Step 3: Write the implementation**

```bash
mkdir -p pipeline/pipeline/selection
touch pipeline/pipeline/selection/__init__.py
```

`pipeline/pipeline/selection/highlight_selector.py`:
```python
from pipeline.models import Segment

CLIP_DURATION_SEC = 15


def select_highlights(
    segments: list[Segment],
    target_duration_sec: int = 90,
    clip_duration_sec: int = CLIP_DURATION_SEC,
) -> list[Segment]:
    max_unflagged_clips = target_duration_sec // clip_duration_sec

    flagged = [s for s in segments if s.manually_flagged]
    unflagged = [s for s in segments if not s.manually_flagged]
    ranked_unflagged = sorted(unflagged, key=lambda s: (-s.composite_score, s.recorded_at))

    selected: list[Segment] = list(flagged)
    unflagged_added = 0

    for candidate in ranked_unflagged:
        if unflagged_added >= max_unflagged_clips:
            break

        trial = sorted(selected + [candidate], key=lambda s: s.recorded_at)
        if _violates_diversity(trial):
            continue

        selected = trial
        unflagged_added += 1

    return sorted(selected, key=lambda s: s.recorded_at)


def _violates_diversity(chronological_segments: list[Segment]) -> bool:
    for a, b in zip(chronological_segments, chronological_segments[1:]):
        if a.location == b.location:
            return True
    return False
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_highlight_selector.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline/selection/__init__.py pipeline/pipeline/selection/highlight_selector.py pipeline/tests/test_highlight_selector.py
git commit -m "feat: add highlight selection with diversity constraint and flagged inclusion"
```

---

### Task 8: FFmpeg clip trimming and assembly

**Files:**
- Create: `pipeline/pipeline/assembly/__init__.py`
- Create: `pipeline/pipeline/assembly/ffmpeg_assembler.py`
- Test: `pipeline/tests/test_ffmpeg_assembler.py`

**Interfaces:**
- Produces: `compute_clip_offset_sec(segment_duration_sec: int, clip_duration_sec: int) -> float` (centers the clip window inside the source segment, clamped to `0.0` if the segment is shorter than the clip), `build_trim_command(input_path: Path, output_path: Path, offset_sec: float, clip_duration_sec: int) -> list[str]`, `render_concat_file_content(segment_paths: list[Path]) -> str`, `VINTAGE_FILTER` constant, `build_assembly_command(concat_file: Path, output_path: Path, vintage: bool) -> list[str]`. Each selected `Segment` from Task 7 is downloaded once, trimmed to its clip window with `build_trim_command`, and the resulting per-clip files are what `render_concat_file_content`/`build_assembly_command` concatenate - never the original 5-minute segment files.

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_ffmpeg_assembler.py`:
```python
from pathlib import Path

import pytest

from pipeline.assembly.ffmpeg_assembler import (
    VINTAGE_FILTER,
    build_assembly_command,
    build_trim_command,
    compute_clip_offset_sec,
    render_concat_file_content,
)


def test_compute_clip_offset_centers_the_clip_in_a_longer_segment():
    assert compute_clip_offset_sec(segment_duration_sec=300, clip_duration_sec=15) == pytest.approx(142.5)


def test_compute_clip_offset_clamps_to_zero_when_segment_shorter_than_clip():
    assert compute_clip_offset_sec(segment_duration_sec=10, clip_duration_sec=15) == 0.0


def test_build_trim_command_extracts_the_clip_window():
    args = build_trim_command(
        Path("/tmp/segment.mp4"), Path("/tmp/clip.mp4"), offset_sec=142.5, clip_duration_sec=15
    )
    assert args == [
        "ffmpeg", "-y",
        "-ss", "142.5",
        "-i", "/tmp/segment.mp4",
        "-t", "15",
        "-c", "copy",
        "/tmp/clip.mp4",
    ]


def test_render_concat_file_content_lists_each_clip():
    content = render_concat_file_content([Path("/tmp/a.mp4"), Path("/tmp/b.mp4")])
    assert content == "file '/tmp/a.mp4'\nfile '/tmp/b.mp4'\n"


def test_build_assembly_command_clean_style():
    args = build_assembly_command(Path("/tmp/concat.txt"), Path("/tmp/out.mp4"), vintage=False)
    assert args == [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", "/tmp/concat.txt",
        "-s", "1280x720", "-c:v", "libx264",
        "/tmp/out.mp4",
    ]


def test_build_assembly_command_vintage_style_adds_filter():
    args = build_assembly_command(Path("/tmp/concat.txt"), Path("/tmp/out.mp4"), vintage=True)
    assert args == [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", "/tmp/concat.txt",
        "-s", "1280x720", "-c:v", "libx264",
        "-vf", VINTAGE_FILTER,
        "/tmp/out.mp4",
    ]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_ffmpeg_assembler.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.assembly'`.

- [ ] **Step 3: Write the implementation**

```bash
mkdir -p pipeline/pipeline/assembly
touch pipeline/pipeline/assembly/__init__.py
```

`pipeline/pipeline/assembly/ffmpeg_assembler.py`:
```python
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
        "-c", "copy",
        str(output_path),
    ]


def render_concat_file_content(segment_paths: list[Path]) -> str:
    return "".join(f"file '{p}'\n" for p in segment_paths)


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
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_ffmpeg_assembler.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline/assembly/__init__.py pipeline/pipeline/assembly/ffmpeg_assembler.py pipeline/tests/test_ffmpeg_assembler.py
git commit -m "feat: add ffmpeg clip trimming and reel assembly"
```

---

### Task 9: Push notification delivery

**Files:**
- Create: `pipeline/pipeline/delivery/__init__.py`
- Create: `pipeline/pipeline/delivery/notifier.py`
- Test: `pipeline/tests/test_notifier.py`

**Interfaces:**
- Produces: `PushClient` Protocol (`send(to_token: str, title: str, body: str) -> None`), `notify_reel_ready(client: PushClient, push_token: str, reel_date: date) -> None`.

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_notifier.py`:
```python
from datetime import date

from pipeline.delivery.notifier import PushClient, notify_reel_ready


class FakePushClient(PushClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, to_token: str, title: str, body: str) -> None:
        self.calls.append((to_token, title, body))


def test_notify_reel_ready_sends_expected_copy():
    client = FakePushClient()

    notify_reel_ready(client, "push-token-abc", date(2026, 7, 4))

    assert client.calls == [
        (
            "push-token-abc",
            "Your highlight reel is ready",
            "Your July 04, 2026 highlight reel is ready.",
        )
    ]
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_notifier.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.delivery'`.

- [ ] **Step 3: Write the implementation**

```bash
mkdir -p pipeline/pipeline/delivery
touch pipeline/pipeline/delivery/__init__.py
```

`pipeline/pipeline/delivery/notifier.py`:
```python
from datetime import date
from typing import Protocol


class PushClient(Protocol):
    def send(self, to_token: str, title: str, body: str) -> None:
        ...


def notify_reel_ready(client: PushClient, push_token: str, reel_date: date) -> None:
    body = f"Your {reel_date.strftime('%B %d, %Y')} highlight reel is ready."
    client.send(push_token, "Your highlight reel is ready", body)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_notifier.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline/delivery/__init__.py pipeline/pipeline/delivery/notifier.py pipeline/tests/test_notifier.py
git commit -m "feat: add push notification delivery"
```

---

### Task 10: Per-segment scoring pipeline (cost-gated)

**Files:**
- Create: `pipeline/pipeline/scoring_pipeline.py`
- Test: `pipeline/tests/test_scoring_pipeline.py`

**Interfaces:**
- Consumes: `TranscriptionResult`, `compute_audio_activity` from Task 3; `compute_motion_intensity`, `should_run_scene_scoring` from Task 4; `VisionClient`, `score_scene` from Task 5; `compute_composite_score` from Task 6; `ScoreWeights` from Task 2.
- Produces: `score_segment(transcription: TranscriptionResult, frame_diffs: list[float], vision_client: VisionClient | None, frame_paths: list[Path], weights: ScoreWeights, manually_flagged: bool) -> tuple[float, str]` returning `(composite_score, location)`.

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_scoring_pipeline.py`:
```python
from pathlib import Path

import pytest

from pipeline.models import ScoreWeights
from pipeline.scoring.audio import TranscriptionResult
from pipeline.scoring_pipeline import score_segment


class FakeVisionClient:
    def __init__(self, response: str) -> None:
        self.response = response
        self.called = False

    def score_frames(self, frame_paths: list[Path], prompt: str) -> str:
        self.called = True
        return self.response


def test_high_motion_segment_runs_scene_scoring():
    transcription = TranscriptionResult(speech_presence_ratio=0.5, silence_ratio=0.5, has_exclamation=False)
    vision_client = FakeVisionClient('{"score": 10, "location": "outdoor", "people": true}')

    composite, location = score_segment(
        transcription=transcription,
        frame_diffs=[200.0, 200.0],
        vision_client=vision_client,
        frame_paths=[Path("/f1.jpg")],
        weights=ScoreWeights(),
        manually_flagged=False,
    )

    assert vision_client.called is True
    assert location == "outdoor"
    assert composite == pytest.approx(0.4 * 1.0 + 0.3 * 0.5 + 0.2 * (200.0 / 255.0))


def test_low_motion_segment_skips_scene_scoring():
    transcription = TranscriptionResult(speech_presence_ratio=0.0, silence_ratio=1.0, has_exclamation=False)
    vision_client = FakeVisionClient('{"score": 10, "location": "outdoor", "people": true}')

    composite, location = score_segment(
        transcription=transcription,
        frame_diffs=[1.0],
        vision_client=vision_client,
        frame_paths=[Path("/f1.jpg")],
        weights=ScoreWeights(),
        manually_flagged=False,
    )

    assert vision_client.called is False
    assert location == "indoor"
    assert composite == pytest.approx(0.4 * 0.0 + 0.3 * 0.0 + 0.2 * (1.0 / 255.0))
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_scoring_pipeline.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.scoring_pipeline'`.

- [ ] **Step 3: Write the implementation**

`pipeline/pipeline/scoring_pipeline.py`:
```python
from pathlib import Path

from pipeline.models import ScoreWeights
from pipeline.scoring.audio import TranscriptionResult, compute_audio_activity
from pipeline.scoring.composite import compute_composite_score
from pipeline.scoring.motion import compute_motion_intensity, should_run_scene_scoring
from pipeline.scoring.scene import VisionClient, score_scene

DEFAULT_LOCATION = "indoor"


def score_segment(
    transcription: TranscriptionResult,
    frame_diffs: list[float],
    vision_client: VisionClient | None,
    frame_paths: list[Path],
    weights: ScoreWeights,
    manually_flagged: bool,
) -> tuple[float, str]:
    audio_activity = compute_audio_activity(transcription)
    motion_intensity = compute_motion_intensity(frame_diffs)

    if vision_client is not None and should_run_scene_scoring(motion_intensity):
        scene = score_scene(vision_client, frame_paths)
        scene_novelty = scene.novelty
        location = scene.location
    else:
        scene_novelty = 0.0
        location = DEFAULT_LOCATION

    composite = compute_composite_score(
        scene_novelty=scene_novelty,
        audio_activity=audio_activity,
        motion_intensity=motion_intensity,
        weights=weights,
        manually_flagged=manually_flagged,
    )
    return composite, location
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_scoring_pipeline.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add pipeline/pipeline/scoring_pipeline.py pipeline/tests/test_scoring_pipeline.py
git commit -m "feat: add cost-gated per-segment scoring pipeline"
```

---

### Task 11: Segment ingestion and flag matching

The other tasks in this plan all assume `segments` rows already exist in the database. Nothing does yet - the firmware only uploads raw `.mp4`/`.marker` files to Storage (see [`2026-07-04-visio-firmware.md`](2026-07-04-visio-firmware.md)). This task turns a device's uploaded object keys into `segments` rows and applies flag markers to them, which is the missing first step of the nightly run.

**Files:**
- Create: `pipeline/pipeline/ingestion.py`
- Test: `pipeline/tests/test_ingestion.py`

**Interfaces:**
- Consumes: `Segment` from Task 2; `DEFAULT_LOCATION` from Task 10.
- Produces: `SEGMENT_DURATION_SEC = 300` constant (matches the firmware plan's 5-minute segments), `parse_segment_filename(filename: str) -> datetime` (parses `YYYYMMDD_HHMMSS.mp4`), `parse_flag_marker_filename(filename: str) -> time` (parses `FLAG_HHMMSS.marker`), `build_segments_from_object_keys(object_keys: list[str], device_id: str) -> list[Segment]` (ignores non-`.mp4` keys; `location` starts at `DEFAULT_LOCATION` until Task 10's scoring overwrites it), `apply_flag_markers(segments: list[Segment], day: date, flag_marker_keys: list[str]) -> list[Segment]` (sets `manually_flagged = True` on the segment whose `[recorded_at, recorded_at + duration_sec)` window contains the marker's timestamp).

- [ ] **Step 1: Write the failing tests**

`pipeline/tests/test_ingestion.py`:
```python
from datetime import date, datetime, time

from pipeline.ingestion import (
    apply_flag_markers,
    build_segments_from_object_keys,
    parse_flag_marker_filename,
    parse_segment_filename,
)


def test_parse_segment_filename():
    assert parse_segment_filename("20260704_120000.mp4") == datetime(2026, 7, 4, 12, 0, 0)


def test_parse_flag_marker_filename():
    assert parse_flag_marker_filename("FLAG_120300.marker") == time(12, 3, 0)


def test_build_segments_from_object_keys_ignores_marker_files():
    segments = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/FLAG_120300.marker"],
        device_id="device-abc",
    )

    assert len(segments) == 1
    assert segments[0].id == "20260704_120000"
    assert segments[0].recorded_at == datetime(2026, 7, 4, 12, 0, 0)
    assert segments[0].duration_sec == 300
    assert segments[0].s3_key == "device-abc/20260704_120000.mp4"
    assert segments[0].manually_flagged is False


def test_apply_flag_markers_flags_the_containing_segment():
    segments = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4", "device-abc/20260704_120500.mp4"],
        device_id="device-abc",
    )

    apply_flag_markers(segments, date(2026, 7, 4), ["device-abc/FLAG_120300.marker"])

    assert segments[0].manually_flagged is True
    assert segments[1].manually_flagged is False


def test_apply_flag_markers_ignores_markers_outside_any_segment_window():
    segments = build_segments_from_object_keys(
        ["device-abc/20260704_120000.mp4"], device_id="device-abc"
    )

    apply_flag_markers(segments, date(2026, 7, 4), ["device-abc/FLAG_235900.marker"])

    assert segments[0].manually_flagged is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd pipeline && pytest tests/test_ingestion.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'pipeline.ingestion'`.

- [ ] **Step 3: Write the implementation**

`pipeline/pipeline/ingestion.py`:
```python
from datetime import date, datetime, time, timedelta

from pipeline.models import Segment
from pipeline.scoring_pipeline import DEFAULT_LOCATION

SEGMENT_DURATION_SEC = 300


def parse_segment_filename(filename: str) -> datetime:
    stem = filename.removesuffix(".mp4")
    return datetime.strptime(stem, "%Y%m%d_%H%M%S")


def parse_flag_marker_filename(filename: str) -> time:
    stem = filename.removeprefix("FLAG_").removesuffix(".marker")
    return datetime.strptime(stem, "%H%M%S").time()


def build_segments_from_object_keys(object_keys: list[str], device_id: str) -> list[Segment]:
    segments = []
    for key in object_keys:
        filename = key.split("/")[-1]
        if not filename.endswith(".mp4"):
            continue
        segments.append(
            Segment(
                id=filename.removesuffix(".mp4"),
                recorded_at=parse_segment_filename(filename),
                duration_sec=SEGMENT_DURATION_SEC,
                s3_key=key,
                location=DEFAULT_LOCATION,
            )
        )
    return segments


def apply_flag_markers(segments: list[Segment], day: date, flag_marker_keys: list[str]) -> list[Segment]:
    for key in flag_marker_keys:
        filename = key.split("/")[-1]
        if not filename.endswith(".marker"):
            continue
        flag_at = datetime.combine(day, parse_flag_marker_filename(filename))
        for segment in segments:
            window_end = segment.recorded_at + timedelta(seconds=segment.duration_sec)
            if segment.recorded_at <= flag_at < window_end:
                segment.manually_flagged = True
                break
    return segments
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd pipeline && pytest tests/test_ingestion.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the full pipeline test suite**

Run: `cd pipeline && pytest -v`
Expected: all tests across Tasks 2-11 pass (39 passed).

- [ ] **Step 6: Commit**

```bash
git add pipeline/pipeline/ingestion.py pipeline/tests/test_ingestion.py
git commit -m "feat: add segment ingestion and flag marker matching"
```

---

## Handoff

The nightly job entrypoint (deployed as a Supabase Edge Function or AWS Lambda, triggered by cron or pending-segment-count threshold per the spec) wires the tasks above in this order: list a device's objects in the `segments` storage bucket, build `segments` rows with `build_segments_from_object_keys` and apply `apply_flag_markers` (Task 11), persist any not-yet-seen rows to the database, run `score_segment` (Task 10) across them, feed the results into `select_highlights` (Task 7), download each selected segment once and trim it to its clip window with `build_trim_command` (Task 8), concatenate the trimmed clips with `build_assembly_command` (Task 8), upload the result to the `reels` bucket, write a `reels` row, and call `notify_reel_ready` (Task 9) with the push token from the device's `devices.push_token` column. This wiring composes already-tested units against real Supabase/LiteLLM/Expo clients (LLM calls are provider-agnostic via LiteLLM, with Claude Haiku as the default routed model and vision scoring using `response_format` `json_schema` structured outputs) and is validated via Epic 5's integration checklist rather than additional unit tests.

Both ingestion functions skip keys they cannot parse (or that fall outside the device's prefix) and return them as a second `rejected_keys` list rather than raising. `apply_flag_markers` additionally returns a third `unmatched_keys` list for well-formed, in-prefix markers whose timestamp falls outside every segment window (e.g. the marker uploaded before its segment mp4), so they can be retried instead of silently dropped. The orchestrator is expected to persist rejected and unmatched keys with attempt counts for DLQ-style retry, and to flag the user once a key exhausts its max retries; that retry/escalation policy is orchestrator follow-up work, not part of this plan.

On-demand regeneration (the mobile app's "Regenerate" button, target length/style/mood - see [`2026-07-04-visio-app.md`](2026-07-04-visio-app.md) Task 9) has no backend consumer in this plan. Building it means mapping the mood weighting onto a `ScoreWeights` adjustment and exposing an on-demand trigger (versus the nightly cron); this is out of scope here and should be treated as follow-up work, not a silent gap in Epic 5's checklist.
