# Dependabot triage - 2026-11-16

Triage of the 14 open Dependabot PRs on `vietwillnguyen/vision` as of 2026-11-16.
No Dependabot PR is merged or closed by this work.
Each one carries a comment pointing at the consolidated PR that supersedes it, or at the rejection reason below.

Consolidated PRs:

- Python packages: __PY__ (`chore/deps-python-packages`)
- App (npm): __APP__ (`chore/deps-app`)
- GitHub Actions: __CI__ (`ci/actions-majors`)

## State at triage time

All 14 report `MERGEABLE` on GitHub, but most are stale: they were opened between 2026-08-02 and 2026-10-19, newer upstream versions exist, and several overlap.
They are superseded rather than rebased because the consolidated PRs regenerate each lockfile once with the proper tool (`uv lock`, `npm install` / `npx expo install`).

## Triage table

| PR | Ecosystem | Proposed change | State / conflicts | Superseded by | Breaking-change notes (upstream changelog) | Verdict |
|---|---|---|---|---|---|---|
| #64 | uv `firmware/` | supabase 2.31 -> 2.32, ruff 0.16.1 -> 0.17.0, mypy 2.3 -> 2.4 | Open since 2026-10-13, mergeable, stale versions | Python PR | ruff 0.17 is a 0.x minor (may add rules / format changes): `ruff check` and `ruff format --check` stay clean. mypy 2.4: no new errors. supabase-py: minor, drops `strenum`. | Take with changes - current latest instead (supabase 2.34.1, ruff 0.17.5, mypy 2.4.1) |
| #65 | uv `integration/` | Same three as #64 | Open since 2026-10-13, mergeable, stale | Python PR | As #64 | Take with changes - as #64 |
| #66 | uv `pipeline/` | litellm 1.91 -> 1.104.2 plus #64's three | Open since 2026-10-13, mergeable, stale | Python PR | litellm adds boto3/botocore, huggingface-hub, pydantic-settings as hard deps. The `completion` / `transcription` call shapes the adapters use are unchanged (adapter smoke passes). | Take with changes - litellm 1.109.1 |
| #36 | uv (titled `/integration`, touches all three `pyproject.toml`) | `setuptools>=68` -> `>=83` | Open since 2026-08-02, mergeable, overlaps #51 on `firmware/pyproject.toml` | Python PR | 83.0.0 requires Python 3.10+ (we require 3.11). | Take with changes - one `>=84` floor in all three packages |
| #51 | uv `firmware/` | `setuptools>=68` -> `>=84` | Open since 2026-08-17, mergeable, conflicts in content with #36 | Python PR | 84.0.0 only reshapes the C-compiler internals (`Compiler.call`, new loggers, `compilers.errors`); our packages are pure Python, built under uv build isolation. | Take - applied to all three packages |
| #67 | npm `app/` | 14 minor/patch bumps incl. react 19.3.1, react-native 0.88.0, screens 4.30, safe-area 5.10, svg 15.16 | Open since 2026-10-19, mergeable | App PR | Expo SDK 57 pins react 19.2.3, react-native 0.86.3, react-native-screens ~4.26.0, safe-area-context ~5.7.0, react-native-svg 15.15.4 (`bundledNativeModules.json`); react-native 0.88 is two RN minors past what SDK 57's native code is built for and pins its own react renderer. | Take with changes - non-SDK bumps (react-navigation, supabase-js, qrcode-svg, react-native-web) plus SDK-57 patch alignment via `npx expo install --check`; reject the SDK-owned minors |
| #41 | npm `app/` (lockfile only) | ws 7.5.11 -> 7.5.13 (transitive, via react-native / metro) | Open since 2026-08-02, mergeable | App PR | Bug-fix backports only; 7.5.13 fixes a regression in 7.5.12. | Take - `npm update ws` resolves 7.5.14 |
| #39 | npm `app/` dev | node-fetch 2.7.0 -> 3.3.2 | Open since 2026-08-02, mergeable | App PR | v3 is ESM-only and requires Node 12.20+; ships its own types. As proposed it breaks `useDeviceStatus.live.test.tsx`: `SyntaxError: Cannot use import statement outside a module` at `node_modules/node-fetch/src/index.js:9`. | Take with changes - add node-fetch and its ESM deps to jest `transformIgnorePatterns`, drop `@types/node-fetch`; full jest suite incl. the live test passes |
| #38 | npm `app/` dev | typescript 6.0.3 -> 7.0.2 | Open since 2026-08-02, mergeable | - | TS 7 is the native (Go) compiler without the JS compiler API. `tsc --noEmit` passes, but `npm run lint` crashes: `TypeError: Cannot read properties of undefined (reading 'Intrinsic')` in `ts-api-utils`, loaded by `@typescript-eslint/typescript-estree`, whose peer range is `typescript >=4.8.4 <6.1.0` (also in the latest typescript-eslint 8.72.2). | Reject - revisit when typescript-eslint supports TS 7 |
| #40 | npm `app/` | @react-native-async-storage/async-storage 2.2.0 -> 3.1.1 | Open since 2026-08-02, mergeable | - | v3: scoped storages, callback API dropped, `multi*` batch methods renamed, extra Android install step. Expo SDK 57 pins 2.2.0, and it is a native module, so it moves with the SDK. | Reject - take with the Expo SDK upgrade that bundles v3 |
| #30 | actions | supabase/setup-cli v1 -> v3 | Open since 2026-08-02, mergeable | Actions PR | v2 became a composite action; v3 improves musl/Alpine handling. The `version` input we pin via `SUPABASE_CLI_VERSION` is kept. | Take |
| #31 | actions | actions/setup-node v4 -> v7 | Open since 2026-08-02, mergeable | Actions PR | v5 auto-caches from `packageManager`, v6 limits that to npm, v7 updates `@actions/cache`; all on node24. We set `cache: npm` explicitly. | Take |
| #32 | actions | astral-sh/setup-uv v5 -> v7 | Open since 2026-08-02, mergeable, stale (v10 is current) | Actions PR | v7 node24; v8 new `manifest-file` format (unused); v9 `prune-cache` default false; v10 disables auto caching on `pull_request_target`, `workflow_run`, `release` (we use `enable-cache: true` on pull_request/push/schedule). | Take with changes - v10.3.3 instead of v7 (pinned to the full tag, since setup-uv no longer publishes floating major tags from v8 on) |
| #33 | actions | actions/checkout v4 -> v7 | Open since 2026-08-02, mergeable | Actions PR | node24 runtime; safer `pull_request_target` default (not used here). | Take |

## Follow-up recorded with the app PR

Dependabot will keep proposing SDK-owned minors (react, react-native, native modules) because it cannot see the Expo SDK pin.
The app PR adds `ignore` rules to `.github/dependabot.yml` for those, so SDK upgrades go through `npx expo install --fix` instead.

## Evidence

- Python: [python-e2e.log](python-e2e.log), [litellm_smoke.py](litellm_smoke.py) (on the Python PR's branch).
- App: [app-e2e.log](app-e2e.log), [seed-screens.sh](seed-screens.sh), screenshots `01-auth.png` - `05-archive.png` (on the app PR's branch).
- Actions: actionlint clean and the Actions PR's own CI run of every workflow job.
