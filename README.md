# vision

Visio pendant - a wearable camera that records your day and turns it into a nightly highlight reel.

**Design spec:** [`docs/superpowers/specs/2026-07-04-visio-pendant-design.md`](docs/superpowers/specs/2026-07-04-visio-pendant-design.md)
**Roadmap:** [`docs/superpowers/plans/2026-07-04-visio-epics-overview.md`](docs/superpowers/plans/2026-07-04-visio-epics-overview.md)

## Repository layout

```
supabase/            -- Epic 0: Postgres migrations, pgTAP tests, local stack config
  migrations/        -- schema, RLS policies, storage buckets
  tests/database/    -- pgTAP test suites run via `supabase test db`
app/                 -- Epic 3: React Native (Expo) mobile companion app
  src/logic/         -- pure TypeScript calculation (no React imports)
  src/hooks/         -- Supabase data hooks (client injected for testing)
  src/screens/       -- presentational screens (props in, JSX out)
  src/components/    -- presentational components (timeline, segment preview)
  __tests__/         -- Jest suites run via `npm test`
docs/
  superpowers/
    specs/           -- approved design spec
    plans/           -- executable implementation plans (one per epic)
```

The `firmware/` and `pipeline/` subsystems from the spec's repository structure are planned but not yet implemented (Epics 1 and 2).

## Supabase foundation (Epic 0)

The shared database contract every other subsystem codes against:

- Tables: `devices`, `device_status`, `segments`, `reels`, `score_weights` (score weight defaults 0.4/0.3/0.2).
- `devices.push_token` (nullable) stores the mobile app's Expo push token for nightly reel notifications.
- Row-level security on every table: rows are visible only to the owning user (`auth.uid()` matched directly or via the owning device's `user_id`).
- Private `segments` and `reels` storage buckets with owner-scoped object policies keyed on the `{device_id}/` path prefix.
- Table privileges are granted explicitly: `service_role` gets full DML, `authenticated` gets select everywhere, writes only on `devices`, `score_weights`, and `segments.user_feedback`. The `anon` role gets no grants - devices are paired to authenticated users only.

### Local development

Requires the [Supabase CLI](https://supabase.com/docs/guides/local-development) (2.x) and Docker.

```bash
supabase start      # start the local stack
supabase db reset   # recreate the database from migrations
supabase test db    # run the pgTAP test suites
```

## Mobile companion app (Epic 3)

The React Native (Expo, TypeScript) app foundation: auth form validation, regenerate-preferences validation, timeline bucketing, archive heat-map cells, segment preview/export, and a realtime device-status hook.

- Strict layering: `src/logic/` is pure TypeScript with zero React imports, hooks take an injected `SupabaseClient` (tested with a fake client, never a real network call), and screens/components are presentational.
- Screen wiring is deferred to Epic 5 per the plan's Handoff section: the bottom tab navigator, container screens, real Supabase Auth, video playback, and timeline thumbnails.
- The Supabase client reads `EXPO_PUBLIC_SUPABASE_URL` and `EXPO_PUBLIC_SUPABASE_ANON_KEY` from `app/.env` (gitignored).

### Local development

```bash
cd app
npm install         # install dependencies
npm test            # run the Jest test suites
npx tsc --noEmit    # type-check
npx expo start      # launch the Expo dev server
```
