from pathlib import Path

from visio_recorder.led import LedDriver, LedPattern, LedState
from visio_recorder.muxer import CommandRunner
from visio_recorder.recording_loop import next_led_state, on_segment_complete
from visio_recorder.uploader import StorageClient


class FakeBatteryReader:
    def __init__(self, pct: int) -> None:
        self._pct = pct

    def get_charge_pct(self) -> int:
        return self._pct


class FakeLedDriver(LedDriver):
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[int, int, int], LedPattern]] = []

    def set(self, color, pattern):
        self.calls.append((color, pattern))


class FakeCommandRunner(CommandRunner):
    def run(self, args: list[str]) -> None:
        # For mux_segment, create the mp4 file from the h264 input
        if "ffmpeg" in args:
            # args format: ["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path]
            input_idx = args.index("-i") + 1
            output_idx = args.index("copy") + 1
            input_path = Path(args[input_idx])
            output_path = Path(args[output_idx])
            # Copy the h264 file as mp4
            if input_path.exists():
                output_path.write_bytes(input_path.read_bytes())


class FakeStorageClient(StorageClient):
    def __init__(self) -> None:
        self.uploaded: list[str] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        self.uploaded.append(object_path)


class FakeStatusClient:
    def __init__(self) -> None:
        self.upserts: list[dict] = []

    def upsert_device_status(self, status: dict) -> None:
        self.upserts.append(status)


def test_next_led_state_prioritizes_low_battery_over_uploading():
    assert next_led_state(battery_is_low=True, is_uploading=True) == LedState.LOW_BATTERY
    assert next_led_state(battery_is_low=True, is_uploading=False) == LedState.LOW_BATTERY


def test_next_led_state_shows_uploading_when_battery_is_healthy():
    assert next_led_state(battery_is_low=False, is_uploading=True) == LedState.UPLOADING


def test_next_led_state_shows_recording_when_idle_and_healthy():
    assert next_led_state(battery_is_low=False, is_uploading=False) == LedState.RECORDING


def test_on_segment_complete_happy_path(tmp_path):
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    led = FakeLedDriver()
    storage = FakeStorageClient()
    status_client = FakeStatusClient()

    new_count = on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=FakeCommandRunner(),
        storage_client=storage,
        status_client=status_client,
        led_driver=led,
        battery_reader=FakeBatteryReader(85),
        device_id="device-abc",
        segments_uploaded_today=41,
    )

    assert new_count == 42
    assert storage.uploaded == ["device-abc/20260704_120000.mp4"]
    assert led.calls == [
        ((0, 0, 255), LedPattern.PULSING),
        ((0, 255, 0), LedPattern.SOLID),
    ]
    assert status_client.upserts == [
        {
            "device_id": "device-abc",
            "battery_pct": 85,
            "storage_used_gb": 0.0,
            "storage_free_gb": 0.0,
            "segments_pending": 0,
            "segments_uploaded_today": 42,
            "recording_active": True,
        }
    ]


def test_on_segment_complete_with_low_battery_uses_low_battery_led(tmp_path):
    h264_path = tmp_path / "raw" / "20260704_120000.h264"
    h264_path.parent.mkdir()
    h264_path.write_bytes(b"")
    queue_dir = tmp_path / "queue"
    led = FakeLedDriver()

    on_segment_complete(
        h264_path=h264_path,
        queue_dir=queue_dir,
        command_runner=FakeCommandRunner(),
        storage_client=FakeStorageClient(),
        status_client=FakeStatusClient(),
        led_driver=led,
        battery_reader=FakeBatteryReader(15),
        device_id="device-abc",
        segments_uploaded_today=0,
    )

    assert led.calls == [
        ((255, 255, 0), LedPattern.PULSING),
        ((255, 255, 0), LedPattern.PULSING),
    ]
