"""The pendant's own login on disk: ``session.json``.

Shared by pairing, which creates the login, and the paired boot, which
restores it.
"""

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from visio_recorder.wifi_onboard import write_session_credentials

_logger = logging.getLogger(__name__)


def read_saved_session(session_path: Path) -> dict[str, str] | None:
    """The saved tokens, or None for a missing or unusable ``session.json``.

    Every shape the pendant cannot sign in with (empty, not JSON, not an
    object, a missing, empty or non-string token) is None, so callers treat
    it as no login at all instead of crashing on it. Deleting the file is
    left to the caller that is about to replace it.
    """
    try:
        data = json.loads(session_path.read_text())
        access_token, refresh_token = data["access_token"], data["refresh_token"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(access_token, str) or not isinstance(refresh_token, str):
        return None
    if not access_token or not refresh_token:
        return None
    return {"access_token": access_token, "refresh_token": refresh_token}


def session_persister(session_path: Path) -> Callable[[str, Any], None]:
    """An auth listener that keeps ``session.json`` on the newest tokens.

    Supabase rotates the refresh token on every refresh and accepts only the
    newest one (plus its immediate predecessor), so a pendant that kept only
    the token it was onboarded with lost its login on the first reboot after
    two refreshes - about two hours of running. Events without a session (a
    sign-out) leave the file alone, so a transient revoke is not turned into
    a deleted login. The lock serialises supabase-py's auto-refresh timer
    thread with any other refresh.
    """
    lock = threading.Lock()

    def persist(event: str, session: Any) -> None:
        if session is None:
            return
        with lock:
            write_session_credentials(
                session_path, session.access_token, session.refresh_token
            )
        _logger.info("session %s; saved the rotated tokens", event.lower())

    return persist
