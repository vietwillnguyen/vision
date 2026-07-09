from datetime import datetime

from visio_recorder.flag_button import write_flag_marker


def test_write_flag_marker_creates_timestamped_file(tmp_path):
    queue_dir = tmp_path / "queue"
    pressed_at = datetime(2026, 7, 4, 12, 3, 45)

    marker_path = write_flag_marker(queue_dir, pressed_at)

    assert marker_path == queue_dir / "FLAG_20260704_120345.marker"
    assert marker_path.exists()


def test_write_flag_marker_name_matches_the_pipeline_parser_contract(tmp_path):
    # Pins the marker filename to the format the pipeline's
    # parse_flag_marker_filename hard-requires (FLAG_YYYYMMDD_HHMMSS.marker).
    pressed_at = datetime(2026, 7, 4, 12, 3, 45)

    marker_path = write_flag_marker(tmp_path / "queue", pressed_at)

    parsed = datetime.strptime(marker_path.name, "FLAG_%Y%m%d_%H%M%S.marker")
    assert parsed == pressed_at


def test_write_flag_marker_creates_queue_dir_if_missing(tmp_path):
    queue_dir = tmp_path / "does" / "not" / "exist"
    write_flag_marker(queue_dir, datetime(2026, 7, 4, 8, 0, 0))
    assert queue_dir.exists()
