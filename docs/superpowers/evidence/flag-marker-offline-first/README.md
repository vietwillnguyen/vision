# Local-first flag markers - end-to-end evidence

Every run here is firmware's own code.
Two pieces of hardware are simulated and nothing else: the camera is ffmpeg writing a real H.264 stream wherever `rpicam-vid` would, which the firmware's own ffmpeg runner then muxes, and `/dev/spidev0.0` is `SimulatedSpiDevice`, whose bytes are decoded back into the colours a WS2812B would latch by [`firmware/tests/ws2812_waveform.py`](../../../../firmware/tests/ws2812_waveform.py) - an independent receiver written from the datasheet timing windows rather than from the encoder.
Storage is a fake client with a switchable network, because the whole point is what the pendant does when there is no network to reach.

`run_recording_loop`, `flush_pending`, `FlagUploadWorker`, `make_flag_press_handler`, `LedController` and the real `Ws2812LedDriver` are all the shipped code.

The retry schedule is scaled 1500:1 (30 s becomes 20 ms, the 480 s cap becomes 320 ms) so a two-hour outage plays out in a few seconds, and scenario 1 prints the unscaled production schedule straight out of `RetryBackoff` beside the scaled one.
The 3 s restart gap a segment waits out before its marker flush is scaled the same way in scenarios 1-4, and runs unscaled in scenario 5, the one about it.
Nothing else is scaled.

## Before (firmware at `5073cce`, the branch point)

[`before-gaps.log`](before-gaps.log) ([`before_gaps.py`](before_gaps.py)), run against a `visio_recorder/` tree extracted from the base commit:

1. **A failed marker waited for the next boot.** One upload attempt, ever. Two seconds of outage - more than four times the 30 s a *segment* would wait before its first in-process retry, at the scale used here - produced no second attempt, and the marker sat on the card. The nightly run scored that segment unflagged.
2. **The startup flush sent segments before their flags.** A boot with four segments and two markers queued, whose battery gave out three uploads in, put three segments in storage and neither flag. `list_pending` sorts by name and digits sort before `FLAG_`, so the footage always overtook the flags.
3. **A press had no local feedback at all.** `make_flag_press_handler` took no acknowledgement hook, and `write_flag_marker` was `touch()` and return - the marker was only as durable as the page cache.

[`unit-before.txt`](unit-before.txt): this branch's tests against that same firmware. Six modules fail to collect, each on a symbol the base commit does not have (`FLAG_UPLOAD_THREAD_NAME`, `pending_markers`, `FLAG_ACK_BLINK_SEC`, `wait_out_restart_gap`, `is_flag_marker`, `LedState.FLAG_ACK`), so not one of their tests runs.

## After ([`after-scenarios.log`](after-scenarios.log), [`e2e_flag_offline.py`](e2e_flag_offline.py))

### 1. WiFi down for hours, then restored

A press 30 s into the first segment, with the network already gone, then four segments recorded into a dead network.

- The marker was retried **19 times across 2.1 device-hours** on the production curve (30 s doubling to a 480 s cap), every attempt on the `flag-upload-worker` thread.
- Nothing reached storage, and the marker stayed on the card.
- The acknowledgement blink still reached the wire: magenta decoded out of the real SPI frames with no network in the scenario at all.
- The segment failures still latched Critical red, which the blink interrupted and gave back.
- When WiFi returned, storage received the marker **first**, then all five segments.

### 2. Flaky WiFi

Every other upload attempt fails, across six segments and three presses - one mid-segment, one mid-segment later, and one in the 3 s restart gap, which is the press the pipeline attaches to the segment that just ended.
All three markers reached storage, the queue drained, and each press produced exactly one magenta frame on the wire.

### 3. Reboot in the middle of a backlog

Four segments and two markers on the card; the boot flush's battery gives out three uploads in.
Both flags went up and one segment followed, which is the inverse of the before run.
No flag was left behind a segment that had already reached storage, and the next boot cleared the remaining three segments.

### 4. A press while a segment upload is in flight

The segment upload is blocked inside the storage client when the button is pressed.

- The press handler returned in **5 ms** while that upload was still blocked.
- The marker was on the card before the uploader was even told about it.
- The wearer was acknowledged - magenta on the wire - before the upload finished.
- The upload then completed mid-blink, so that state change won and the restore stood down; the LED ended on the recording green rather than magenta.
- Only `flag-upload-worker` ever uploaded a marker.

### 5. A press in the restart gap

The segment is already muxed when the wearer presses 2.5 s after it ended, inside the 3 s gap the pipeline attaches to that segment.

- The upload worker waited out the rest of the gap - 2.93 s of the unscaled 3 s - before flushing markers, and no longer.
- Storage received the marker first and the segment it flags second, so the pipeline sees the segment with its flag rather than relying on the late-marker backstop.
- Recording carries on into a second segment and the stop lands there, because a stopping loop deliberately skips the gap wait (see the pendant spec's flag-button section); that cut-short segment went up last without a second wait.

`overlapping_writes == 0` on every scenario: no two threads interleaved a frame on the wire.

### A bug this found

Scenario 2 crashed the flag worker the first time it ran.
A flush or a retry round reads the queue rather than the work item, so it can reach a marker before the press's own hand-off is dequeued; the second pass re-uploaded the same object and then died unlinking a file that was already gone, taking the only marker uploader with it for the rest of the run.
`FlagUploadWorker._upload` now treats a path that is gone as one it has already sent, pinned by `test_a_marker_a_round_already_sent_is_not_sent_again`.

## Reproducing

```sh
cd firmware
uv run --locked --extra dev python \
    ../docs/superpowers/evidence/flag-marker-offline-first/e2e_flag_offline.py
```

The script asserts every claim above and exits non-zero if one stops holding.

For the before run, extract the base firmware and point the script at it:

```sh
git archive 5073cce firmware/visio_recorder | tar -x -C /tmp/before --strip-components=1
VISIO_FIRMWARE=/tmp/before python3 \
    docs/superpowers/evidence/flag-marker-offline-first/before_gaps.py
```

It imports only `flag_button`, `recording_loop`, `upload_limit` and `upload_queue`, none of which pull in a third-party package, so a bare interpreter is enough.

For [`unit-before.txt`](unit-before.txt), run this branch's tests against that same base package:

```sh
git archive 5073cce firmware/visio_recorder | tar -x -C /tmp/before-pkg --strip-components=1
cp firmware/pyproject.toml firmware/uv.lock /tmp/before-pkg/ && cp -r firmware/tests /tmp/before-pkg/
(cd /tmp/before-pkg && uv run --extra dev pytest -q)
(cd /tmp/before-pkg && uv run --extra dev pytest -q tests/test_recording_loop.py)
```

[`unit-after.txt`](unit-after.txt) is `uv run --locked --extra dev pytest -q`, `ruff check .`, `ruff format --check .` and `mypy` from `firmware/`.
