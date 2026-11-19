"""The pendant keeps its login across token rotation, against a live stack.

Supabase rotates the refresh token on every refresh and accepts only the
newest one and its immediate predecessor (GoTrue v2.192, reuse interval 10 s).
These tests drive the real firmware path - the pairing QR the app builds,
the pendant's anonymous sign-in and claim, the boot-time
``build_supabase_clients`` - and simulate "an hour passes" by calling
``refresh_session()``, which is what supabase-py's auto-refresh timer does when
the access token expires.

The pendant has its own login, so nothing the phone does to its session (two
refreshes, a logout) reaches the pendant. The rest of the file pins what that
login can and cannot do, and that Remove device and a re-pair revoke it.
"""

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError
from supabase import Client, create_client
from supabase_auth._sync import gotrue_client
from supabase_auth.errors import AuthApiError

from tests.pendant import (
    PairedDevice,
    boot_pendant,
    create_owner,
    delete_owner,
    make_pairing_qr,
    pair_pendant,
    sign_in_owner,
)
from visio_recorder.pairing import (
    CLAIM_ARG,
    CLAIM_RPC,
    PAIRING_FILE,
    SESSION_FILE,
    complete_pairing,
    write_pending_pairing,
)
from visio_recorder.supabase_clients import SessionRevokedError, SupabaseStorage
from visio_recorder.uploader import upload_segment
from visio_recorder.wifi_onboard import (
    parse_onboarding_qr_payload,
    write_session_credentials,
)

# supabase/config.toml refresh_token_reuse_interval, plus a margin.
PAST_REUSE_INTERVAL_SEC = 12


@pytest.fixture
def owner(live_supabase_env):
    admin = create_client(
        live_supabase_env["url"], live_supabase_env["service_role_key"]
    )
    owner_id, email = create_owner(admin, "session")
    yield email
    delete_owner(admin, owner_id)


def _phone(env, email):
    return sign_in_owner(env, email)


def _onboard(env, phone, data_dir: Path) -> str:
    """What the app's pairing QR and the pendant's pairing now do."""
    return pair_pendant(env, phone, data_dir)


def _boot(env, data_dir: Path):
    """One daemon boot; returns the underlying client so a test can age it."""
    return boot_pendant(env, data_dir).client


def _hour_passes(client) -> None:
    client.auth.refresh_session()
    time.sleep(PAST_REUSE_INTERVAL_SEC)


def test_the_pendant_keeps_its_login_across_a_reboot_after_a_long_run(
    live_supabase_env, owner, tmp_path
):
    _onboard(live_supabase_env, _phone(live_supabase_env, owner), tmp_path)

    pendant = _boot(live_supabase_env, tmp_path)
    _hour_passes(pendant)
    _hour_passes(pendant)

    rebooted = _boot(live_supabase_env, tmp_path)
    rebooted.auth.refresh_session()  # raised "Already Used" before the fix


def test_the_phone_refreshing_does_not_cost_the_pendant_its_login(
    live_supabase_env, owner, tmp_path
):
    phone = _phone(live_supabase_env, owner)
    _onboard(live_supabase_env, phone, tmp_path)

    _hour_passes(phone)
    _hour_passes(phone)

    pendant = _boot(live_supabase_env, tmp_path)
    pendant.auth.refresh_session()


def test_logging_out_of_the_app_does_not_log_the_pendant_out(
    live_supabase_env, owner, tmp_path
):
    phone = _phone(live_supabase_env, owner)
    _onboard(live_supabase_env, phone, tmp_path)

    phone.auth.sign_out({"scope": "local"})

    _boot(live_supabase_env, tmp_path)


def _segment(tmp_path: Path, name: str = "20261119_090000.mp4") -> Path:
    path = tmp_path / name
    path.write_bytes(b"segment bytes")
    return path


def _status(device_id: str, battery_pct: int) -> dict:
    return {"device_id": device_id, "battery_pct": battery_pct}


@pytest.fixture
def anonymous_user(paired_device: PairedDevice) -> Iterator[Callable[[], Client]]:
    """Signs in fresh anonymous users, and deletes them after the test."""
    created: list[str] = []

    def sign_in() -> Client:
        client = create_client(paired_device.env["url"], paired_device.env["anon_key"])
        response = client.auth.sign_in_anonymously()
        assert response.user is not None
        created.append(response.user.id)
        return client

    yield sign_in
    for user_id in created:
        paired_device.admin.auth.admin.delete_user(user_id)


def _other_device(paired: PairedDevice) -> str:
    rows = cast(
        list[dict[str, Any]],
        paired.admin.table("devices")
        .insert({"user_id": paired.owner_id, "name": "second device"})
        .execute()
        .data,
    )
    return str(rows[0]["device_id"])


def test_the_paired_pendant_uploads_and_reports_and_the_owner_reads_both(
    paired_device: PairedDevice, tmp_path
):
    pendant = paired_device.pendant.clients
    device_id = paired_device.device_id

    # The first real upload through storage-api also settles that it sets
    # owner_id before the insert policy's owner_id = auth.uid() check runs.
    object_path = upload_segment(pendant.storage, device_id, _segment(tmp_path))
    pendant.status.upsert_device_status(_status(device_id, 77))

    owner = paired_device.owner
    listed = owner.storage.from_("segments").list(device_id)
    assert [item["name"] for item in listed] == ["20261119_090000.mp4"]
    assert owner.storage.from_("segments").create_signed_url(object_path, 60)
    rows = (
        owner.table("device_status")
        .select("battery_pct")
        .eq("device_id", device_id)
        .execute()
        .data
    )
    assert rows == [{"battery_pct": 77}]


def test_a_retried_upload_is_recognised_through_the_pendants_own_read(
    paired_device: PairedDevice, tmp_path
):
    # The idempotency check reads back the object, so the pendant's select
    # policy (its own uploads only) must let it see what it just stored.
    pendant = paired_device.pendant.clients
    segment = _segment(tmp_path)
    upload_segment(pendant.storage, paired_device.device_id, segment)

    upload_segment(pendant.storage, paired_device.device_id, segment)


def test_the_pendant_cannot_reach_beyond_its_own_device(
    paired_device: PairedDevice, tmp_path
):
    other = _other_device(paired_device)
    paired_device.admin.storage.from_("reels").upload(
        f"{paired_device.device_id}/reel.mp4", b"reel", {"content-type": "video/mp4"}
    )
    client = paired_device.pendant.client

    with pytest.raises(StorageApiError):
        upload_segment(paired_device.pendant.clients.storage, other, _segment(tmp_path))
    with pytest.raises(APIError):
        paired_device.pendant.clients.status.upsert_device_status(_status(other, 1))
    assert client.storage.from_("reels").list(paired_device.device_id) == []
    assert client.table("devices").select("device_id").execute().data == []
    assert client.table("reels").select("id").execute().data == []
    with pytest.raises(APIError):
        client.rpc("create_pairing_code", {"p_device_id": None}).execute()


def test_remove_device_revokes_the_pendant_and_keeps_the_device(
    paired_device: PairedDevice, tmp_path
):
    device_id = paired_device.device_id
    pendant = paired_device.pendant.clients
    upload_segment(
        pendant.storage, device_id, _segment(tmp_path, "20261119_080000.mp4")
    )

    paired_device.owner.rpc("unpair_device", {"p_device_id": device_id}).execute()

    # The running pendant's access token is still cryptographically valid, but
    # every policy looks the link up at query time.
    with pytest.raises(StorageApiError):
        upload_segment(pendant.storage, device_id, _segment(tmp_path))
    with pytest.raises(APIError):
        pendant.status.upsert_device_status(_status(device_id, 5))
    # Its next boot is a definite rejection, which sends it back to scanning.
    with pytest.raises(SessionRevokedError):
        boot_pendant(paired_device.env, paired_device.data_dir)
    # The device and its footage stay with the owner.
    rows = (
        paired_device.owner.table("devices")
        .select("device_id, device_user_id")
        .execute()
        .data
    )
    assert rows == [{"device_id": device_id, "device_user_id": None}]
    listed = paired_device.owner.storage.from_("segments").list(device_id)
    assert [item["name"] for item in listed] == ["20261119_080000.mp4"]


def test_re_pairing_the_device_replaces_the_previous_pendant(
    paired_device: PairedDevice, tmp_path
):
    replacement_dir = tmp_path / "replacement"
    replacement_dir.mkdir()

    device_id = pair_pendant(
        paired_device.env, paired_device.owner, replacement_dir, paired_device.device_id
    )

    assert device_id == paired_device.device_id
    with pytest.raises(SessionRevokedError):
        boot_pendant(paired_device.env, paired_device.data_dir)
    replacement = boot_pendant(paired_device.env, replacement_dir).clients
    replacement.status.upsert_device_status(_status(device_id, 64))
    upload_segment(replacement.storage, device_id, _segment(tmp_path))


def test_a_used_pairing_code_is_refused_for_anyone_else(
    paired_device: PairedDevice, anonymous_user, tmp_path
):
    # Expiry cannot be aged through the public API without a ten-minute sleep,
    # so the expired-code refusal is covered by pgTAP (device_pairing.test.sql).
    qr = make_pairing_qr(paired_device.owner, paired_device.device_id)
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    code = parse_onboarding_qr_payload(qr).pairing_code
    write_pending_pairing(first_dir / PAIRING_FILE, code)
    complete_pairing(paired_device.env["url"], paired_device.env["anon_key"], first_dir)

    thief = anonymous_user()
    with pytest.raises(APIError) as exc_info:
        thief.rpc(CLAIM_RPC, {CLAIM_ARG: code}).execute()

    assert exc_info.value.code == "P0001"
    assert exc_info.value.message == "invalid pairing code"


def test_an_anonymous_user_that_never_paired_can_do_nothing(
    paired_device: PairedDevice, anonymous_user, tmp_path
):
    stranger = anonymous_user()
    device_id = paired_device.device_id

    assert stranger.table("devices").select("device_id").execute().data == []
    assert stranger.table("device_status").select("device_id").execute().data == []
    with pytest.raises(APIError):
        stranger.table("device_status").upsert(_status(device_id, 1)).execute()
    with pytest.raises(APIError):
        stranger.table("devices").insert({"user_id": "x", "name": "squat"}).execute()
    with pytest.raises(StorageApiError):
        upload_segment(SupabaseStorage(stranger), device_id, _segment(tmp_path))
    with pytest.raises(APIError):
        stranger.rpc("create_pairing_code", {"p_device_id": None}).execute()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _expired_copy(access_token: str, jwt_secret: str) -> str:
    """The same login's access token, as it reads an hour after it was issued.

    GoTrue still verifies the stack's legacy HS256 secret, so a token with the
    real claims and a past ``exp`` is refused exactly as an aged one is.
    """
    payload_part = access_token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload_part + "=" * 4))
    now = int(time.time())
    claims.update(iat=now - 3660, exp=now - 60)
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    signing_input = f"{header}.{_b64url(json.dumps(claims).encode())}"
    signature = hmac.new(
        jwt_secret.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_b64url(signature)}"


class _ClockBehind:
    """``time`` as a Pi without an RTC sees it before NTP syncs."""

    def __init__(self, behind_sec: float) -> None:
        self._behind_sec = behind_sec

    def time(self) -> float:
        return time.time() - self._behind_sec


def test_a_pendant_booting_before_its_clock_syncs_keeps_its_login(
    paired_device: PairedDevice, live_jwt_secret, monkeypatch, tmp_path
):
    session_path = paired_device.data_dir / SESSION_FILE
    saved = json.loads(session_path.read_text())
    expired = _expired_copy(saved["access_token"], live_jwt_secret)
    # The scenario is real: GoTrue refuses this token as expired.
    probe = create_client(paired_device.env["url"], paired_device.env["anon_key"])
    with pytest.raises(AuthApiError) as refused:
        probe.auth.get_user(expired)
    assert refused.value.code == "bad_jwt"
    assert "expired" in refused.value.message
    write_session_credentials(session_path, expired, saved["refresh_token"])
    # Two hours behind, so supabase-py's own expiry check calls it valid.
    monkeypatch.setattr(gotrue_client, "time", _ClockBehind(7200))

    rebooted = boot_pendant(paired_device.env, paired_device.data_dir)

    device_id = paired_device.device_id
    rebooted.clients.status.upsert_device_status(_status(device_id, 41))
    upload_segment(rebooted.clients.storage, device_id, _segment(tmp_path))
    rotated = json.loads(session_path.read_text())
    assert rotated["refresh_token"] != saved["refresh_token"]
