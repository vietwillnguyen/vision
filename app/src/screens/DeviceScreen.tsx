import React from 'react';
import { Button, StyleSheet, Text, View } from 'react-native';

import type { DeviceStatusState } from '../hooks/useDeviceStatus';
import { colors, spacing } from '../theme';

export type RemovalState =
  | { kind: 'idle' }
  | { kind: 'confirming' }
  | { kind: 'removing' }
  | { kind: 'error'; message: string };

export interface DeviceScreenProps {
  state: DeviceStatusState;
  // False once Remove device has revoked the pendant's login.
  paired: boolean;
  removal: RemovalState;
  onReonboardPress: () => void;
  onRemovePress: () => void;
  onConfirmRemove: () => void;
  onCancelRemove: () => void;
}

function StatusSection({ state, paired }: { state: DeviceStatusState; paired: boolean }) {
  if (state.kind === 'loading') {
    // A removed pendant may never have reported; there is nothing to wait for.
    return paired ? <Text style={styles.muted}>Loading device status...</Text> : null;
  }

  if (state.kind === 'error') {
    return (
      <Text testID="device-error" style={styles.error} accessibilityLabel="Device status error">
        {state.message}
      </Text>
    );
  }

  const { status, realtime } = state;
  return (
    <View>
      {paired && realtime === 'stale' ? (
        <Text
          testID="stale-banner"
          style={styles.staleBanner}
          accessibilityLabel="Realtime connection lost"
        >
          Live updates disconnected - data may be stale
        </Text>
      ) : null}
      {!paired ? <Text style={styles.sectionLabel}>Last report from the pendant</Text> : null}
      <Text style={styles.row}>Battery: {status.batteryPct}%</Text>
      <Text style={styles.row}>Storage used: {status.storageUsedGb} GB</Text>
      <Text style={styles.row}>Storage free: {status.storageFreeGb} GB</Text>
      <Text style={styles.row}>Segments pending: {status.segmentsPending}</Text>
      <Text style={styles.row}>Segments uploaded today: {status.segmentsUploadedToday}</Text>
      <Text
        style={[styles.row, status.recordingActive && paired ? styles.recording : styles.paused]}
      >
        {status.recordingActive ? 'Recording' : 'Paused'}
      </Text>
    </View>
  );
}

function RemoveDevice({
  removal,
  onRemovePress,
  onConfirmRemove,
  onCancelRemove,
}: Pick<DeviceScreenProps, 'removal' | 'onRemovePress' | 'onConfirmRemove' | 'onCancelRemove'>) {
  if (removal.kind === 'confirming' || removal.kind === 'removing') {
    const removing = removal.kind === 'removing';
    return (
      <View testID="remove-confirmation" style={styles.card} accessibilityRole="alert">
        <Text style={styles.cardTitle}>Remove this device?</Text>
        <Text style={styles.cardBody}>
          The pendant will be signed out. Your footage stays in your account.
        </Text>
        <View style={styles.actions}>
          <View style={styles.action}>
            <Button
              testID="cancel-remove-button"
              title="Cancel"
              color={colors.textMuted}
              accessibilityLabel="Keep the device"
              disabled={removing}
              onPress={onCancelRemove}
            />
          </View>
          <View style={styles.action}>
            <Button
              testID="confirm-remove-button"
              title={removing ? 'Removing...' : 'Remove'}
              color={colors.danger}
              accessibilityLabel="Remove the device and sign the pendant out"
              disabled={removing}
              onPress={onConfirmRemove}
            />
          </View>
        </View>
      </View>
    );
  }

  return (
    <View>
      {removal.kind === 'error' ? (
        <Text testID="remove-error" style={styles.error} accessibilityLabel="Remove device error">
          {removal.message}
        </Text>
      ) : null}
      <View style={styles.buttonWrap}>
        <Button
          testID="remove-device-button"
          title="Remove device"
          color={colors.danger}
          accessibilityLabel="Remove device"
          onPress={onRemovePress}
        />
      </View>
    </View>
  );
}

export function DeviceScreen({
  state,
  paired,
  removal,
  onReonboardPress,
  onRemovePress,
  onConfirmRemove,
  onCancelRemove,
}: DeviceScreenProps) {
  if (!paired) {
    return (
      <View style={styles.container}>
        <View testID="not-paired" style={[styles.card, styles.firstCard]}>
          <Text style={styles.cardTitle}>Not paired</Text>
          <Text style={styles.cardBody}>
            No pendant is signed in to this device. Pair one to start recording and uploading again;
            your footage is still here.
          </Text>
          <Button
            testID="pair-button"
            title="Pair pendant"
            color={colors.accent}
            accessibilityLabel="Pair a pendant"
            onPress={onReonboardPress}
          />
        </View>
        <StatusSection state={state} paired={false} />
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <StatusSection state={state} paired />
      <View style={styles.buttonWrap}>
        <Button
          testID="reonboard-button"
          title="Re-onboard WiFi"
          color={colors.accent}
          accessibilityLabel="Re-onboard device WiFi"
          onPress={onReonboardPress}
        />
      </View>
      <RemoveDevice
        removal={removal}
        onRemovePress={onRemovePress}
        onConfirmRemove={onConfirmRemove}
        onCancelRemove={onCancelRemove}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background, padding: spacing.md },
  row: { color: colors.text, fontSize: 16, marginBottom: spacing.sm },
  muted: { color: colors.textMuted, fontSize: 16 },
  error: { color: colors.danger, fontSize: 16, marginTop: spacing.md },
  recording: { color: colors.success },
  paused: { color: colors.textMuted },
  staleBanner: {
    backgroundColor: colors.warning,
    color: colors.background,
    padding: spacing.sm,
    borderRadius: 6,
    marginBottom: spacing.md,
  },
  sectionLabel: { color: colors.textMuted, fontSize: 14, marginBottom: spacing.sm },
  buttonWrap: { marginTop: spacing.lg },
  card: {
    backgroundColor: colors.surface,
    borderRadius: 8,
    padding: spacing.md,
    marginTop: spacing.lg,
    marginBottom: spacing.lg,
  },
  firstCard: { marginTop: 0 },
  cardTitle: { color: colors.text, fontSize: 18, fontWeight: '700', marginBottom: spacing.sm },
  cardBody: { color: colors.textMuted, fontSize: 15, marginBottom: spacing.md },
  actions: { flexDirection: 'row', justifyContent: 'flex-end' },
  action: { marginLeft: spacing.sm },
});
