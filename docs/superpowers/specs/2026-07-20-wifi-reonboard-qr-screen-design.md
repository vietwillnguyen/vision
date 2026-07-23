# WiFi + Auth Re-onboarding QR Screen

**Date:** 2026-07-20
**Status:** Approved, ready for implementation planning.

## Background

The Visio app's Device tab has always had a "Re-onboard WiFi" button (see
`docs/superpowers/plans/2026-07-04-visio-app.md`), but it was deliberately
left out of scope for issue #8 and currently shows a
"Not available yet" alert (`app/src/containers/DeviceContainer.tsx`).

This is the same QR mechanism used for the device's first-boot onboarding
(`docs/superpowers/specs/2026-07-04-visio-pendant-design.md`, "WiFi
Onboarding" section): the device's camera scans a QR code displayed by the
app and decodes WiFi credentials plus an authenticated Supabase session.
Firmware's `parse_onboarding_qr_payload`
(`firmware/tests/test_wifi_onboard.py`) already defines the exact contract:

```json
{
  "ssid": "string",
  "password": "string",
  "user_access_token": "string",
  "user_refresh_token": "string"
}
```

The access/refresh tokens must come from the signed-in user's current
Supabase session (`client.auth.getSession()`), not just WiFi credentials,
since the device needs an authenticated session to pass Supabase RLS after
it reconnects.

This spec closes that gap: build the screen, wire the existing button to it.
Docs already flag this as blocking Epic 5's integration checklist step
"Onboard the device to WiFi via the Epic 3 app's QR flow."

## Goal

A user on the Device tab can tap "Re-onboard WiFi", enter a WiFi network's
SSID and password, and see a QR code encoding that plus their current
session tokens in the exact shape firmware expects - ready for the device's
camera to scan.

## Non-goals

- Detecting or confirming the device actually reconnected after scanning
  (manual dismiss is sufficient for v1).
- Reading the phone's currently-connected SSID automatically (not reliably
  available across iOS/Android without extra permissions; manual entry only).
- Any change to firmware's `parse_onboarding_qr_payload` or the onboarding
  contract itself - this consumes an existing, already-tested contract.

## Dependencies

- `react-native-qrcode-svg` - QR rendering, SVG-based (works with the
  `expo export --platform web` smoke check already in CI, unlike canvas- or
  native-module-based alternatives).
- `react-native-svg` - required peer dependency of the above.
- `@react-navigation/native-stack` - the app currently has no stack
  navigator (`App.tsx`'s `Tab.Navigator` renders each tab as a single flat
  container); this introduces one, scoped to the Device tab only.

## Navigation

`App.tsx`'s Device tab currently renders `DeviceContainer` directly:

```tsx
<Tab.Screen name="Device">{() => <DeviceContainer client={client} deviceId={deviceId} />}</Tab.Screen>
```

This becomes a small stack with two routes, still mounted as the single
"Device" tab:

```tsx
<Tab.Screen name="Device">{() => <DeviceStack client={client} deviceId={deviceId} />}</Tab.Screen>
```

`DeviceStack` (new, `app/src/navigation/DeviceStack.tsx`) defines:

- `DeviceHome` - renders `DeviceContainer`, which now receives `navigation`
  and calls `navigation.navigate('Reonboard')` from `onReonboardPress`
  instead of showing the stub `Alert`.
- `Reonboard` - renders `ReonboardContainer`, which calls
  `navigation.goBack()` when the user taps "Done".

Scoping the stack to just this one tab (rather than wrapping the whole app)
keeps the other three tabs, and `App.tsx`'s top-level auth/device-loading
states, untouched.

## Components

### `ReonboardScreen` (presentational, `app/src/screens/ReonboardScreen.tsx`)

Two internal steps, driven by a `step` prop from the container (`'form' |
'ready' | 'error'`) rather than owning its own state - keeping it a pure
presentational component consistent with the rest of the app
(`DeviceScreen`, `AuthScreen`, etc. all follow this pattern).

- **`form`**: SSID `TextInput`, password `TextInput` with
  `secureTextEntry`, a "Generate QR" button that calls
  `onSubmit(ssid, password)`. Disabled while empty (as built: also disabled,
  with a "Generating..." label, while the container's `getSession()` call is
  in flight, so a slow network can't be raced into a duplicate submit).
- **`ready`**: renders `<QRCode value={qrValue} />` (as built: with an
  explicit `size={260}` and `ecl="L"` - the payload embeds two ~700-1000
  char Supabase JWTs, which pushes the QR to a high version that the
  library's 100px default renders too small/dense to scan reliably), a
  short privacy hint ("This code contains your WiFi password and an active
  login - only show it to your Visio device's camera"), and a "Done" button
  that calls `onDone()`.
- **`error`**: the error message plus a "Try again" button that calls
  `onRetry()`, returning to the `form` step.

### `ReonboardContainer` (`app/src/containers/ReonboardContainer.tsx`)

- Owns `step` state as above, plus the last-entered `ssid`/`password` (so
  "Try again" doesn't clear the form) and the built `qrValue` once ready.
- `onSubmit(ssid, password)`: calls `client.auth.getSession()`. If it
  errors or returns no session, transitions to `error` (a signed-in user
  reaching this screen should always have a session, but network/expiry
  edge cases are handled explicitly rather than crashing). On success,
  builds the JSON payload with the exact four fields firmware expects and
  transitions to `ready`. As built: the `getSession()` call itself is
  wrapped in `try`/`catch`, since a rejected promise (as opposed to a
  resolved `{ error }`) would otherwise leave the screen silently stuck
  with no feedback - both paths transition to `error`.
- `onDone`: calls `navigation.goBack()`.

### `DeviceContainer` changes

`onReonboardPress` becomes `() => navigation.navigate('Reonboard')`. The
component now takes a `navigation` prop (typed via
`NativeStackScreenProps<DeviceStackParamList, 'DeviceHome'>` from the new
stack's param list).

## Error handling

The only failure mode with real user impact is `client.auth.getSession()`
failing or returning `session: null` after the form is submitted - handled
via the `error` step above rather than silently producing a QR code with
missing/empty token fields (which would make the device's onboarding fail
in a confusing way with no error shown here at all).

## Testing

- `ReonboardScreen.test.tsx`: renders each step (`form`, `ready`, `error`)
  in isolation; asserts `onSubmit` fires with the typed SSID/password,
  `<QRCode>` receives exactly `qrValue`, `onDone`/`onRetry` fire on their
  respective buttons.
- `ReonboardContainer.test.tsx`: mocks `client.auth.getSession()`;
  asserts the built QR payload is valid JSON with exactly
  `{ssid, password, user_access_token, user_refresh_token}` (this is the
  field firmware's parser round-trips against - see
  `firmware/tests/test_wifi_onboard.py`'s `VALID_PAYLOAD` shape); asserts
  the `error` step is reached when `getSession()` fails or returns no
  session; asserts `navigation.goBack()` is called from `onDone`.
- `DeviceContainer.test.tsx`: update the existing "Re-onboard WiFi" test to
  assert `navigation.navigate('Reonboard')` is called instead of asserting
  an `Alert`.

## Docs

Update the "As-built gap" notes to mark this resolved, matching how
#18/#19 (firmware) and #8 (app screen wiring) were closed out:

- `docs/superpowers/plans/2026-07-04-visio-app.md`'s Handoff section.
- `docs/superpowers/plans/2026-07-04-visio-epics-overview.md`'s Epic 5
  follow-up-issues list and checklist note.
- `docs/superpowers/specs/2026-07-04-visio-pendant-design.md`'s "As-built
  gap" line under Tab 3 - Device.

## Delivery

This ships as its own PR, branched off
`gnhf/continue-your-worktr-bd0870` (not committed onto it) once that
branch's PR #24 is stable, since it is a separate scope from the
integration-test work PR #24 covers. Validated and pushed via the
`/no-mistakes` pipeline like PR #24 was.
