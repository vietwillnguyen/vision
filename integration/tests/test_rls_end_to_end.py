"""RLS end-to-end test against a live local Supabase instance (Stage 2/3 of
the test-validation-plan, see .lavish/test-validation-plan.html).

Covers the plan's remaining "Now covered locally" bullet that pgTAP's
per-assertion policy tests don't: "RLS still permits each real
service-role/authenticated flow end to end, not just per pgTAP assertion".

This drives pipeline's real `pipeline.adapters.supabase_store.SupabaseStore`
- the same class the orchestrator uses in production - first as service_role
(the pipeline's write path: persist_segments/insert_reel), then as two real
authenticated users (a real Postgres auth.uid() identity, not a mocked JWT)
to prove the owner can read what pipeline wrote and a non-owner cannot, via
the `segments_owner_access`/`reels_owner_access` policies in
supabase/migrations/20260704120500_rls_policies.sql.

Needs a running `supabase start` instance; skips automatically otherwise
(see conftest.py and integration/README.md).
"""

import uuid
from datetime import UTC, date, datetime
from typing import Any, cast

import pytest
from supabase import create_client

from pipeline.adapters.supabase_store import SupabaseStore

pytestmark = pytest.mark.usefixtures("live_supabase_env")


def _make_owner_user(admin_client, email: str, password: str) -> str:
    response = admin_client.auth.admin.create_user(
        {"email": email, "password": password, "email_confirm": True}
    )
    return response.user.id


def _sign_in_store(url: str, anon_key: str, email: str, password: str) -> SupabaseStore:
    client = create_client(url, anon_key)
    client.auth.sign_in_with_password({"email": email, "password": password})
    return SupabaseStore(client)


def test_owner_can_read_pipeline_written_rows_but_other_user_cannot(
    live_supabase_env,
):
    url = live_supabase_env["url"]
    admin_client = create_client(url, live_supabase_env["service_role_key"])
    run_id = uuid.uuid4().hex[:8]
    owner_email = f"rls-owner-{run_id}@example.test"
    other_email = f"rls-other-{run_id}@example.test"
    password = "correct-horse-battery-staple"

    owner_id = _make_owner_user(admin_client, owner_email, password)
    other_id = _make_owner_user(admin_client, other_email, password)

    try:
        # supabase-py types APIResponse.data as bare JSON (it can be any JSON
        # value), so the row shape has to be narrowed here before it is indexed.
        inserted = cast(
            list[dict[str, Any]],
            admin_client.table("devices")
            .insert({"user_id": owner_id, "name": f"RLS test device {run_id}"})
            .execute()
            .data,
        )
        device_id = str(inserted[0]["device_id"])

        day = date(2026, 7, 14)
        recorded_at = datetime(2026, 7, 14, 9, 0, 0, tzinfo=UTC)
        service_store = SupabaseStore(admin_client)
        service_store.persist_segments(
            [
                {
                    "device_id": device_id,
                    "recorded_at": recorded_at.isoformat(),
                    "duration_sec": 60,
                    "s3_key": f"{device_id}/{run_id}.mp4",
                    "composite_score": 5.0,
                    "manually_flagged": False,
                }
            ]
        )
        service_store.insert_reel(
            {
                "device_id": device_id,
                "date": day.isoformat(),
                "s3_key": f"{device_id}/{day.strftime('%Y%m%d')}.mp4",
                "duration_sec": 60,
            }
        )

        owner_store = _sign_in_store(
            url, live_supabase_env["anon_key"], owner_email, password
        )
        owner_rows = owner_store.list_segment_rows(device_id, day)
        assert len(owner_rows) == 1, (
            "owner should read the segment row pipeline wrote via service_role"
        )

        other_store = _sign_in_store(
            url, live_supabase_env["anon_key"], other_email, password
        )
        other_rows = other_store.list_segment_rows(device_id, day)
        assert other_rows == [], (
            "a different authenticated user must not see another owner's segments"
        )
    finally:
        admin_client.auth.admin.delete_user(owner_id)
        admin_client.auth.admin.delete_user(other_id)
