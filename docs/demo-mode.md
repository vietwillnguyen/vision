# Public demo runbook

The public demo lets a stranger open the web build, tap **View demo** and browse a real reel and its raw clips without an account and without any way to change anything.
It needs no pendant: the footage is uploaded the way the firmware uploads it and the reel is built by the real nightly pipeline.

## How it fits together

- **A read-only account.** The demo signs in as one shared account whose `auth.users.raw_app_meta_data` carries `"read_only": true`.
  Only the Auth admin API (the service role) can set that flag.
- **Enforced in the database, not the app.** [`supabase/migrations/20261121120000_read_only_accounts.sql`](../supabase/migrations/20261121120000_read_only_accounts.sql) adds a restrictive policy for every write command on every `public` table and on `storage.objects`, so the account can read its own device's rows and objects through the ordinary owner policies but cannot insert, update or delete anything.
  Triggers on `auth.users` and `auth.mfa_factors` stop it changing its own password, email or profile, or enrolling an MFA factor, any of which would lock out the next visitor.
- **Tested at both layers.** [`supabase/tests/database/read_only_users.test.sql`](../supabase/tests/database/read_only_users.test.sql) proves each denial in SQL, pairs it with the same write succeeding for an ordinary owner, and fails if a `public` table is ever added without its read-only policies.
  [`integration/tests/test_read_only_demo_live.py`](../integration/tests/test_read_only_demo_live.py) seeds a demo and drives every write through the real REST, Storage and Auth APIs with the anon key, as the web build would.
- **The seed tool.** `python -m pipeline.demo` (in [`pipeline/pipeline/demo/`](../pipeline/pipeline/demo)) takes a directory of clips, cuts them into the firmware's one-minute 1280x720 H.264 segments, recreates the demo account and device, uploads the segments and flag markers, and runs `run_nightly` for that device only.
  Scoring is stubbed unless `--scoring real` is passed.
- **The app.** The sign-in screen shows **View demo** only when the build carries `EXPO_PUBLIC_DEMO_EMAIL` and `EXPO_PUBLIC_DEMO_PASSWORD` (read in one place, [`app/src/lib/demoConfig.ts`](../app/src/lib/demoConfig.ts)).
  A read-only session shows a persistent "Demo - sample footage" banner with an Exit button, browses the day of its newest reel instead of today, and hides or disables the controls that write (clip preferences, Wi-Fi setup) with a line saying why.
  On a desktop browser the app sits in a centred phone-width frame.

The demo password ships in the public JavaScript bundle by design.
Knowing it grants a look at the sample day and nothing else.

## Run it locally

```sh
supabase start
eval "$(supabase status -o env)"

cd pipeline
SUPABASE_URL=$API_URL SUPABASE_SERVICE_ROLE_KEY=$SERVICE_ROLE_KEY \
VISIO_DEMO_PASSWORD=visio-demo-local \
  uv run --locked python -m pipeline.demo --placeholder
```

`--placeholder` generates six clearly synthetic test-pattern clips with FFmpeg (captioned "SAMPLE FOOTAGE - synthetic placeholder"), so nothing large is committed.
It takes a few minutes, most of it encoding.
The day defaults to yesterday (UTC); pass `--day YYYY-MM-DD` to pick one.
Re-running the seed deletes the previous demo account, its rows and its storage objects, and builds a fresh one; it refuses to delete an account with the demo email that is not read-only.

Then point the app at the stack and the demo account in `app/.env` (gitignored):

```sh
EXPO_PUBLIC_SUPABASE_URL=http://127.0.0.1:54321
EXPO_PUBLIC_SUPABASE_ANON_KEY=<ANON_KEY from supabase status>
EXPO_PUBLIC_DEMO_EMAIL=demo@vision.vnlabs.dev
EXPO_PUBLIC_DEMO_PASSWORD=visio-demo-local
```

and run `npx expo start --web` from `app/`.

## Seed with real clips

Put the clips in a directory, in the order they happened (they are placed in name order).
Any common container works (`.mp4`, `.mov`, `.m4v`, `.mkv`, `.webm`); portrait or odd sizes are scaled and centre-cropped to 1280x720 like the pendant's camera, and sound is kept as mono AAC.
An optional `demo.toml` beside them sets the wall-clock times and the flag presses:

```toml
start = "09:00"        # first clip's start (UTC), default 09:00
gap_minutes = 7        # between clips that carry no start, default 7

[clips."coffee.mp4"]
start = "09:12:30"     # optional; must not overlap the clip before it
flags = [4.5, 30]      # seconds into the clip where the button was pressed
```

```sh
uv run --locked python -m pipeline.demo --clips ~/visio-demo-clips --day 2026-12-01
```

Stub scoring is free and deterministic but knows nothing about the pictures.
For a reel that reflects what is in the clips, add `--scoring real` with `ANTHROPIC_API_KEY` set (and `OPENAI_API_KEY` if any clip has sound, for Whisper).
A demo day of about ten one-minute segments costs a few cents.
Real scoring is never used unless asked for by name, even when the keys are in the environment.

## Seed a hosted demo project

Use a **separate** Supabase project for the demo, never the one a real pendant uploads to.
The demo account's password is public, and a separate project keeps the blast radius at the sample day.

1. Create the project (the free plan is enough: ten one-minute segments at the 4 Mbps cap are at most about 300 MB of the 1 GB storage quota).
2. Apply the schema: `supabase link --project-ref <demo-ref>` then `supabase db push`.
3. In Authentication settings: turn **off** new sign-ups (the demo account is created with the admin API), leave anonymous sign-ins and MFA enrollment off, and set the Site URL to the demo's URL.
   Signing up is the one write a stranger could still make without the demo account, so it must be off.
4. Seed it from a machine with FFmpeg, using that project's URL and service role key:

   ```sh
   SUPABASE_URL=https://<demo-ref>.supabase.co SUPABASE_SERVICE_ROLE_KEY=<demo service role key> \
   VISIO_DEMO_PASSWORD=<pick one> \
     uv run --locked python -m pipeline.demo --clips ~/visio-demo-clips --scoring real
   ```

5. Sign in as the demo account in the hosted build and confirm the banner, the reel and a clip all play.

Do not add the demo project to the `nightly-reel` workflow; the seed already ran the pipeline, and the demo day never changes until it is reseeded.

## Export and host the web build

```sh
cd app
EXPO_PUBLIC_SUPABASE_URL=https://<demo-ref>.supabase.co \
EXPO_PUBLIC_SUPABASE_ANON_KEY=<demo anon key> \
EXPO_PUBLIC_DEMO_EMAIL=demo@vision.vnlabs.dev \
EXPO_PUBLIC_DEMO_PASSWORD=<the seeded password> \
  npx expo export --platform web --output-dir dist
```

`dist/` is a static single-page app.
Any static host works (Cloudflare Pages, Netlify, GitHub Pages); serve `index.html` for unknown paths and point `vision.vnlabs.dev` at it.
Without the two `EXPO_PUBLIC_DEMO_*` variables the build has no **View demo** button, which is what a private build should look like.
Videos stream straight from Supabase Storage through short-lived signed URLs, so the host serves only the app.

## Footage privacy rules

- **Owner only.** Every clip is shot by the owner, of the owner's own spaces and things.
- **No bystanders.** No other person may be identifiable: no faces, no recognisable voices, no names on screen, no screens or documents showing someone else's data.
  If someone wanders into frame, drop the clip rather than blur it.
- **No locations that identify a home.** No house numbers, street signs or views from a window that place the owner's home.
- **Label it honestly.** The demo is a bench prototype with the wearable hardware still in progress.
  The app's banner and Device tab say so; captions, the landing page and any post about the demo must say so too, and must not present the footage as captured by a finished wearable.
- **Placeholder until then.** Until the owner's clips exist, the demo runs on the generated test patterns, which are captioned as synthetic on every frame.

## Limits worth knowing

- Everyone shares one account, so any visitor can sign every visitor out (`signOut` with global scope deletes the account's sessions).
  The next tap on **View demo** signs them straight back in.
- Sign-in is rate limited per IP by Supabase Auth (30 per 5 minutes by default), which also bounds abuse of the shared login.
- A Supabase Realtime broadcast on a public channel is not a database write and is not blocked; the app subscribes only to `postgres_changes` on `device_status`, which RLS still filters.
