"""Expo push notification adapter implementing the delivery PushClient."""

from typing import Callable

import httpx

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def build_expo_payload(to_token: str, title: str, body: str) -> dict:
    return {"to": to_token, "title": title, "body": body}


def _default_post(url: str, json_body: dict) -> None:
    response = httpx.post(url, json=json_body, timeout=30.0)
    response.raise_for_status()


class ExpoPushClient:
    def __init__(self, post: Callable[[str, dict], None] = _default_post):
        self._post = post

    def send(self, to_token: str, title: str, body: str) -> None:
        self._post(EXPO_PUSH_URL, build_expo_payload(to_token, title, body))
