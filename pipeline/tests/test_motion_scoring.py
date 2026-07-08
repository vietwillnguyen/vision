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
