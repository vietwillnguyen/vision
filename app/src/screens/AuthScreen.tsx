import React from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';

import type { AuthFormErrors } from '../logic/authValidation';
import { colors, spacing } from '../theme';

interface AuthScreenProps {
  email: string;
  password: string;
  errors: AuthFormErrors;
  authError?: string | null;
  onEmailChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: () => void;
  /** Present only when the build carries public demo config. */
  onViewDemo?: () => void;
  demoPending?: boolean;
}

export function AuthScreen({
  email,
  password,
  errors,
  authError,
  onEmailChange,
  onPasswordChange,
  onSubmit,
  onViewDemo,
  demoPending = false,
}: AuthScreenProps) {
  return (
    <View style={styles.container}>
      <Text style={styles.title} accessibilityRole="header">
        Visio
      </Text>
      <TextInput
        testID="email-input"
        style={styles.input}
        placeholder="Email"
        placeholderTextColor={colors.textMuted}
        value={email}
        onChangeText={onEmailChange}
        autoCapitalize="none"
        keyboardType="email-address"
        accessibilityLabel="Email address"
      />
      {errors.email ? (
        <Text testID="email-error" style={styles.fieldError}>
          {errors.email}
        </Text>
      ) : null}
      <TextInput
        testID="password-input"
        style={styles.input}
        placeholder="Password"
        placeholderTextColor={colors.textMuted}
        value={password}
        onChangeText={onPasswordChange}
        secureTextEntry
        accessibilityLabel="Password"
      />
      {errors.password ? (
        <Text testID="password-error" style={styles.fieldError}>
          {errors.password}
        </Text>
      ) : null}
      {authError ? (
        <Text testID="auth-error" style={styles.authError} accessibilityLabel="Sign in error">
          {authError}
        </Text>
      ) : null}
      <Pressable
        testID="submit-button"
        accessibilityRole="button"
        accessibilityLabel="Sign in"
        onPress={onSubmit}
        style={({ pressed }) => [styles.signIn, pressed ? styles.signInPressed : null]}
      >
        <Text style={styles.signInText}>Sign in</Text>
      </Pressable>
      {onViewDemo ? (
        <View style={styles.demo}>
          <View style={styles.divider}>
            <View style={styles.rule} />
            <Text style={styles.dividerText}>or</Text>
            <View style={styles.rule} />
          </View>
          <Pressable
            testID="view-demo-button"
            accessibilityRole="button"
            accessibilityLabel="View demo"
            accessibilityState={{ busy: demoPending, disabled: demoPending }}
            disabled={demoPending}
            onPress={onViewDemo}
            style={({ pressed }) => [styles.demoButton, pressed ? styles.demoButtonPressed : null]}
          >
            {demoPending ? (
              <ActivityIndicator color={colors.accent} />
            ) : (
              <Text style={styles.demoButtonText}>View demo</Text>
            )}
          </Pressable>
          <Text style={styles.demoCaption}>
            Browse a sample day from the bench prototype. Read-only, no account needed.
          </Text>
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: colors.background,
    justifyContent: 'center',
    padding: spacing.lg,
  },
  title: {
    color: colors.text,
    fontSize: 32,
    fontWeight: '700',
    textAlign: 'center',
    marginBottom: spacing.lg,
  },
  input: {
    backgroundColor: colors.surface,
    color: colors.text,
    borderRadius: 8,
    padding: spacing.md,
    marginBottom: spacing.sm,
  },
  fieldError: { color: colors.danger, marginBottom: spacing.sm },
  authError: { color: colors.danger, marginTop: spacing.sm, textAlign: 'center' },
  signIn: {
    backgroundColor: colors.accent,
    borderRadius: 8,
    minHeight: 44,
    alignItems: 'center',
    justifyContent: 'center',
    marginTop: spacing.md,
  },
  signInPressed: { opacity: 0.85 },
  signInText: { color: colors.text, fontSize: 16, fontWeight: '600' },
  demo: { marginTop: spacing.lg },
  divider: { flexDirection: 'row', alignItems: 'center', marginBottom: spacing.md },
  rule: { flex: 1, height: StyleSheet.hairlineWidth, backgroundColor: colors.textMuted },
  dividerText: { color: colors.textMuted, marginHorizontal: spacing.sm },
  demoButton: {
    borderWidth: 1,
    borderColor: colors.accent,
    borderRadius: 8,
    minHeight: 44,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: spacing.md,
  },
  demoButtonPressed: { backgroundColor: colors.surface },
  demoButtonText: { color: colors.accent, fontSize: 16, fontWeight: '600' },
  demoCaption: {
    color: colors.textMuted,
    fontSize: 13,
    textAlign: 'center',
    marginTop: spacing.sm,
  },
});
