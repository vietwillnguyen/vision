import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import React from 'react';

import type { DeviceStackParamList } from '../navigation/DeviceStack';
import { useDeviceStatus } from '../hooks/useDeviceStatus';
import { DeviceScreen } from '../screens/DeviceScreen';
import type { VisionClient } from '../lib/supabase';

type DeviceContainerProps = NativeStackScreenProps<DeviceStackParamList, 'DeviceHome'> & {
  client: VisionClient;
  deviceId: string;
};

export function DeviceContainer({ client, deviceId, navigation }: DeviceContainerProps) {
  const state = useDeviceStatus(client, deviceId);
  return (
    <DeviceScreen state={state} onReonboardPress={() => navigation.navigate('Reonboard')} />
  );
}
