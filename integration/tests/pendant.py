"""Pairing a pendant through the real flow, for the live suites.

The owner's app makes a code, the QR payload goes through firmware's parser,
and the pendant signs in anonymously and claims it, exactly as on hardware.
docs/superpowers/specs/2026-11-19-visio-device-pairing-design.md
"""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from supabase import Client, create_client

from visio_recorder.pairing import (
    PAIRING_FILE,
    SESSION_FILE,
    complete_pairing,
    write_pending_pairing,
)
from visio_recorder.supabase_clients import SupabaseClients, build_supabase_clients
from visio_recorder.wifi_onboard import parse_onboarding_qr_payload

PASSWORD = "correct-horse-battery-staple"


def create_owner(admin: Client, label: str) -> tuple[str, str]:
    """A confirmed email-and-password account; returns (user id, email)."""
    email = f"{label}-{uuid.uuid4().hex[:10]}@example.test"
    user = admin.auth.admin.create_user(
        {"email": email, "password": PASSWORD, "email_confirm": True}
    ).user
    return user.id, email


def sign_in_owner(env: dict[str, str], email: str) -> Client:
    client = create_client(env["url"], env["anon_key"])
    client.auth.sign_in_with_password({"email": email, "password": PASSWORD})
    return client


def delete_owner(admin: Client, owner_id: str) -> None:
    """Remove an owner, its pendants' logins and every object they stored."""
    devices = cast(
        list[dict[str, Any]],
        admin.table("devices")
        .select("device_id, device_user_id")
        .eq("user_id", owner_id)
        .execute()
        .data,
    )
    for device in devices:
        for bucket in ("segments", "reels"):
            listed = admin.storage.from_(bucket).list(device["device_id"])
            names = [f"{device['device_id']}/{item['name']}" for item in listed]
            if names:
                admin.storage.from_(bucket).remove(names)
        if device["device_user_id"] is not None:
            admin.auth.admin.delete_user(device["device_user_id"])
    admin.auth.admin.delete_user(owner_id)


def make_pairing_qr(owner_client: Client, device_id: str | None) -> str:
    """What ReonboardContainer.tsx renders: QR payload v2."""
    rows = cast(
        list[dict[str, Any]],
        owner_client.rpc("create_pairing_code", {"p_device_id": device_id})
        .execute()
        .data,
    )
    return json.dumps(
        {
            "v": 2,
            "ssid": "home",
            "password": "password1",
            "pairing_code": rows[0]["code"],
        }
    )


def pair_pendant(
    env: dict[str, str],
    owner_client: Client,
    data_dir: Path,
    device_id: str | None = None,
) -> str:
    """The whole pairing as the app and the pendant do it; returns the device id."""
    payload = parse_onboarding_qr_payload(make_pairing_qr(owner_client, device_id))
    write_pending_pairing(data_dir / PAIRING_FILE, payload.pairing_code)
    return complete_pairing(env["url"], env["anon_key"], data_dir)


@dataclass
class BootedPendant:
    client: Client
    clients: SupabaseClients


def boot_pendant(env: dict[str, str], data_dir: Path) -> BootedPendant:
    """One daemon boot from ``data_dir``, keeping the underlying client."""
    holder: dict[str, Client] = {}

    def factory(url: str, key: str) -> Client:
        holder["client"] = create_client(url, key)
        return holder["client"]

    clients = build_supabase_clients(
        env["url"], env["anon_key"], data_dir / SESSION_FILE, client_factory=factory
    )
    return BootedPendant(client=holder["client"], clients=clients)


@dataclass
class PairedDevice:
    env: dict[str, str]
    admin: Client
    owner: Client
    owner_id: str
    device_id: str
    data_dir: Path
    pendant: BootedPendant
