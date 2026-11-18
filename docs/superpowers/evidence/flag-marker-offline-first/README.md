# Local-first flag markers - end-to-end evidence

Every run here is firmware's own code.
Two pieces of hardware are simulated and nothing else: the camera is ffmpeg writing a real H.264 stream wherever `rpicam-vid` would, which the firmware's own ffmpeg runner then muxes, and `/dev/spidev0.0` is `SimulatedSpiDevice`, whose bytes are decoded back into the colours a WS2812B would latch by [`firmware/tests/ws2812_waveform.py`](../../../../firmware/tests/ws2812_waveform.py) - an independent receiver written from the datasheet timing windows rather than from the encoder.
Storage is a fake client with a switchable network, because the whole point is what the pendant does when there is no network to reach.

`run_recording_loop`, `flush_pending`, `FlagUploadWorker`, `make_flag_press_handler`, `LedController` and the real `Ws2812LedDriver` are all the shipped code.

The retry schedule is scaled 1500:1 (30 s becomes 20 ms, the 480 s cap becomes 320 ms) so a two-hour outage plays out in a few seconds.
Nothing else is scaled, and scenario 1 prints the unscaled production schedule straight out of `RetryBackoff` beside the scaled one.

## Before (firmware at `5073cce`, the branch point)

[`before-gaps.log`](before-gaps.log) ([`before_gaps.py`](before_gaps.py)), run against a `visio_recorder/` tree extracted from the base commit:

1. **A failed marker waited for the next boot.** One upload attempt, ever. Two seconds of outage - more than four times the 30 s a *segment* would wait before its first in-process retry, at the scale used here - produced no second attempt, and the marker sat on the card. The nightly run scored that segment unflagged.
2. **The startup flush sent segments before their flags.** A boot with four segments and two markers queued, whose battery gave out three uploads in, put three segments in storage and neither flag. `list_pending` sorts by name and digits sort before `FLAG_`, so the footage always overtook the flags.
3. **A press had no local feedback at all.** `make_flag_press_handler` took no acknowledgement hook, and `write_flag_marker` was `touch()` and return - the marker was only as durable as the page cache.

[`unit-before.txt`](unit-before.txt): this branch's tests against that same firmware. Five modules fail to import (`FLAG_ACK_BLINK_SEC`, `LedState.FLAG_ACK`, `is_flag_marker`, `FLAG_UPLOAD_THREAD_NAME`), and `test_recording_loop.py`, the one module that still collects, fails 19.

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
