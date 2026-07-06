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
