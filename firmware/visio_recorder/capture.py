from datetime import datetime


def segment_filename(started_at: datetime) -> str:
    return f"{started_at.strftime('%Y%m%d_%H%M%S')}.h264"
