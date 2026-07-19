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
