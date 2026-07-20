import React from 'react';
import { Button, StyleSheet, Text, TextInput, View } from 'react-native';
import QRCode from 'react-native-qrcode-svg';

import { colors, spacing } from '../theme';

export type ReonboardStep = 'form' | 'ready' | 'error';

// The payload embeds two Supabase JWTs (~700-1000 chars each), pushing the
// QR to a high version - the library's 100px default packs those modules
// too small for a phone-held-to-camera scan, so size up and use the lowest
// error-correction level to keep module density scannable.
const QR_SIZE = 260;

interface ReonboardScreenProps {
  step: ReonboardStep;
  ssid: string;
  password: string;
  qrValue: string;
  errorMessage: string;
  onSsidChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: (ssid: string, password: string) => void;
  onDone: () => void;
  onRetry: () => void;
}

export function ReonboardScreen({
  step,
  ssid,
  password,
  qrValue,
  errorMessage,
  onSsidChange,
  onPasswordChange,
  onSubmit,
  onDone,
  onRetry,
}: ReonboardScreenProps) {
  if (step === 'ready') {
    return (
      <View style={styles.container}>
        <View style={styles.qrWrap}>
          <QRCode value={qrValue} size={QR_SIZE} ecl="L" />
        </View>
        <Text style={styles.hint}>
          This code contains your WiFi password and an active login - only show it to your Visio
          device&apos;s camera
        </Text>
        <View style={styles.buttonWrap}>
          <Button
            testID="done-button"
            title="Done"
            color={colors.accent}
            accessibilityLabel="Finish re-onboarding"
            onPress={onDone}
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
          title="Generate QR"
          color={colors.accent}
          accessibilityLabel="Generate onboarding QR code"
          disabled={!ssid || !password}
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
  qrWrap: { alignItems: 'center', marginTop: spacing.lg, marginBottom: spacing.lg },
  hint: { color: colors.textMuted, fontSize: 14, textAlign: 'center', marginBottom: spacing.md },
  error: { color: colors.danger, fontSize: 16, marginBottom: spacing.md },
  buttonWrap: { marginTop: spacing.md },
});
