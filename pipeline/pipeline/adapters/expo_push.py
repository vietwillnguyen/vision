"""Expo push notification adapter implementing the delivery PushClient.

Expo answers HTTP 200 even when delivery fails, reporting per-ticket errors
(e.g. DeviceNotRegistered) in the response body, so the client inspects the
returned tickets instead of trusting the status code alone.
"""

from typing import Callable

import httpx

from pipeline.delivery.notifier import PushTokenNotRegisteredError

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


class ExpoPushError(RuntimeError):
    pass


class ExpoDeviceNotRegisteredError(ExpoPushError, PushTokenNotRegisteredError):
    pass


def build_expo_payload(to_token: str, title: str, body: str) -> dict:
    return {"to": to_token, "title": title, "body": body}


def raise_for_ticket_errors(response_body: dict) -> None:
    tickets = response_body.get("data", [])
    if isinstance(tickets, dict):
        tickets = [tickets]
    failed = [t for t in tickets if isinstance(t, dict) and t.get("status") == "error"]
    if not failed:
        return
    message = f"Expo push rejected {len(failed)} ticket(s): {failed}"
    if any(
        (t.get("details") or {}).get("error") == "DeviceNotRegistered" for t in failed
    ):
        raise ExpoDeviceNotRegisteredError(message)
    raise ExpoPushError(message)


def _default_post(url: str, json_body: dict) -> dict:
    response = httpx.post(url, json=json_body, timeout=30.0)
    response.raise_for_status()
    return response.json()


class ExpoPushClient:
    def __init__(self, post: Callable[[str, dict], dict | None] = _default_post):
        self._post = post

    def send(self, to_token: str, title: str, body: str) -> None:
        response_body = self._post(
            EXPO_PUSH_URL, build_expo_payload(to_token, title, body)
        )
        if response_body is not None:
            raise_for_ticket_errors(response_body)
