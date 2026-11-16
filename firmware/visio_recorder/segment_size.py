"""How long a segment may be so that its file fits under the upload limit.

The hosted Supabase project rejects any object over 50 MiB (free plan, cannot
be raised), so every segment file must stay below that or it can never be
uploaded. A segment's size is set by the H.264 bitrate the firmware passes
``rpicam-vid`` (``--bitrate``), not by resolution: the encoder spends the
bitrate it is given. So size is predicted from the configured bitrate:

    bytes ~= (video_bps + audio_bps) / 8 * seconds * (1 + container overhead)

and the default segment length is the longest whole number of ten-second steps
whose predicted size uses at most ``SIZE_BUDGET_FRACTION`` of the limit. The
remaining third is headroom for the Pi's hardware encoder, which rate-controls
towards the target (variable bitrate by default) and can overshoot it on
high-motion stretches. A file that still ends up over the limit is split before
upload (see ``visio_recorder.upload_limit``), so the budget only has to make
splitting rare, not impossible.
"""

import math

# The hosted free plan's per-object limit, 50 MiB as storage-api counts it.
DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
# Bitrate reserved for an audio track (an AAC microphone track is 64-128 kbps)
# so adding audio never pushes a default-length segment over budget.
AUDIO_BITRATE_RESERVE_BPS = 128_000
# MP4 container overhead (moov index, sample tables) on top of the raw streams.
CONTAINER_OVERHEAD = 0.02
# Predicted size may use this much of the limit; the rest is encoder headroom.
SIZE_BUDGET_FRACTION = 2 / 3
SEGMENT_STEP_MS = 10_000
# Never longer than the original five-minute segment, even under a raised
# limit: shorter segments also bound what an unclean stop can lose.
MAX_SEGMENT_DURATION_MS = 300_000


def predicted_segment_bytes(
    duration_ms: int,
    video_bitrate_bps: int,
    audio_bitrate_bps: int = AUDIO_BITRATE_RESERVE_BPS,
) -> int:
    """Expected file size of a segment recorded at the given bitrates."""
    bytes_per_sec = (video_bitrate_bps + audio_bitrate_bps) / 8
    return math.ceil(bytes_per_sec * duration_ms / 1000 * (1 + CONTAINER_OVERHEAD))


def default_segment_duration_ms(
    max_upload_bytes: int,
    video_bitrate_bps: int,
    audio_bitrate_bps: int = AUDIO_BITRATE_RESERVE_BPS,
) -> int:
    """Longest segment, in whole ten-second steps, that fits the size budget."""
    budget = max_upload_bytes * SIZE_BUDGET_FRACTION
    per_step = predicted_segment_bytes(
        SEGMENT_STEP_MS, video_bitrate_bps, audio_bitrate_bps
    )
    steps = int(budget // per_step)
    if steps < 1:
        raise ValueError(
            f"VISIO_VIDEO_BITRATE={video_bitrate_bps} is too high for even a "
            f"{SEGMENT_STEP_MS // 1000} s segment under "
            f"VISIO_MAX_UPLOAD_BYTES={max_upload_bytes}"
        )
    return min(steps * SEGMENT_STEP_MS, MAX_SEGMENT_DURATION_MS)
