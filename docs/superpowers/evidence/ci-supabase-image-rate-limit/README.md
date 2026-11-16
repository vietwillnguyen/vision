# CI Supabase image rate limit: evidence

## Root cause

The pinned Supabase CLI (2.109.0) names every image `public.ecr.aws/supabase/<name>:<tag>` and pulls it from Amazon ECR Public when it is missing locally.
Anonymous ECR Public clients are limited to 1 image pull per second and 500GB of data a month ([ECR Public service quotas](https://docs.aws.amazon.com/AmazonECR/latest/public/public-service-quotas.html), [ECR pricing](https://aws.amazon.com/ecr/pricing/)).
GitHub-hosted runners share egress IPs, so pulls fail with `toomanyrequests: Rate exceeded` or `toomanyrequests: Data limit exceeded`.

Three things made it hurt:

- `supabase/setup-cli` only exports `SUPABASE_INTERNAL_IMAGE_REGISTRY=ghcr.io` for CLI versions below 2.108.0 ([`shouldUseGhcrRegistry` in src/main.ts](https://github.com/supabase/setup-cli/blob/main/src/main.ts)), so after the setup-cli v3 bump every pull went to ECR first.
- `supabase gen types --local` and `supabase test db` run a bare `docker run` with no retry and no registry fallback, and `gen types` ignores a `ghcr.io` override entirely ([`resolvePgmetaImage`](https://github.com/supabase/cli/blob/v2.109.0/apps/cli/src/legacy/commands/gen/types/types.shared.ts) only honours `docker.io`).
  `before-main-58-app.log` shows it: `SUPABASE_INTERNAL_IMAGE_REGISTRY: ghcr.io` was set, and postgres-meta was still pulled from public.ecr.aws and rate limited.
- `supabase start` pulled images CI never runs: imgproxy (pulled whenever storage is on, started only with image transformation) and the 1.1GB edge-runtime image, run once after migrating because `[experimental.pgdelta]` is enabled ([`TryCacheMigrationsCatalog`](https://github.com/supabase/cli/blob/v2.109.0/apps/cli-go/internal/db/start/start.go)).

## Before (real failed runs)

| File | Run | What failed |
| --- | --- | --- |
| `before-pr84-app.log` | [43865896759 attempt 1](https://github.com/vietwillnguyen/vision/actions/runs/43865896759/attempts/1) | every `supabase start` pull hit `Data limit exceeded` (start took 4m48s), then gen types' postgres-meta pull failed the "Verify generated database types are current" step |
| `before-pr76-app.log` | [43929330704 attempt 1](https://github.com/vietwillnguyen/vision/actions/runs/43929330704/attempts/1) | `Rate exceeded` on mailpit during start, then postgres-meta in gen types failed the job |
| `before-main-58-app.log` | [32822248259](https://github.com/vietwillnguyen/vision/actions/runs/32822248259) | post-merge on main: postgres-meta `Rate exceeded` with the ghcr.io override set |
| `before-pr76-integration-pass-attempt2.log` | [43929330704 attempt 2](https://github.com/vietwillnguyen/vision/actions/runs/43929330704/attempts/2) | a passing rerun that was still throttled and still pulled edge-runtime |

## After (PR #88 CI, run [43942898507](https://github.com/vietwillnguyen/vision/actions/runs/43942898507))

The GHCR mirror images are byte-identical to ECR: all nine manifest images have the same manifest digest on `ghcr.io/supabase` and `public.ecr.aws/supabase` (checked with registry `HEAD` requests).

| Job | Attempt 1, cold cache | Attempt 2, warm cache |
| --- | --- | --- |
| integration: Provide Supabase images | 75s (seed from GHCR 34s, save 31s, cache upload 8s) | 58s (774MB cache restore ~12s, `docker load` 44s, seed 0.1s, 0 pulls) |
| integration: Start local Supabase | 28s | 30s |
| app: Provide Supabase images | 127s (seed 80s, save 39s; the integration job won the cache save) | 65s (restore ~21s, `docker load` 43s, seed 0.1s, 0 pulls) |
| app: Start local Supabase | 29s | 31s |

For comparison, the passing pre-fix integration run spent 82s in `supabase start` alone, most of it pulling, plus a pg_prove pull in `supabase test db`.

In every job of both attempts:

- the cache key `supabase-images-Linux-X64-cli2.109.0-<manifest hash>` is reported as `Cache not found` (cold) or `Cache restored from key` (warm), and the cold save step reports `Cache saved` in the integration job and `Unable to reserve cache` in the app job, which lost the save race;
- the block step probes the Docker daemon and gets `dial tcp 0.0.0.0:443: connect: connection refused` for public.ecr.aws, ghcr.io and docker.io, before `supabase start` runs;
- no `Pulling from` or `Unable to find image` line appears after that, and `start`, `test db` and `gen types` all succeed, so nothing contacted a registry;
- `supabase-images.sh verify` reports `all 7 running container images are in the manifest`.

Excerpts: `after-cold-integration.log`, `after-cold-app.log`, `after-warm-integration.log`, `after-warm-app.log`.
