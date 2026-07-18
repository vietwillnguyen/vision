import React, { useState } from 'react';

import type { AuthFormErrors } from '../logic/authValidation';
import { validateAuthForm } from '../logic/authValidation';
import { AuthScreen } from '../screens/AuthScreen';

interface AuthContainerProps {
  signIn: (email: string, password: string) => Promise<string | null>;
}

export function AuthContainer({ signIn }: AuthContainerProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [errors, setErrors] = useState<AuthFormErrors>({});
  const [authError, setAuthError] = useState<string | null>(null);

  const onSubmit = async () => {
    const validation = validateAuthForm(email, password);
    setErrors(validation);
    if (validation.email || validation.password) {
      return;
    }
    setAuthError(null);
    setAuthError(await signIn(email, password));
  };

  return (
    <AuthScreen
      email={email}
      password={password}
      errors={errors}
      authError={authError}
      onEmailChange={setEmail}
      onPasswordChange={setPassword}
      onSubmit={onSubmit}
    />
  );
}
