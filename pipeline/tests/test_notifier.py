from datetime import date

from pipeline.delivery.notifier import PushClient, notify_reel_ready


class FakePushClient(PushClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, to_token: str, title: str, body: str) -> None:
        self.calls.append((to_token, title, body))


def test_notify_reel_ready_sends_expected_copy():
    client = FakePushClient()

    notify_reel_ready(client, "push-token-abc", date(2026, 7, 4))

    assert client.calls == [
        (
            "push-token-abc",
            "Your highlight reel is ready",
            "Your July 04, 2026 highlight reel is ready.",
        )
    ]
