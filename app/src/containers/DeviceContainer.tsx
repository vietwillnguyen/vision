import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import React, { useState } from 'react';

import type { DeviceStackParamList } from '../navigation/DeviceStack';
import { useDeviceStatus } from '../hooks/useDeviceStatus';
import type { RemovalState } from '../screens/DeviceScreen';
import { DeviceScreen } from '../screens/DeviceScreen';
import type { VisionClient } from '../lib/supabase';

type DeviceContainerProps = NativeStackScreenProps<DeviceStackParamList, 'DeviceHome'> & {
  client: VisionClient;
  deviceId: string;
  paired: boolean;
  // Re-read the device after its pairing changed.
  onDeviceChanged: () => void;
};

export function DeviceContainer({
  client,
  deviceId,
  paired,
  onDeviceChanged,
  navigation,
}: DeviceContainerProps) {
  const state = useDeviceStatus(client, deviceId);
  const [removal, setRemoval] = useState<RemovalState>({ kind: 'idle' });

  // Revoke only: the database clears the pendant's link and deletes its
  // login; the device row and its footage stay.
  const onConfirmRemove = async () => {
    setRemoval({ kind: 'removing' });
    try {
      const { error } = await client.rpc('unpair_device', { p_device_id: deviceId });
      if (error) {
        setRemoval({ kind: 'error', message: error.message });
        return;
      }
      setRemoval({ kind: 'idle' });
      onDeviceChanged();
    } catch (caught) {
      setRemoval({
        kind: 'error',
        message: caught instanceof Error ? caught.message : String(caught),
      });
    }
  };

  return (
    <DeviceScreen
      state={state}
      paired={paired}
      removal={removal}
      onReonboardPress={() => navigation.navigate('Reonboard')}
      onRemovePress={() => setRemoval({ kind: 'confirming' })}
      onConfirmRemove={onConfirmRemove}
      onCancelRemove={() => setRemoval({ kind: 'idle' })}
    />
  );
}
