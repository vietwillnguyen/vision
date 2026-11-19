import React, { useCallback, useEffect, useRef, useState } from 'react';

import type { VisionClient } from '../lib/supabase';
import { buildPairingQrPayload } from '../logic/pairingQr';
import type { ReonboardStep } from '../screens/ReonboardScreen';
import { ReonboardScreen } from '../screens/ReonboardScreen';

// How often the QR screen checks whether the pendant has claimed the code.
export const PAIRING_POLL_MS = 3000;

interface ReonboardContainerProps {
  client: VisionClient;
  // The device to pair a pendant to, or null to have the pendant's claim
  // create the account's first device.
  deviceId: string | null;
  onDone: () => void;
}

interface PendingPairing {
  // When the QR stops being useful, on this phone's clock: the moment the
  // code arrived plus the lifetime the server gave it. Comparing the
  // server's expires_at with the phone's clock instead is off by however
  // far that clock is, which would retire a live code early or keep showing
  // a dead one.
  expiresAt: number;
  // The pairing link before the code was made: the device's pendant login,
  // or (with no device yet) whether any device exists. A change means the
  // pendant claimed the code.
  baseline: string | null;
}

type LinkRead = { ok: true; link: string | null } | { ok: false; message: string };

async function readLink(client: VisionClient, deviceId: string | null): Promise<LinkRead> {
  if (deviceId === null) {
    const { data, error } = await client.from('devices').select('device_id').limit(1);
    if (error) return { ok: false, message: error.message };
    return { ok: true, link: data && data.length > 0 ? data[0].device_id : null };
  }
  const { data, error } = await client
    .from('devices')
    .select('device_user_id')
    .eq('device_id', deviceId)
    .limit(1);
  if (error) return { ok: false, message: error.message };
  return { ok: true, link: data && data.length > 0 ? data[0].device_user_id : null };
}

function messageOf(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}

export function ReonboardContainer({ client, deviceId, onDone }: ReonboardContainerProps) {
  const [step, setStep] = useState<ReonboardStep>('form');
  const [ssid, setSsid] = useState('');
  const [password, setPassword] = useState('');
  const [qrValue, setQrValue] = useState('');
  const [errorMessage, setErrorMessage] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [pending, setPending] = useState<PendingPairing | null>(null);
  // A ref, not the isSubmitting state: a second press can land before the
  // re-render that disables the button, and would see a stale false.
  const inFlight = useRef(false);

  // The QR holds the Wi-Fi password and a live pairing code, so neither is
  // kept a moment longer than the QR is on screen. Clearing state is all
  // there is to do on close: the database has no call that retires a code
  // without unpairing, a new code replaces it, and it expires on its own.
  const forgetSecrets = useCallback(() => {
    setPassword('');
    setQrValue('');
    setPending(null);
  }, []);

  const finish = useCallback(
    (next: ReonboardStep) => {
      forgetSecrets();
      setStep(next);
    },
    [forgetSecrets],
  );

  const fail = (message: string) => {
    setErrorMessage(message);
    finish('error');
  };

  const close = () => {
    forgetSecrets();
    onDone();
  };

  const onSubmit = async (submittedSsid: string, submittedPassword: string) => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    setSsid(submittedSsid);
    setPassword(submittedPassword);
    setIsSubmitting(true);

    try {
      const before = await readLink(client, deviceId);
      if (!before.ok) {
        fail(before.message);
        return;
      }
      const { data, error } = await client.rpc(
        'create_pairing_code',
        deviceId === null ? {} : { p_device_id: deviceId },
      );
      if (error || !data || data.length === 0) {
        fail(error ? error.message : 'Could not create a pairing code.');
        return;
      }
      setQrValue(buildPairingQrPayload(submittedSsid, submittedPassword, data[0].code));
      setPending({ expiresAt: Date.now() + data[0].expires_in * 1000, baseline: before.link });
      setStep('ready');
    } catch (caught) {
      fail(messageOf(caught));
    } finally {
      inFlight.current = false;
      setIsSubmitting(false);
    }
  };

  // While the QR is shown: watch for the pendant's claim, and retire the
  // code when it expires.
  useEffect(() => {
    if (step !== 'ready' || pending === null) {
      return;
    }
    let active = true;
    let polling = false;
    const poll = setInterval(() => {
      if (polling) return;
      polling = true;
      readLink(client, deviceId)
        .then((read) => {
          // A failed read is retried on the next tick; the code is still live.
          if (active && read.ok && read.link !== pending.baseline) finish('paired');
        })
        .catch(() => {})
        .finally(() => {
          polling = false;
        });
    }, PAIRING_POLL_MS);
    const expiry = setTimeout(
      () => {
        if (active) finish('expired');
      },
      Math.max(0, pending.expiresAt - Date.now()),
    );
    return () => {
      active = false;
      clearInterval(poll);
      clearTimeout(expiry);
    };
  }, [step, pending, client, deviceId, finish]);

  return (
    <ReonboardScreen
      step={step}
      ssid={ssid}
      password={password}
      qrValue={qrValue}
      errorMessage={errorMessage}
      isSubmitting={isSubmitting}
      onSsidChange={setSsid}
      onPasswordChange={setPassword}
      onSubmit={onSubmit}
      onDone={close}
      onRetry={() => setStep('form')}
      onNewCode={() => setStep('form')}
    />
  );
}
