import React from 'react';
import { Button, StyleSheet, Text, TextInput, View } from 'react-native';

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
}

export function AuthScreen({
  email,
  password,
  errors,
  authError,
  onEmailChange,
  onPasswordChange,
  onSubmit,
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
      <View style={styles.buttonWrap}>
        <Button
          testID="submit-button"
          title="Sign In"
          color={colors.accent}
          accessibilityLabel="Sign in"
          onPress={onSubmit}
        />
      </View>
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
  buttonWrap: { marginTop: spacing.md },
});
