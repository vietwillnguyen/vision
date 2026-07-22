import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
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

function fakeNavigation() {
  return { navigate: jest.fn(), goBack: jest.fn() } as unknown as Parameters<
    typeof DeviceContainer
  >[0]['navigation'];
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
      navigation={fakeNavigation()}
      route={{ key: 'DeviceHome', name: 'DeviceHome' }}
    />,
  );
  await waitFor(() => expect(screen.getByText('Battery: 55%')).toBeTruthy());
  expect(screen.getByText('Paused')).toBeTruthy();
});

it('navigates to Reonboard when the re-onboard button is pressed', async () => {
  const navigation = fakeNavigation();
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
      navigation={navigation}
      route={{ key: 'DeviceHome', name: 'DeviceHome' }}
    />,
  );
  await waitFor(() => expect(screen.getByTestId('reonboard-button')).toBeTruthy());
  fireEvent.press(screen.getByTestId('reonboard-button'));
  expect(navigation.navigate).toHaveBeenCalledWith('Reonboard');
});
