import React from 'react';
import { Button, StyleSheet, Text, TextInput, View } from 'react-native';
import QRCode from 'react-native-qrcode-svg';

import { colors, spacing } from '../theme';

export type ReonboardStep = 'form' | 'ready' | 'paired' | 'expired' | 'error';

// The v2 payload (Wi-Fi credentials plus a 43-character pairing code) is
// under 150 characters, so the library's default error correction is fine;
// the size only has to suit a phone held up to the pendant's camera.
const QR_SIZE = 260;

interface ReonboardScreenProps {
  step: ReonboardStep;
  ssid: string;
  password: string;
  qrValue: string;
  errorMessage: string;
  isSubmitting: boolean;
  onSsidChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: (ssid: string, password: string) => void;
  onDone: () => void;
  onRetry: () => void;
  onNewCode: () => void;
}

export function ReonboardScreen({
  step,
  ssid,
  password,
  qrValue,
  errorMessage,
  isSubmitting,
  onSsidChange,
  onPasswordChange,
  onSubmit,
  onDone,
  onRetry,
  onNewCode,
}: ReonboardScreenProps) {
  if (step === 'ready') {
    return (
      <View style={styles.container}>
        <View style={styles.qrWrap}>
          <View style={styles.qrQuietZone}>
            <QRCode value={qrValue} size={QR_SIZE} />
          </View>
        </View>
        <Text style={styles.waiting}>Waiting for your pendant to scan this code...</Text>
        <Text style={styles.hint}>
          This code contains your Wi-Fi password and a one-time pairing code. Only show it to your
          Visio pendant&apos;s camera.
        </Text>
        <View style={styles.buttonWrap}>
          <Button
            testID="done-button"
            title="Done"
            color={colors.accent}
            accessibilityLabel="Close the pairing code"
            onPress={onDone}
          />
        </View>
      </View>
    );
  }

  if (step === 'paired') {
    return (
      <View style={styles.container}>
        <Text testID="paired-message" style={styles.title} accessibilityRole="header">
          Pendant paired
        </Text>
        <Text style={styles.body}>
          Your Visio pendant is signed in with its own login and will start uploading.
        </Text>
        <View style={styles.buttonWrap}>
          <Button
            testID="done-button"
            title="Done"
            color={colors.accent}
            accessibilityLabel="Finish pairing"
            onPress={onDone}
          />
        </View>
      </View>
    );
  }

  if (step === 'expired') {
    return (
      <View style={styles.container}>
        <Text testID="expired-message" style={styles.title} accessibilityRole="header">
          This code has expired
        </Text>
        <Text style={styles.body}>
          Pairing codes work for 10 minutes. Make a new one to continue.
        </Text>
        <View style={styles.buttonWrap}>
          <Button
            testID="new-code-button"
            title={isSubmitting ? 'Generating...' : 'Generate a new code'}
            color={colors.accent}
            accessibilityLabel="Generate a new pairing code"
            disabled={isSubmitting}
            onPress={onNewCode}
          />
        </View>
      </View>
    );
  }

  if (step === 'error') {
    return (
      <View style={styles.container}>
        <Text
          testID="reonboard-error"
          style={styles.error}
          accessibilityLabel="Re-onboarding error"
        >
          {errorMessage}
        </Text>
        <View style={styles.buttonWrap}>
          <Button
            testID="try-again-button"
            title="Try again"
            color={colors.accent}
            accessibilityLabel="Try again"
            onPress={onRetry}
          />
        </View>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <TextInput
        testID="ssid-input"
        style={styles.input}
        placeholder="WiFi network name"
        placeholderTextColor={colors.textMuted}
        value={ssid}
        onChangeText={onSsidChange}
        autoCapitalize="none"
        accessibilityLabel="WiFi network name"
      />
      <TextInput
        testID="password-input"
        style={styles.input}
        placeholder="WiFi password"
        placeholderTextColor={colors.textMuted}
        value={password}
        onChangeText={onPasswordChange}
        secureTextEntry
        accessibilityLabel="WiFi password"
      />
      <View style={styles.buttonWrap}>
        <Button
          testID="generate-qr-button"
          title={isSubmitting ? 'Generating...' : 'Generate QR'}
          color={colors.accent}
          accessibilityLabel="Generate onboarding QR code"
          disabled={!ssid || !password || isSubmitting}
          onPress={() => onSubmit(ssid, password)}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.background, padding: spacing.md },
  input: {
    backgroundColor: colors.surface,
    color: colors.text,
    borderRadius: 8,
    padding: spacing.md,
    marginBottom: spacing.sm,
  },
  qrWrap: { alignItems: 'center', marginTop: spacing.lg, marginBottom: spacing.md },
  // A white margin around the modules: a dark QR edge against the dark
  // background is harder for the pendant's camera to find.
  qrQuietZone: { backgroundColor: '#ffffff', padding: spacing.md, borderRadius: 8 },
  waiting: { color: colors.text, fontSize: 16, textAlign: 'center', marginBottom: spacing.sm },
  title: { color: colors.text, fontSize: 22, fontWeight: '700', marginBottom: spacing.sm },
  body: { color: colors.textMuted, fontSize: 16, marginBottom: spacing.md },
  hint: { color: colors.textMuted, fontSize: 14, textAlign: 'center', marginBottom: spacing.md },
  error: { color: colors.danger, fontSize: 16, marginBottom: spacing.md },
  buttonWrap: { marginTop: spacing.md },
});
