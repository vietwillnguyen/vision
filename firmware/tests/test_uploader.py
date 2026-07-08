from pathlib import Path

from visio_recorder.uploader import StorageClient, upload_segment


class FakeStorageClient(StorageClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Path]] = []

    def upload(self, bucket: str, object_path: str, local_path: Path) -> None:
        self.calls.append((bucket, object_path, local_path))


def test_upload_segment_uses_segments_bucket_and_device_prefixed_path():
    client = FakeStorageClient()
    local_path = Path("/queue/20260704_120000.mp4")

    object_path = upload_segment(client, "device-abc", local_path)

    assert object_path == "device-abc/20260704_120000.mp4"
    assert client.calls == [("segments", "device-abc/20260704_120000.mp4", local_path)]


def test_upload_segment_also_uploads_flag_marker_files():
    client = FakeStorageClient()
    local_path = Path("/queue/FLAG_120300.marker")

    object_path = upload_segment(client, "device-abc", local_path)

    assert object_path == "device-abc/FLAG_120300.marker"
