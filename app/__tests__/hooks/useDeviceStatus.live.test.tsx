/**
 * Live-Supabase realtime test (Stage 3 of the test-validation-plan, see
 * .lavish/test-validation-plan.html and integration/README.md's "Deferred"
 * section).
 *
 * Every other useDeviceStatus test (useDeviceStatus.test.tsx) drives the hook
 * against a hand-rolled fake client, so `subscribe((status) => ...)` firing
 * 'SUBSCRIBED'/'CHANNEL_ERROR' is only ever a callback *we* invoke - it never
 * proves the real `@supabase/supabase-js` realtime channel actually reports
 * those statuses against a live Postgres `postgres_changes` feed. This test
 * uses the real `createClient()` (same call as src/lib/supabase.ts) against a
 * running local Supabase instance: an authenticated device owner subscribes
 * through the real hook, then a second real client upserts that owner's
 * `device_status` row (the firmware daemon's write path, granted by
 * supabase/migrations/20260710090000_grant_device_status_writes.sql), and we
 * assert the hook's `realtime` field reaches 'live' and its `status` reflects
 * the row over the wire - not a mocked channel callback.
 *
 * Needs SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY pointing
 * at a `supabase start` instance (same env vars as integration/'s Python live
 * tests); skips automatically otherwise, e.g. in sandboxes without Docker.
 */
import { renderHook, waitFor } from '@testing-library/react-native';
import { createClient } from '@supabase/supabase-js';

import { useDeviceStatus } from '../../src/hooks/useDeviceStatus';

const SUPABASE_URL = process.env.SUPABASE_URL;
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY;
const SUPABASE_SERVICE_ROLE_KEY = process.env.SUPABASE_SERVICE_ROLE_KEY;

const hasLiveEnv = Boolean(SUPABASE_URL && SUPABASE_ANON_KEY && SUPABASE_SERVICE_ROLE_KEY);
const describeLive = hasLiveEnv ? describe : describe.skip;

if (!hasLiveEnv) {
  // eslint-disable-next-line no-console
  console.warn(
    'Skipping useDeviceStatus.live.test.tsx: SUPABASE_URL / SUPABASE_ANON_KEY / ' +
      'SUPABASE_SERVICE_ROLE_KEY not set - needs a running `supabase start` instance.',
  );
}

describeLive('useDeviceStatus against a live Supabase instance', () => {
  jest.setTimeout(20000);

  it('reaches live realtime and reflects a real postgres_changes upsert', async () => {
    const runId = Math.random().toString(36).slice(2, 10);
    const email = `device-status-live-${runId}@example.test`;
    const password = 'correct-horse-battery-staple';

    const adminClient = createClient(SUPABASE_URL as string, SUPABASE_SERVICE_ROLE_KEY as string);
    const { data: created, error: createUserError } = await adminClient.auth.admin.createUser({
      email,
      password,
      email_confirm: true,
    });
    if (createUserError || !created.user) {
      throw createUserError ?? new Error('failed to create test user');
    }
    const userId = created.user.id;

    const { data: device, error: deviceError } = await adminClient
      .from('devices')
      .insert({ user_id: userId, name: `live device status test ${runId}` })
      .select()
      .single();
    if (deviceError || !device) {
      throw deviceError ?? new Error('failed to create test device');
    }
    const deviceId = device.device_id as string;

    try {
      const ownerClient = createClient(SUPABASE_URL as string, SUPABASE_ANON_KEY as string);
      const { error: signInError } = await ownerClient.auth.signInWithPassword({ email, password });
      if (signInError) throw signInError;

      const { result, unmount } = renderHook(() => useDeviceStatus(ownerClient, deviceId));

      await waitFor(() => expect(result.current).toMatchObject({ realtime: 'live' }), {
        timeout: 15000,
      });

      const { error: upsertError } = await ownerClient.from('device_status').upsert({
        device_id: deviceId,
        battery_pct: 55,
        storage_used_gb: 3.5,
        storage_free_gb: 100,
        segments_pending: 2,
        segments_uploaded_today: 7,
        recording_active: true,
      });
      if (upsertError) throw upsertError;

      await waitFor(
        () =>
          expect(result.current).toMatchObject({
            kind: 'ready',
            status: { batteryPct: 55, recordingActive: true },
          }),
        { timeout: 15000 },
      );

      unmount();
    } finally {
      await adminClient.auth.admin.deleteUser(userId);
    }
  });
});
