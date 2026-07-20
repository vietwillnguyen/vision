import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import type { SupabaseClient } from '@supabase/supabase-js';
import React, { useState } from 'react';

import type { DeviceStackParamList } from '../navigation/DeviceStack';
import type { ReonboardStep } from '../screens/ReonboardScreen';
import { ReonboardScreen } from '../screens/ReonboardScreen';

type ReonboardContainerProps = NativeStackScreenProps<DeviceStackParamList, 'Reonboard'> & {
  client: SupabaseClient;
};

export function ReonboardContainer({ client, navigation }: ReonboardContainerProps) {
  const [step, setStep] = useState<ReonboardStep>('form');
  const [ssid, setSsid] = useState('');
  const [password, setPassword] = useState('');
  const [qrValue, setQrValue] = useState('');
  const [errorMessage, setErrorMessage] = useState('');

  const onSubmit = async (submittedSsid: string, submittedPassword: string) => {
    setSsid(submittedSsid);
    setPassword(submittedPassword);

    try {
      const { data, error } = await client.auth.getSession();
      if (error || !data.session) {
        setErrorMessage(error ? error.message : 'No active session - please sign in again.');
        setStep('error');
        return;
      }

      setQrValue(
        JSON.stringify({
          ssid: submittedSsid,
          password: submittedPassword,
          user_access_token: data.session.access_token,
          user_refresh_token: data.session.refresh_token,
        }),
      );
      setStep('ready');
    } catch (caughtError) {
      setErrorMessage(caughtError instanceof Error ? caughtError.message : String(caughtError));
      setStep('error');
    }
  };

  return (
    <ReonboardScreen
      step={step}
      ssid={ssid}
      password={password}
      qrValue={qrValue}
      errorMessage={errorMessage}
      onSsidChange={setSsid}
      onPasswordChange={setPassword}
      onSubmit={onSubmit}
      onDone={() => navigation.goBack()}
      onRetry={() => setStep('form')}
    />
  );
}
