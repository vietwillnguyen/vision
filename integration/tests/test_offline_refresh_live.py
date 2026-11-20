"""A pendant off Wi-Fi when its login falls due uploads again once back online.

Issue #134. The access token lives an hour (supabase/config.toml
``jwt_expiry``), so a pendant worn away from home for the afternoon is often
offline at the moment it must refresh. This drives the real firmware path
(the pairing QR, the anonymous sign-in and claim, the boot's
``build_supabase_clients``) through a TCP link the test cuts, and simulates
the hour without waiting for it:

- the server's side of the hour: the client's requests carry an expired copy
  of the same login's access token (real claims, past ``exp``, signed with
  the stack's legacy HS256 secret), which storage and PostgREST refuse
  exactly as they refuse an aged token;
- the pendant's side of the hour: the clocks the code under test reads are
  moved forward. ``visio_recorder.session_store``'s ``time`` is replaced by
  a clock an hour ahead, and supabase_auth's refresh timer is made to fall
  due now (``_start_auto_refresh_token(1)``, a 1 ms delay) with its retry
  delays scaled down a hundredfold, so its ten tries over about 100 s run in
  about one;
- the outage: the link drops every connection, so every request of that
  window, the timer's refreshes included, fails as it does out of range.

Nothing else is faked: every request reaches the real local stack.
"""

import json
import time
from types import SimpleNamespace
from typing import cast

import pytest
from supabase_auth import timer as auth_timer
from supabase_auth._sync import gotrue_client
from supabase_auth.types import Session

from tests.network import SwitchableLink
from tests.pendant import PairedDevice, boot_pendant
from tests.test_device_session_live import _expired_copy, _segment, _status
from visio_recorder import session_store
from visio_recorder.pairing import SESSION_FILE, PairingState, pairing_state
from visio_recorder.uploader import upload_segment

AN_HOUR_SEC = 3600.0
# recording_loop.RETRY_MAX_SEC: the longest any retry backoff waits.
LONGEST_BACKOFF_SEC = 480.0


class _ScaledTimer(auth_timer.Timer):
    """supabase_auth's Timer, its delays (in ms) a hundred times shorter."""

    def __init__(self, seconds, function) -> None:
        super().__init__(seconds / 100, function)


class _ClockAhead:
    """``time`` as the pendant reads it once ``ahead_sec`` has passed."""

    def __init__(self, ahead_sec: float) -> None:
        self.ahead_sec = ahead_sec

    def monotonic(self) -> float:
        return time.monotonic() + self.ahead_sec

    def time(self) -> float:
        return time.time() + self.ahead_sec


def _wait_for(condition, timeout_sec: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.05)


def test_a_pendant_offline_when_its_login_falls_due_uploads_once_back_online(
    paired_device: PairedDevice, live_jwt_secret, monkeypatch, tmp_path
):
    env, data_dir = paired_device.env, paired_device.data_dir
    device_id = paired_device.device_id
    session_path = data_dir / SESSION_FILE
    monkeypatch.setattr(gotrue_client, "Timer", _ScaledTimer)

    with SwitchableLink(env["url"]) as link:
        pendant = boot_pendant({**env, "url": link.url}, data_dir)
        booted = json.loads(session_path.read_text())

        # An hour later, the token the client sends has expired.
        clock = _ClockAhead(AN_HOUR_SEC)
        monkeypatch.setattr(session_store, "time", clock, raising=False)
        aged = _expired_copy(booted["access_token"], live_jwt_secret)
        pendant.client._listen_to_auth_events(  # the client's header listener
            "TOKEN_REFRESHED", cast(Session, SimpleNamespace(access_token=aged))
        )

        # The pendant is out of range when its refresh falls due.
        link.cut()
        auth = pendant.client.auth
        auth._start_auto_refresh_token(1)

        def refresh_timer_gave_up() -> bool:
            timer = auth._refresh_token_timer
            return timer is None or (
                not timer.is_alive()
                and auth._network_retries >= gotrue_client.MAX_RETRIES
            )

        _wait_for(refresh_timer_gave_up)
        segment = _segment(tmp_path)
        with pytest.raises(Exception):  # noqa: B017 - any failure while out of range
            upload_segment(pendant.clients.storage, device_id, segment)
        assert pairing_state(data_dir) is PairingState.PAIRED

        # Back in range, after the longest backoff any retry waits.
        link.restore()
        clock.ahead_sec += LONGEST_BACKOFF_SEC
        upload_segment(pendant.clients.storage, device_id, segment)
        pendant.clients.status.upsert_device_status(_status(device_id, 42))

    stored = paired_device.admin.storage.from_("segments").list(device_id)
    assert segment.name in {item["name"] for item in stored}
    status = (
        paired_device.admin.table("device_status")
        .select("battery_pct")
        .eq("device_id", device_id)
        .execute()
        .data
    )
    assert status == [{"battery_pct": 42}]
    rotated = json.loads(session_path.read_text())
    assert rotated["refresh_token"] != booted["refresh_token"]
