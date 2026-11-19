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

from supabase_auth.errors import AuthApiError

from visio_recorder.wifi_onboard import write_session_credentials

_logger = logging.getLogger(__name__)

# GoTrue's error codes for a refresh-token grant that can never succeed: the
# token, its session or its user is gone (Remove device and a re-pair delete
# the device user; reproduced against the local stack), or the server ended
# the session. Only these unpair a pendant. Anything else, including a 4xx
# with no code or another code, a 429, a 5xx or a network error, says
# nothing about the login and is retried. supabase_auth's ErrorCode literal
# omits the refresh_token_* and session_expired codes, but its error parser
# passes GoTrue's code string through unchanged.
REFUSED_LOGIN_CODES = frozenset(
    {
        "refresh_token_not_found",
        "refresh_token_already_used",
        "session_not_found",
        "session_expired",
        "user_not_found",
        "user_banned",
    }
)


class LoginRefusedError(Exception):
    """GoTrue definitely refused this login; it will never work again."""


def restore_session(client: Any, refresh_token: str) -> None:
    """Sign ``client`` back in with a saved refresh token.

    This always runs the refresh-token grant instead of ``set_session``.
    ``set_session`` decides whether the saved access token has expired by the
    pendant's own clock, and a Pi has no RTC: booting before NTP syncs, it
    trusts a token that really expired, GoTrue answers 403 bad_jwt "token is
    expired", and that answer is about the token, not the login. The refresh
    grant is judged by the server alone, and its answer is the verdict. It
    rotates the refresh token, which the caller's persister saves; GoTrue
    accepts the previous one again if a power cut loses that write.
    """
    try:
        client.auth.refresh_session(refresh_token)
    except AuthApiError as err:
        if err.code in REFUSED_LOGIN_CODES:
            raise LoginRefusedError(
                f"the login was refused ({err.status} {err.code}: {err.message})"
            ) from err
        raise


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
