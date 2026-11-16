#!/usr/bin/env bash
# Seed a local Supabase stack with one user, one device, its status, today's
# segments and a reel, so the expo-web screenshots show populated screens.
# Usage: SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... ./seed-screens.sh
# Prints the device id. Idempotent for the user: re-running reuses it.
set -euo pipefail

: "${SUPABASE_URL:?}" "${SUPABASE_SERVICE_ROLE_KEY:?}"
email="${SEED_EMAIL:-screens@example.com}"
password="${SEED_PASSWORD:-screens-pass-123}"
auth=(-H "apikey: $SUPABASE_SERVICE_ROLE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_ROLE_KEY" -H "Content-Type: application/json")

curl -s "${auth[@]}" -X POST "$SUPABASE_URL/auth/v1/admin/users" \
  -d "{\"email\":\"$email\",\"password\":\"$password\",\"email_confirm\":true}" >/dev/null
user_id=$(curl -s "${auth[@]}" "$SUPABASE_URL/auth/v1/admin/users?per_page=1000" |
  python3 -c "import json,sys; print(next(u['id'] for u in json.load(sys.stdin)['users'] if u['email']=='$email'))")

rest() { curl -s "${auth[@]}" -H "Prefer: return=representation,resolution=merge-duplicates" "$@"; }
rest -X DELETE "$SUPABASE_URL/rest/v1/devices?user_id=eq.$user_id" >/dev/null
device_id=$(rest -X POST "$SUPABASE_URL/rest/v1/devices" -d "{\"user_id\":\"$user_id\",\"name\":\"Visio pendant\"}" |
  python3 -c "import json,sys; print(json.load(sys.stdin)[0]['device_id'])")
rest -X POST "$SUPABASE_URL/rest/v1/device_status" -d "{\"device_id\":\"$device_id\",\"battery_pct\":82,\"storage_used_gb\":3.4,\"storage_free_gb\":24.6,\"segments_pending\":2,\"segments_uploaded_today\":14,\"recording_active\":true}" >/dev/null

today=$(date -u +%F)
segments_json=$(mktemp)
trap 'rm -f "$segments_json"' EXIT
python3 - "$device_id" "$today" <<'EOF' >"$segments_json"
import json, sys
device, day = sys.argv[1], sys.argv[2]
rows = []
for i, minute in enumerate([8 * 60 + 5, 9 * 60 + 30, 12 * 60 + 15, 15 * 60 + 40, 18 * 60 + 10]):
    rows.append({
        "device_id": device,
        "recorded_at": f"{day}T{minute // 60:02d}:{minute % 60:02d}:00Z",
        "duration_sec": 30,
        "s3_key": f"{device}/{day}/seg-{i}.mp4",
        "composite_score": round(4 + i * 1.1, 2),
    })
print(json.dumps(rows))
EOF
rest -X POST "$SUPABASE_URL/rest/v1/segments" -d @"$segments_json" >/dev/null
rest -X POST "$SUPABASE_URL/rest/v1/reels" -d "{\"device_id\":\"$device_id\",\"date\":\"$today\",\"duration_sec\":60,\"s3_key\":\"$device_id/$today/reel.mp4\"}" >/dev/null
echo "$device_id"
