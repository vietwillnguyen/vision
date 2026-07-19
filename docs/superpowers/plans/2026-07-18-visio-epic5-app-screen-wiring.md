# Visio Epic 5 App Screen Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Epic 3 presentational app components into a working Expo app: bottom tab navigation, real Supabase auth with session persistence, video playback, timeline thumbnails, realtime-health surfacing, styling, and UTC-safe date handling (GitHub issue #8).

**Architecture:** Presentational components stay pure (props in, callbacks out); all I/O moves into hooks (`src/hooks/`) that take a `SupabaseClient` parameter so tests inject fakes.
Container components (`src/containers/`) bind hooks to presentational components, and `App.tsx` composes containers behind an auth gate and a bottom tab navigator.
Every hook returns a discriminated-union state (`kind: 'loading' | 'error' | ...`) so screens render loading and error states explicitly.

**Tech Stack:** Expo SDK 57, React Native 0.86, TypeScript strict, `@supabase/supabase-js` v2, `@react-navigation/bottom-tabs` v7, `expo-video`, `expo-video-thumbnails`, `expo-media-library`, `expo-file-system`, `@react-native-async-storage/async-storage`, jest-expo + `@testing-library/react-native`.

## Global Constraints

- Playback uses `expo-video`, never the deprecated `expo-av` (issue #8; app plan Handoff).
- Supabase client must pass `{ auth: { storage: AsyncStorage, persistSession: true, detectSessionInUrl: false } }` to `createClient` (app plan Handoff, verbatim), plus `autoRefreshToken: true`.
- Archive heatmap date ranges must be normalized to UTC midnight before calling `buildHeatmapCells` (its date strings are `toISOString().slice(0, 10)` and its loop assumes midnight-aligned bounds).
- Tabs, in order: Today's Reel, Raw Footage, Device, Archive.
- Realtime channel statuses `CHANNEL_ERROR`, `TIMED_OUT`, `CLOSED` must surface to the UI as a stale indicator, never go silent.
- Every interactive element gets an `accessibilityLabel`; screens get StyleSheet styling (they currently render unstyled by design).
- The regenerate bottom sheet and the WiFi re-onboarding QR screen are **out of scope** (no backend consumer / separate issue); the existing Re-onboard button shows a "not available yet" alert.
- Storage buckets are `segments` and `reels`; object keys follow `{device_id}/{filename}` and RLS resolves the first path folder to the owning user.
- All hooks accept `client: SupabaseClient` as their first parameter (existing `useDeviceStatus` convention) so tests inject fakes; no module-level `supabase` import inside hooks or containers.
- Run all commands from `app/`.
- TDD per task: failing test first, then minimal implementation, then commit.

---

### Task 1: Dependencies, session persistence, theme

**Files:**
- Modify: `app/package.json` (via `npx expo install`)
- Modify: `app/src/lib/supabase.ts`
- Create: `app/src/theme.ts`
- Test: `app/__tests__/lib/supabase.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `supabaseClientOptions` (exported const in `src/lib/supabase.ts`); `colors` and `spacing` (exported consts in `src/theme.ts`) used by every styled component in later tasks.

- [ ] **Step 1: Install dependencies**

```bash
npx expo install @react-native-async-storage/async-storage expo-video expo-video-thumbnails expo-media-library expo-file-system
```

Expected: package.json gains the five deps at SDK-57-compatible versions.

- [ ] **Step 2: Write the failing test**

`app/__tests__/lib/supabase.test.ts`:

```ts
import AsyncStorage from '@react-native-async-storage/async-storage';

import { supabaseClientOptions } from '../../src/lib/supabase';

describe('supabaseClientOptions', () => {
  it('persists sessions to AsyncStorage', () => {
    expect(supabaseClientOptions.auth).toEqual({
      storage: AsyncStorage,
      persistSession: true,
      detectSessionInUrl: false,
      autoRefreshToken: true,
    });
  });
});
```

- [ ] **Step 3: Run test to verify it fails**

Run: `npx jest __tests__/lib/supabase.test.ts`
Expected: FAIL - `supabaseClientOptions` is not exported.
Note: the module reads `process.env.EXPO_PUBLIC_SUPABASE_URL`; if `createClient` throws on undefined env in tests, set placeholder env in the test via `process.env.EXPO_PUBLIC_SUPABASE_URL = 'https://placeholder.supabase.co'; process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY = 'placeholder';` **before** the import (use `require` after setting env instead of a top-level import if needed).

- [ ] **Step 4: Write minimal implementation**

`app/src/lib/supabase.ts`:

```ts
import AsyncStorage from '@react-native-async-storage/async-storage';
import { createClient } from '@supabase/supabase-js';

const SUPABASE_URL = process.env.EXPO_PUBLIC_SUPABASE_URL as string;
const SUPABASE_ANON_KEY = process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY as string;

// React Native has no URL-based session detection and needs explicit storage,
// or sessions will not survive app restarts.
export const supabaseClientOptions = {
  auth: {
    storage: AsyncStorage,
    persistSession: true,
    detectSessionInUrl: false,
    autoRefreshToken: true,
  },
};

export const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, supabaseClientOptions);
```

`app/src/theme.ts`:

```ts
export const colors = {
  background: '#0F1115',
  surface: '#1A1E26',
  text: '#F2F4F8',
  textMuted: '#8B93A5',
  accent: '#4F8EF7',
  danger: '#E5484D',
  success: '#3DD68C',
  warning: '#F5A623',
};

export const spacing = {
  xs: 4,
  sm: 8,
  md: 16,
  lg: 24,
};
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `npx jest __tests__/lib/supabase.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add package.json package-lock.json src/lib/supabase.ts src/theme.ts __tests__/lib/supabase.test.ts
git commit -m "feat(app): add Epic 5 deps, AsyncStorage session persistence, theme"
```

---

### Task 2: UTC date helpers

**Files:**
- Create: `app/src/logic/dates.ts`
- Test: `app/__tests__/logic/dates.test.ts`

**Interfaces:**
- Consumes: nothing.
- Produces: `toUtcMidnight(d: Date): Date`; `utcDateString(d: Date): string` (returns `YYYY-MM-DD`); `utcRangeEndingAt(end: Date, days: number): { start: Date; end: Date }` (both UTC-midnight-aligned, `days` total cells inclusive of `end`'s day).

- [ ] **Step 1: Write the failing tests**

`app/__tests__/logic/dates.test.ts`:

```ts
import { toUtcMidnight, utcDateString, utcRangeEndingAt } from '../../src/logic/dates';

describe('toUtcMidnight', () => {
  it('truncates a mid-day timestamp to 00:00:00.000 UTC', () => {
    const d = new Date('2026-07-18T17:45:12.345Z');
    expect(toUtcMidnight(d).toISOString()).toBe('2026-07-18T00:00:00.000Z');
  });

  it('keeps the UTC calendar day for a local-time west-of-UTC evening', () => {
    // 2026-07-18T23:30-07:00 is 2026-07-19T06:30Z; UTC day is the 19th.
    const d = new Date('2026-07-19T06:30:00.000Z');
    expect(toUtcMidnight(d).toISOString()).toBe('2026-07-19T00:00:00.000Z');
  });
});

describe('utcDateString', () => {
  it('formats as YYYY-MM-DD in UTC', () => {
    expect(utcDateString(new Date('2026-07-18T17:45:12.345Z'))).toBe('2026-07-18');
  });
});

describe('utcRangeEndingAt', () => {
  it('returns a midnight-aligned inclusive range of N days', () => {
    const { start, end } = utcRangeEndingAt(new Date('2026-07-18T17:45:12.345Z'), 30);
    expect(end.toISOString()).toBe('2026-07-18T00:00:00.000Z');
    expect(start.toISOString()).toBe('2026-06-19T00:00:00.000Z');
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/logic/dates.test.ts`
Expected: FAIL - module not found.

- [ ] **Step 3: Write minimal implementation**

`app/src/logic/dates.ts`:

```ts
export function toUtcMidnight(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

export function utcDateString(d: Date): string {
  return toUtcMidnight(d).toISOString().slice(0, 10);
}

export function utcRangeEndingAt(end: Date, days: number): { start: Date; end: Date } {
  const endMidnight = toUtcMidnight(end);
  const start = new Date(endMidnight);
  start.setUTCDate(start.getUTCDate() - (days - 1));
  return { start, end: endMidnight };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/logic/dates.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/logic/dates.ts __tests__/logic/dates.test.ts
git commit -m "feat(app): add UTC-midnight date helpers for heatmap and day queries"
```

---

### Task 3: Reel signed-URL requests

**Files:**
- Modify: `app/src/logic/segmentExport.ts`
- Test: `app/__tests__/logic/segmentExport.test.ts` (append)

**Interfaces:**
- Consumes: existing `SignedUrlRequest`, `InvalidSegmentKeyError`, `InvalidExpiryError`.
- Produces: `SignedUrlRequest.bucket` widened to `'segments' | 'reels'`; new `buildReelSignedUrlRequest(s3Key: string, expiresInSec?: number): SignedUrlRequest` with identical validation to `buildSegmentSignedUrlRequest`.

- [ ] **Step 1: Write the failing tests** (append to existing test file)

```ts
import { buildReelSignedUrlRequest } from '../../src/logic/segmentExport';

describe('buildReelSignedUrlRequest', () => {
  it('targets the reels bucket with a default 1h expiry', () => {
    expect(buildReelSignedUrlRequest('dev-1/2026-07-18.mp4')).toEqual({
      bucket: 'reels',
      path: 'dev-1/2026-07-18.mp4',
      expiresInSec: 3600,
    });
  });

  it('rejects an empty key', () => {
    expect(() => buildReelSignedUrlRequest('')).toThrow('segment key must not be empty');
  });

  it('rejects an out-of-range expiry', () => {
    expect(() => buildReelSignedUrlRequest('k', 0)).toThrow(/between 1 and/);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/logic/segmentExport.test.ts`
Expected: new tests FAIL - `buildReelSignedUrlRequest` not exported; existing tests still PASS.

- [ ] **Step 3: Implement**

In `app/src/logic/segmentExport.ts`, widen the bucket type and extract the shared validation:

```ts
export interface SignedUrlRequest {
  bucket: 'segments' | 'reels';
  path: string;
  expiresInSec: number;
}

export class InvalidSegmentKeyError extends Error {}

export class InvalidExpiryError extends Error {}

const MAX_EXPIRY_SEC = 86400;

function buildSignedUrlRequest(
  bucket: SignedUrlRequest['bucket'],
  s3Key: string,
  expiresInSec: number,
): SignedUrlRequest {
  if (!s3Key) {
    throw new InvalidSegmentKeyError('segment key must not be empty');
  }
  if (!Number.isFinite(expiresInSec) || expiresInSec < 1 || expiresInSec > MAX_EXPIRY_SEC) {
    throw new InvalidExpiryError(`expiresInSec must be between 1 and ${MAX_EXPIRY_SEC}`);
  }
  return { bucket, path: s3Key, expiresInSec };
}

export function buildSegmentSignedUrlRequest(
  s3Key: string,
  expiresInSec: number = 3600,
): SignedUrlRequest {
  return buildSignedUrlRequest('segments', s3Key, expiresInSec);
}

export function buildReelSignedUrlRequest(
  s3Key: string,
  expiresInSec: number = 3600,
): SignedUrlRequest {
  return buildSignedUrlRequest('reels', s3Key, expiresInSec);
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/logic/segmentExport.test.ts`
Expected: PASS (old and new).

- [ ] **Step 5: Commit**

```bash
git add src/logic/segmentExport.ts __tests__/logic/segmentExport.test.ts
git commit -m "feat(app): add reel signed-URL requests alongside segment requests"
```

---

### Task 4: useDeviceStatus rework - discriminated state + realtime health

**Files:**
- Modify: `app/src/hooks/useDeviceStatus.ts`
- Test: `app/__tests__/hooks/useDeviceStatus.test.tsx` (rewrite affected parts)

**Interfaces:**
- Consumes: `DeviceStatus` from `src/types.ts`.
- Produces:

```ts
export type RealtimeHealth = 'connecting' | 'live' | 'stale';

export type DeviceStatusState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; status: DeviceStatus; realtime: RealtimeHealth };

export function useDeviceStatus(client: SupabaseClient, deviceId: string): DeviceStatusState;
```

Semantics: `loading` until the initial fetch resolves with a row or a realtime event arrives; a rejected/errored initial fetch yields `error` **unless** realtime data already arrived; `single()`'s no-row error (`PGRST116`) keeps `loading` (device not booted yet, existing behavior); the `subscribe()` status callback maps `SUBSCRIBED → live` and `CHANNEL_ERROR | TIMED_OUT | CLOSED → stale`.

- [ ] **Step 1: Update the fake client and write failing tests**

In `app/__tests__/hooks/useDeviceStatus.test.tsx`, extend `createFakeClient` so `subscribe` captures its status callback, and the initial fetch resolves `{ data, error }`:

```ts
// inside createFakeClient, replace the channel/subscribe section:
let statusCallback: ((status: string) => void) | null = null;
// ...
channel: () => ({
  on: (
    _type: string,
    filter: { event: string },
    callback: (payload: { new: Record<string, unknown> }) => void,
  ) => {
    registrations.push({ event: filter.event, callback });
    return {
      subscribe: (cb?: (status: string) => void) => {
        subscribeCalled = true;
        statusCallback = cb ?? null;
        return {};
      },
    };
  },
}),
// ...and expose:
emitChannelStatus: (status: string) => act(() => statusCallback?.(status)),
```

Rewrite assertions against the new state shape and add these tests:

```ts
it('starts loading', () => {
  const { client } = createFakeClient(null, { deferInitialFetch: true });
  const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
  expect(result.current).toEqual({ kind: 'loading' });
});

it('reaches ready with connecting realtime after the initial fetch', async () => {
  const { client } = createFakeClient(ROW);
  const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
  await waitFor(() => expect(result.current.kind).toBe('ready'));
  expect(result.current).toMatchObject({ kind: 'ready', realtime: 'connecting' });
});

it('surfaces an initial fetch failure as an error state', async () => {
  const { client } = createFakeClient(null, { initialFetchError: new Error('network down') });
  const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
  await waitFor(() => expect(result.current.kind).toBe('error'));
});

it('goes live on SUBSCRIBED and stale on CHANNEL_ERROR', async () => {
  const { client, emitChannelStatus } = createFakeClient(ROW);
  const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
  await waitFor(() => expect(result.current.kind).toBe('ready'));
  emitChannelStatus('SUBSCRIBED');
  expect(result.current).toMatchObject({ realtime: 'live' });
  emitChannelStatus('CHANNEL_ERROR');
  expect(result.current).toMatchObject({ realtime: 'stale' });
});

it('marks stale on TIMED_OUT and CLOSED too', async () => {
  const { client, emitChannelStatus } = createFakeClient(ROW);
  const { result } = renderHook(() => useDeviceStatus(client, 'dev-1'));
  await waitFor(() => expect(result.current.kind).toBe('ready'));
  emitChannelStatus('TIMED_OUT');
  expect(result.current).toMatchObject({ realtime: 'stale' });
  emitChannelStatus('SUBSCRIBED');
  expect(result.current).toMatchObject({ realtime: 'live' });
  emitChannelStatus('CLOSED');
  expect(result.current).toMatchObject({ realtime: 'stale' });
});
```

Keep (adapted to the new shape) the existing behaviors: realtime INSERT/UPDATE win over a slower initial fetch; `removeChannel` on unmount; snake_case mapping.

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `npx jest __tests__/hooks/useDeviceStatus.test.tsx`
Expected: FAIL - hook still returns `DeviceStatus | null`.

- [ ] **Step 3: Implement**

`app/src/hooks/useDeviceStatus.ts`:

```ts
import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useState } from 'react';

import type { DeviceStatus } from '../types';

export type RealtimeHealth = 'connecting' | 'live' | 'stale';

export type DeviceStatusState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; status: DeviceStatus; realtime: RealtimeHealth };

type FetchState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; status: DeviceStatus };

const NO_ROW_CODE = 'PGRST116';

export function useDeviceStatus(client: SupabaseClient, deviceId: string): DeviceStatusState {
  const [fetchState, setFetchState] = useState<FetchState>({ kind: 'loading' });
  const [realtime, setRealtime] = useState<RealtimeHealth>('connecting');

  useEffect(() => {
    let isMounted = true;
    setFetchState({ kind: 'loading' });
    setRealtime('connecting');

    client
      .from('device_status')
      .select('*')
      .eq('device_id', deviceId)
      .single()
      .then(
        ({ data, error }: { data: Record<string, unknown> | null; error?: { code?: string; message?: string } | null }) => {
          if (!isMounted) return;
          setFetchState((prev) => {
            if (prev.kind === 'ready') return prev; // realtime beat the fetch
            if (data) return { kind: 'ready', status: mapRow(data) };
            // No row yet (first boot) stays loading; anything else is an error.
            if (error && error.code !== NO_ROW_CODE) {
              return { kind: 'error', message: error.message ?? 'device status fetch failed' };
            }
            return prev;
          });
        },
        (error: unknown) => {
          if (!isMounted) return;
          setFetchState((prev) =>
            prev.kind === 'ready' ? prev : { kind: 'error', message: String(error) },
          );
        },
      );

    // '*' rather than UPDATE: the device's first-ever status upsert arrives
    // as an INSERT, and the initial fetch above finds no row on first boot.
    const channel = client
      .channel(`device_status:${deviceId}`)
      .on(
        'postgres_changes',
        { event: '*', schema: 'public', table: 'device_status', filter: `device_id=eq.${deviceId}` },
        (payload: { new: Record<string, unknown> }) => {
          if (isMounted) {
            setFetchState({ kind: 'ready', status: mapRow(payload.new) });
          }
        },
      )
      .subscribe((status: string) => {
        if (!isMounted) return;
        if (status === 'SUBSCRIBED') {
          setRealtime('live');
        } else if (status === 'CHANNEL_ERROR' || status === 'TIMED_OUT' || status === 'CLOSED') {
          setRealtime('stale');
        }
      });

    return () => {
      isMounted = false;
      client.removeChannel(channel);
    };
  }, [client, deviceId]);

  if (fetchState.kind === 'ready') {
    return { kind: 'ready', status: fetchState.status, realtime };
  }
  return fetchState;
}

function mapRow(row: Record<string, unknown>): DeviceStatus {
  return {
    batteryPct: row.battery_pct as number,
    storageUsedGb: row.storage_used_gb as number,
    storageFreeGb: row.storage_free_gb as number,
    segmentsPending: row.segments_pending as number,
    segmentsUploadedToday: row.segments_uploaded_today as number,
    recordingActive: row.recording_active as boolean,
  };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/hooks/useDeviceStatus.test.tsx`
Expected: PASS.
Note: `DeviceScreen`/its test still reference the old shape; they are fixed in Task 5.
Run only this test file here.

- [ ] **Step 5: Commit**

```bash
git add src/hooks/useDeviceStatus.ts __tests__/hooks/useDeviceStatus.test.tsx
git commit -m "feat(app): discriminated device-status state with realtime channel health"
```

---

### Task 5: DeviceScreen rework + DeviceContainer

**Files:**
- Modify: `app/src/screens/DeviceScreen.tsx`
- Create: `app/src/containers/DeviceContainer.tsx`
- Test: `app/__tests__/screens/DeviceScreen.test.tsx` (rewrite), `app/__tests__/containers/DeviceContainer.test.tsx`

**Interfaces:**
- Consumes: `DeviceStatusState`, `useDeviceStatus` from Task 4; `colors`, `spacing` from Task 1.
- Produces:

```ts
// DeviceScreen (presentational)
interface DeviceScreenProps {
  state: DeviceStatusState;
  onReonboardPress: () => void;
}
// DeviceContainer
interface DeviceContainerProps {
  client: SupabaseClient;
  deviceId: string;
}
export function DeviceContainer(props: DeviceContainerProps): React.JSX.Element;
```

- [ ] **Step 1: Write failing tests**

Rewrite `app/__tests__/screens/DeviceScreen.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react-native';
import React from 'react';

import { DeviceScreen } from '../../src/screens/DeviceScreen';

const READY = {
  kind: 'ready' as const,
  status: {
    batteryPct: 72,
    storageUsedGb: 4.2,
    storageFreeGb: 118,
    segmentsPending: 1,
    segmentsUploadedToday: 12,
    recordingActive: true,
  },
  realtime: 'live' as const,
};

describe('DeviceScreen', () => {
  it('shows a loading state', () => {
    render(<DeviceScreen state={{ kind: 'loading' }} onReonboardPress={jest.fn()} />);
    expect(screen.getByText('Loading device status...')).toBeTruthy();
  });

  it('shows an error state', () => {
    render(
      <DeviceScreen state={{ kind: 'error', message: 'network down' }} onReonboardPress={jest.fn()} />,
    );
    expect(screen.getByTestId('device-error')).toHaveTextContent('network down');
  });

  it('renders status fields when ready and live, without a stale banner', () => {
    render(<DeviceScreen state={READY} onReonboardPress={jest.fn()} />);
    expect(screen.getByText('Battery: 72%')).toBeTruthy();
    expect(screen.getByText('Recording')).toBeTruthy();
    expect(screen.queryByTestId('stale-banner')).toBeNull();
  });

  it('shows a stale banner when realtime is stale', () => {
    render(<DeviceScreen state={{ ...READY, realtime: 'stale' }} onReonboardPress={jest.fn()} />);
    expect(screen.getByTestId('stale-banner')).toHaveTextContent(
      'Live updates disconnected - data may be stale',
    );
  });

  it('labels the re-onboard button for accessibility', () => {
    render(<DeviceScreen state={READY} onReonboardPress={jest.fn()} />);
    expect(screen.getByLabelText('Re-onboard device WiFi')).toBeTruthy();
  });
});
```

Create `app/__tests__/containers/DeviceContainer.test.tsx` with a fake client (reuse the shape from the `useDeviceStatus` test - `from().select().eq().single()` resolving a row, `channel().on().subscribe()`, `removeChannel`):

```tsx
import { render, screen, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

import { DeviceContainer } from '../../src/containers/DeviceContainer';

function fakeClient(row: Record<string, unknown>): SupabaseClient {
  return {
    from: () => ({
      select: () => ({ eq: () => ({ single: () => Promise.resolve({ data: row, error: null }) }) }),
    }),
    channel: () => ({ on: () => ({ subscribe: () => ({}) }) }),
    removeChannel: () => {},
  } as unknown as SupabaseClient;
}

it('wires useDeviceStatus into DeviceScreen', async () => {
  render(
    <DeviceContainer
      client={fakeClient({
        battery_pct: 55,
        storage_used_gb: 1,
        storage_free_gb: 10,
        segments_pending: 0,
        segments_uploaded_today: 3,
        recording_active: false,
      })}
      deviceId="dev-1"
    />,
  );
  await waitFor(() => expect(screen.getByText('Battery: 55%')).toBeTruthy());
  expect(screen.getByText('Paused')).toBeTruthy();
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/screens/DeviceScreen.test.tsx __tests__/containers/DeviceContainer.test.tsx`
Expected: FAIL - props mismatch / container missing.

- [ ] **Step 3: Implement**

`app/src/screens/DeviceScreen.tsx`:

```tsx
import React from 'react';
import { Button, StyleSheet, Text, View } from 'react-native';

import type { DeviceStatusState } from '../hooks/useDeviceStatus';
import { colors, spacing } from '../theme';

interface DeviceScreenProps {
  state: DeviceStatusState;
  onReonboardPress: () => void;
}

export function DeviceScreen({ state, onReonboardPress }: DeviceScreenProps) {
  if (state.kind === 'loading') {
    return (
      <View style={styles.container}>
        <Text style={styles.muted}>Loading device status...</Text>
      </View>
    );
  }

  if (state.kind === 'error') {
    return (
      <View style={styles.container}>
        <Text testID="device-error" style={styles.error} accessibilityLabel="Device status error">
          {state.message}
        </Text>
      </View>
    );
  }

  const { status, realtime } = state;
  return (
    <View style={styles.container}>
      {realtime === 'stale' ? (
        <Text testID="stale-banner" style={styles.staleBanner} accessibilityLabel="Realtime connection lost">
          Live updates disconnected - data may be stale
        </Text>
      ) : null}
      <Text style={styles.row}>Battery: {status.batteryPct}%</Text>
      <Text style={styles.row}>Storage used: {status.storageUsedGb} GB</Text>
      <Text style={styles.row}>Storage free: {status.storageFreeGb} GB</Text>
      <Text style={styles.row}>Segments pending: {status.segmentsPending}</Text>
      <Text style={styles.row}>Segments uploaded today: {status.segmentsUploadedToday}</Text>
      <Text style={[styles.row, status.recordingActive ? styles.recording : styles.paused]}>
        {status.recordingActive ? 'Recording' : 'Paused'}
      </Text>
      <View style={styles.buttonWrap}>
        <Button
          testID="reonboard-button"
          title="Re-onboard WiFi"
          color={colors.accent}
          accessibilityLabel="Re-onboard device WiFi"
          onPress={onReonboardPress}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background, padding: spacing.md },
  row: { color: colors.text, fontSize: 16, marginBottom: spacing.sm },
  muted: { color: colors.textMuted, fontSize: 16 },
  error: { color: colors.danger, fontSize: 16 },
  recording: { color: colors.success },
  paused: { color: colors.textMuted },
  staleBanner: {
    backgroundColor: colors.warning,
    color: colors.background,
    padding: spacing.sm,
    borderRadius: 6,
    marginBottom: spacing.md,
  },
  buttonWrap: { marginTop: spacing.lg },
});
```

`app/src/containers/DeviceContainer.tsx`:

```tsx
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';
import { Alert } from 'react-native';

import { useDeviceStatus } from '../hooks/useDeviceStatus';
import { DeviceScreen } from '../screens/DeviceScreen';

interface DeviceContainerProps {
  client: SupabaseClient;
  deviceId: string;
}

export function DeviceContainer({ client, deviceId }: DeviceContainerProps) {
  const state = useDeviceStatus(client, deviceId);
  return (
    <DeviceScreen
      state={state}
      onReonboardPress={() =>
        // The re-onboarding QR screen ships separately (see issue #8 scope);
        // surfacing that honestly beats a dead button.
        Alert.alert('Not available yet', 'WiFi re-onboarding is coming in a later update.')
      }
    />
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/screens/DeviceScreen.test.tsx __tests__/containers/DeviceContainer.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/screens/DeviceScreen.tsx src/containers/DeviceContainer.tsx __tests__/screens/DeviceScreen.test.tsx __tests__/containers/DeviceContainer.test.tsx
git commit -m "feat(app): device screen states, stale banner, and container wiring"
```

---

### Task 6: Real auth - useAuth hook, AuthScreen styling, AuthContainer

**Files:**
- Create: `app/src/hooks/useAuth.ts`
- Modify: `app/src/screens/AuthScreen.tsx`
- Create: `app/src/containers/AuthContainer.tsx`
- Test: `app/__tests__/hooks/useAuth.test.tsx`, `app/__tests__/screens/AuthScreen.test.tsx` (extend), `app/__tests__/containers/AuthContainer.test.tsx`

**Interfaces:**
- Consumes: `validateAuthForm`, `AuthFormErrors` from `src/logic/authValidation.ts`; `colors`, `spacing` from Task 1.
- Produces:

```ts
import type { Session } from '@supabase/supabase-js';

export type AuthState =
  | { kind: 'loading' }
  | { kind: 'signed-out' }
  | { kind: 'signed-in'; session: Session };

export function useAuth(client: SupabaseClient): {
  state: AuthState;
  signIn: (email: string, password: string) => Promise<string | null>; // resolves error message or null
  signOut: () => Promise<void>;
};

// AuthScreen gains an optional prop:
//   authError?: string | null  — rendered under the form with testID "auth-error"
// AuthContainer:
interface AuthContainerProps {
  signIn: (email: string, password: string) => Promise<string | null>;
}
export function AuthContainer(props: AuthContainerProps): React.JSX.Element;
```

- [ ] **Step 1: Write failing tests**

`app/__tests__/hooks/useAuth.test.tsx`:

```tsx
import { act, renderHook, waitFor } from '@testing-library/react-native';
import type { Session, SupabaseClient } from '@supabase/supabase-js';

import { useAuth } from '../../src/hooks/useAuth';

const SESSION = { access_token: 'tok', user: { id: 'u1' } } as unknown as Session;

function fakeAuthClient(initialSession: Session | null) {
  let authCallback: ((event: string, session: Session | null) => void) | null = null;
  const signInWithPassword = jest.fn();
  const signOut = jest.fn().mockResolvedValue({ error: null });
  const client = {
    auth: {
      getSession: () => Promise.resolve({ data: { session: initialSession } }),
      onAuthStateChange: (cb: (event: string, session: Session | null) => void) => {
        authCallback = cb;
        return { data: { subscription: { unsubscribe: jest.fn() } } };
      },
      signInWithPassword,
      signOut,
    },
  } as unknown as SupabaseClient;
  return {
    client,
    signInWithPassword,
    emitAuthChange: (event: string, session: Session | null) =>
      act(() => authCallback?.(event, session)),
  };
}

describe('useAuth', () => {
  it('starts loading then resolves to signed-out without a session', async () => {
    const { client } = fakeAuthClient(null);
    const { result } = renderHook(() => useAuth(client));
    expect(result.current.state).toEqual({ kind: 'loading' });
    await waitFor(() => expect(result.current.state).toEqual({ kind: 'signed-out' }));
  });

  it('restores a persisted session', async () => {
    const { client } = fakeAuthClient(SESSION);
    const { result } = renderHook(() => useAuth(client));
    await waitFor(() =>
      expect(result.current.state).toEqual({ kind: 'signed-in', session: SESSION }),
    );
  });

  it('follows auth state changes', async () => {
    const { client, emitAuthChange } = fakeAuthClient(null);
    const { result } = renderHook(() => useAuth(client));
    await waitFor(() => expect(result.current.state.kind).toBe('signed-out'));
    emitAuthChange('SIGNED_IN', SESSION);
    expect(result.current.state).toEqual({ kind: 'signed-in', session: SESSION });
    emitAuthChange('SIGNED_OUT', null);
    expect(result.current.state).toEqual({ kind: 'signed-out' });
  });

  it('signIn resolves null on success and a message on failure', async () => {
    const { client, signInWithPassword } = fakeAuthClient(null);
    const { result } = renderHook(() => useAuth(client));
    await waitFor(() => expect(result.current.state.kind).toBe('signed-out'));

    signInWithPassword.mockResolvedValueOnce({ data: {}, error: null });
    await expect(result.current.signIn('a@b.co', 'password123')).resolves.toBeNull();
    expect(signInWithPassword).toHaveBeenCalledWith({ email: 'a@b.co', password: 'password123' });

    signInWithPassword.mockResolvedValueOnce({ data: {}, error: { message: 'Invalid login credentials' } });
    await expect(result.current.signIn('a@b.co', 'wrong-password')).resolves.toBe(
      'Invalid login credentials',
    );
  });
});
```

Extend `app/__tests__/screens/AuthScreen.test.tsx` (keep existing tests; adapt if props changed):

```tsx
it('renders an auth error when provided', () => {
  render(
    <AuthScreen
      email=""
      password=""
      errors={{}}
      authError="Invalid login credentials"
      onEmailChange={jest.fn()}
      onPasswordChange={jest.fn()}
      onSubmit={jest.fn()}
    />,
  );
  expect(screen.getByTestId('auth-error')).toHaveTextContent('Invalid login credentials');
});

it('labels inputs for accessibility', () => {
  render(
    <AuthScreen email="" password="" errors={{}} onEmailChange={jest.fn()} onPasswordChange={jest.fn()} onSubmit={jest.fn()} />,
  );
  expect(screen.getByLabelText('Email address')).toBeTruthy();
  expect(screen.getByLabelText('Password')).toBeTruthy();
  expect(screen.getByLabelText('Sign in')).toBeTruthy();
});
```

`app/__tests__/containers/AuthContainer.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import React from 'react';

import { AuthContainer } from '../../src/containers/AuthContainer';

describe('AuthContainer', () => {
  it('validates locally before calling signIn', () => {
    const signIn = jest.fn();
    render(<AuthContainer signIn={signIn} />);
    fireEvent.changeText(screen.getByTestId('email-input'), 'not-an-email');
    fireEvent.changeText(screen.getByTestId('password-input'), 'short');
    fireEvent.press(screen.getByTestId('submit-button'));
    expect(signIn).not.toHaveBeenCalled();
    expect(screen.getByTestId('email-error')).toBeTruthy();
    expect(screen.getByTestId('password-error')).toBeTruthy();
  });

  it('calls signIn with valid credentials and surfaces auth errors', async () => {
    const signIn = jest.fn().mockResolvedValue('Invalid login credentials');
    render(<AuthContainer signIn={signIn} />);
    fireEvent.changeText(screen.getByTestId('email-input'), 'a@b.co');
    fireEvent.changeText(screen.getByTestId('password-input'), 'password123');
    fireEvent.press(screen.getByTestId('submit-button'));
    expect(signIn).toHaveBeenCalledWith('a@b.co', 'password123');
    await waitFor(() =>
      expect(screen.getByTestId('auth-error')).toHaveTextContent('Invalid login credentials'),
    );
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/hooks/useAuth.test.tsx __tests__/screens/AuthScreen.test.tsx __tests__/containers/AuthContainer.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

`app/src/hooks/useAuth.ts`:

```ts
import type { Session, SupabaseClient } from '@supabase/supabase-js';
import { useCallback, useEffect, useState } from 'react';

export type AuthState =
  | { kind: 'loading' }
  | { kind: 'signed-out' }
  | { kind: 'signed-in'; session: Session };

export function useAuth(client: SupabaseClient): {
  state: AuthState;
  signIn: (email: string, password: string) => Promise<string | null>;
  signOut: () => Promise<void>;
} {
  const [state, setState] = useState<AuthState>({ kind: 'loading' });

  useEffect(() => {
    let isMounted = true;

    client.auth.getSession().then(({ data: { session } }) => {
      if (isMounted) {
        setState(session ? { kind: 'signed-in', session } : { kind: 'signed-out' });
      }
    });

    const {
      data: { subscription },
    } = client.auth.onAuthStateChange((_event, session) => {
      if (isMounted) {
        setState(session ? { kind: 'signed-in', session } : { kind: 'signed-out' });
      }
    });

    return () => {
      isMounted = false;
      subscription.unsubscribe();
    };
  }, [client]);

  const signIn = useCallback(
    async (email: string, password: string): Promise<string | null> => {
      const { error } = await client.auth.signInWithPassword({ email, password });
      return error ? error.message : null;
    },
    [client],
  );

  const signOut = useCallback(async () => {
    await client.auth.signOut();
  }, [client]);

  return { state, signIn, signOut };
}
```

`app/src/screens/AuthScreen.tsx` - add `authError?: string | null` prop, accessibility labels, and styling:

```tsx
import React from 'react';
import { Button, StyleSheet, Text, TextInput, View } from 'react-native';

import type { AuthFormErrors } from '../logic/authValidation';
import { colors, spacing } from '../theme';

interface AuthScreenProps {
  email: string;
  password: string;
  errors: AuthFormErrors;
  authError?: string | null;
  onEmailChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: () => void;
}

export function AuthScreen({
  email,
  password,
  errors,
  authError,
  onEmailChange,
  onPasswordChange,
  onSubmit,
}: AuthScreenProps) {
  return (
    <View style={styles.container}>
      <Text style={styles.title} accessibilityRole="header">
        Visio
      </Text>
      <TextInput
        testID="email-input"
        style={styles.input}
        placeholder="Email"
        placeholderTextColor={colors.textMuted}
        value={email}
        onChangeText={onEmailChange}
        autoCapitalize="none"
        keyboardType="email-address"
        accessibilityLabel="Email address"
      />
      {errors.email ? (
        <Text testID="email-error" style={styles.fieldError}>
          {errors.email}
        </Text>
      ) : null}
      <TextInput
        testID="password-input"
        style={styles.input}
        placeholder="Password"
        placeholderTextColor={colors.textMuted}
        value={password}
        onChangeText={onPasswordChange}
        secureTextEntry
        accessibilityLabel="Password"
      />
      {errors.password ? (
        <Text testID="password-error" style={styles.fieldError}>
          {errors.password}
        </Text>
      ) : null}
      {authError ? (
        <Text testID="auth-error" style={styles.authError} accessibilityLabel="Sign in error">
          {authError}
        </Text>
      ) : null}
      <View style={styles.buttonWrap}>
        <Button
          testID="submit-button"
          title="Sign In"
          color={colors.accent}
          accessibilityLabel="Sign in"
          onPress={onSubmit}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
    justifyContent: 'center',
    padding: spacing.lg,
  },
  title: {
    color: colors.text,
    fontSize: 32,
    fontWeight: '700',
    textAlign: 'center',
    marginBottom: spacing.lg,
  },
  input: {
    backgroundColor: colors.surface,
    color: colors.text,
    borderRadius: 8,
    padding: spacing.md,
    marginBottom: spacing.sm,
  },
  fieldError: { color: colors.danger, marginBottom: spacing.sm },
  authError: { color: colors.danger, marginTop: spacing.sm, textAlign: 'center' },
  buttonWrap: { marginTop: spacing.md },
});
```

`app/src/containers/AuthContainer.tsx`:

```tsx
import React, { useState } from 'react';

import type { AuthFormErrors } from '../logic/authValidation';
import { validateAuthForm } from '../logic/authValidation';
import { AuthScreen } from '../screens/AuthScreen';

interface AuthContainerProps {
  signIn: (email: string, password: string) => Promise<string | null>;
}

export function AuthContainer({ signIn }: AuthContainerProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [errors, setErrors] = useState<AuthFormErrors>({});
  const [authError, setAuthError] = useState<string | null>(null);

  const onSubmit = async () => {
    const validation = validateAuthForm(email, password);
    setErrors(validation);
    if (validation.email || validation.password) {
      return;
    }
    setAuthError(null);
    setAuthError(await signIn(email, password));
  };

  return (
    <AuthScreen
      email={email}
      password={password}
      errors={errors}
      authError={authError}
      onEmailChange={setEmail}
      onPasswordChange={setPassword}
      onSubmit={onSubmit}
    />
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/hooks/useAuth.test.tsx __tests__/screens/AuthScreen.test.tsx __tests__/containers/AuthContainer.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hooks/useAuth.ts src/screens/AuthScreen.tsx src/containers/AuthContainer.tsx __tests__/hooks/useAuth.test.tsx __tests__/screens/AuthScreen.test.tsx __tests__/containers/AuthContainer.test.tsx
git commit -m "feat(app): real Supabase auth with session restore and styled auth screen"
```

---

### Task 7: useDevice hook

**Files:**
- Create: `app/src/hooks/useDevice.ts`
- Test: `app/__tests__/hooks/useDevice.test.tsx`

**Interfaces:**
- Consumes: nothing new.
- Produces:

```ts
export type DeviceState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'none' }
  | { kind: 'ready'; deviceId: string; name: string };

export function useDevice(client: SupabaseClient): DeviceState;
```

Fetches the signed-in user's oldest device (`devices` is RLS-scoped to the user): `from('devices').select('device_id, name').order('created_at', { ascending: true }).limit(1)`.

- [ ] **Step 1: Write failing tests**

`app/__tests__/hooks/useDevice.test.tsx`:

```tsx
import { renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useDevice } from '../../src/hooks/useDevice';

function fakeClient(result: { data: unknown[] | null; error: { message: string } | null }) {
  return {
    from: () => ({
      select: () => ({ order: () => ({ limit: () => Promise.resolve(result) }) }),
    }),
  } as unknown as SupabaseClient;
}

describe('useDevice', () => {
  it('resolves the first device', async () => {
    const client = fakeClient({ data: [{ device_id: 'dev-1', name: 'Pendant' }], error: null });
    const { result } = renderHook(() => useDevice(client));
    expect(result.current).toEqual({ kind: 'loading' });
    await waitFor(() =>
      expect(result.current).toEqual({ kind: 'ready', deviceId: 'dev-1', name: 'Pendant' }),
    );
  });

  it('resolves none when the user has no devices', async () => {
    const { result } = renderHook(() => useDevice(fakeClient({ data: [], error: null })));
    await waitFor(() => expect(result.current).toEqual({ kind: 'none' }));
  });

  it('surfaces query errors', async () => {
    const { result } = renderHook(() =>
      useDevice(fakeClient({ data: null, error: { message: 'boom' } })),
    );
    await waitFor(() => expect(result.current).toEqual({ kind: 'error', message: 'boom' }));
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/hooks/useDevice.test.tsx`
Expected: FAIL - module not found.

- [ ] **Step 3: Implement**

`app/src/hooks/useDevice.ts`:

```ts
import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useState } from 'react';

export type DeviceState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'none' }
  | { kind: 'ready'; deviceId: string; name: string };

export function useDevice(client: SupabaseClient): DeviceState {
  const [state, setState] = useState<DeviceState>({ kind: 'loading' });

  useEffect(() => {
    let isMounted = true;
    setState({ kind: 'loading' });

    client
      .from('devices')
      .select('device_id, name')
      .order('created_at', { ascending: true })
      .limit(1)
      .then(
        ({ data, error }: { data: Record<string, unknown>[] | null; error: { message: string } | null }) => {
          if (!isMounted) return;
          if (error) {
            setState({ kind: 'error', message: error.message });
          } else if (!data || data.length === 0) {
            setState({ kind: 'none' });
          } else {
            setState({
              kind: 'ready',
              deviceId: data[0].device_id as string,
              name: data[0].name as string,
            });
          }
        },
        (error: unknown) => {
          if (isMounted) setState({ kind: 'error', message: String(error) });
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client]);

  return state;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/hooks/useDevice.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hooks/useDevice.ts __tests__/hooks/useDevice.test.tsx
git commit -m "feat(app): useDevice hook resolving the user's pendant"
```

---

### Task 8: useSegments hook with user feedback writes

**Files:**
- Create: `app/src/hooks/useSegments.ts`
- Test: `app/__tests__/hooks/useSegments.test.tsx`

**Interfaces:**
- Consumes: `Segment` from `src/types.ts`.
- Produces:

```ts
export type SegmentsState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; segments: Segment[] };

export function useSegments(
  client: SupabaseClient,
  deviceId: string,
  dayStartIso: string, // UTC-midnight ISO string; the query covers [dayStartIso, +24h)
): {
  state: SegmentsState;
  setUserFeedback: (segmentId: string, feedback: 'include' | 'exclude' | null) => Promise<void>;
};
```

`setUserFeedback` updates local state optimistically, then `client.from('segments').update({ user_feedback: feedback }).eq('id', segmentId)`; on error it reverts the local change (the column-level grant `update (user_feedback)` is the only app write to segments).

- [ ] **Step 1: Write failing tests**

`app/__tests__/hooks/useSegments.test.tsx`:

```tsx
import { act, renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useSegments } from '../../src/hooks/useSegments';

const ROWS = [
  {
    id: 's1',
    recorded_at: '2026-07-18T08:00:00Z',
    duration_sec: 120,
    s3_key: 'dev-1/s1.mp4',
    manually_flagged: true,
    user_feedback: null,
  },
];

function fakeClient(options: { updateError?: { message: string } } = {}) {
  const updateCalls: { values: Record<string, unknown>; id: string }[] = [];
  const client = {
    from: () => ({
      select: () => ({
        eq: () => ({
          gte: () => ({
            lt: () => ({
              order: () => Promise.resolve({ data: ROWS, error: null }),
            }),
          }),
        }),
      }),
      update: (values: Record<string, unknown>) => ({
        eq: (_col: string, id: string) => {
          updateCalls.push({ values, id });
          return Promise.resolve({ error: options.updateError ?? null });
        },
      }),
    }),
  } as unknown as SupabaseClient;
  return { client, updateCalls };
}

describe('useSegments', () => {
  it('fetches and maps segments for the day', async () => {
    const { client } = fakeClient();
    const { result } = renderHook(() =>
      useSegments(client, 'dev-1', '2026-07-18T00:00:00.000Z'),
    );
    expect(result.current.state).toEqual({ kind: 'loading' });
    await waitFor(() => expect(result.current.state.kind).toBe('ready'));
    expect(result.current.state).toEqual({
      kind: 'ready',
      segments: [
        {
          id: 's1',
          recordedAt: '2026-07-18T08:00:00Z',
          durationSec: 120,
          s3Key: 'dev-1/s1.mp4',
          manuallyFlagged: true,
          userFeedback: null,
        },
      ],
    });
  });

  it('applies user feedback optimistically and persists it', async () => {
    const { client, updateCalls } = fakeClient();
    const { result } = renderHook(() =>
      useSegments(client, 'dev-1', '2026-07-18T00:00:00.000Z'),
    );
    await waitFor(() => expect(result.current.state.kind).toBe('ready'));
    await act(() => result.current.setUserFeedback('s1', 'include'));
    expect(updateCalls).toEqual([{ values: { user_feedback: 'include' }, id: 's1' }]);
    expect(result.current.state).toMatchObject({
      segments: [expect.objectContaining({ userFeedback: 'include' })],
    });
  });

  it('reverts the optimistic update when the write fails', async () => {
    const { client } = fakeClient({ updateError: { message: 'denied' } });
    const { result } = renderHook(() =>
      useSegments(client, 'dev-1', '2026-07-18T00:00:00.000Z'),
    );
    await waitFor(() => expect(result.current.state.kind).toBe('ready'));
    await act(() => result.current.setUserFeedback('s1', 'exclude'));
    expect(result.current.state).toMatchObject({
      segments: [expect.objectContaining({ userFeedback: null })],
    });
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/hooks/useSegments.test.tsx`
Expected: FAIL - module not found.

- [ ] **Step 3: Implement**

`app/src/hooks/useSegments.ts`:

```ts
import type { SupabaseClient } from '@supabase/supabase-js';
import { useCallback, useEffect, useState } from 'react';

import type { Segment } from '../types';

export type SegmentsState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; segments: Segment[] };

const DAY_MS = 24 * 60 * 60 * 1000;

export function useSegments(
  client: SupabaseClient,
  deviceId: string,
  dayStartIso: string,
): {
  state: SegmentsState;
  setUserFeedback: (segmentId: string, feedback: Segment['userFeedback']) => Promise<void>;
} {
  const [state, setState] = useState<SegmentsState>({ kind: 'loading' });

  useEffect(() => {
    let isMounted = true;
    setState({ kind: 'loading' });
    const dayEndIso = new Date(new Date(dayStartIso).getTime() + DAY_MS).toISOString();

    client
      .from('segments')
      .select('*')
      .eq('device_id', deviceId)
      .gte('recorded_at', dayStartIso)
      .lt('recorded_at', dayEndIso)
      .order('recorded_at', { ascending: true })
      .then(
        ({ data, error }: { data: Record<string, unknown>[] | null; error: { message: string } | null }) => {
          if (!isMounted) return;
          if (error || !data) {
            setState({ kind: 'error', message: error?.message ?? 'segments fetch failed' });
          } else {
            setState({ kind: 'ready', segments: data.map(mapSegmentRow) });
          }
        },
        (error: unknown) => {
          if (isMounted) setState({ kind: 'error', message: String(error) });
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client, deviceId, dayStartIso]);

  const setUserFeedback = useCallback(
    async (segmentId: string, feedback: Segment['userFeedback']) => {
      let previous: Segment['userFeedback'] = null;
      setState((prev) => {
        if (prev.kind !== 'ready') return prev;
        return {
          kind: 'ready',
          segments: prev.segments.map((s) => {
            if (s.id !== segmentId) return s;
            previous = s.userFeedback;
            return { ...s, userFeedback: feedback };
          }),
        };
      });

      const { error } = await client
        .from('segments')
        .update({ user_feedback: feedback })
        .eq('id', segmentId);

      if (error) {
        setState((prev) => {
          if (prev.kind !== 'ready') return prev;
          return {
            kind: 'ready',
            segments: prev.segments.map((s) =>
              s.id === segmentId ? { ...s, userFeedback: previous } : s,
            ),
          };
        });
      }
    },
    [client],
  );

  return { state, setUserFeedback };
}

function mapSegmentRow(row: Record<string, unknown>): Segment {
  return {
    id: row.id as string,
    recordedAt: row.recorded_at as string,
    durationSec: row.duration_sec as number,
    s3Key: row.s3_key as string,
    manuallyFlagged: row.manually_flagged as boolean,
    userFeedback: (row.user_feedback as Segment['userFeedback']) ?? null,
  };
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/hooks/useSegments.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hooks/useSegments.ts __tests__/hooks/useSegments.test.tsx
git commit -m "feat(app): useSegments hook with optimistic user-feedback writes"
```

---

### Task 9: useReel, useReelsInRange, useSignedUrl hooks

**Files:**
- Create: `app/src/hooks/useReel.ts`
- Create: `app/src/hooks/useSignedUrl.ts`
- Test: `app/__tests__/hooks/useReel.test.tsx`, `app/__tests__/hooks/useSignedUrl.test.tsx`

**Interfaces:**
- Consumes: `Reel` from `src/types.ts`; `SignedUrlRequest` from Task 3.
- Produces:

```ts
// useReel.ts
export type ReelState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'none' }
  | { kind: 'ready'; reel: Reel };

// date is YYYY-MM-DD; null date short-circuits to 'none' without querying
export function useReel(client: SupabaseClient, deviceId: string, date: string | null): ReelState;

export type ReelsState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; reels: Reel[] };

export function useReelsInRange(
  client: SupabaseClient,
  deviceId: string,
  startDate: string, // YYYY-MM-DD inclusive
  endDate: string, // YYYY-MM-DD inclusive
): ReelsState;

// useSignedUrl.ts — resolves a storage signed URL, null while pending/absent
export function useSignedUrl(client: SupabaseClient, request: SignedUrlRequest | null): string | null;
```

Queries: `useReel` uses `.eq('device_id', deviceId).eq('date', date).order('created_at', { ascending: false }).limit(1)` (newest reel wins; `reels` has a device+date unique constraint but regenerated styles may versionize later).
`useReelsInRange` uses `.eq('device_id', deviceId).gte('date', startDate).lte('date', endDate)`.
`useSignedUrl` calls `client.storage.from(request.bucket).createSignedUrl(request.path, request.expiresInSec)` and stores `data.signedUrl`.

- [ ] **Step 1: Write failing tests**

`app/__tests__/hooks/useReel.test.tsx`:

```tsx
import { renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useReel, useReelsInRange } from '../../src/hooks/useReel';

const ROW = {
  id: 'r1',
  date: '2026-07-18',
  s3_key: 'dev-1/2026-07-18.mp4',
  duration_sec: 60,
  style: 'clean',
};

function fakeReelClient(rows: Record<string, unknown>[]) {
  const chain = {
    select: () => chain,
    eq: () => chain,
    gte: () => chain,
    lte: () => chain,
    order: () => chain,
    limit: () => Promise.resolve({ data: rows, error: null }),
    then: (onFulfilled: (v: { data: unknown; error: null }) => unknown) =>
      Promise.resolve({ data: rows, error: null }).then(onFulfilled),
  };
  return { from: () => chain } as unknown as SupabaseClient;
}

describe('useReel', () => {
  it('maps the reel row', async () => {
    const { result } = renderHook(() => useReel(fakeReelClient([ROW]), 'dev-1', '2026-07-18'));
    await waitFor(() =>
      expect(result.current).toEqual({
        kind: 'ready',
        reel: { id: 'r1', date: '2026-07-18', s3Key: 'dev-1/2026-07-18.mp4', durationSec: 60, style: 'clean' },
      }),
    );
  });

  it('resolves none when no reel exists', async () => {
    const { result } = renderHook(() => useReel(fakeReelClient([]), 'dev-1', '2026-07-18'));
    await waitFor(() => expect(result.current).toEqual({ kind: 'none' }));
  });

  it('short-circuits to none for a null date', () => {
    const { result } = renderHook(() => useReel(fakeReelClient([ROW]), 'dev-1', null));
    expect(result.current).toEqual({ kind: 'none' });
  });
});

describe('useReelsInRange', () => {
  it('maps all rows in the range', async () => {
    const { result } = renderHook(() =>
      useReelsInRange(fakeReelClient([ROW]), 'dev-1', '2026-06-19', '2026-07-18'),
    );
    await waitFor(() =>
      expect(result.current).toMatchObject({ kind: 'ready', reels: [{ id: 'r1' }] }),
    );
  });
});
```

`app/__tests__/hooks/useSignedUrl.test.tsx`:

```tsx
import { renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

import { useSignedUrl } from '../../src/hooks/useSignedUrl';

function fakeStorageClient(signedUrl: string | null) {
  const createSignedUrl = jest.fn().mockResolvedValue({
    data: signedUrl ? { signedUrl } : null,
    error: signedUrl ? null : { message: 'not found' },
  });
  const client = { storage: { from: jest.fn(() => ({ createSignedUrl })) } } as unknown as SupabaseClient;
  return { client, createSignedUrl };
}

describe('useSignedUrl', () => {
  it('resolves a signed url for the request', async () => {
    const { client, createSignedUrl } = fakeStorageClient('https://signed/url.mp4');
    const { result } = renderHook(() =>
      useSignedUrl(client, { bucket: 'reels', path: 'dev-1/r.mp4', expiresInSec: 3600 }),
    );
    expect(result.current).toBeNull();
    await waitFor(() => expect(result.current).toBe('https://signed/url.mp4'));
    expect(createSignedUrl).toHaveBeenCalledWith('dev-1/r.mp4', 3600);
  });

  it('stays null for a null request', () => {
    const { client } = fakeStorageClient('https://signed/url.mp4');
    const { result } = renderHook(() => useSignedUrl(client, null));
    expect(result.current).toBeNull();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/hooks/useReel.test.tsx __tests__/hooks/useSignedUrl.test.tsx`
Expected: FAIL - modules not found.

- [ ] **Step 3: Implement**

`app/src/hooks/useReel.ts`:

```ts
import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useState } from 'react';

import type { Reel } from '../types';

export type ReelState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'none' }
  | { kind: 'ready'; reel: Reel };

export type ReelsState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; reels: Reel[] };

export function useReel(client: SupabaseClient, deviceId: string, date: string | null): ReelState {
  const [state, setState] = useState<ReelState>({ kind: date ? 'loading' : 'none' });

  useEffect(() => {
    if (!date) {
      setState({ kind: 'none' });
      return;
    }
    let isMounted = true;
    setState({ kind: 'loading' });

    client
      .from('reels')
      .select('*')
      .eq('device_id', deviceId)
      .eq('date', date)
      .order('created_at', { ascending: false })
      .limit(1)
      .then(
        ({ data, error }: { data: Record<string, unknown>[] | null; error: { message: string } | null }) => {
          if (!isMounted) return;
          if (error || !data) {
            setState({ kind: 'error', message: error?.message ?? 'reel fetch failed' });
          } else if (data.length === 0) {
            setState({ kind: 'none' });
          } else {
            setState({ kind: 'ready', reel: mapReelRow(data[0]) });
          }
        },
        (error: unknown) => {
          if (isMounted) setState({ kind: 'error', message: String(error) });
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client, deviceId, date]);

  return state;
}

export function useReelsInRange(
  client: SupabaseClient,
  deviceId: string,
  startDate: string,
  endDate: string,
): ReelsState {
  const [state, setState] = useState<ReelsState>({ kind: 'loading' });

  useEffect(() => {
    let isMounted = true;
    setState({ kind: 'loading' });

    client
      .from('reels')
      .select('*')
      .eq('device_id', deviceId)
      .gte('date', startDate)
      .lte('date', endDate)
      .then(
        ({ data, error }: { data: Record<string, unknown>[] | null; error: { message: string } | null }) => {
          if (!isMounted) return;
          if (error || !data) {
            setState({ kind: 'error', message: error?.message ?? 'reels fetch failed' });
          } else {
            setState({ kind: 'ready', reels: data.map(mapReelRow) });
          }
        },
        (error: unknown) => {
          if (isMounted) setState({ kind: 'error', message: String(error) });
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client, deviceId, startDate, endDate]);

  return state;
}

function mapReelRow(row: Record<string, unknown>): Reel {
  return {
    id: row.id as string,
    date: row.date as string,
    s3Key: row.s3_key as string,
    durationSec: row.duration_sec as number,
    style: row.style as Reel['style'],
  };
}
```

`app/src/hooks/useSignedUrl.ts`:

```ts
import type { SupabaseClient } from '@supabase/supabase-js';
import { useEffect, useState } from 'react';

import type { SignedUrlRequest } from '../logic/segmentExport';

export function useSignedUrl(
  client: SupabaseClient,
  request: SignedUrlRequest | null,
): string | null {
  const [url, setUrl] = useState<string | null>(null);
  const bucket = request?.bucket ?? null;
  const path = request?.path ?? null;
  const expiresInSec = request?.expiresInSec ?? null;

  useEffect(() => {
    setUrl(null);
    if (!bucket || !path || !expiresInSec) {
      return;
    }
    let isMounted = true;

    client.storage
      .from(bucket)
      .createSignedUrl(path, expiresInSec)
      .then(
        ({ data }: { data: { signedUrl: string } | null }) => {
          if (isMounted && data) {
            setUrl(data.signedUrl);
          }
        },
        (error: unknown) => {
          console.error('useSignedUrl: signing failed', error);
        },
      );

    return () => {
      isMounted = false;
    };
  }, [client, bucket, path, expiresInSec]);

  return url;
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/hooks/useReel.test.tsx __tests__/hooks/useSignedUrl.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hooks/useReel.ts src/hooks/useSignedUrl.ts __tests__/hooks/useReel.test.tsx __tests__/hooks/useSignedUrl.test.tsx
git commit -m "feat(app): reel fetch hooks and storage signed-url hook"
```

---

### Task 10: ReelPlayer component + TodayReelContainer

**Files:**
- Create: `app/src/components/ReelPlayer.tsx`
- Create: `app/src/containers/TodayReelContainer.tsx`
- Test: `app/__tests__/components/ReelPlayer.test.tsx`, `app/__tests__/containers/TodayReelContainer.test.tsx`

**Interfaces:**
- Consumes: `useReel` (Task 9), `useSignedUrl` (Task 9), `buildReelSignedUrlRequest` (Task 3), `utcDateString` (Task 2), `ReelState`.
- Produces:

```tsx
// ReelPlayer (presentational; owns the expo-video player)
interface ReelPlayerProps {
  videoUri: string | null; // null renders a "preparing playback" placeholder
  title: string;
  onShare: () => void;
}
export function ReelPlayer(props: ReelPlayerProps): React.JSX.Element;

// TodayReelContainer
interface TodayReelContainerProps {
  client: SupabaseClient;
  deviceId: string;
  now?: () => Date; // injectable clock, defaults to () => new Date()
}
export function TodayReelContainer(props: TodayReelContainerProps): React.JSX.Element;
```

- [ ] **Step 1: Write failing tests**

Both test files mock `expo-video` (jest-expo does not ship a native video mock):

`app/__tests__/components/ReelPlayer.test.tsx`:

```tsx
import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});

import { ReelPlayer } from '../../src/components/ReelPlayer';

describe('ReelPlayer', () => {
  it('renders the video view with an accessibility label when a uri is ready', () => {
    render(<ReelPlayer videoUri="https://signed/reel.mp4" title="Today's Reel" onShare={jest.fn()} />);
    expect(screen.getByTestId('video-view')).toBeTruthy();
    expect(screen.getByLabelText('Reel video player')).toBeTruthy();
  });

  it('shows a placeholder while the uri is pending', () => {
    render(<ReelPlayer videoUri={null} title="Today's Reel" onShare={jest.fn()} />);
    expect(screen.getByText('Preparing playback...')).toBeTruthy();
    expect(screen.queryByTestId('video-view')).toBeNull();
  });

  it('invokes onShare from the labeled share button', () => {
    const onShare = jest.fn();
    render(<ReelPlayer videoUri="https://signed/reel.mp4" title="Today's Reel" onShare={onShare} />);
    fireEvent.press(screen.getByLabelText('Share reel'));
    expect(onShare).toHaveBeenCalled();
  });
});
```

`app/__tests__/containers/TodayReelContainer.test.tsx`:

```tsx
import { render, screen, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});

import { TodayReelContainer } from '../../src/containers/TodayReelContainer';

const REEL_ROW = {
  id: 'r1',
  date: '2026-07-18',
  s3_key: 'dev-1/2026-07-18.mp4',
  duration_sec: 60,
  style: 'clean',
};

function fakeClient(rows: Record<string, unknown>[]) {
  const chain = {
    select: () => chain,
    eq: () => chain,
    order: () => chain,
    limit: () => Promise.resolve({ data: rows, error: null }),
  };
  return {
    from: () => chain,
    storage: {
      from: () => ({
        createSignedUrl: () => Promise.resolve({ data: { signedUrl: 'https://signed/reel.mp4' }, error: null }),
      }),
    },
  } as unknown as SupabaseClient;
}

describe('TodayReelContainer', () => {
  it("plays today's reel through a signed url", async () => {
    render(
      <TodayReelContainer
        client={fakeClient([REEL_ROW])}
        deviceId="dev-1"
        now={() => new Date('2026-07-18T15:00:00Z')}
      />,
    );
    await waitFor(() => expect(screen.getByTestId('video-view')).toBeTruthy());
  });

  it('explains when no reel exists yet', async () => {
    render(
      <TodayReelContainer
        client={fakeClient([])}
        deviceId="dev-1"
        now={() => new Date('2026-07-18T15:00:00Z')}
      />,
    );
    await waitFor(() => expect(screen.getByText("Today's reel isn't ready yet.")).toBeTruthy());
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/components/ReelPlayer.test.tsx __tests__/containers/TodayReelContainer.test.tsx`
Expected: FAIL - modules not found.

- [ ] **Step 3: Implement**

`app/src/components/ReelPlayer.tsx`:

```tsx
import { useVideoPlayer, VideoView } from 'expo-video';
import React from 'react';
import { Button, StyleSheet, Text, View } from 'react-native';

import { colors, spacing } from '../theme';

interface ReelPlayerProps {
  videoUri: string | null;
  title: string;
  onShare: () => void;
}

export function ReelPlayer({ videoUri, title, onShare }: ReelPlayerProps) {
  // useVideoPlayer must be called unconditionally; it accepts a null source.
  const player = useVideoPlayer(videoUri);

  return (
    <View style={styles.container}>
      <Text style={styles.title} accessibilityRole="header">
        {title}
      </Text>
      {videoUri ? (
        <VideoView
          player={player}
          style={styles.video}
          nativeControls
          accessibilityLabel="Reel video player"
        />
      ) : (
        <View style={[styles.video, styles.placeholder]}>
          <Text style={styles.muted}>Preparing playback...</Text>
        </View>
      )}
      <View style={styles.buttonWrap}>
        <Button title="Share" color={colors.accent} accessibilityLabel="Share reel" onPress={onShare} />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background, padding: spacing.md },
  title: { color: colors.text, fontSize: 20, fontWeight: '600', marginBottom: spacing.md },
  video: { width: '100%', aspectRatio: 9 / 16, borderRadius: 12, backgroundColor: colors.surface },
  placeholder: { alignItems: 'center', justifyContent: 'center' },
  muted: { color: colors.textMuted },
  buttonWrap: { marginTop: spacing.md },
});
```

`app/src/containers/TodayReelContainer.tsx`:

```tsx
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';
import { Share, StyleSheet, Text, View } from 'react-native';

import { ReelPlayer } from '../components/ReelPlayer';
import { useReel } from '../hooks/useReel';
import { useSignedUrl } from '../hooks/useSignedUrl';
import { utcDateString } from '../logic/dates';
import { buildReelSignedUrlRequest } from '../logic/segmentExport';
import { colors, spacing } from '../theme';

interface TodayReelContainerProps {
  client: SupabaseClient;
  deviceId: string;
  now?: () => Date;
}

export function TodayReelContainer({ client, deviceId, now = () => new Date() }: TodayReelContainerProps) {
  const today = utcDateString(now());
  const reelState = useReel(client, deviceId, today);
  const signedUrl = useSignedUrl(
    client,
    reelState.kind === 'ready' ? buildReelSignedUrlRequest(reelState.reel.s3Key) : null,
  );

  if (reelState.kind === 'loading') {
    return (
      <View style={styles.message}>
        <Text style={styles.muted}>Loading today's reel...</Text>
      </View>
    );
  }
  if (reelState.kind === 'error') {
    return (
      <View style={styles.message}>
        <Text style={styles.error} accessibilityLabel="Reel error">
          {reelState.message}
        </Text>
      </View>
    );
  }
  if (reelState.kind === 'none') {
    return (
      <View style={styles.message}>
        <Text style={styles.muted}>Today's reel isn't ready yet.</Text>
      </View>
    );
  }

  return (
    <ReelPlayer
      videoUri={signedUrl}
      title="Today's Reel"
      onShare={() => {
        if (signedUrl) {
          Share.share({ url: signedUrl, message: signedUrl });
        }
      }}
    />
  );
}

const styles = StyleSheet.create({
  message: {
    flex: 1,
    backgroundColor: colors.background,
    alignItems: 'center',
    justifyContent: 'center',
    padding: spacing.lg,
  },
  muted: { color: colors.textMuted, fontSize: 16 },
  error: { color: colors.danger, fontSize: 16 },
});
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/components/ReelPlayer.test.tsx __tests__/containers/TodayReelContainer.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/components/ReelPlayer.tsx src/containers/TodayReelContainer.tsx __tests__/components/ReelPlayer.test.tsx __tests__/containers/TodayReelContainer.test.tsx
git commit -m "feat(app): expo-video reel player and Today's Reel container"
```

---

### Task 11: Timeline thumbnails - useSlotThumbnails + TimelineScrubber images

**Files:**
- Create: `app/src/hooks/useSlotThumbnails.ts`
- Modify: `app/src/components/TimelineScrubber.tsx`
- Test: `app/__tests__/hooks/useSlotThumbnails.test.tsx`, `app/__tests__/components/TimelineScrubber.test.tsx` (extend)

**Interfaces:**
- Consumes: `TimelineSlot` from `src/logic/timeline.ts`; `buildSegmentSignedUrlRequest` (Task 3).
- Produces:

```ts
// keyed by slot.startMinute; only slots whose thumbnail resolved appear
export function useSlotThumbnails(
  client: SupabaseClient,
  slots: TimelineSlot[],
): Record<number, string>;

// TimelineScrubber gains an optional prop:
//   thumbnails?: Record<number, string>
// Slots with a thumbnail render <Image testID={`thumb-${slot.startMinute}`} source={{ uri }} />.
```

Implementation notes: per segment, sign the URL then `VideoThumbnails.getThumbnailAsync(signedUrl, { time: 0 })`; cache resolved URIs in a `useRef<Map<string, string>>` keyed by `segment.id` so re-renders and day switches don't re-extract; per-slot failures log and skip (a broken thumbnail must not break the strip).

- [ ] **Step 1: Write failing tests**

`app/__tests__/hooks/useSlotThumbnails.test.tsx`:

```tsx
import { renderHook, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';

jest.mock('expo-video-thumbnails', () => ({
  getThumbnailAsync: jest.fn((uri: string) => Promise.resolve({ uri: `thumb:${uri}` })),
}));

import * as VideoThumbnails from 'expo-video-thumbnails';

import { useSlotThumbnails } from '../../src/hooks/useSlotThumbnails';
import type { TimelineSlot } from '../../src/logic/timeline';

const SEGMENT = {
  id: 's1',
  recordedAt: '2026-07-18T08:00:00Z',
  durationSec: 120,
  s3Key: 'dev-1/s1.mp4',
  manuallyFlagged: false,
  userFeedback: null,
};

const SLOTS: TimelineSlot[] = [
  { startMinute: 480, segment: SEGMENT, isFlagged: false },
  { startMinute: 485, segment: null, isFlagged: false },
];

function fakeStorageClient() {
  return {
    storage: {
      from: () => ({
        createSignedUrl: (path: string) =>
          Promise.resolve({ data: { signedUrl: `https://signed/${path}` }, error: null }),
      }),
    },
  } as unknown as SupabaseClient;
}

describe('useSlotThumbnails', () => {
  it('extracts thumbnails for occupied slots only', async () => {
    const { result } = renderHook(() => useSlotThumbnails(fakeStorageClient(), SLOTS));
    await waitFor(() =>
      expect(result.current).toEqual({ 480: 'thumb:https://signed/dev-1/s1.mp4' }),
    );
  });

  it('caches by segment id across slot-array identity changes', async () => {
    const { result, rerender } = renderHook(({ slots }) => useSlotThumbnails(fakeStorageClient(), slots), {
      initialProps: { slots: SLOTS },
    });
    await waitFor(() => expect(Object.keys(result.current)).toHaveLength(1));
    rerender({ slots: [...SLOTS] });
    await waitFor(() => expect(Object.keys(result.current)).toHaveLength(1));
    expect(VideoThumbnails.getThumbnailAsync).toHaveBeenCalledTimes(1);
  });
});
```

Extend `app/__tests__/components/TimelineScrubber.test.tsx` (keep existing tests):

```tsx
it('renders a thumbnail image for slots that have one', () => {
  const slots = buildTimelineSlots([SEGMENT], new Date('2026-07-18T00:00:00Z'));
  render(
    <TimelineScrubber
      slots={slots}
      thumbnails={{ 480: 'file:///thumb.jpg' }}
      onSlotLongPress={jest.fn()}
    />,
  );
  expect(screen.getByTestId('thumb-480')).toBeTruthy();
});
```

(Use the segment fixture already present in that test file; `480` = 08:00 UTC.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/hooks/useSlotThumbnails.test.tsx __tests__/components/TimelineScrubber.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

`app/src/hooks/useSlotThumbnails.ts`:

```ts
import type { SupabaseClient } from '@supabase/supabase-js';
import * as VideoThumbnails from 'expo-video-thumbnails';
import { useEffect, useRef, useState } from 'react';

import { buildSegmentSignedUrlRequest } from '../logic/segmentExport';
import type { TimelineSlot } from '../logic/timeline';

export function useSlotThumbnails(
  client: SupabaseClient,
  slots: TimelineSlot[],
): Record<number, string> {
  const [thumbnails, setThumbnails] = useState<Record<number, string>>({});
  const cache = useRef(new Map<string, string>());

  useEffect(() => {
    let cancelled = false;

    (async () => {
      for (const slot of slots) {
        if (!slot.segment) continue;
        let uri = cache.current.get(slot.segment.id);
        if (!uri) {
          try {
            const request = buildSegmentSignedUrlRequest(slot.segment.s3Key);
            const { data } = await client.storage
              .from(request.bucket)
              .createSignedUrl(request.path, request.expiresInSec);
            if (!data?.signedUrl) continue;
            const result = await VideoThumbnails.getThumbnailAsync(data.signedUrl, { time: 0 });
            uri = result.uri;
            cache.current.set(slot.segment.id, uri);
          } catch (error) {
            console.warn('useSlotThumbnails: extraction failed', slot.segment.id, error);
            continue;
          }
        }
        if (cancelled) return;
        const resolved = uri;
        setThumbnails((prev) =>
          prev[slot.startMinute] === resolved ? prev : { ...prev, [slot.startMinute]: resolved },
        );
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [client, slots]);

  return thumbnails;
}
```

`app/src/components/TimelineScrubber.tsx`:

```tsx
import React from 'react';
import { Image, Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import type { TimelineSlot } from '../logic/timeline';
import { colors, spacing } from '../theme';

interface TimelineScrubberProps {
  slots: TimelineSlot[];
  thumbnails?: Record<number, string>;
  onSlotLongPress: (slot: TimelineSlot) => void;
}

export function TimelineScrubber({ slots, thumbnails = {}, onSlotLongPress }: TimelineScrubberProps) {
  return (
    <ScrollView horizontal testID="timeline-scrubber" style={styles.strip}>
      {slots.map((slot) => (
        <Pressable
          key={slot.startMinute}
          testID={`slot-${slot.startMinute}`}
          accessibilityLabel={`Timeline slot ${formatMinute(slot.startMinute)}`}
          onLongPress={() => onSlotLongPress(slot)}
        >
          <View style={[styles.slot, slot.segment ? styles.occupied : null]}>
            {thumbnails[slot.startMinute] ? (
              <Image
                testID={`thumb-${slot.startMinute}`}
                source={{ uri: thumbnails[slot.startMinute] }}
                style={styles.thumb}
                accessibilityLabel={`Preview at ${formatMinute(slot.startMinute)}`}
              />
            ) : null}
            {slot.isFlagged ? <Text testID={`flag-${slot.startMinute}`}>🚩</Text> : null}
          </View>
        </Pressable>
      ))}
    </ScrollView>
  );
}

function formatMinute(startMinute: number): string {
  const h = String(Math.floor(startMinute / 60)).padStart(2, '0');
  const m = String(startMinute % 60).padStart(2, '0');
  return `${h}:${m}`;
}

const styles = StyleSheet.create({
  strip: { flexGrow: 0 },
  slot: {
    width: 48,
    height: 64,
    marginRight: spacing.xs,
    borderRadius: 6,
    backgroundColor: colors.surface,
    alignItems: 'center',
    justifyContent: 'center',
    overflow: 'hidden',
  },
  occupied: { borderWidth: 1, borderColor: colors.accent },
  thumb: { position: 'absolute', width: '100%', height: '100%' },
});
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/hooks/useSlotThumbnails.test.tsx __tests__/components/TimelineScrubber.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/hooks/useSlotThumbnails.ts src/components/TimelineScrubber.tsx __tests__/hooks/useSlotThumbnails.test.tsx __tests__/components/TimelineScrubber.test.tsx
git commit -m "feat(app): timeline slot thumbnails via expo-video-thumbnails"
```

---

### Task 12: SegmentPreview playback + RawFootageContainer

**Files:**
- Modify: `app/src/components/SegmentPreview.tsx`
- Create: `app/src/containers/RawFootageContainer.tsx`
- Test: `app/__tests__/components/SegmentPreview.test.tsx` (extend), `app/__tests__/containers/RawFootageContainer.test.tsx`

**Interfaces:**
- Consumes: `useSegments` (Task 8), `useSignedUrl` (Task 9), `useSlotThumbnails` (Task 11), `buildTimelineSlots`, `buildSegmentSignedUrlRequest`, `toUtcMidnight` (Task 2), `TimelineScrubber`, `SegmentPreview`.
- Produces:

```tsx
// SegmentPreview gains:
//   videoUri: string | null  — VideoView when present, "Preparing playback..." placeholder otherwise
interface SegmentPreviewProps {
  segment: Segment | null;
  videoUri: string | null;
  onSave: () => void;
  onShare: () => void;
  onClose: () => void;
}

// RawFootageContainer
interface RawFootageContainerProps {
  client: SupabaseClient;
  deviceId: string;
  now?: () => Date;
}
export function RawFootageContainer(props: RawFootageContainerProps): React.JSX.Element;
```

Behavior: long-pressing an occupied slot opens `Alert.alert('Segment options', <recordedAt>, [...])` with buttons Preview / Always include / Never include / Clear preference / Cancel; feedback buttons call `setUserFeedback` with `'include' | 'exclude' | null`; Preview selects the segment, rendering `SegmentPreview` with its signed URL; Save downloads the signed URL with `File.downloadFileAsync` into `Paths.cache` then `MediaLibrary.saveToLibraryAsync` (after `requestPermissionsAsync`); Share uses `Share.share({ url })`.

- [ ] **Step 1: Write failing tests**

Extend `app/__tests__/components/SegmentPreview.test.tsx` - add the same `expo-video` mock as Task 10 at the top, pass `videoUri` in existing renders, and add:

```tsx
it('renders the video player when a uri is ready', () => {
  render(
    <SegmentPreview segment={SEGMENT} videoUri="https://signed/s1.mp4" onSave={jest.fn()} onShare={jest.fn()} onClose={jest.fn()} />,
  );
  expect(screen.getByTestId('video-view')).toBeTruthy();
});

it('shows a placeholder while the uri is pending', () => {
  render(
    <SegmentPreview segment={SEGMENT} videoUri={null} onSave={jest.fn()} onShare={jest.fn()} onClose={jest.fn()} />,
  );
  expect(screen.getByText('Preparing playback...')).toBeTruthy();
});
```

(`SEGMENT` = the fixture already in that file, or add one matching `src/types.ts`.)

Create `app/__tests__/containers/RawFootageContainer.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';
import { Alert } from 'react-native';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});
jest.mock('expo-video-thumbnails', () => ({
  getThumbnailAsync: jest.fn(() => Promise.resolve({ uri: 'file:///thumb.jpg' })),
}));
jest.mock('expo-media-library', () => ({
  requestPermissionsAsync: jest.fn(() => Promise.resolve({ granted: true })),
  saveToLibraryAsync: jest.fn(() => Promise.resolve()),
}));
jest.mock('expo-file-system', () => ({
  File: { downloadFileAsync: jest.fn(() => Promise.resolve({ uri: 'file:///cache/s1.mp4' })) },
  Directory: jest.fn(),
  Paths: { cache: 'file:///cache/' },
}));

import { RawFootageContainer } from '../../src/containers/RawFootageContainer';

const SEGMENT_ROW = {
  id: 's1',
  recorded_at: '2026-07-18T08:00:00Z',
  duration_sec: 120,
  s3_key: 'dev-1/s1.mp4',
  manually_flagged: false,
  user_feedback: null,
};

function fakeClient() {
  const updateCalls: { values: Record<string, unknown>; id: string }[] = [];
  const chain = {
    select: () => chain,
    eq: () => chain,
    gte: () => chain,
    lt: () => chain,
    order: () => Promise.resolve({ data: [SEGMENT_ROW], error: null }),
  };
  const client = {
    from: () => ({
      ...chain,
      update: (values: Record<string, unknown>) => ({
        eq: (_c: string, id: string) => {
          updateCalls.push({ values, id });
          return Promise.resolve({ error: null });
        },
      }),
    }),
    storage: {
      from: () => ({
        createSignedUrl: () =>
          Promise.resolve({ data: { signedUrl: 'https://signed/s1.mp4' }, error: null }),
      }),
    },
  } as unknown as SupabaseClient;
  return { client, updateCalls };
}

describe('RawFootageContainer', () => {
  it('renders the timeline once segments load', async () => {
    const { client } = fakeClient();
    render(
      <RawFootageContainer client={client} deviceId="dev-1" now={() => new Date('2026-07-18T15:00:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('timeline-scrubber')).toBeTruthy());
    expect(screen.getByTestId('slot-480')).toBeTruthy();
  });

  it('long-press offers feedback options that persist user_feedback', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    const { client, updateCalls } = fakeClient();
    render(
      <RawFootageContainer client={client} deviceId="dev-1" now={() => new Date('2026-07-18T15:00:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('slot-480')).toBeTruthy());
    fireEvent(screen.getByTestId('slot-480'), 'longPress');
    expect(alertSpy).toHaveBeenCalled();
    const buttons = alertSpy.mock.calls[0][2] as { text: string; onPress?: () => void }[];
    const alwaysInclude = buttons.find((b) => b.text === 'Always include');
    expect(alwaysInclude).toBeDefined();
    await waitFor(async () => {
      alwaysInclude?.onPress?.();
      expect(updateCalls).toEqual([{ values: { user_feedback: 'include' }, id: 's1' }]);
    });
    alertSpy.mockRestore();
  });

  it('long-press Preview opens the segment preview with playback', async () => {
    const alertSpy = jest.spyOn(Alert, 'alert').mockImplementation(() => {});
    const { client } = fakeClient();
    render(
      <RawFootageContainer client={client} deviceId="dev-1" now={() => new Date('2026-07-18T15:00:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('slot-480')).toBeTruthy());
    fireEvent(screen.getByTestId('slot-480'), 'longPress');
    const buttons = alertSpy.mock.calls[0][2] as { text: string; onPress?: () => void }[];
    buttons.find((b) => b.text === 'Preview')?.onPress?.();
    await waitFor(() => expect(screen.getByTestId('segment-preview')).toBeTruthy());
    alertSpy.mockRestore();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/components/SegmentPreview.test.tsx __tests__/containers/RawFootageContainer.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

`app/src/components/SegmentPreview.tsx`:

```tsx
import { useVideoPlayer, VideoView } from 'expo-video';
import React from 'react';
import { Button, StyleSheet, Text, View } from 'react-native';

import type { Segment } from '../types';
import { colors, spacing } from '../theme';

interface SegmentPreviewProps {
  segment: Segment | null;
  videoUri: string | null;
  onSave: () => void;
  onShare: () => void;
  onClose: () => void;
}

export function SegmentPreview({ segment, videoUri, onSave, onShare, onClose }: SegmentPreviewProps) {
  // Must run unconditionally before any early return (rules of hooks).
  const player = useVideoPlayer(videoUri);

  if (!segment) {
    return null;
  }

  return (
    <View testID="segment-preview" style={styles.container}>
      <Text style={styles.timestamp}>{segment.recordedAt}</Text>
      {videoUri ? (
        <VideoView
          player={player}
          style={styles.video}
          nativeControls
          accessibilityLabel="Segment video player"
        />
      ) : (
        <View style={[styles.video, styles.placeholder]}>
          <Text style={styles.muted}>Preparing playback...</Text>
        </View>
      )}
      <Button testID="save-button" title="Save to camera roll" color={colors.accent} accessibilityLabel="Save segment to camera roll" onPress={onSave} />
      <Button testID="share-button" title="Share" color={colors.accent} accessibilityLabel="Share segment" onPress={onShare} />
      <Button testID="close-button" title="Close" color={colors.textMuted} accessibilityLabel="Close preview" onPress={onClose} />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { backgroundColor: colors.surface, borderRadius: 12, padding: spacing.md },
  timestamp: { color: colors.textMuted, marginBottom: spacing.sm },
  video: { width: '100%', aspectRatio: 16 / 9, borderRadius: 8, backgroundColor: colors.background, marginBottom: spacing.sm },
  placeholder: { alignItems: 'center', justifyContent: 'center' },
  muted: { color: colors.textMuted },
});
```

`app/src/containers/RawFootageContainer.tsx`:

```tsx
import type { SupabaseClient } from '@supabase/supabase-js';
import { Directory, File, Paths } from 'expo-file-system';
import * as MediaLibrary from 'expo-media-library';
import React, { useMemo, useState } from 'react';
import { Alert, Share, StyleSheet, Text, View } from 'react-native';

import { SegmentPreview } from '../components/SegmentPreview';
import { TimelineScrubber } from '../components/TimelineScrubber';
import { useSegments } from '../hooks/useSegments';
import { useSignedUrl } from '../hooks/useSignedUrl';
import { useSlotThumbnails } from '../hooks/useSlotThumbnails';
import { toUtcMidnight } from '../logic/dates';
import { buildSegmentSignedUrlRequest } from '../logic/segmentExport';
import { buildTimelineSlots, type TimelineSlot } from '../logic/timeline';
import { colors, spacing } from '../theme';
import type { Segment } from '../types';

interface RawFootageContainerProps {
  client: SupabaseClient;
  deviceId: string;
  now?: () => Date;
}

export function RawFootageContainer({ client, deviceId, now = () => new Date() }: RawFootageContainerProps) {
  const dayStart = useMemo(() => toUtcMidnight(now()), [now]);
  const { state, setUserFeedback } = useSegments(client, deviceId, dayStart.toISOString());
  const [selected, setSelected] = useState<Segment | null>(null);

  const slots = useMemo(
    () => (state.kind === 'ready' ? buildTimelineSlots(state.segments, dayStart) : []),
    [state, dayStart],
  );
  const thumbnails = useSlotThumbnails(client, slots);
  const previewUrl = useSignedUrl(
    client,
    selected ? buildSegmentSignedUrlRequest(selected.s3Key) : null,
  );

  const onSlotLongPress = (slot: TimelineSlot) => {
    const segment = slot.segment;
    if (!segment) return;
    Alert.alert('Segment options', segment.recordedAt, [
      { text: 'Preview', onPress: () => setSelected(segment) },
      { text: 'Always include', onPress: () => void setUserFeedback(segment.id, 'include') },
      { text: 'Never include', onPress: () => void setUserFeedback(segment.id, 'exclude') },
      { text: 'Clear preference', onPress: () => void setUserFeedback(segment.id, null) },
      { text: 'Cancel', style: 'cancel' },
    ]);
  };

  const onSave = async () => {
    if (!selected || !previewUrl) return;
    try {
      const { granted } = await MediaLibrary.requestPermissionsAsync();
      if (!granted) {
        Alert.alert('Permission needed', 'Allow photo library access to save segments.');
        return;
      }
      const file = await File.downloadFileAsync(previewUrl, new Directory(Paths.cache));
      await MediaLibrary.saveToLibraryAsync(file.uri);
      Alert.alert('Saved', 'Segment saved to your camera roll.');
    } catch (error) {
      Alert.alert('Save failed', String(error));
    }
  };

  if (state.kind === 'loading') {
    return (
      <View style={styles.message}>
        <Text style={styles.muted}>Loading footage...</Text>
      </View>
    );
  }
  if (state.kind === 'error') {
    return (
      <View style={styles.message}>
        <Text style={styles.error} accessibilityLabel="Footage error">
          {state.message}
        </Text>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <Text style={styles.title} accessibilityRole="header">
        Raw Footage
      </Text>
      <TimelineScrubber slots={slots} thumbnails={thumbnails} onSlotLongPress={onSlotLongPress} />
      <View style={styles.previewWrap}>
        <SegmentPreview
          segment={selected}
          videoUri={previewUrl}
          onSave={() => void onSave()}
          onShare={() => {
            if (previewUrl) Share.share({ url: previewUrl, message: previewUrl });
          }}
          onClose={() => setSelected(null)}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background, padding: spacing.md },
  title: { color: colors.text, fontSize: 20, fontWeight: '600', marginBottom: spacing.md },
  previewWrap: { marginTop: spacing.md },
  message: { flex: 1, backgroundColor: colors.background, alignItems: 'center', justifyContent: 'center' },
  muted: { color: colors.textMuted, fontSize: 16 },
  error: { color: colors.danger, fontSize: 16 },
});
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/components/SegmentPreview.test.tsx __tests__/containers/RawFootageContainer.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/components/SegmentPreview.tsx src/containers/RawFootageContainer.tsx __tests__/components/SegmentPreview.test.tsx __tests__/containers/RawFootageContainer.test.tsx
git commit -m "feat(app): raw footage screen with preview playback, save/share, feedback menu"
```

---

### Task 13: ArchiveScreen styling + ArchiveContainer with UTC-normalized heatmap

**Files:**
- Modify: `app/src/screens/ArchiveScreen.tsx`
- Create: `app/src/containers/ArchiveContainer.tsx`
- Test: `app/__tests__/screens/ArchiveScreen.test.tsx` (extend), `app/__tests__/containers/ArchiveContainer.test.tsx`

**Interfaces:**
- Consumes: `useReelsInRange`, `useReel`, `useSignedUrl` (Task 9), `buildReelSignedUrlRequest` (Task 3), `utcRangeEndingAt`, `utcDateString` (Task 2), `buildHeatmapCells`, `ReelPlayer` (Task 10).
- Produces: `ArchiveScreen` keeps its `{ cells, onDayPress }` props (styled grid, a11y labels per cell: `Day <date>, reel available|no reel`); `ArchiveContainer({ client, deviceId, now? })` renders a 30-day heatmap; pressing a day with a reel plays it via `ReelPlayer` (title = the date), with a labeled "Back to archive" button clearing the selection.

- [ ] **Step 1: Write failing tests**

Extend `app/__tests__/screens/ArchiveScreen.test.tsx`:

```tsx
it('labels each cell for accessibility', () => {
  render(
    <ArchiveScreen
      cells={[
        { date: '2026-07-17', hasReel: false },
        { date: '2026-07-18', hasReel: true },
      ]}
      onDayPress={jest.fn()}
    />,
  );
  expect(screen.getByLabelText('Day 2026-07-18, reel available')).toBeTruthy();
  expect(screen.getByLabelText('Day 2026-07-17, no reel')).toBeTruthy();
});
```

Create `app/__tests__/containers/ArchiveContainer.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});

import { ArchiveContainer } from '../../src/containers/ArchiveContainer';

const REEL_ROW = {
  id: 'r1',
  date: '2026-07-10',
  s3_key: 'dev-1/2026-07-10.mp4',
  duration_sec: 60,
  style: 'clean',
};

function fakeClient() {
  const chain = {
    select: () => chain,
    eq: () => chain,
    gte: () => chain,
    lte: () => chain,
    order: () => chain,
    limit: () => Promise.resolve({ data: [REEL_ROW], error: null }),
    then: (onFulfilled: (v: { data: unknown; error: null }) => unknown) =>
      Promise.resolve({ data: [REEL_ROW], error: null }).then(onFulfilled),
  };
  return {
    from: () => chain,
    storage: {
      from: () => ({
        createSignedUrl: () =>
          Promise.resolve({ data: { signedUrl: 'https://signed/r1.mp4' }, error: null }),
      }),
    },
  } as unknown as SupabaseClient;
}

describe('ArchiveContainer', () => {
  it('renders 30 UTC-midnight-aligned cells ending today', async () => {
    render(
      <ArchiveContainer client={fakeClient()} deviceId="dev-1" now={() => new Date('2026-07-18T23:30:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('archive-heatmap')).toBeTruthy());
    expect(screen.getByTestId('heatmap-cell-2026-06-19')).toBeTruthy();
    expect(screen.getByTestId('heatmap-cell-2026-07-18')).toBeTruthy();
    expect(screen.queryByTestId('heatmap-cell-2026-06-18')).toBeNull();
    expect(screen.queryByTestId('heatmap-cell-2026-07-19')).toBeNull();
  });

  it('plays the reel for a pressed day', async () => {
    render(
      <ArchiveContainer client={fakeClient()} deviceId="dev-1" now={() => new Date('2026-07-18T12:00:00Z')} />,
    );
    await waitFor(() => expect(screen.getByTestId('heatmap-cell-2026-07-10')).toBeTruthy());
    fireEvent.press(screen.getByTestId('heatmap-cell-2026-07-10'));
    await waitFor(() => expect(screen.getByTestId('video-view')).toBeTruthy());
    fireEvent.press(screen.getByLabelText('Back to archive'));
    expect(screen.getByTestId('archive-heatmap')).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/screens/ArchiveScreen.test.tsx __tests__/containers/ArchiveContainer.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement**

`app/src/screens/ArchiveScreen.tsx`:

```tsx
import React from 'react';
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';

import type { HeatmapCell } from '../logic/heatmap';
import { colors, spacing } from '../theme';

interface ArchiveScreenProps {
  cells: HeatmapCell[];
  onDayPress: (date: string) => void;
}

export function ArchiveScreen({ cells, onDayPress }: ArchiveScreenProps) {
  return (
    <ScrollView testID="archive-heatmap" style={styles.container} contentContainerStyle={styles.grid}>
      {cells.map((cell) => (
        <Pressable
          key={cell.date}
          testID={`heatmap-cell-${cell.date}`}
          accessibilityLabel={`Day ${cell.date}, ${cell.hasReel ? 'reel available' : 'no reel'}`}
          onPress={() => onDayPress(cell.date)}
        >
          <View style={[styles.cell, cell.hasReel ? styles.hasReel : null]}>
            <Text style={cell.hasReel ? styles.dotActive : styles.dot}>{cell.hasReel ? '●' : '○'}</Text>
            <Text style={styles.date}>{cell.date.slice(5)}</Text>
          </View>
        </Pressable>
      ))}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background },
  grid: { flexDirection: 'row', flexWrap: 'wrap', padding: spacing.md },
  cell: {
    width: 56,
    height: 56,
    margin: spacing.xs,
    borderRadius: 8,
    backgroundColor: colors.surface,
    alignItems: 'center',
    justifyContent: 'center',
  },
  hasReel: { borderWidth: 1, borderColor: colors.accent },
  dot: { color: colors.textMuted },
  dotActive: { color: colors.accent },
  date: { color: colors.textMuted, fontSize: 10, marginTop: 2 },
});
```

`app/src/containers/ArchiveContainer.tsx`:

```tsx
import type { SupabaseClient } from '@supabase/supabase-js';
import React, { useMemo, useState } from 'react';
import { Button, Share, StyleSheet, Text, View } from 'react-native';

import { ReelPlayer } from '../components/ReelPlayer';
import { useReel, useReelsInRange } from '../hooks/useReel';
import { useSignedUrl } from '../hooks/useSignedUrl';
import { buildHeatmapCells } from '../logic/heatmap';
import { utcDateString, utcRangeEndingAt } from '../logic/dates';
import { buildReelSignedUrlRequest } from '../logic/segmentExport';
import { ArchiveScreen } from '../screens/ArchiveScreen';
import { colors, spacing } from '../theme';

const HEATMAP_DAYS = 30;

interface ArchiveContainerProps {
  client: SupabaseClient;
  deviceId: string;
  now?: () => Date;
}

export function ArchiveContainer({ client, deviceId, now = () => new Date() }: ArchiveContainerProps) {
  // buildHeatmapCells assumes UTC-midnight-aligned bounds; normalize before calling it
  // or users west of UTC get off-by-one day cells.
  const { start, end } = useMemo(() => utcRangeEndingAt(now(), HEATMAP_DAYS), [now]);
  const reelsState = useReelsInRange(client, deviceId, utcDateString(start), utcDateString(end));
  const [selectedDate, setSelectedDate] = useState<string | null>(null);
  const selectedReel = useReel(client, deviceId, selectedDate);
  const signedUrl = useSignedUrl(
    client,
    selectedReel.kind === 'ready' ? buildReelSignedUrlRequest(selectedReel.reel.s3Key) : null,
  );

  if (reelsState.kind === 'loading') {
    return (
      <View style={styles.message}>
        <Text style={styles.muted}>Loading archive...</Text>
      </View>
    );
  }
  if (reelsState.kind === 'error') {
    return (
      <View style={styles.message}>
        <Text style={styles.error} accessibilityLabel="Archive error">
          {reelsState.message}
        </Text>
      </View>
    );
  }

  if (selectedDate) {
    return (
      <View style={styles.playerWrap}>
        <ReelPlayer
          videoUri={signedUrl}
          title={selectedDate}
          onShare={() => {
            if (signedUrl) Share.share({ url: signedUrl, message: signedUrl });
          }}
        />
        {selectedReel.kind === 'none' ? (
          <Text style={styles.muted}>No reel for this day.</Text>
        ) : null}
        <View style={styles.backWrap}>
          <Button
            title="Back to archive"
            color={colors.textMuted}
            accessibilityLabel="Back to archive"
            onPress={() => setSelectedDate(null)}
          />
        </View>
      </View>
    );
  }

  const cells = buildHeatmapCells(reelsState.reels, start, end);
  return <ArchiveScreen cells={cells} onDayPress={setSelectedDate} />;
}

const styles = StyleSheet.create({
  message: { flex: 1, backgroundColor: colors.background, alignItems: 'center', justifyContent: 'center' },
  playerWrap: { flex: 1, backgroundColor: colors.background },
  backWrap: { padding: spacing.md },
  muted: { color: colors.textMuted, fontSize: 16, textAlign: 'center' },
  error: { color: colors.danger, fontSize: 16 },
});
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `npx jest __tests__/screens/ArchiveScreen.test.tsx __tests__/containers/ArchiveContainer.test.tsx`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/screens/ArchiveScreen.tsx src/containers/ArchiveContainer.tsx __tests__/screens/ArchiveScreen.test.tsx __tests__/containers/ArchiveContainer.test.tsx
git commit -m "feat(app): styled archive heatmap with UTC-normalized range and day playback"
```

---

### Task 14: App composition - auth gate + bottom tabs + full verification

**Files:**
- Modify: `app/App.tsx`
- Test: `app/__tests__/App.test.tsx`

**Interfaces:**
- Consumes: everything above; `supabase` from `src/lib/supabase.ts`.
- Produces:

```tsx
// App.tsx exports:
export function AppRoot({ client }: { client: SupabaseClient }): React.JSX.Element; // testable seam
export default function App(): React.JSX.Element; // <AppRoot client={supabase} />
```

`AppRoot`: `useAuth` gates everything - `loading` renders a splash (`Visio` title), `signed-out` renders `AuthContainer`, `signed-in` renders `SignedInApp` which resolves `useDevice` and then a `NavigationContainer` + bottom tab navigator with tabs Today's Reel / Raw Footage / Device / Archive (dark theme colors, `headerShown: false`); `none` explains "No device linked to this account yet."; a Sign out button lives in the Device tab header area - keep simpler: `tabBar` untouched, sign-out is **not** added (not in issue scope).

- [ ] **Step 1: Write failing tests**

`app/__tests__/App.test.tsx`:

```tsx
import { render, screen, waitFor } from '@testing-library/react-native';
import type { Session, SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

jest.mock('expo-video', () => {
  const { View } = jest.requireActual('react-native');
  return {
    useVideoPlayer: jest.fn(() => ({})),
    VideoView: (props: Record<string, unknown>) => <View testID="video-view" {...props} />,
  };
});
jest.mock('expo-video-thumbnails', () => ({
  getThumbnailAsync: jest.fn(() => Promise.resolve({ uri: 'file:///thumb.jpg' })),
}));
// App.tsx imports src/lib/supabase for the default export; keep env-independent.
jest.mock('../src/lib/supabase', () => ({ supabase: {}, supabaseClientOptions: {} }));

import { AppRoot } from '../App';

const SESSION = { access_token: 'tok', user: { id: 'u1' } } as unknown as Session;

function fakeClient(session: Session | null, devices: Record<string, unknown>[]) {
  const emptyChain = {
    select: () => emptyChain,
    eq: () => emptyChain,
    gte: () => emptyChain,
    lt: () => emptyChain,
    lte: () => emptyChain,
    single: () => Promise.resolve({ data: null, error: { code: 'PGRST116' } }),
    order: (..._a: unknown[]) => emptyChain,
    limit: () => Promise.resolve({ data: devices, error: null }),
    then: (onFulfilled: (v: { data: unknown[]; error: null }) => unknown) =>
      Promise.resolve({ data: [], error: null }).then(onFulfilled),
  };
  return {
    auth: {
      getSession: () => Promise.resolve({ data: { session } }),
      onAuthStateChange: () => ({ data: { subscription: { unsubscribe: jest.fn() } } }),
      signInWithPassword: jest.fn(),
      signOut: jest.fn(),
    },
    from: () => emptyChain,
    channel: () => ({ on: () => ({ subscribe: () => ({}) }) }),
    removeChannel: () => {},
    storage: { from: () => ({ createSignedUrl: () => Promise.resolve({ data: null, error: null }) }) },
  } as unknown as SupabaseClient;
}

describe('AppRoot', () => {
  it('shows the auth screen when signed out', async () => {
    render(<AppRoot client={fakeClient(null, [])} />);
    await waitFor(() => expect(screen.getByTestId('email-input')).toBeTruthy());
  });

  it('shows the four tabs when signed in with a device', async () => {
    render(<AppRoot client={fakeClient(SESSION, [{ device_id: 'dev-1', name: 'Pendant' }])} />);
    await waitFor(() => expect(screen.getByText("Today's Reel", { exact: false })).toBeTruthy());
    expect(screen.getByText('Raw Footage', { exact: false })).toBeTruthy();
    expect(screen.getByText('Device', { exact: false })).toBeTruthy();
    expect(screen.getByText('Archive', { exact: false })).toBeTruthy();
  });

  it('explains when the account has no device', async () => {
    render(<AppRoot client={fakeClient(SESSION, [])} />);
    await waitFor(() =>
      expect(screen.getByText('No device linked to this account yet.')).toBeTruthy(),
    );
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npx jest __tests__/App.test.tsx`
Expected: FAIL - `AppRoot` not exported.

- [ ] **Step 3: Implement**

`app/App.tsx`:

```tsx
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { NavigationContainer, DarkTheme } from '@react-navigation/native';
import type { SupabaseClient } from '@supabase/supabase-js';
import { StatusBar } from 'expo-status-bar';
import React from 'react';
import { StyleSheet, Text, View } from 'react-native';

import { ArchiveContainer } from './src/containers/ArchiveContainer';
import { AuthContainer } from './src/containers/AuthContainer';
import { DeviceContainer } from './src/containers/DeviceContainer';
import { RawFootageContainer } from './src/containers/RawFootageContainer';
import { TodayReelContainer } from './src/containers/TodayReelContainer';
import { useAuth } from './src/hooks/useAuth';
import { useDevice } from './src/hooks/useDevice';
import { supabase } from './src/lib/supabase';
import { colors } from './src/theme';

const Tab = createBottomTabNavigator();

export function AppRoot({ client }: { client: SupabaseClient }) {
  const { state, signIn } = useAuth(client);

  if (state.kind === 'loading') {
    return (
      <View style={styles.splash}>
        <Text style={styles.splashTitle} accessibilityRole="header">
          Visio
        </Text>
        <StatusBar style="light" />
      </View>
    );
  }

  if (state.kind === 'signed-out') {
    return (
      <>
        <AuthContainer signIn={signIn} />
        <StatusBar style="light" />
      </>
    );
  }

  return <SignedInApp client={client} />;
}

function SignedInApp({ client }: { client: SupabaseClient }) {
  const device = useDevice(client);

  if (device.kind === 'loading') {
    return (
      <View style={styles.splash}>
        <Text style={styles.muted}>Loading your device...</Text>
      </View>
    );
  }
  if (device.kind === 'error') {
    return (
      <View style={styles.splash}>
        <Text style={styles.error} accessibilityLabel="Device lookup error">
          {device.message}
        </Text>
      </View>
    );
  }
  if (device.kind === 'none') {
    return (
      <View style={styles.splash}>
        <Text style={styles.muted}>No device linked to this account yet.</Text>
      </View>
    );
  }

  const deviceId = device.deviceId;
  return (
    <NavigationContainer theme={DarkTheme}>
      <Tab.Navigator
        screenOptions={{
          headerShown: false,
          tabBarActiveTintColor: colors.accent,
          tabBarInactiveTintColor: colors.textMuted,
          tabBarStyle: { backgroundColor: colors.surface, borderTopColor: colors.background },
        }}
      >
        <Tab.Screen name="Today's Reel">
          {() => <TodayReelContainer client={client} deviceId={deviceId} />}
        </Tab.Screen>
        <Tab.Screen name="Raw Footage">
          {() => <RawFootageContainer client={client} deviceId={deviceId} />}
        </Tab.Screen>
        <Tab.Screen name="Device">
          {() => <DeviceContainer client={client} deviceId={deviceId} />}
        </Tab.Screen>
        <Tab.Screen name="Archive">
          {() => <ArchiveContainer client={client} deviceId={deviceId} />}
        </Tab.Screen>
      </Tab.Navigator>
      <StatusBar style="light" />
    </NavigationContainer>
  );
}

export default function App() {
  return <AppRoot client={supabase} />;
}

const styles = StyleSheet.create({
  splash: {
    flex: 1,
    backgroundColor: colors.background,
    alignItems: 'center',
    justifyContent: 'center',
  },
  splashTitle: { color: colors.text, fontSize: 40, fontWeight: '700' },
  muted: { color: colors.textMuted, fontSize: 16 },
  error: { color: colors.danger, fontSize: 16 },
});
```

- [ ] **Step 4: Run the App tests, then the whole suite and typecheck**

Run: `npx jest __tests__/App.test.tsx`
Expected: PASS.

Run: `npx jest`
Expected: ALL tests PASS (every suite from Tasks 1-14 plus the pre-existing logic/component suites).

Run: `npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add App.tsx __tests__/App.test.tsx
git commit -m "feat(app): auth-gated bottom tab navigation composing all four screens"
```

---

## Self-Review

- Issue #8 scope coverage: tabs + containers (Tasks 5, 10, 12, 13, 14), real auth with AsyncStorage persistence (Tasks 1, 6), `expo-video` playback (Tasks 10, 12), timeline thumbnails + reel playback surfaces (Tasks 10, 11, 13), styling + a11y labels (every screen task), `subscribe()` status callback + fetch discriminant (Task 4), UTC-midnight normalization (Tasks 2, 13). Out of scope per issue: regenerate sheet, QR re-onboarding screen (button alerts honestly, Task 5).
- Type consistency: `DeviceStatusState`/`RealtimeHealth` (Task 4) consumed in Task 5; `SignedUrlRequest` widened in Task 3 consumed by Tasks 9, 11; `signIn` signature `(email, password) => Promise<string | null>` consistent across Tasks 6 and 14; `now?: () => Date` injection consistent across Tasks 10, 12, 13.
- Mocked-native caveat: `expo-video`, `expo-video-thumbnails`, `expo-media-library`, and `expo-file-system` are jest-mocked; real-device behavior needs a manual Expo run (out of CI scope, noted for the PR description).
