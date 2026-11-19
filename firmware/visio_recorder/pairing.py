"""Pairing the pendant with its own login.

The app's QR carries a one-time pairing code. Onboarding saves it to
``pairing.json``; once Wi-Fi is up the pendant signs in anonymously and claims
the code, which links its new login to the owner's device and returns that
device's id. docs/superpowers/specs/2026-11-19-visio-device-pairing-design.md
"""

import json
import logging
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from postgrest.exceptions import APIError
from supabase import create_client
from supabase_auth.errors import AuthApiError, AuthError

from visio_recorder.device_identity import (
    drop_pairing_marker,
    load_paired_device_id,
    store_paired_device_id,
)
from visio_recorder.private_files import write_private_text_atomic
from visio_recorder.session_store import read_saved_session, session_persister

_logger = logging.getLogger(__name__)

PAIRING_FILE = "pairing.json"
SESSION_FILE = "session.json"
DEVICE_FILE = "device.json"


class PairingState(Enum):
    # Nothing to resume: delete any session and scan for a QR.
    UNPAIRED = "unpaired"
    # A good scan saved a code that has not been claimed yet.
    PENDING = "pending"
    # A finished pairing: the normal boot.
    PAIRED = "paired"


def write_pending_pairing(path: Path, pairing_code: str) -> None:
    write_private_text_atomic(path, json.dumps({"pairing_code": pairing_code}))


def read_pending_pairing(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("pairing_code")
    return code if isinstance(code, str) and code else None


def pairing_state(data_dir: Path) -> PairingState:
    """Which of the three boot paths this data directory is on.

    A pending code wins over everything, so a power cut after the claim but
    before ``pairing.json`` was deleted resumes the claim (which is
    idempotent). A ``pairing.json`` with no readable code cannot be resumed,
    so it counts as unpaired and the pendant scans again.
    """
    if read_pending_pairing(data_dir / PAIRING_FILE) is not None:
        return PairingState.PENDING
    if (
        load_paired_device_id(data_dir / DEVICE_FILE) is not None
        and (data_dir / SESSION_FILE).exists()
    ):
        return PairingState.PAIRED
    return PairingState.UNPAIRED


def clear_local_pairing(data_dir: Path) -> None:
    """Forget this pendant's login and pairing; the queue is left alone.

    Queued footage uploads to whichever account the pendant is next paired
    to, which adds no exposure: whoever holds the pendant holds the SD card.
    """
    (data_dir / SESSION_FILE).unlink(missing_ok=True)
    (data_dir / PAIRING_FILE).unlink(missing_ok=True)
    drop_pairing_marker(data_dir / DEVICE_FILE)


# The RPC and argument name firmware sends; integration/tests/
# test_row_schema_contract.py checks both against the migration.
CLAIM_RPC = "claim_pairing"
CLAIM_ARG = "p_code"

# The errors claim_pairing raises itself: a wrong, expired or used code, or a
# login that is not a device session. supabase-py does not expose the HTTP
# status of a PostgREST error, so a definite refusal is recognised by its
# SQLSTATE. Anything else (a proxy's 5xx, PostgREST's own PGRST codes such as
# a migration not applied yet) is not the code's fault and is retried.
_REJECTION_SQLSTATES = frozenset({"P0001", "42501"})


class PairingRejectedError(Exception):
    """The code was refused for good; the pendant has to scan a new QR."""


class PairingUnavailableError(Exception):
    """Pairing could not finish now; retry on the next boot."""


def _is_definite_auth_rejection(err: AuthApiError) -> bool:
    return 400 <= err.status < 500 and err.status != 429


def _sign_in(client: Any, session_path: Path) -> None:
    saved = read_saved_session(session_path)
    if saved is not None:
        try:
            client.auth.set_session(saved["access_token"], saved["refresh_token"])
            return
        except AuthApiError as err:
            if not _is_definite_auth_rejection(err):
                raise PairingUnavailableError(
                    f"could not resume the pairing session: {err.message}"
                ) from err
            _logger.warning(
                "the saved pairing session was refused (%s); signing in again",
                err.status,
            )
            session_path.unlink(missing_ok=True)
        except (AuthError, httpx.HTTPError) as err:
            raise PairingUnavailableError(
                f"could not resume the pairing session: {err}"
            ) from err
    try:
        client.auth.sign_in_anonymously()
    except AuthApiError as err:
        if _is_definite_auth_rejection(err):
            _logger.error(
                "anonymous sign-in was refused (%s %s); enable anonymous sign-ins "
                "on the Supabase project (Authentication, Sign In / Providers)",
                err.status,
                err.message,
            )
        raise PairingUnavailableError(
            f"anonymous sign-in failed: {err.message}"
        ) from err
    except (AuthError, httpx.HTTPError) as err:
        raise PairingUnavailableError(f"anonymous sign-in failed: {err}") from err


def _claim(client: Any, code: str, data_dir: Path) -> str:
    try:
        response = client.rpc(CLAIM_RPC, {CLAIM_ARG: code}).execute()
    except APIError as err:
        if err.code in _REJECTION_SQLSTATES:
            clear_local_pairing(data_dir)
            raise PairingRejectedError(
                f"the pairing code was refused ({err.code}: {err.message}); "
                "show a new QR from the app"
            ) from err
        raise PairingUnavailableError(
            f"claiming the pairing code failed ({err.code}: {err.message})"
        ) from err
    except (AuthError, httpx.HTTPError) as err:
        raise PairingUnavailableError(
            f"claiming the pairing code failed: {err}"
        ) from err
    device_id = response.data
    if not isinstance(device_id, str) or not device_id:
        raise PairingUnavailableError(
            "claiming the pairing code returned no device id "
            f"({type(device_id).__name__})"
        )
    return device_id


def complete_pairing(
    url: str,
    key: str,
    data_dir: Path,
    client_factory: Callable[[str, str], Any] = create_client,
) -> str:
    """Sign in as this pendant's own user and claim the saved pairing code.

    Returns the device id the owner's code named. The order makes every
    crash point resumable: the session persister is registered before the
    sign-in so the new session reaches ``session.json`` through the same
    atomic write as every later rotation; ``device.json`` is written before
    ``pairing.json`` is deleted, and a repeated claim by the same device user
    returns the same device id.
    """
    pending_path = data_dir / PAIRING_FILE
    session_path = data_dir / SESSION_FILE
    code = read_pending_pairing(pending_path)
    if code is None:
        clear_local_pairing(data_dir)
        raise PairingRejectedError("no pending pairing code to claim")
    client = client_factory(url, key)
    client.auth.on_auth_state_change(session_persister(session_path))
    _sign_in(client, session_path)
    device_id = _claim(client, code, data_dir)
    store_paired_device_id(data_dir / DEVICE_FILE, device_id)
    pending_path.unlink(missing_ok=True)
    _logger.info("paired with device %s", device_id)
    return device_id
