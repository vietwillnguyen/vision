/**
 * Live-Supabase realtime test (Stage 3 of the test-validation-plan, see
 * .lavish/test-validation-plan.html and integration/README.md's "Realtime
 * channel-error handling (app package)" section).
 *
 * Every other useDeviceStatus test (useDeviceStatus.test.tsx) drives the hook
 * against a hand-rolled fake client, so `subscribe((status) => ...)` firing
 * 'SUBSCRIBED'/'CHANNEL_ERROR' is only ever a callback *we* invoke - it never
 * proves the real `@supabase/supabase-js` realtime channel actually reports
 * those statuses against a live Postgres `postgres_changes` feed. This test
 * uses the real `createClient()` (same call as src/lib/supabase.ts) against a
 * running local Supabase instance: an authenticated device owner subscribes
 * through the real hook, then that same client - standing in for the
 * firmware daemon, which authenticates as the device owner's own session
 * rather than holding a service-role key (see
 * supabase/migrations/20260710090000_grant_device_status_writes.sql) - upserts
 * the owner's `device_status` row, and we assert the hook's `realtime` field
 * reaches 'live' and its `status` reflects the row over the wire - not a
 * mocked channel callback.
 *
 * Needs SUPABASE_URL, SUPABASE_ANON_KEY, SUPABASE_SERVICE_ROLE_KEY pointing
 * at a `supabase start` instance (same env vars as integration/'s Python live
 * tests); skips automatically otherwise, e.g. in sandboxes without Docker.
 */
import { renderHook, waitFor } from '@testing-library/react-native';
import { createClient, type SupabaseClient } from '@supabase/supabase-js';
import WebSocket from 'ws';
import nodeFetch from 'node-fetch';

import { useDeviceStatus } from '../../src/hooks/useDeviceStatus';

// Node 20 (this test runner's target) has no native WebSocket global, which
// @supabase/realtime-js requires at RealtimeClient construction time (thrown
// synchronously from createClient, even for clients that never subscribe).
(globalThis as { WebSocket?: unknown }).WebSocket ??= WebSocket;

// jest-expo's preset replaces global.fetch with React Native's XHR-based
// polyfill, which silently fails (returns a response with no status/body)
// against a plain http:// localhost URL outside a real RN runtime. The REST
// calls below (auth/admin, postgrest) need a real fetch; the hook's realtime
// subscription itself only depends on WebSocket, so this doesn't affect what
// this test is actually verifying.
const restFetch = nodeFetch as unknown as typeof fetch;

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
  // GitHub-hosted runners replicate postgres_changes noticeably slower than
  // this suite's local dev sandbox (where 15s was always plenty) - the first
  // real CI run of this test (PR #24) timed out waiting for the post-upsert
  // update to arrive over realtime, even though the identical assertion
  // against the seeded row passed well within the same window. A first fix
  // raised the post-upsert waitFor to 30s, but that still wasn't enough on a
  // subsequent CI run (received the pre-upsert seed values unchanged after
  // the full 30s), which only makes sense as replication delay: this `app`
  // job's `npx jest --ci` fans out ~30 test suites across parallel workers on
  // a 2-vCPU GH-hosted runner, and this is the only suite whose assertion
  // depends on the local Supabase `realtime` container's WAL delivery, which
  // is CPU-scheduled same as everything else - under that contention its
  // latency doesn't just track a bit above local, it can spike well past 30s.
  // Give the upsert's realtime propagation a much larger margin without
  // loosening the assertion itself. Set well above the sum of both waitFor
  // timeouts (15000 + 90000) so the rest of the test's setup/teardown work
  // (admin createUser, inserts, sign-in, upsert, unmount, disconnect,
  // deleteUser) doesn't get squeezed out by Jest's own test-level timeout.
  jest.setTimeout(120000);

  it('reaches live realtime and reflects a real postgres_changes upsert', async () => {
    const runId = Math.random().toString(36).slice(2, 10);
    const email = `device-status-live-${runId}@example.test`;
    const password = 'correct-horse-battery-staple';

    const adminClient = createClient(SUPABASE_URL as string, SUPABASE_SERVICE_ROLE_KEY as string, {
      global: { fetch: restFetch },
    });
    const { data: created, error: createUserError } = await adminClient.auth.admin.createUser({
      email,
      password,
      email_confirm: true,
    });
    if (createUserError || !created.user) {
      throw createUserError ?? new Error('failed to create test user');
    }
    const userId = created.user.id;

    let ownerClient: SupabaseClient | undefined;
    try {
      const { data: device, error: deviceError } = await adminClient
        .from('devices')
        .insert({ user_id: userId, name: `live device status test ${runId}` })
        .select()
        .single();
      if (deviceError || !device) {
        throw deviceError ?? new Error('failed to create test device');
      }
      const deviceId = device.device_id as string;

      // Seed an initial row before the hook ever subscribes, mirroring real
      // device behavior (firmware upserts a status row well before the app
      // opens it). The hook only exposes its `realtime` field once its own
      // initial fetch resolves to `kind: 'ready'` (see useDeviceStatus.ts) - with
      // no row at all it stays `{ kind: 'loading' }` forever, so `realtime`
      // would never appear in the returned object regardless of the actual
      // channel status.
      const { error: seedError } = await adminClient.from('device_status').insert({
        device_id: deviceId,
        battery_pct: 10,
        storage_used_gb: 0,
        storage_free_gb: 100,
        segments_pending: 0,
        segments_uploaded_today: 0,
        recording_active: false,
      });
      if (seedError) throw seedError;

      const client = createClient(SUPABASE_URL as string, SUPABASE_ANON_KEY as string, {
        global: { fetch: restFetch },
      });
      ownerClient = client;
      const { error: signInError } = await client.auth.signInWithPassword({ email, password });
      if (signInError) throw signInError;

      const { result, unmount } = renderHook(() => useDeviceStatus(client, deviceId));

      await waitFor(
        () =>
          expect(result.current).toMatchObject({
            kind: 'ready',
            realtime: 'live',
            status: { batteryPct: 10, recordingActive: false },
          }),
        { timeout: 15000 },
      );

      const { error: upsertError } = await client.from('device_status').upsert({
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
            realtime: 'live',
            status: { batteryPct: 55, recordingActive: true },
          }),
        { timeout: 90000 },
      );

      unmount();
    } finally {
      // The hook's own cleanup only unsubscribes/tears down its channel
      // (RealtimeClient.removeChannel never closes the underlying socket -
      // see node_modules/@supabase/realtime-js's RealtimeClient.removeChannel),
      // so the websocket connection created by createClient() above stays
      // open and leaves Jest hanging ("did not exit one second after the
      // test run has completed") without this explicit disconnect.
      if (ownerClient) {
        await ownerClient.realtime.disconnect();
      }
      await adminClient.auth.admin.deleteUser(userId);
    }
  });
});
