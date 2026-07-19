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
