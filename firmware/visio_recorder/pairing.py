"""Pairing the pendant with its own login.

The app's QR carries a one-time pairing code. Onboarding saves it to
``pairing.json``; once Wi-Fi is up the pendant signs in anonymously and claims
the code, which links its new login to the owner's device and returns that
device's id. docs/superpowers/specs/2026-11-19-visio-device-pairing-design.md
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from postgrest.exceptions import APIError
from supabase_auth.errors import AuthApiError, AuthError

from visio_recorder.device_identity import (
    drop_pairing_marker,
    load_paired_device_id,
    store_paired_device_id,
)
from visio_recorder.private_files import write_private_text_atomic
from visio_recorder.session_store import (
    LoginRefusedError,
    SessionKeeper,
    create_pendant_client,
    read_saved_session,
)

_logger = logging.getLogger(__name__)

PAIRING_FILE = "pairing.json"
SESSION_FILE = "session.json"
DEVICE_FILE = "device.json"

# Boots in a row that a pending pairing may fail to bring Wi-Fi up before the
# code is dropped and the pendant scans again. A mistyped Wi-Fi password at
# first pairing can never connect; a router that is briefly down usually
# comes back within a couple of restarts.
MAX_FAILED_PENDING_ACTIVATIONS = 3


class PairingState(Enum):
    # Nothing to resume: delete any session and scan for a QR.
    UNPAIRED = "unpaired"
    # A good scan saved a code that has not been claimed yet.
    PENDING = "pending"
    # A finished pairing: the normal boot.
    PAIRED = "paired"


def write_pending_pairing(
    path: Path, pairing_code: str, failed_activations: int = 0
) -> None:
    write_private_text_atomic(
        path,
        json.dumps(
            {"pairing_code": pairing_code, "failed_activations": failed_activations}
        ),
    )


def _read_pending(path: Path) -> tuple[str, int] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    code = data.get("pairing_code")
    if not isinstance(code, str) or not code:
        return None
    failures = data.get("failed_activations", 0)
    if type(failures) is not int or failures < 0:
        failures = 0
    return code, failures


def read_pending_pairing(path: Path) -> str | None:
    pending = _read_pending(path)
    return None if pending is None else pending[0]


def record_failed_activation(path: Path) -> bool:
    """Count a boot whose Wi-Fi did not come up; True once it is time to give up.

    Giving up deletes ``pairing.json``, so the next boot is unpaired and
    scans for a new QR (with, presumably, the right Wi-Fi password).
    """
    pending = _read_pending(path)
    if pending is None:
        return False
    code, failures = pending
    failures += 1
    if failures >= MAX_FAILED_PENDING_ACTIVATIONS:
        path.unlink(missing_ok=True)
        return True
    write_pending_pairing(path, code, failures)
    return False


def reset_failed_activations(path: Path) -> None:
    """Wi-Fi came up, so earlier failures were not a wrong password."""
    pending = _read_pending(path)
    if pending is not None and pending[1] != 0:
        write_pending_pairing(path, pending[0])


def pairing_state(data_dir: Path) -> PairingState:
    """Which of the three boot paths this data directory is on.

    A pending code wins over everything, so a power cut after the claim but
    before ``pairing.json`` was deleted resumes the claim (which is
    idempotent). A ``pairing.json`` with no readable code cannot be resumed,
    and an unreadable ``session.json`` cannot sign in, so both count as
    unpaired and the pendant scans again instead of crash-looping.
    """
    if read_pending_pairing(data_dir / PAIRING_FILE) is not None:
        return PairingState.PENDING
    if (
        load_paired_device_id(data_dir / DEVICE_FILE) is not None
        and read_saved_session(data_dir / SESSION_FILE) is not None
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


@dataclass(frozen=True)
class Pairing:
    """A boot's paired device, and the login that paired it on this boot.

    ``session`` keeps the signed-in supabase-py client when this boot ran the
    pairing, and is None on a normal paired boot, which restores the login
    from ``session.json`` instead. A pairing boot must keep using its own
    client: a second one would be a second refresher of one ``session.json``
    and rotate the refresh token out from under the first.
    """

    device_id: str
    session: SessionKeeper | None = None


class PairingRejectedError(Exception):
    """The code was refused for good; the pendant has to scan a new QR."""


class PairingUnavailableError(Exception):
    """Pairing could not finish now; retry on the next boot."""


def _is_definite_auth_rejection(err: AuthApiError) -> bool:
    return 400 <= err.status < 500 and err.status != 429


def _sign_in(session: SessionKeeper, session_path: Path) -> None:
    client = session.client
    saved = read_saved_session(session_path)
    if saved is None:
        session_path.unlink(missing_ok=True)
    else:
        try:
            session.restore(saved["refresh_token"])
            return
        except LoginRefusedError as err:
            _logger.warning(
                "the saved pairing session is gone (%s); signing in again", err
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
    client_factory: Callable[[str, str], Any] = create_pendant_client,
) -> Pairing:
    """Sign in as this pendant's own user and claim the saved pairing code.

    Returns the device id the owner's code named, with the signed-in client
    for the rest of the boot. The order makes every crash point resumable:
    the session keeper, which persists every new session, is registered
    before the sign-in so the new session reaches ``session.json`` through
    the same atomic write as every later rotation; ``device.json`` is written before
    ``pairing.json`` is deleted, and a repeated claim by the same device user
    returns the same device id.
    """
    pending_path = data_dir / PAIRING_FILE
    session_path = data_dir / SESSION_FILE
    code = read_pending_pairing(pending_path)
    if code is None:
        clear_local_pairing(data_dir)
        raise PairingRejectedError("no pending pairing code to claim")
    session = SessionKeeper(client_factory(url, key), session_path)
    _sign_in(session, session_path)
    device_id = _claim(session.client, code, data_dir)
    store_paired_device_id(data_dir / DEVICE_FILE, device_id)
    pending_path.unlink(missing_ok=True)
    _logger.info("paired with device %s", device_id)
    return Pairing(device_id=device_id, session=session)
