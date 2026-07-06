MOTION_GATING_THRESHOLD = 0.1


def compute_motion_intensity(frame_diffs: list[float], max_diff: float = 255.0) -> float:
    if not frame_diffs:
        return 0.0
    avg_diff = sum(frame_diffs) / len(frame_diffs)
    return min(avg_diff / max_diff, 1.0)


def should_run_scene_scoring(motion_intensity: float) -> bool:
    return motion_intensity >= MOTION_GATING_THRESHOLD
