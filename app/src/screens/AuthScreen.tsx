import React from 'react';
import { Button, Text, TextInput, View } from 'react-native';

import type { AuthFormErrors } from '../logic/authValidation';

interface AuthScreenProps {
  email: string;
  password: string;
  errors: AuthFormErrors;
  onEmailChange: (value: string) => void;
  onPasswordChange: (value: string) => void;
  onSubmit: () => void;
}

export function AuthScreen({
  email,
  password,
  errors,
  onEmailChange,
  onPasswordChange,
  onSubmit,
}: AuthScreenProps) {
  return (
    <View>
      <TextInput
        testID="email-input"
        placeholder="Email"
        value={email}
        onChangeText={onEmailChange}
        autoCapitalize="none"
      />
      {errors.email ? <Text testID="email-error">{errors.email}</Text> : null}
      <TextInput
        testID="password-input"
        placeholder="Password"
        value={password}
        onChangeText={onPasswordChange}
        secureTextEntry
      />
      {errors.password ? <Text testID="password-error">{errors.password}</Text> : null}
      <Button testID="submit-button" title="Sign In" onPress={onSubmit} />
    </View>
  );
}
