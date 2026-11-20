"""The pendant's own login on disk: ``session.json``.

Shared by pairing, which creates the login, and the paired boot, which
restores it.
"""

import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from supabase import ClientOptions, create_client
from supabase_auth.errors import AuthApiError

from visio_recorder.recording_loop import RetryBackoff
from visio_recorder.uploader import LoginUnavailableError
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


# How long before the access token's expiry the pendant refreshes it. Wide
# enough for a slow request on a weak signal to still land inside the
# token's lifetime.
REFRESH_MARGIN_SEC = 60.0


class LoginRefusedError(Exception):
    """GoTrue definitely refused this login; it will never work again."""


class SessionRevokedError(LoginUnavailableError):
    """The pendant's login no longer exists: removed in the app, or re-paired."""


class LoginRefreshDeferredError(LoginUnavailableError):
    """The access token is due and could not be refreshed yet (offline)."""


def create_pendant_client(url: str, key: str) -> Any:
    """A supabase-py client whose login only ``SessionKeeper`` refreshes.

    supabase_auth's own auto-refresh timer is turned off. In the pinned
    2.34.1 it retries a failed refresh only about 100 s in all (ten tries,
    ``200 * 2**(n-1)`` passed to a ``Timer`` that divides by 1000) and is
    then never rescheduled, its retry count is never reset by a refresh it
    did not run (so after one long outage a later refresh gets no retries
    at all), and it times the refresh by the wall clock, which a Pi without
    an RTC has wrong until NTP syncs. Nothing else in supabase-py refreshes
    before a request, so a pendant off Wi-Fi when the hourly refresh fell
    due kept sending an expired token until the daemon restarted (issue
    #134). A second refresher would also race the keeper and rotate the
    refresh token twice.
    """
    return create_client(url, key, options=ClientOptions(auto_refresh_token=False))


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
    a deleted login. The writer serialises writes to one path across the
    process, so supabase-py's auto-refresh timer thread and any other
    refresh never interleave.
    """

    def persist(event: str, session: Any) -> None:
        if session is None:
            return
        write_session_credentials(
            session_path, session.access_token, session.refresh_token
        )
        _logger.info("session %s; saved the rotated tokens", event.lower())

    return persist


class SessionKeeper:
    """The one owner of the pendant's access-token freshness.

    It is the client's one auth listener: every new session is saved to
    ``session.json`` (``session_persister``) and its lifetime is noted on
    the monotonic clock. ``ensure_fresh`` runs before every request the
    storage and status adapters make, and when the access token is within
    ``REFRESH_MARGIN_SEC`` of expiry it runs the same refresh-token grant
    as the boot (``restore_session``).

    Expiry is judged by the lifetime GoTrue gave the token (``expires_in``)
    counted from when it arrived, never by the wall clock, which a Pi
    without an RTC has wrong until NTP syncs and which NTP then steps.

    A refresh that fails for any reason other than a definite refusal is
    "offline": ``LoginRefreshDeferredError`` is raised, the request is not
    sent, and the next refresh waits out ``RetryBackoff`` (30 s doubling to
    8 minutes, as the upload retries do), so an all-day outage costs one
    failed connect every eight minutes, not one per queued file. A refusal
    with one of ``REFUSED_LOGIN_CODES`` marks the login revoked for good,
    calls ``on_revoked`` once, and raises ``SessionRevokedError`` from then
    on. One lock serialises every refresh, so callers on several threads
    refresh once between them.
    """

    def __init__(
        self,
        client: Any,
        session_path: Path,
        *,
        margin_sec: float = REFRESH_MARGIN_SEC,
        backoff: RetryBackoff | None = None,
    ) -> None:
        self.client = client
        self._session_path = session_path
        self._margin_sec = margin_sec
        self._backoff = RetryBackoff() if backoff is None else backoff
        self._persist = session_persister(session_path)
        # Re-entrant: the refresh's own TOKEN_REFRESHED event arrives on the
        # refreshing thread while it holds the lock.
        self._lock = threading.RLock()
        self._refresh_token: str | None = None
        self._expires_at: float | None = None
        self._retry_at: float | None = None
        self._failed_refreshes = 0
        self._revoked = False
        self._on_revoked: Callable[[], None] | None = None
        client.auth.on_auth_state_change(self._on_auth_event)

    @property
    def revoked(self) -> bool:
        return self._revoked

    def on_revoked(self, callback: Callable[[], None]) -> None:
        """Call ``callback`` once if a refresh finds the login revoked."""
        self._on_revoked = callback

    def restore(self, refresh_token: str) -> None:
        """Sign back in at boot; raises ``LoginRefusedError`` on a refusal."""
        with self._lock:
            restore_session(self.client, refresh_token)

    def mark_stale(self) -> None:
        """A request was refused for an expired token: refresh before the next."""
        with self._lock:
            self._expires_at = None

    def ensure_fresh(self) -> None:
        with self._lock:
            if self._revoked:
                raise SessionRevokedError("the pendant's login was revoked")
            now = time.monotonic()
            if (
                self._expires_at is not None
                and now < self._expires_at - self._margin_sec
            ):
                return
            if self._retry_at is not None and now < self._retry_at:
                raise LoginRefreshDeferredError(
                    f"the login is due for a refresh; next try in "
                    f"{self._retry_at - now:.0f} s"
                )
            self._refresh(now)

    def _refresh(self, now: float) -> None:
        refresh_token = self._refresh_token
        if refresh_token is None:
            saved = read_saved_session(self._session_path)
            if saved is None:
                self._revoke()
                raise SessionRevokedError("the pendant's session.json is unreadable")
            refresh_token = saved["refresh_token"]
        try:
            restore_session(self.client, refresh_token)
        except LoginRefusedError as err:
            _logger.error("the pendant's %s; unpairing", err)
            self._revoke()
            raise SessionRevokedError(f"the pendant's {err}") from err
        except Exception as err:
            self._failed_refreshes += 1
            delay = self._backoff.delay_sec(self._failed_refreshes)
            self._retry_at = now + delay
            _logger.warning(
                "could not refresh the login (%r); retrying in %.0f s", err, delay
            )
            raise LoginRefreshDeferredError(
                f"could not refresh the login: {err}"
            ) from err
        self._failed_refreshes = 0
        self._retry_at = None

    def _revoke(self) -> None:
        self._revoked = True
        if self._on_revoked is not None:
            self._on_revoked()

    def _on_auth_event(self, event: str, session: Any) -> None:
        self._persist(event, session)
        if session is None:
            return
        with self._lock:
            self._refresh_token = session.refresh_token
            lifetime = getattr(session, "expires_in", None)
            # No lifetime: treat the token as due, so the next request refreshes.
            self._expires_at = time.monotonic() + float(lifetime) if lifetime else None
