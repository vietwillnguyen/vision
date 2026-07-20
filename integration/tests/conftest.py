"""Shared fixtures for tests that need a live local Supabase instance.

Tests that require this skip automatically when the instance isn't reachable
(e.g. local sandboxes without Docker) rather than failing the run - see
integration/README.md for which tests these are.
"""

import os

import pytest


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
