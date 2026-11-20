"""The pendant refreshes its own login on demand (issue #134).

supabase_auth's auto-refresh timer gave up after about 100 s of failed
retries, so a pendant off Wi-Fi when the hourly refresh fell due kept sending
an expired token until a restart. ``SessionKeeper`` now owns refresh: before
each storage upload and status upsert it refreshes a token that is due, and
reproduced against the local stack in
integration/tests/test_offline_refresh_live.py.
"""

import json
import threading
from pathlib import Path

import pytest
from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError
from supabase_auth.errors import AuthApiError, AuthRetryableError

from tests.fakes import FakeLedDriver, battery_state, unlimited_upload_limit
from tests.test_supabase_clients import FakeSession, FakeSupabase, _write_session
from visio_recorder import session_store
from visio_recorder.recording_loop import (
    RETRY_BASE_SEC,
    RETRY_MAX_SEC,
    UPLOAD_FAILURES_TO_CRITICAL,
    UploadHealth,
    retry_pending_segments,
)
from visio_recorder.session_store import (
    REFRESH_MARGIN_SEC,
    LoginRefreshDeferredError,
    SessionRevokedError,
    create_pendant_client,
)
from visio_recorder.supabase_clients import SupabaseClients, build_supabase_clients
from visio_recorder.upload_queue import list_pending
from visio_recorder.uploader import LoginUnavailableError, upload_segment

LIFETIME_SEC = 3600.0


class FakeClock:
    """``time`` as session_store reads it, moved by the test."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(session_store, "time", fake)
    return fake


def _boot(tmp_path: Path, fake: FakeSupabase) -> SupabaseClients:
    return build_supabase_clients(
        "http://x", "anon", _write_session(tmp_path), lambda url, key: fake
    )


def _segment(tmp_path: Path) -> Path:
    path = tmp_path / "20261120_090000.mp4"
    path.write_bytes(b"segment")
    return path


def _due(clock: FakeClock) -> None:
    clock.now += LIFETIME_SEC - REFRESH_MARGIN_SEC


def test_the_library_timer_is_off_so_the_keeper_is_the_one_refresher():
    client = create_pendant_client("http://127.0.0.1:9", "anon")
    assert client.auth._auto_refresh_token is False


def test_a_fresh_login_is_used_as_it_is(tmp_path, clock):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    clock.now += LIFETIME_SEC - REFRESH_MARGIN_SEC - 1

    upload_segment(clients.storage, "device-abc", _segment(tmp_path))
    clients.status.upsert_device_status({"device_id": "device-abc"})

    assert fake.auth.refreshed_with == ["rt"]  # the boot's restore only


def test_a_token_near_expiry_is_refreshed_once_before_the_request(tmp_path, clock):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    fake.auth.refreshed = FakeSession("at-2", "rt-2")
    _due(clock)
    order: list[str] = []
    real_refresh = fake.auth.refresh_session

    def refresh(refresh_token=None):
        order.append("refresh")
        return real_refresh(refresh_token)

    fake.auth.refresh_session = refresh  # type: ignore[method-assign]
    real_from = fake.storage.from_

    def from_(bucket):
        order.append("request")
        return real_from(bucket)

    fake.storage.from_ = from_  # type: ignore[method-assign]

    upload_segment(clients.storage, "device-abc", _segment(tmp_path))
    clients.status.upsert_device_status({"device_id": "device-abc"})

    assert order == ["refresh", "request"]
    assert fake.auth.refreshed_with == ["rt", "rt-boot"]


def test_the_rotated_token_from_an_on_demand_refresh_is_persisted(tmp_path, clock):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    fake.auth.refreshed = FakeSession("at-2", "rt-2")
    _due(clock)

    clients.status.upsert_device_status({"device_id": "device-abc"})

    saved = json.loads((tmp_path / "session.json").read_text())
    assert saved == {"access_token": "at-2", "refresh_token": "rt-2"}


def test_the_token_lifetime_is_counted_from_its_arrival_not_the_wall_clock(
    tmp_path, clock
):
    # A short-lived token is due sooner; the wall clock is never read.
    fake = FakeSupabase()
    fake.auth.refreshed = FakeSession("at-boot", "rt-boot", expires_in=120)
    clients = _boot(tmp_path, fake)
    clock.now += 120 - REFRESH_MARGIN_SEC

    clients.status.upsert_device_status({"device_id": "device-abc"})

    assert fake.auth.refreshed_with == ["rt", "rt-boot"]


@pytest.mark.parametrize(
    "error",
    [
        AuthRetryableError("Server disconnected without sending a response.", 0),
        AuthRetryableError("Bad Gateway", 502),
        AuthApiError("Request rate limit reached", 429, "over_request_rate_limit"),
        AuthApiError("Internal Server Error", 500, None),
    ],
)
def test_an_offline_refresh_leaves_the_file_queued_and_health_untouched(
    tmp_path, clock, error
):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    _due(clock)
    fake.auth.refresh_error = error
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "20261120_090000.mp4").write_bytes(b"segment")
    health = UploadHealth(consecutive_failures=UPLOAD_FAILURES_TO_CRITICAL - 1)
    led = FakeLedDriver()

    outcome = retry_pending_segments(
        queue_dir=queue_dir,
        storage_client=clients.storage,
        led_driver=led,
        battery=battery_state(85),
        device_id="device-abc",
        upload_limit=unlimited_upload_limit(),
        upload_health=health,
    )

    assert (outcome.uploaded, outcome.failed, outcome.deferred) == (0, 0, 1)
    assert health.consecutive_failures == UPLOAD_FAILURES_TO_CRITICAL - 1
    assert [p.name for p in list_pending(queue_dir)] == ["20261120_090000.mp4"]
    assert fake.uploads == []
    assert not clients.session.revoked
    assert (tmp_path / "session.json").exists()


def test_an_offline_refresh_also_keeps_critical_latched(tmp_path, clock):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    _due(clock)
    fake.auth.refresh_error = AuthRetryableError("connection refused", 0)
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "20261120_090000.mp4").write_bytes(b"segment")
    health = UploadHealth(consecutive_failures=UPLOAD_FAILURES_TO_CRITICAL)

    retry_pending_segments(
        queue_dir=queue_dir,
        storage_client=clients.storage,
        led_driver=FakeLedDriver(),
        battery=battery_state(85),
        device_id="device-abc",
        upload_limit=unlimited_upload_limit(),
        upload_health=health,
    )

    assert health.is_critical


def test_an_offline_refresh_is_not_retried_before_the_backoff(tmp_path, clock):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    _due(clock)
    fake.auth.refresh_error = AuthRetryableError("connection refused", 0)

    for _ in range(3):
        with pytest.raises(LoginRefreshDeferredError):
            clients.status.upsert_device_status({"device_id": "device-abc"})
    assert fake.auth.refreshed_with == ["rt", "rt-boot"]

    clock.now += RETRY_BASE_SEC
    with pytest.raises(LoginRefreshDeferredError):
        clients.status.upsert_device_status({"device_id": "device-abc"})
    assert len(fake.auth.refreshed_with) == 3

    # Back online after the longest wait: the next request refreshes and goes.
    fake.auth.refresh_error = None
    clock.now += RETRY_MAX_SEC
    clients.status.upsert_device_status({"device_id": "device-abc"})
    assert fake.upserts["device_status"] == [{"device_id": "device-abc"}]


def test_a_deferred_refresh_is_a_login_problem_not_an_upload_failure():
    assert issubclass(LoginRefreshDeferredError, LoginUnavailableError)
    assert issubclass(SessionRevokedError, LoginUnavailableError)


@pytest.mark.parametrize("code", sorted(session_store.REFUSED_LOGIN_CODES))
def test_a_refused_on_demand_refresh_takes_the_revoked_path(tmp_path, clock, code):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    revoked: list[bool] = []
    clients.session.on_revoked(lambda: revoked.append(True))
    _due(clock)
    fake.auth.refresh_error = AuthApiError("refused", 400, code)

    with pytest.raises(SessionRevokedError):
        upload_segment(clients.storage, "device-abc", _segment(tmp_path))
    with pytest.raises(SessionRevokedError):
        clients.status.upsert_device_status({"device_id": "device-abc"})

    assert clients.session.revoked
    assert revoked == [True]
    assert len(fake.auth.refreshed_with) == 2  # never asked again
    assert fake.uploads == []


def test_concurrent_callers_refresh_once(tmp_path, clock):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    fake.auth.refreshed = FakeSession("at-2", "rt-2")
    _due(clock)
    entered = threading.Event()
    release = threading.Event()
    real_refresh = fake.auth.refresh_session

    def slow_refresh(refresh_token=None):
        entered.set()
        assert release.wait(5)
        return real_refresh(refresh_token)

    fake.auth.refresh_session = slow_refresh  # type: ignore[method-assign]
    errors: list[BaseException] = []

    def call() -> None:
        try:
            clients.status.upsert_device_status({"device_id": "device-abc"})
        except BaseException as err:  # pragma: no cover - reported below
            errors.append(err)

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    assert entered.wait(5)
    release.set()
    for thread in threads:
        thread.join(5)

    assert errors == []
    assert fake.auth.refreshed_with == ["rt", "rt-boot"]
    assert len(fake.upserts["device_status"]) == 4


@pytest.mark.parametrize(
    "refusal",
    [
        StorageApiError('"exp" claim timestamp check failed', "Unauthorized", 403),
        APIError({"code": "PGRST303", "message": "JWT expired"}),
    ],
    ids=["storage", "postgrest"],
)
def test_a_request_refused_for_an_expired_token_refreshes_and_retries_once(
    tmp_path, clock, refusal
):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    fake.auth.refreshed = FakeSession("at-2", "rt-2")
    calls: list[str] = []
    real_table = fake.table
    real_from = fake.storage.from_

    def refuse_first(real):
        def wrapped(name):
            calls.append(name)
            if len(calls) == 1:
                raise refusal
            return real(name)

        return wrapped

    if isinstance(refusal, StorageApiError):
        fake.storage.from_ = refuse_first(real_from)  # type: ignore[method-assign]
        upload_segment(clients.storage, "device-abc", _segment(tmp_path))
        assert len(fake.uploads) == 1
    else:
        fake.table = refuse_first(real_table)  # type: ignore[method-assign]
        clients.status.upsert_device_status({"device_id": "device-abc"})
        assert fake.upserts["device_status"] == [{"device_id": "device-abc"}]

    assert fake.auth.refreshed_with == ["rt", "rt-boot"]
    assert len(calls) == 2


def test_another_refusal_is_not_retried(tmp_path, clock):
    fake = FakeSupabase()
    clients = _boot(tmp_path, fake)
    refusal = StorageApiError(
        "new row violates row-level security", "Unauthorized", 403
    )

    def refuse(name):
        raise refusal

    fake.storage.from_ = refuse  # type: ignore[method-assign]
    with pytest.raises(StorageApiError):
        upload_segment(clients.storage, "device-abc", _segment(tmp_path))
    assert fake.auth.refreshed_with == ["rt"]
