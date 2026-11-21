import React from 'react';
import { Button, StyleSheet, Text, View } from 'react-native';

import type { DeviceStatusState } from '../hooks/useDeviceStatus';
import { colors, spacing } from '../theme';

interface DeviceScreenProps {
  state: DeviceStatusState;
  onReonboardPress: () => void;
  deviceName?: string | null;
  /** The read-only demo: Wi-Fi setup is disabled, with the reason shown. */
  readOnly?: boolean;
}

export function DeviceScreen({
  state,
  onReonboardPress,
  deviceName = null,
  readOnly = false,
}: DeviceScreenProps) {
  const header = (
    <>
      <Text style={styles.title} accessibilityRole="header">
        Device
      </Text>
      {deviceName ? <Text style={styles.subtitle}>{deviceName}</Text> : null}
      {readOnly ? (
        <Text style={styles.note}>
          A bench prototype on USB power; the wearable hardware is still in progress.
        </Text>
      ) : null}
    </>
  );

  if (state.kind === 'loading') {
    return (
      <View style={styles.container}>
        {header}
        <Text style={styles.muted}>Loading device status...</Text>
      </View>
    );
  }

  if (state.kind === 'error') {
    return (
      <View style={styles.container}>
        {header}
        <Text testID="device-error" style={styles.error} accessibilityLabel="Device status error">
          {state.message}
        </Text>
      </View>
    );
  }

  const { status, realtime } = state;
  return (
    <View style={styles.container}>
      {header}
      {realtime === 'stale' ? (
        <Text
          testID="stale-banner"
          style={styles.staleBanner}
          accessibilityLabel="Realtime connection lost"
        >
          Live updates disconnected - data may be stale
        </Text>
      ) : null}
      <Text style={styles.row}>Battery: {status.batteryPct}%</Text>
      <Text style={styles.row}>Storage used: {status.storageUsedGb} GB</Text>
      <Text style={styles.row}>Storage free: {status.storageFreeGb} GB</Text>
      <Text style={styles.row}>Segments pending: {status.segmentsPending}</Text>
      <Text style={styles.row}>Segments uploaded today: {status.segmentsUploadedToday}</Text>
      <Text style={[styles.row, status.recordingActive ? styles.recording : styles.paused]}>
        {status.recordingActive ? 'Recording' : 'Stopped'}
      </Text>
      <View style={styles.buttonWrap}>
        <Button
          testID="reonboard-button"
          title="Re-onboard WiFi"
          color={colors.accent}
          accessibilityLabel="Re-onboard device WiFi"
          onPress={onReonboardPress}
          disabled={readOnly}
        />
        {readOnly ? (
          <Text testID="reonboard-disabled-note" style={styles.note}>
            Wi-Fi setup is turned off in this read-only demo. It would hand the pendant this
            account&apos;s login.
          </Text>
        ) : null}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background, padding: spacing.md },
  title: { color: colors.text, fontSize: 20, fontWeight: '600' },
  subtitle: { color: colors.textMuted, fontSize: 14, marginTop: 2, marginBottom: spacing.md },
  note: {
    color: colors.textMuted,
    fontSize: 13,
    marginTop: spacing.sm,
    marginBottom: spacing.sm,
  },
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
