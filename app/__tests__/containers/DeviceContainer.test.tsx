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
