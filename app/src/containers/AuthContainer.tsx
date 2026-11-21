import React, { useState } from 'react';

import type { DemoConfig } from '../lib/demoConfig';
import type { AuthFormErrors } from '../logic/authValidation';
import { validateAuthForm } from '../logic/authValidation';
import { AuthScreen } from '../screens/AuthScreen';

interface AuthContainerProps {
  signIn: (email: string, password: string) => Promise<string | null>;
  /** The public demo's sign-in, or null to hide the "View demo" entry. */
  demo?: DemoConfig | null;
}

export function AuthContainer({ signIn, demo = null }: AuthContainerProps) {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [errors, setErrors] = useState<AuthFormErrors>({});
  const [authError, setAuthError] = useState<string | null>(null);
  const [demoPending, setDemoPending] = useState(false);

  const onSubmit = async () => {
    const validation = validateAuthForm(email, password);
    setErrors(validation);
    if (validation.email || validation.password) {
      return;
    }
    setAuthError(null);
    setAuthError(await signIn(email, password));
  };

  const onViewDemo = demo
    ? async () => {
        setErrors({});
        setAuthError(null);
        setDemoPending(true);
        const error = await signIn(demo.email, demo.password);
        setDemoPending(false);
        setAuthError(error ? `The demo is unavailable right now (${error}).` : null);
      }
    : undefined;

  return (
    <AuthScreen
      email={email}
      password={password}
      errors={errors}
      authError={authError}
      onEmailChange={setEmail}
      onPasswordChange={setPassword}
      onSubmit={onSubmit}
      onViewDemo={onViewDemo}
      demoPending={demoPending}
    />
  );
}
