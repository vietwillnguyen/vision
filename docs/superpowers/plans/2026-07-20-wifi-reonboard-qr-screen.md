# WiFi + Auth Re-onboarding QR Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Device tab's dead "Re-onboard WiFi" button to a real screen that captures a WiFi SSID/password, packages it with the signed-in user's current Supabase session tokens, and renders it as a QR code in the exact JSON shape firmware's `parse_onboarding_qr_payload` expects.

**Architecture:** A new `DeviceStack` (`@react-navigation/native-stack`) replaces the flat `DeviceContainer` mount inside the existing "Device" bottom tab, adding a `DeviceHome` route (unchanged `DeviceContainer`, now navigation-aware) and a `Reonboard` route (new `ReonboardContainer` -> `ReonboardScreen`). `ReonboardScreen` is a pure, fully-controlled presentational component (`step`/`ssid`/`password`/`qrValue`/`errorMessage` props in, callbacks out) matching the existing `AuthScreen`/`DeviceScreen` pattern. `ReonboardContainer` owns all state, calls `client.auth.getSession()` on submit, and builds the `{ssid, password, user_access_token, user_refresh_token}` JSON payload rendered via `react-native-qrcode-svg`.

**Tech Stack:** React Native (Expo SDK 57), TypeScript (strict), `@react-navigation/native-stack`, `react-native-qrcode-svg` + `react-native-svg`, Jest + `@testing-library/react-native` (jest-expo preset).

## Global Constraints

- Firmware payload contract is fixed and already tested - do not change field names or shapes: `{"ssid": string, "password": string, "user_access_token": string, "user_refresh_token": string}` (`firmware/tests/test_wifi_onboard.py`).
- `react-native-qrcode-svg` (SVG-based) is required over canvas/native-module QR libraries specifically because it survives `npx expo export --platform web --output-dir /tmp/web-export-smoke`, the CI smoke check in `.github/workflows/tests.yml`.
- The new stack navigator is scoped to the Device tab only - do not wrap the whole app or touch the other three tabs' mounting in `App.tsx`.
- Screens stay pure/presentational (props in, callbacks out) - no internal state beyond what's explicitly a local UI concern nowhere in this spec (there is none; `ReonboardScreen` owns none).
- Follow existing file/test layout: components in `app/src/{screens,containers,navigation}/`, tests mirrored under `app/__tests__/{screens,containers}/` (flat, no `navigation` test subfolder needed - see Task 5).
- No em dashes in any new prose (docs or in-app copy) - use a plain hyphen, matching the spec doc's own style.

---

## File Structure

- Create `app/src/screens/ReonboardScreen.tsx` - presentational, 3-step UI (`form` / `ready` / `error`).
- Create `app/__tests__/screens/ReonboardScreen.test.tsx`.
- Create `app/src/containers/ReonboardContainer.tsx` - owns state, calls `client.auth.getSession()`, builds QR payload.
- Create `app/__tests__/containers/ReonboardContainer.test.tsx`.
- Create `app/src/navigation/DeviceStack.tsx` - defines `DeviceStackParamList` and the two-route native stack.
- Modify `app/src/containers/DeviceContainer.tsx` - takes a `navigation` prop, navigates instead of alerting.
- Modify `app/__tests__/containers/DeviceContainer.test.tsx` - assert `navigation.navigate('Reonboard')`.
- Modify `app/App.tsx` - Device tab renders `DeviceStack` instead of `DeviceContainer` directly.
- Modify `app/package.json` / `app/package-lock.json` - new dependencies (via `npx expo install`).
- Modify `docs/superpowers/plans/2026-07-04-visio-app.md`, `docs/superpowers/plans/2026-07-04-visio-epics-overview.md`, `docs/superpowers/specs/2026-07-04-visio-pendant-design.md` - mark the as-built gap resolved.

---

## Task 1: Add dependencies

**Files:**
- Modify: `app/package.json`
- Modify: `app/package-lock.json`

**Interfaces:**
- Produces: `react-native-svg` (peer dep), `react-native-qrcode-svg`'s default export `QRCode` (`{ value: string }` prop, used in Task 2), `@react-navigation/native-stack`'s `createNativeStackNavigator` and `NativeStackScreenProps` type (used in Tasks 3-5).

- [x] **Step 1: Install the Expo-managed peer dependency**

Run from `app/`:
```bash
npx expo install react-native-svg
```
Expected: adds `"react-native-svg"` to `app/package.json` `dependencies` at the version Expo SDK 57 recommends (`~15.15.4` at time of writing - accept whatever `expo install` resolves).

- [x] **Step 2: Install the QR renderer and the new stack navigator**

Run from `app/`:
```bash
npx expo install react-native-qrcode-svg @react-navigation/native-stack
```
Expected: adds both to `app/package.json` `dependencies`. Neither is Expo-managed, so `expo install` falls back to `npm install` for them - that's expected and fine.

- [x] **Step 3: Verify the existing suite still passes with the new deps present**

Run from `app/`:
```bash
npm test -- --watchAll=false
```
Expected: all existing tests still pass (no new tests reference the new packages yet, so this only confirms nothing broke at install time - e.g. no peer-dependency conflict).

- [x] **Step 4: Commit**

```bash
git add app/package.json app/package-lock.json
git commit -m "chore(app): add react-native-svg, react-native-qrcode-svg, native-stack"
```

---

## Task 2: `ReonboardScreen` (presentational)

**Files:**
- Create: `app/src/screens/ReonboardScreen.tsx`
- Test: `app/__tests__/screens/ReonboardScreen.test.tsx`

**Interfaces:**
- Consumes: `colors`, `spacing` from `app/src/theme.ts` (existing).
- Produces: `export type ReonboardStep = 'form' | 'ready' | 'error';` and `export function ReonboardScreen(props: ReonboardScreenProps)` with props `{ step: ReonboardStep; ssid: string; password: string; qrValue: string; errorMessage: string; onSsidChange: (value: string) => void; onPasswordChange: (value: string) => void; onSubmit: (ssid: string, password: string) => void; onDone: () => void; onRetry: () => void }`. Consumed by `ReonboardContainer` in Task 3.

- [x] **Step 1: Write the failing test**

Create `app/__tests__/screens/ReonboardScreen.test.tsx`:

```tsx
import { fireEvent, render, screen } from '@testing-library/react-native';
import React from 'react';

jest.mock('react-native-qrcode-svg', () => (props: { value: string }) => {
  const { Text } = jest.requireActual('react-native');
  return <Text testID="qr-code">{props.value}</Text>;
});

import { ReonboardScreen } from '../../src/screens/ReonboardScreen';

function noop() {}

describe('ReonboardScreen', () => {
  it('renders the form step with SSID and password inputs', () => {
    render(
      <ReonboardScreen
        step="form"
        ssid=""
        password=""
        qrValue=""
        errorMessage=""
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={noop}
        onRetry={noop}
      />,
    );
    expect(screen.getByTestId('ssid-input')).toBeTruthy();
    expect(screen.getByTestId('password-input')).toBeTruthy();
  });

  it('disables Generate QR until both fields are filled', () => {
    render(
      <ReonboardScreen
        step="form"
        ssid=""
        password=""
        qrValue=""
        errorMessage=""
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={noop}
        onRetry={noop}
      />,
    );
    expect(screen.getByTestId('generate-qr-button').props.accessibilityState.disabled).toBe(true);
  });

  it('calls onSubmit with the typed SSID and password', () => {
    const onSubmit = jest.fn();
    render(
      <ReonboardScreen
        step="form"
        ssid="HomeNet"
        password="hunter2"
        qrValue=""
        errorMessage=""
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={onSubmit}
        onDone={noop}
        onRetry={noop}
      />,
    );
    fireEvent.press(screen.getByTestId('generate-qr-button'));
    expect(onSubmit).toHaveBeenCalledWith('HomeNet', 'hunter2');
  });

  it('reports typed input via onSsidChange/onPasswordChange', () => {
    const onSsidChange = jest.fn();
    const onPasswordChange = jest.fn();
    render(
      <ReonboardScreen
        step="form"
        ssid=""
        password=""
        qrValue=""
        errorMessage=""
        onSsidChange={onSsidChange}
        onPasswordChange={onPasswordChange}
        onSubmit={noop}
        onDone={noop}
        onRetry={noop}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    expect(onSsidChange).toHaveBeenCalledWith('HomeNet');
    expect(onPasswordChange).toHaveBeenCalledWith('hunter2');
  });

  it('renders the QR code with exactly qrValue on the ready step, plus the privacy hint', () => {
    render(
      <ReonboardScreen
        step="ready"
        ssid="HomeNet"
        password="hunter2"
        qrValue='{"ssid":"HomeNet"}'
        errorMessage=""
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={noop}
        onRetry={noop}
      />,
    );
    expect(screen.getByTestId('qr-code')).toHaveTextContent('{"ssid":"HomeNet"}');
    expect(
      screen.getByText(
        "This code contains your WiFi password and an active login - only show it to your Visio device's camera",
      ),
    ).toBeTruthy();
  });

  it('calls onDone when Done is pressed on the ready step', () => {
    const onDone = jest.fn();
    render(
      <ReonboardScreen
        step="ready"
        ssid="HomeNet"
        password="hunter2"
        qrValue='{"ssid":"HomeNet"}'
        errorMessage=""
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={onDone}
        onRetry={noop}
      />,
    );
    fireEvent.press(screen.getByTestId('done-button'));
    expect(onDone).toHaveBeenCalled();
  });

  it('shows the error message and calls onRetry on the error step', () => {
    const onRetry = jest.fn();
    render(
      <ReonboardScreen
        step="error"
        ssid="HomeNet"
        password="hunter2"
        qrValue=""
        errorMessage="No active session - please sign in again."
        onSsidChange={noop}
        onPasswordChange={noop}
        onSubmit={noop}
        onDone={noop}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByTestId('reonboard-error')).toHaveTextContent(
      'No active session - please sign in again.',
    );
    fireEvent.press(screen.getByTestId('try-again-button'));
    expect(onRetry).toHaveBeenCalled();
  });
});
```

- [x] **Step 2: Run the test to verify it fails**

Run from `app/`:
```bash
npm test -- __tests__/screens/ReonboardScreen.test.tsx --watchAll=false
```
Expected: FAIL - `Cannot find module '../../src/screens/ReonboardScreen'`.

- [x] **Step 3: Write the implementation**

Create `app/src/screens/ReonboardScreen.tsx`:

```tsx
import React from 'react';
import { Button, StyleSheet, Text, TextInput, View } from 'react-native';
import QRCode from 'react-native-qrcode-svg';

import { colors, spacing } from '../theme';

export type ReonboardStep = 'form' | 'ready' | 'error';

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
          <QRCode value={qrValue} />
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
```

- [x] **Step 4: Run the test to verify it passes**

Run from `app/`:
```bash
npm test -- __tests__/screens/ReonboardScreen.test.tsx --watchAll=false
```
Expected: PASS (7 tests).

- [x] **Step 5: Commit**

```bash
git add app/src/screens/ReonboardScreen.tsx app/__tests__/screens/ReonboardScreen.test.tsx
git commit -m "feat(app): add ReonboardScreen presentational component"
```

---

## Task 3: `ReonboardContainer`

**Files:**
- Create: `app/src/containers/ReonboardContainer.tsx`
- Test: `app/__tests__/containers/ReonboardContainer.test.tsx`

**Interfaces:**
- Consumes: `ReonboardScreen`, `ReonboardStep` from Task 2. `NativeStackScreenProps<DeviceStackParamList, 'Reonboard'>` type from `../navigation/DeviceStack` (Task 5 - forward reference via `import type`, safe: Babel's TypeScript transform strips `import type` entirely, so Jest never tries to `require` the not-yet-created file).
- Produces: `export function ReonboardContainer(props: ReonboardContainerProps)` where `ReonboardContainerProps = NativeStackScreenProps<DeviceStackParamList, 'Reonboard'> & { client: SupabaseClient }`. Consumed by `DeviceStack` in Task 5.

- [x] **Step 1: Write the failing test**

Create `app/__tests__/containers/ReonboardContainer.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { Session, SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

jest.mock('react-native-qrcode-svg', () => (props: { value: string }) => {
  const { Text } = jest.requireActual('react-native');
  return <Text testID="qr-code">{props.value}</Text>;
});

import { ReonboardContainer } from '../../src/containers/ReonboardContainer';

const SESSION = {
  access_token: 'access-abc',
  refresh_token: 'refresh-xyz',
} as unknown as Session;

function fakeClient(session: Session | null, error: { message: string } | null = null): SupabaseClient {
  return {
    auth: {
      getSession: () => Promise.resolve({ data: { session }, error }),
    },
  } as unknown as SupabaseClient;
}

function fakeNavigation() {
  return { navigate: jest.fn(), goBack: jest.fn() } as unknown as Parameters<
    typeof ReonboardContainer
  >[0]['navigation'];
}

describe('ReonboardContainer', () => {
  it('builds a QR payload with exactly the fields firmware expects', async () => {
    render(
      <ReonboardContainer
        client={fakeClient(SESSION)}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));

    await waitFor(() => expect(screen.getByTestId('qr-code')).toBeTruthy());
    const payload = JSON.parse(screen.getByTestId('qr-code').props.children);
    expect(payload).toEqual({
      ssid: 'HomeNet',
      password: 'hunter2',
      user_access_token: 'access-abc',
      user_refresh_token: 'refresh-xyz',
    });
  });

  it('shows the error step when getSession returns no session', async () => {
    render(
      <ReonboardContainer
        client={fakeClient(null)}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));

    await waitFor(() => expect(screen.getByTestId('reonboard-error')).toBeTruthy());
  });

  it('shows the error step when getSession errors', async () => {
    render(
      <ReonboardContainer
        client={fakeClient(null, { message: 'network down' })}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));

    await waitFor(() =>
      expect(screen.getByTestId('reonboard-error')).toHaveTextContent('network down'),
    );
  });

  it('calls navigation.goBack() from onDone', async () => {
    const navigation = fakeNavigation();
    render(
      <ReonboardContainer
        client={fakeClient(SESSION)}
        navigation={navigation}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));
    await waitFor(() => expect(screen.getByTestId('done-button')).toBeTruthy());

    fireEvent.press(screen.getByTestId('done-button'));
    expect(navigation.goBack).toHaveBeenCalled();
  });

  it('keeps the typed SSID/password after Try again', async () => {
    render(
      <ReonboardContainer
        client={fakeClient(null)}
        navigation={fakeNavigation()}
        route={{ key: 'Reonboard', name: 'Reonboard' }}
      />,
    );
    fireEvent.changeText(screen.getByTestId('ssid-input'), 'HomeNet');
    fireEvent.changeText(screen.getByTestId('password-input'), 'hunter2');
    fireEvent.press(screen.getByTestId('generate-qr-button'));
    await waitFor(() => expect(screen.getByTestId('try-again-button')).toBeTruthy());

    fireEvent.press(screen.getByTestId('try-again-button'));
    expect(screen.getByTestId('ssid-input').props.value).toBe('HomeNet');
    expect(screen.getByTestId('password-input').props.value).toBe('hunter2');
  });
});
```

- [x] **Step 2: Run the test to verify it fails**

Run from `app/`:
```bash
npm test -- __tests__/containers/ReonboardContainer.test.tsx --watchAll=false
```
Expected: FAIL - `Cannot find module '../../src/containers/ReonboardContainer'`.

- [x] **Step 3: Write the implementation**

Create `app/src/containers/ReonboardContainer.tsx`:

```tsx
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
```

- [x] **Step 4: Run the test to verify it passes**

Run from `app/`:
```bash
npm test -- __tests__/containers/ReonboardContainer.test.tsx --watchAll=false
```
Expected: PASS (5 tests). Note: this only exercises `ReonboardContainer`/`ReonboardScreen` in isolation - `DeviceStackParamList` is a type-only import that Babel strips, so the test runs fine even though `app/src/navigation/DeviceStack.tsx` doesn't exist until Task 5. Full type-checking of this forward reference happens in Task 6's `tsc --noEmit`.

- [x] **Step 5: Commit**

```bash
git add app/src/containers/ReonboardContainer.tsx app/__tests__/containers/ReonboardContainer.test.tsx
git commit -m "feat(app): add ReonboardContainer wiring getSession to QR payload"
```

---

## Task 4: Update `DeviceContainer` to navigate instead of alerting

**Files:**
- Modify: `app/src/containers/DeviceContainer.tsx`
- Modify: `app/__tests__/containers/DeviceContainer.test.tsx`

**Interfaces:**
- Consumes: `NativeStackScreenProps<DeviceStackParamList, 'DeviceHome'>` type from `../navigation/DeviceStack` (Task 5 - forward reference, same `import type` erasure reasoning as Task 3).
- Produces: `DeviceContainer` now requires a `navigation` prop. Consumed by `DeviceStack` in Task 5.

- [x] **Step 1: Update the failing test**

Replace `app/__tests__/containers/DeviceContainer.test.tsx` in full:

```tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react-native';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

import { DeviceContainer } from '../../src/containers/DeviceContainer';

function fakeClient(row: Record<string, unknown>): SupabaseClient {
  return {
    from: () => ({
      select: () => ({ eq: () => ({ single: () => Promise.resolve({ data: row, error: null }) }) }),
    }),
    channel: () => ({ on: () => ({ subscribe: () => ({}) }) }),
    removeChannel: () => {},
  } as unknown as SupabaseClient;
}

function fakeNavigation() {
  return { navigate: jest.fn(), goBack: jest.fn() } as unknown as Parameters<
    typeof DeviceContainer
  >[0]['navigation'];
}

it('wires useDeviceStatus into DeviceScreen', async () => {
  render(
    <DeviceContainer
      client={fakeClient({
        battery_pct: 55,
        storage_used_gb: 1,
        storage_free_gb: 10,
        segments_pending: 0,
        segments_uploaded_today: 3,
        recording_active: false,
      })}
      deviceId="dev-1"
      navigation={fakeNavigation()}
      route={{ key: 'DeviceHome', name: 'DeviceHome' }}
    />,
  );
  await waitFor(() => expect(screen.getByText('Battery: 55%')).toBeTruthy());
  expect(screen.getByText('Paused')).toBeTruthy();
});

it('navigates to Reonboard when the re-onboard button is pressed', async () => {
  const navigation = fakeNavigation();
  render(
    <DeviceContainer
      client={fakeClient({
        battery_pct: 55,
        storage_used_gb: 1,
        storage_free_gb: 10,
        segments_pending: 0,
        segments_uploaded_today: 3,
        recording_active: false,
      })}
      deviceId="dev-1"
      navigation={navigation}
      route={{ key: 'DeviceHome', name: 'DeviceHome' }}
    />,
  );
  await waitFor(() => expect(screen.getByTestId('reonboard-button')).toBeTruthy());
  fireEvent.press(screen.getByTestId('reonboard-button'));
  expect(navigation.navigate).toHaveBeenCalledWith('Reonboard');
});
```

- [x] **Step 2: Run the test to verify it fails**

Run from `app/`:
```bash
npm test -- __tests__/containers/DeviceContainer.test.tsx --watchAll=false
```
Expected: FAIL - TypeScript/prop-shape error or `navigation.navigate` not called, since `DeviceContainer` doesn't accept/use a `navigation` prop yet.

- [x] **Step 3: Update the implementation**

Replace `app/src/containers/DeviceContainer.tsx` in full:

```tsx
import type { NativeStackScreenProps } from '@react-navigation/native-stack';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

import type { DeviceStackParamList } from '../navigation/DeviceStack';
import { useDeviceStatus } from '../hooks/useDeviceStatus';
import { DeviceScreen } from '../screens/DeviceScreen';

type DeviceContainerProps = NativeStackScreenProps<DeviceStackParamList, 'DeviceHome'> & {
  client: SupabaseClient;
  deviceId: string;
};

export function DeviceContainer({ client, deviceId, navigation }: DeviceContainerProps) {
  const state = useDeviceStatus(client, deviceId);
  return (
    <DeviceScreen state={state} onReonboardPress={() => navigation.navigate('Reonboard')} />
  );
}
```

- [x] **Step 4: Run the test to verify it passes**

Run from `app/`:
```bash
npm test -- __tests__/containers/DeviceContainer.test.tsx --watchAll=false
```
Expected: PASS (2 tests).

- [x] **Step 5: Commit**

```bash
git add app/src/containers/DeviceContainer.tsx app/__tests__/containers/DeviceContainer.test.tsx
git commit -m "feat(app): navigate to Reonboard from DeviceContainer instead of alerting"
```

---

## Task 5: `DeviceStack` navigator

**Files:**
- Create: `app/src/navigation/DeviceStack.tsx`

**Interfaces:**
- Consumes: `DeviceContainer` (Task 4), `ReonboardContainer` (Task 3) as real value imports (both already exist on disk by this task, resolving the forward references from Tasks 3-4).
- Produces: `export type DeviceStackParamList = { DeviceHome: undefined; Reonboard: undefined }` and `export function DeviceStack(props: { client: SupabaseClient; deviceId: string })`. Consumed by `App.tsx` in Task 6.

There is no dedicated `DeviceStack.test.tsx` per the spec's Testing section (only `ReonboardScreen`, `ReonboardContainer`, and `DeviceContainer` are listed) - this navigator gets exercised end-to-end by the existing `App.test.tsx` suite once wired in Task 6, and by `npx tsc --noEmit` for type correctness of the forward references from Tasks 3-4.

- [x] **Step 1: Write the implementation**

Create `app/src/navigation/DeviceStack.tsx`:

```tsx
import { createNativeStackNavigator } from '@react-navigation/native-stack';
import type { SupabaseClient } from '@supabase/supabase-js';
import React from 'react';

import { DeviceContainer } from '../containers/DeviceContainer';
import { ReonboardContainer } from '../containers/ReonboardContainer';

export type DeviceStackParamList = {
  DeviceHome: undefined;
  Reonboard: undefined;
};

const Stack = createNativeStackNavigator<DeviceStackParamList>();

interface DeviceStackProps {
  client: SupabaseClient;
  deviceId: string;
}

export function DeviceStack({ client, deviceId }: DeviceStackProps) {
  return (
    <Stack.Navigator screenOptions={{ headerShown: false }}>
      <Stack.Screen name="DeviceHome">
        {(props) => <DeviceContainer {...props} client={client} deviceId={deviceId} />}
      </Stack.Screen>
      <Stack.Screen name="Reonboard">
        {(props) => <ReonboardContainer {...props} client={client} />}
      </Stack.Screen>
    </Stack.Navigator>
  );
}
```

- [x] **Step 2: Type-check this file in isolation**

Run from `app/`:
```bash
npx tsc --noEmit
```
Expected: this will still report errors from `App.tsx` (not updated until Task 6) if any, but must report zero errors originating from `app/src/navigation/DeviceStack.tsx`, `app/src/containers/DeviceContainer.tsx`, or `app/src/containers/ReonboardContainer.tsx`. If it reports an error on one of those three files, fix it before proceeding - this is the check that the Task 3/4 forward references now resolve correctly.

- [x] **Step 3: Commit**

```bash
git add app/src/navigation/DeviceStack.tsx
git commit -m "feat(app): add DeviceStack navigator for Device tab"
```

---

## Task 6: Wire `DeviceStack` into `App.tsx`

**Files:**
- Modify: `app/App.tsx:10` (import), `app/App.tsx:90-92` (Device tab screen)

**Interfaces:**
- Consumes: `DeviceStack` from Task 5.

- [x] **Step 1: Update the import**

In `app/App.tsx`, replace line 10:

```tsx
import { DeviceContainer } from './src/containers/DeviceContainer';
```

with:

```tsx
import { DeviceStack } from './src/navigation/DeviceStack';
```

- [x] **Step 2: Update the Device tab screen**

In `app/App.tsx`, replace lines 90-92:

```tsx
        <Tab.Screen name="Device">
          {() => <DeviceContainer client={client} deviceId={deviceId} />}
        </Tab.Screen>
```

with:

```tsx
        <Tab.Screen name="Device">
          {() => <DeviceStack client={client} deviceId={deviceId} />}
        </Tab.Screen>
```

- [x] **Step 3: Run the full app test suite**

Run from `app/`:
```bash
npm test -- --watchAll=false
```
Expected: PASS, all suites (including `__tests__/App.test.tsx`, which renders the Device tab through `AppRoot` and asserts on tab labels - this confirms `DeviceStack` mounts correctly inside `NavigationContainer`/`Tab.Navigator` without a nested `NavigationContainer` conflict).

- [x] **Step 4: Type-check the whole project**

Run from `app/`:
```bash
npx tsc --noEmit
```
Expected: zero errors.

- [x] **Step 5: Commit**

```bash
git add app/App.tsx
git commit -m "feat(app): mount DeviceStack in the Device tab"
```

---

## Task 7: Update as-built docs

**Files:**
- Modify: `docs/superpowers/plans/2026-07-04-visio-app.md:1338`
- Modify: `docs/superpowers/plans/2026-07-04-visio-epics-overview.md:99`
- Modify: `docs/superpowers/specs/2026-07-04-visio-pendant-design.md:304`

- [x] **Step 1: Update `2026-07-04-visio-app.md`'s Handoff section**

In `docs/superpowers/plans/2026-07-04-visio-app.md`, replace the bullet at line 1338:

```
- **The WiFi + auth re-onboarding QR display screen - still not built, deliberately out of scope for issue #8.** It must still encode the shape the firmware's `parse_onboarding_qr_payload` from [`2026-07-04-visio-firmware.md`](2026-07-04-visio-firmware.md) Task 8 expects: `{"ssid": "...", "password": "...", "user_access_token": "...", "user_refresh_token": "..."}` - the access/refresh tokens come from the signed-in user's current Supabase session (`supabase.auth.getSession()`), not just the WiFi credentials, since the device needs an authenticated session to pass Supabase RLS. The Device screen's "Re-onboard WiFi" button currently shows a "not available yet" alert instead.
```

with:

```
- **The WiFi + auth re-onboarding QR display screen - resolved** (see [`2026-07-20-wifi-reonboard-qr-screen-design.md`](../specs/2026-07-20-wifi-reonboard-qr-screen-design.md)). `DeviceStack` adds a `Reonboard` route; `ReonboardContainer` calls `supabase.auth.getSession()` and encodes `{"ssid": "...", "password": "...", "user_access_token": "...", "user_refresh_token": "..."}` - the exact shape firmware's `parse_onboarding_qr_payload` from [`2026-07-04-visio-firmware.md`](2026-07-04-visio-firmware.md) Task 8 expects - as a `react-native-qrcode-svg` QR code. The Device screen's "Re-onboard WiFi" button now navigates there instead of showing a "not available yet" alert.
```

- [x] **Step 2: Update `2026-07-04-visio-epics-overview.md`'s follow-up-issues list**

In `docs/superpowers/plans/2026-07-04-visio-epics-overview.md`, replace the `#8` bullet at line 99:

```
- [#8 App: Epic 5 screen wiring](https://github.com/vietwillnguyen/vision/issues/8) - landed as the bottom tab navigator, real Supabase Auth, `expo-video` playback, timeline thumbnails, styling/accessibility, and realtime channel-error handling via [PR #22](https://github.com/vietwillnguyen/vision/pull/22). The WiFi + auth re-onboarding QR screen was deliberately left out of scope (see [`2026-07-04-visio-app.md`](2026-07-04-visio-app.md)'s Handoff) - the checklist's "Onboard the device to WiFi via the Epic 3 app's QR flow" step below has no screen to run yet and needs a follow-up issue before Epic 5 can complete end-to-end.
```

with:

```
- [#8 App: Epic 5 screen wiring](https://github.com/vietwillnguyen/vision/issues/8) - landed as the bottom tab navigator, real Supabase Auth, `expo-video` playback, timeline thumbnails, styling/accessibility, and realtime channel-error handling via [PR #22](https://github.com/vietwillnguyen/vision/pull/22). The WiFi + auth re-onboarding QR screen was deliberately left out of scope at the time (see [`2026-07-04-visio-app.md`](2026-07-04-visio-app.md)'s Handoff) but has since landed per [`2026-07-20-wifi-reonboard-qr-screen-design.md`](2026-07-20-wifi-reonboard-qr-screen-design.md) - the checklist's "Onboard the device to WiFi via the Epic 3 app's QR flow" step below now has a screen to run.
```

- [x] **Step 3: Update `2026-07-04-visio-pendant-design.md`'s As-built gap line**

In `docs/superpowers/specs/2026-07-04-visio-pendant-design.md`, replace line 304:

```
**As-built gap:** WiFi re-onboarding is not wired in Epic 5 - the Re-onboard button shows a "not available yet" alert instead of the QR display screen, since it has no backend consumer yet either (separate issue, out of scope for #8).
```

with:

```
**As-built gap:** resolved - see [`2026-07-20-wifi-reonboard-qr-screen-design.md`](2026-07-20-wifi-reonboard-qr-screen-design.md). The Re-onboard button navigates to a QR display screen that encodes the WiFi credentials plus the signed-in user's current Supabase session tokens.
```

- [x] **Step 4: Commit**

```bash
git add docs/superpowers/plans/2026-07-04-visio-app.md docs/superpowers/plans/2026-07-04-visio-epics-overview.md docs/superpowers/specs/2026-07-04-visio-pendant-design.md
git commit -m "docs: mark WiFi re-onboarding QR screen gap resolved"
```

---

## Task 8: Final verification

**Files:** none (verification only).

- [x] **Step 1: Full test suite**

Run from `app/`:
```bash
npm test -- --watchAll=false
```
Expected: PASS, all suites, no `console.error`/`console.warn` noise from the new files.

- [x] **Step 2: Full type-check**

Run from `app/`:
```bash
npx tsc --noEmit
```
Expected: zero errors.

- [x] **Step 3: Web export smoke check (the reason `react-native-qrcode-svg` was chosen over canvas/native-module alternatives)**

Run from `app/`:
```bash
npx expo export --platform web --output-dir /tmp/web-export-smoke
```
Expected: exits 0, no bundling error referencing `react-native-qrcode-svg` or `react-native-svg`.

- [ ] **Step 4: Manual walkthrough (documented, not automated - matches the spec's non-goal of not verifying real device reconnection)**

Run from `app/`:
```bash
npx expo start --web
```
In the browser: sign in, open the Device tab, tap "Re-onboard WiFi", confirm the form renders, type an SSID and password, tap "Generate QR", confirm a QR code renders with the privacy hint text, tap "Done", confirm it returns to the Device screen.

- [x] **Step 5: Confirm branch state**

```bash
git log --oneline main..HEAD
git status
```
Expected: a clean sequence of commits from Task 1-7 on top of `main`, no uncommitted changes.

- [x] **Step 6: Hand off to `/no-mistakes`**

Once Steps 1-5 all pass, run the `/no-mistakes` pipeline (code review, tests, lint, docs, push, PR, CI) per the spec's Delivery section, targeting `main` (the branch this work is built on, since the spec's originally-named target branch `gnhf/continue-your-worktr-bd0870` has since merged into `main` as PR #24).
