import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

import type { DeviceStackParamList } from '../navigation/DeviceStack';
import { useDeviceStatus } from '../hooks/useDeviceStatus';
import { DeviceScreen } from '../screens/DeviceScreen';

type DeviceContainerProps = NativeStackScreenProps<DeviceStackParamList, 'DeviceHome'> & {
  client: SupabaseClient;
  deviceId: string;
};

export function DeviceContainer({ client, deviceId, navigation }: DeviceContainerProps) {
  const state = useDeviceStatus(client, deviceId);
  return (
    <DeviceScreen state={state} onReonboardPress={() => navigation.navigate('Reonboard')} />
  );
}
