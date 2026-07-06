from datetime import date
from typing import Protocol


class PushClient(Protocol):
    def send(self, to_token: str, title: str, body: str) -> None:
        ...


def notify_reel_ready(client: PushClient, push_token: str, reel_date: date) -> None:
    body = f"Your {reel_date.strftime('%B %d, %Y')} highlight reel is ready."
    client.send(push_token, "Your highlight reel is ready", body)
