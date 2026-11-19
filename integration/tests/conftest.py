"""Shared fixtures for tests that need a live local Supabase instance.

Tests that require this skip automatically when the instance isn't reachable
(e.g. local sandboxes without Docker) rather than failing the run - see
integration/README.md for which tests these are.
"""

import os
from collections.abc import Iterator
from typing import Any, cast

import pytest
from supabase import create_client

from tests.pendant import (
    PairedDevice,
    boot_pendant,
    create_owner,
    delete_owner,
    pair_pendant,
    sign_in_owner,
)


def _live_supabase_env() -> dict[str, str] | None:
    url = os.environ.get("SUPABASE_URL")
    service_role_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    anon_key = os.environ.get("SUPABASE_ANON_KEY")
    if not (url and service_role_key and anon_key):
        return None
    return {
        "url": url,
        "service_role_key": service_role_key,
        "anon_key": anon_key,
    }


@pytest.fixture(scope="session")
def live_supabase_env():
    env = _live_supabase_env()
    if env is None:
        pytest.skip(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / SUPABASE_ANON_KEY not set - "
            "needs a running `supabase start` instance (see integration/README.md)"
        )
    return env


@pytest.fixture
def paired_device(live_supabase_env, tmp_path) -> Iterator[PairedDevice]:
    """An owner with a device row and a pendant paired to it, booted."""
    admin = create_client(
        live_supabase_env["url"], live_supabase_env["service_role_key"]
    )
    owner_id, email = create_owner(admin, "paired-owner")
    try:
        inserted = cast(
            list[dict[str, Any]],
            admin.table("devices")
            .insert({"user_id": owner_id, "name": "paired test device"})
            .execute()
            .data,
        )
        device_id = str(inserted[0]["device_id"])
        owner = sign_in_owner(live_supabase_env, email)
        data_dir = tmp_path / "pendant"
        data_dir.mkdir()
        assert pair_pendant(live_supabase_env, owner, data_dir, device_id) == device_id
        yield PairedDevice(
            env=live_supabase_env,
            admin=admin,
            owner=owner,
            owner_id=owner_id,
            device_id=device_id,
            data_dir=data_dir,
            pendant=boot_pendant(live_supabase_env, data_dir),
        )
    finally:
        delete_owner(admin, owner_id)
