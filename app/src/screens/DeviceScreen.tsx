import React from 'react';
import { Button, Text, View } from 'react-native';

import type { DeviceStatus } from '../types';

interface DeviceScreenProps {
  status: DeviceStatus | null;
  onReonboardPress: () => void;
}

export function DeviceScreen({ status, onReonboardPress }: DeviceScreenProps) {
  if (!status) {
    return (
      <View>
        <Text>Loading device status...</Text>
      </View>
    );
  }

  return (
    <View>
      <Text>Battery: {status.batteryPct}%</Text>
      <Text>Storage used: {status.storageUsedGb} GB</Text>
      <Text>Storage free: {status.storageFreeGb} GB</Text>
      <Text>Segments pending: {status.segmentsPending}</Text>
      <Text>Segments uploaded today: {status.segmentsUploadedToday}</Text>
      <Text>{status.recordingActive ? 'Recording' : 'Paused'}</Text>
      <Button testID="reonboard-button" title="Re-onboard WiFi" onPress={onReonboardPress} />
    </View>
  );
}
