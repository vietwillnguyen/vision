import json
import logging
import stat
from typing import Any

import httpx
import pytest
from postgrest.exceptions import APIError
from supabase_auth.errors import AuthApiError, AuthRetryableError

from visio_recorder.device_identity import store_paired_device_id
from visio_recorder.pairing import (
    CLAIM_ARG,
    CLAIM_RPC,
    PairingRejectedError,
    PairingState,
    PairingUnavailableError,
    clear_local_pairing,
    complete_pairing,
    pairing_state,
    read_pending_pairing,
    write_pending_pairing,
)
from visio_recorder.wifi_onboard import write_session_credentials

DEVICE_ID = "11111111-1111-1111-1111-111111111111"
CODE = "one-time-pairing-code"


def _session(data_dir):
    write_session_credentials(data_dir / "session.json", "access", "refresh")


# The state table from the spec's "On-disk state".


def test_an_empty_data_dir_is_unpaired(tmp_path):
    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_pending_pairing_file_is_pending(tmp_path):
    write_pending_pairing(tmp_path / "pairing.json", CODE)

    assert pairing_state(tmp_path) is PairingState.PENDING


def test_a_pending_pairing_file_wins_over_everything(tmp_path):
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)
    _session(tmp_path)
    write_pending_pairing(tmp_path / "pairing.json", CODE)

    assert pairing_state(tmp_path) is PairingState.PENDING


def test_a_marked_device_file_and_a_session_is_paired(tmp_path):
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)
    _session(tmp_path)

    assert pairing_state(tmp_path) is PairingState.PAIRED


def test_a_marked_device_file_without_a_session_is_unpaired(tmp_path):
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_session_without_a_device_file_is_unpaired(tmp_path):
    _session(tmp_path)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_legacy_device_file_and_the_owners_session_is_unpaired(tmp_path):
    # A pendant onboarded under the old contract: new firmware must rescan.
    (tmp_path / "device.json").write_text(json.dumps({"device_id": DEVICE_ID}))
    _session(tmp_path)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_corrupt_device_file_is_unpaired(tmp_path):
    (tmp_path / "device.json").write_text('{"device_id')
    _session(tmp_path)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


# Every shape of session.json the pendant cannot sign in with. A power cut is
# not one of them (the write is atomic), but an SD card fault or a hand edit
# is, and a PAIRED boot that then cannot parse the file crash-looped forever.
UNREADABLE_SESSIONS = [
    "",
    "{",
    "[]",
    "null",
    '"tokens"',
    "{}",
    '{"access_token": "at"}',
    '{"refresh_token": "rt"}',
    '{"access_token": 1, "refresh_token": "rt"}',
    '{"access_token": "at", "refresh_token": ["rt"]}',
    '{"access_token": "at", "refresh_token": ""}',
]


@pytest.mark.parametrize("content", UNREADABLE_SESSIONS)
def test_a_marked_device_file_with_an_unreadable_session_is_unpaired(tmp_path, content):
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)
    (tmp_path / "session.json").write_text(content)

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


def test_a_corrupt_pending_pairing_file_is_unpaired(tmp_path):
    # Nothing to claim with, so the only way forward is a fresh scan.
    (tmp_path / "pairing.json").write_text("{not json")

    assert pairing_state(tmp_path) is PairingState.UNPAIRED


# The pending pairing file.


def test_write_pending_pairing_round_trips_private_and_atomic(tmp_path):
    path = tmp_path / "pairing.json"

    write_pending_pairing(path, CODE)

    assert read_pending_pairing(path) == CODE
    assert json.loads(path.read_text()) == {"pairing_code": CODE}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["pairing.json"]


def test_read_pending_pairing_of_a_missing_file_is_none(tmp_path):
    assert read_pending_pairing(tmp_path / "pairing.json") is None


@pytest.mark.parametrize(
    "content",
    ["", "{not json", "[]", "{}", '{"pairing_code": ""}', '{"pairing_code": 5}'],
)
def test_read_pending_pairing_of_a_corrupt_file_is_none(tmp_path, content):
    path = tmp_path / "pairing.json"
    path.write_text(content)

    assert read_pending_pairing(path) is None


# Clearing the local pairing.


def test_clear_local_pairing_returns_a_paired_pendant_to_unpaired(tmp_path):
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)
    _session(tmp_path)
    write_pending_pairing(tmp_path / "pairing.json", CODE)
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "seg.mp4").write_bytes(b"footage")

    clear_local_pairing(tmp_path)

    assert not (tmp_path / "session.json").exists()
    assert not (tmp_path / "pairing.json").exists()
    assert json.loads((tmp_path / "device.json").read_text()) == {
        "device_id": DEVICE_ID
    }
    assert pairing_state(tmp_path) is PairingState.UNPAIRED
    assert (queue_dir / "seg.mp4").read_bytes() == b"footage"


def test_clear_local_pairing_of_an_empty_data_dir_is_a_no_op(tmp_path):
    clear_local_pairing(tmp_path)

    assert list(tmp_path.iterdir()) == []


# complete_pairing, against a fake supabase-py client.


class FakeSession:
    def __init__(self, access_token: str, refresh_token: str) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token


class FakeAuth:
    def __init__(self, events: list) -> None:
        self._events = events
        self._subscribers: list = []
        self.sign_in_error: Exception | None = None
        self.set_session_error: Exception | None = None
        self.refresh_error: Exception | None = None
        self.anonymous_session = FakeSession("anon-at", "anon-rt")

    def on_auth_state_change(self, callback) -> None:
        self._events.append("subscribe")
        self._subscribers.append(callback)

    def _emit(self, event: str, session: FakeSession) -> None:
        for callback in self._subscribers:
            callback(event, session)

    def sign_in_anonymously(self):
        self._events.append("sign_in_anonymously")
        if self.sign_in_error is not None:
            raise self.sign_in_error
        self._emit("SIGNED_IN", self.anonymous_session)

    def set_session(self, access_token: str, refresh_token: str):
        self._events.append(("set_session", access_token, refresh_token))
        if self.set_session_error is not None:
            raise self.set_session_error
        self._emit("SIGNED_IN", FakeSession(access_token, refresh_token))

    def refresh_session(self, refresh_token: str | None = None):
        self._events.append(("refresh_session", refresh_token))
        if self.refresh_error is not None:
            raise self.refresh_error
        self._emit("TOKEN_REFRESHED", FakeSession("resumed-at", "resumed-rt"))


class FakeResponse:
    def __init__(self, data) -> None:
        self.data = data


class FakeRpc:
    def __init__(self, client: "FakePairingClient", name: str, params: dict) -> None:
        self._client = client
        self._name = name
        self._params = params

    def execute(self) -> FakeResponse:
        client = self._client
        client.events.append(("rpc", self._name, self._params))
        # What is on disk at the moment the claim is sent.
        client.device_file_at_claim = (client.data_dir / "device.json").exists()
        if client.rpc_error is not None:
            raise client.rpc_error
        return FakeResponse(client.claimed_device_id)


class FakePairingClient:
    def __init__(self, data_dir) -> None:
        self.data_dir = data_dir
        self.events: list = []
        self.auth = FakeAuth(self.events)
        self.rpc_error: Exception | None = None
        self.claimed_device_id: object = DEVICE_ID
        self.device_file_at_claim: bool | None = None

    def rpc(self, name: str, params: dict) -> FakeRpc:
        return FakeRpc(self, name, params)


def _pair(data_dir, client: FakePairingClient) -> str:
    return complete_pairing(
        "http://x", "anon", data_dir, client_factory=lambda url, key: client
    ).device_id


def _pending(data_dir) -> None:
    write_pending_pairing(data_dir / "pairing.json", CODE)


def test_pairing_signs_in_anonymously_and_saves_that_session(tmp_path):
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)

    _pair(tmp_path, client)

    # The persister is registered first, so the new session is saved by the
    # same atomic write as every later rotation.
    assert client.events[:2] == ["subscribe", "sign_in_anonymously"]
    assert json.loads((tmp_path / "session.json").read_text()) == {
        "access_token": "anon-at",
        "refresh_token": "anon-rt",
    }


def test_pairing_claims_the_code_then_adopts_the_device_and_drops_the_code(tmp_path):
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)

    device_id = _pair(tmp_path, client)

    assert device_id == DEVICE_ID
    assert client.events[-1] == (
        "rpc",
        CLAIM_RPC,
        {CLAIM_ARG: CODE},
    )
    assert client.device_file_at_claim is False
    assert json.loads((tmp_path / "device.json").read_text()) == {
        "device_id": DEVICE_ID,
        "pairing": 2,
    }
    assert not (tmp_path / "pairing.json").exists()
    assert pairing_state(tmp_path) is PairingState.PAIRED


def test_pairing_hands_back_the_client_that_signed_in(tmp_path):
    # The boot keeps using it rather than opening a second client.
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)

    paired = complete_pairing(
        "http://x", "anon", tmp_path, client_factory=lambda url, key: client
    )

    assert paired.client is client


def test_pairing_resumes_with_the_saved_session_instead_of_signing_in_again(tmp_path):
    # Entering a scan always deletes session.json, so one seen while pending
    # is this attempt's own anonymous session; reusing it keeps a crash-looping
    # pendant from burning the anonymous sign-in rate limit.
    _pending(tmp_path)
    write_session_credentials(tmp_path / "session.json", "saved-at", "saved-rt")
    client = FakePairingClient(tmp_path)

    _pair(tmp_path, client)

    assert ("refresh_session", "saved-rt") in client.events
    assert "sign_in_anonymously" not in client.events
    assert json.loads((tmp_path / "session.json").read_text())["refresh_token"] == (
        "resumed-rt"
    )


def test_a_rejected_saved_session_is_replaced_by_a_new_anonymous_sign_in(tmp_path):
    # The daily purge deletes an unlinked anonymous user after a day, so a
    # pendant left pending that long resumes with a session that is gone.
    _pending(tmp_path)
    write_session_credentials(tmp_path / "session.json", "saved-at", "saved-rt")
    client = FakePairingClient(tmp_path)
    # GoTrue sends this code, but supabase_auth's ErrorCode literal omits it.
    code: Any = "refresh_token_not_found"
    client.auth.refresh_error = AuthApiError(
        "Invalid Refresh Token: Refresh Token Not Found", 400, code
    )

    assert _pair(tmp_path, client) == DEVICE_ID

    assert "sign_in_anonymously" in client.events
    assert json.loads((tmp_path / "session.json").read_text())["refresh_token"] == (
        "anon-rt"
    )


@pytest.mark.parametrize(
    "error",
    [
        # A clock behind real time makes set_session trust an expired token.
        AuthApiError("token is expired", 403, "bad_jwt"),
        AuthApiError("Bad Request", 400, None),
        AuthApiError("Request rate limit reached", 429, "over_request_rate_limit"),
        AuthRetryableError("connection refused", 0),
    ],
    ids=["bad-jwt", "uncoded-4xx", "429", "network"],
)
def test_a_saved_session_not_definitely_refused_is_kept_for_a_retry(tmp_path, error):
    # Signing in again would orphan a session that may already have claimed
    # the code, and the code works for one device user only.
    _pending(tmp_path)
    write_session_credentials(tmp_path / "session.json", "saved-at", "saved-rt")
    client = FakePairingClient(tmp_path)
    client.auth.set_session_error = AuthApiError("token is expired", 403, "bad_jwt")
    client.auth.refresh_error = error

    with pytest.raises(PairingUnavailableError):
        _pair(tmp_path, client)

    assert "sign_in_anonymously" not in client.events
    assert json.loads((tmp_path / "session.json").read_text())["refresh_token"] == (
        "saved-rt"
    )
    assert pairing_state(tmp_path) is PairingState.PENDING


def test_a_saved_session_is_resumed_even_when_the_clock_is_behind(tmp_path):
    _pending(tmp_path)
    write_session_credentials(tmp_path / "session.json", "saved-at", "saved-rt")
    client = FakePairingClient(tmp_path)
    client.auth.set_session_error = AuthApiError("token is expired", 403, "bad_jwt")

    assert _pair(tmp_path, client) == DEVICE_ID

    assert "sign_in_anonymously" not in client.events


def test_a_crash_between_saving_the_device_and_dropping_the_code_reclaims(tmp_path):
    # claim_pairing is idempotent for the device user that claimed.
    store_paired_device_id(tmp_path / "device.json", DEVICE_ID)
    write_session_credentials(tmp_path / "session.json", "saved-at", "saved-rt")
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)

    assert _pair(tmp_path, client) == DEVICE_ID

    assert ("rpc", CLAIM_RPC, {CLAIM_ARG: CODE}) in client.events
    assert not (tmp_path / "pairing.json").exists()
    assert pairing_state(tmp_path) is PairingState.PAIRED


@pytest.mark.parametrize("sqlstate", ["P0001", "42501"])
def test_a_refused_claim_is_a_rejection_that_clears_the_attempt(tmp_path, sqlstate):
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)
    client.rpc_error = APIError({"message": "invalid pairing code", "code": sqlstate})

    with pytest.raises(PairingRejectedError):
        _pair(tmp_path, client)

    assert not (tmp_path / "pairing.json").exists()
    assert not (tmp_path / "session.json").exists()
    assert pairing_state(tmp_path) is PairingState.UNPAIRED


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("no route to host"),
        APIError({"message": "Bad Gateway", "code": "502"}),
        APIError({"message": "Could not find the function", "code": "PGRST202"}),
    ],
    ids=["network", "5xx", "rpc-missing"],
)
def test_an_unreachable_claim_keeps_every_file_for_a_retry(tmp_path, error):
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)
    client.rpc_error = error

    with pytest.raises(PairingUnavailableError):
        _pair(tmp_path, client)

    assert read_pending_pairing(tmp_path / "pairing.json") == CODE
    assert (tmp_path / "session.json").exists()
    assert pairing_state(tmp_path) is PairingState.PENDING


def test_a_claim_answer_that_is_not_a_device_id_is_retried(tmp_path):
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)
    client.claimed_device_id = None

    with pytest.raises(PairingUnavailableError):
        _pair(tmp_path, client)

    assert pairing_state(tmp_path) is PairingState.PENDING


@pytest.mark.parametrize(
    "error",
    [
        AuthRetryableError("connection refused", 0),
        AuthRetryableError("Service Unavailable", 503),
        AuthApiError("Request rate limit reached", 429, "over_request_rate_limit"),
    ],
    ids=["network", "5xx", "429"],
)
def test_an_unreachable_sign_in_keeps_the_code_for_a_retry(tmp_path, error):
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)
    client.auth.sign_in_error = error

    with pytest.raises(PairingUnavailableError):
        _pair(tmp_path, client)

    assert read_pending_pairing(tmp_path / "pairing.json") == CODE
    assert pairing_state(tmp_path) is PairingState.PENDING


def test_anonymous_sign_ins_turned_off_is_logged_explicitly(tmp_path, caplog):
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)
    client.auth.sign_in_error = AuthApiError(
        "Anonymous sign-ins are disabled", 422, "anonymous_provider_disabled"
    )

    with caplog.at_level(logging.ERROR), pytest.raises(PairingUnavailableError):
        _pair(tmp_path, client)

    assert "anonymous sign-ins" in caplog.text
    assert pairing_state(tmp_path) is PairingState.PENDING


def test_the_pairing_code_is_never_logged(tmp_path, caplog):
    _pending(tmp_path)
    client = FakePairingClient(tmp_path)
    client.rpc_error = APIError({"message": "invalid pairing code", "code": "P0001"})

    with caplog.at_level(logging.DEBUG), pytest.raises(PairingRejectedError):
        _pair(tmp_path, client)

    assert CODE not in caplog.text
