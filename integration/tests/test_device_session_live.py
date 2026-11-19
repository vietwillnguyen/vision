"""The pendant keeps its login across token rotation, against a live stack.

Supabase rotates the refresh token on every refresh and accepts only the
newest one and its immediate predecessor (GoTrue v2.192, reuse interval 10 s).
These tests drive the real firmware path - the QR payload the app builds,
onboarding's session.json write, the boot-time ``build_supabase_clients`` -
and simulate "an hour passes" by calling ``refresh_session()``, which is what
supabase-py's auto-refresh timer does when the access token expires.
"""

import json
import time
import uuid
from pathlib import Path

import pytest
from supabase import create_client

from visio_recorder.supabase_clients import build_supabase_clients
from visio_recorder.wifi_onboard import (
    parse_onboarding_qr_payload,
    write_session_credentials,
)

# supabase/config.toml refresh_token_reuse_interval, plus a margin.
PAST_REUSE_INTERVAL_SEC = 12
PASSWORD = "pw-123456-session"


@pytest.fixture
def owner(live_supabase_env):
    admin = create_client(
        live_supabase_env["url"], live_supabase_env["service_role_key"]
    )
    email = f"session-{uuid.uuid4().hex[:10]}@example.com"
    user = admin.auth.admin.create_user(
        {"email": email, "password": PASSWORD, "email_confirm": True}
    ).user
    yield email
    admin.auth.admin.delete_user(user.id)


def _phone(env, email):
    phone = create_client(env["url"], env["anon_key"])
    phone.auth.sign_in_with_password({"email": email, "password": PASSWORD})
    return phone


def _onboard(phone, session_path: Path) -> None:
    """What ReonboardContainer.tsx puts in the QR, then the pendant's write."""
    session = phone.auth.get_session()
    payload = parse_onboarding_qr_payload(
        json.dumps(
            {
                "ssid": "home",
                "password": "password1",
                "user_access_token": session.access_token,
                "user_refresh_token": session.refresh_token,
            }
        )
    )
    write_session_credentials(
        session_path, payload.user_access_token, payload.user_refresh_token
    )


def _boot(env, session_path: Path):
    """One daemon boot; returns the underlying client so a test can age it."""
    holder = {}

    def factory(url, key):
        holder["client"] = create_client(url, key)
        return holder["client"]

    build_supabase_clients(
        env["url"], env["anon_key"], session_path, client_factory=factory
    )
    return holder["client"]


def _hour_passes(client) -> None:
    client.auth.refresh_session()
    time.sleep(PAST_REUSE_INTERVAL_SEC)


def test_the_pendant_keeps_its_login_across_a_reboot_after_a_long_run(
    live_supabase_env, owner, tmp_path
):
    session_path = tmp_path / "session.json"
    _onboard(_phone(live_supabase_env, owner), session_path)

    pendant = _boot(live_supabase_env, session_path)
    _hour_passes(pendant)
    _hour_passes(pendant)

    rebooted = _boot(live_supabase_env, session_path)
    rebooted.auth.refresh_session()  # raised "Already Used" before the fix


@pytest.mark.xfail(
    strict=True,
    reason="the QR still hands the pendant the phone's own session; "
    "fixed by giving the pendant its own login",
)
def test_the_phone_refreshing_does_not_cost_the_pendant_its_login(
    live_supabase_env, owner, tmp_path
):
    session_path = tmp_path / "session.json"
    phone = _phone(live_supabase_env, owner)
    _onboard(phone, session_path)

    _hour_passes(phone)
    _hour_passes(phone)

    pendant = _boot(live_supabase_env, session_path)
    pendant.auth.refresh_session()


@pytest.mark.xfail(
    strict=True,
    reason="the QR still hands the pendant the phone's own session; "
    "fixed by giving the pendant its own login",
)
def test_logging_out_of_the_app_does_not_log_the_pendant_out(
    live_supabase_env, owner, tmp_path
):
    session_path = tmp_path / "session.json"
    phone = _phone(live_supabase_env, owner)
    _onboard(phone, session_path)

    phone.auth.sign_out({"scope": "local"})

    _boot(live_supabase_env, session_path)
