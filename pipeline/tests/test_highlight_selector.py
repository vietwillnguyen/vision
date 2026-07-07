from datetime import datetime

from pipeline.models import Segment
from pipeline.selection.highlight_selector import select_highlights


def _segment(id_, minute, score, location, flagged=False):
    return Segment(
        id=id_,
        device_id="device",
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


def test_adjacent_same_location_flagged_segments_do_not_veto_unrelated_candidates():
    segments = [
        _segment("before", 0, 0.9, "outdoor"),
        _segment("f1", 10, 0.1, "indoor", flagged=True),
        _segment("f2", 15, 0.1, "indoor", flagged=True),
        _segment("after", 30, 0.8, "outdoor"),
    ]

    selected = select_highlights(segments, target_duration_sec=30, clip_duration_sec=15)

    assert [s.id for s in selected] == ["before", "f1", "f2", "after"]


def test_candidate_adjacent_to_same_location_flagged_segment_is_still_rejected():
    segments = [
        _segment("f1", 10, 0.1, "indoor", flagged=True),
        _segment("f2", 15, 0.1, "indoor", flagged=True),
        _segment("clash", 20, 0.9, "indoor"),
        _segment("ok", 30, 0.5, "outdoor"),
    ]

    selected = select_highlights(segments, target_duration_sec=15, clip_duration_sec=15)

    assert [s.id for s in selected] == ["f1", "f2", "ok"]
