# @testing-library/react-native 14 migration evidence

Supersedes Dependabot PR #91, whose `app` job failed type-checking because RNTL 14 makes `render` and friends async.

## Compatibility with Expo SDK 57

- RNTL 14.1.0 peers: `jest >=29`, `react >=19.0.0`, `react-native >=0.78`, `test-renderer ^1.0.0`; engines `node ^22.13.0 || >=24`.
- The app pins `react 19.2.3` and `react-native 0.86.3` (SDK 57), runs jest 29 via `jest-expo ~57.0.6`, and CI uses Node 22.
- `test-renderer` is installed at `~1.2.0`, the line the migration guide maps to React 19.2.
- `npx expo install --check` reports `Dependencies are up to date` after the bump.

## Before

[tsc-before-migration.log](tsc-before-migration.log) is `npx tsc --noEmit` with the new dependencies and the old tests: 50 errors, exit 2, the same failure as #91's CI.

## After

- [checks-after.log](checks-after.log): `tsc`, `lint`, `format:check` and `expo install --check` all exit 0.
- [jest-after.log](jest-after.log): `npx jest --ci` after `npx jest --clearCache` against a local Supabase stack, so `useDeviceStatus.live.test.tsx` runs instead of skipping.
  33 suites and 130 tests pass, and the log has no console output at all (no act warnings, no RNTL event warnings).
- `npx expo export --platform web` (the CI web bundle smoke check) also completes.
