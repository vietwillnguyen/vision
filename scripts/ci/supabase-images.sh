#!/usr/bin/env bash
# Keep the Supabase CLI's Docker images off public.ecr.aws in CI.
#
# The pinned Supabase CLI names every image public.ecr.aws/supabase/<name>:<tag>
# and pulls it from there whenever it is missing locally. Anonymous ECR Public
# clients get 1 pull per second and 500GB a month, and GitHub-hosted runners
# share egress IPs, so those pulls intermittently fail with
# `toomanyrequests: Rate exceeded` or `toomanyrequests: Data limit exceeded`.
# `supabase start` retries a little; `supabase gen types` runs a bare
# `docker run` that does not retry at all.
#
# This script makes every image the CLI wants already present under that exact
# name, so the CLI never pulls:
#
#   list          print the images in the manifest (.github/supabase-images.txt)
#   seed          pull each missing image from Supabase's GHCR mirror
#                 (ghcr.io/supabase/<name>:<tag>, same image), with bounded
#                 retries, and tag it with its public.ecr.aws name
#   save <file>   `docker save` every manifest image into one archive
#   load <file>   `docker load` an archive written by `save`
#   verify [project_id]
#                 fail if a running container of that Supabase project (default:
#                 project_id from supabase/config.toml) uses an image the
#                 manifest does not list (a stale manifest after a CLI bump)
#
# Env: SUPABASE_IMAGES_MANIFEST (default .github/supabase-images.txt),
#      SUPABASE_IMAGES_MIRROR (default ghcr.io),
#      SUPABASE_IMAGES_RETRY_DELAYS (seconds between attempts, default "10 30").
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
manifest="${SUPABASE_IMAGES_MANIFEST:-$repo_root/.github/supabase-images.txt}"
mirror="${SUPABASE_IMAGES_MIRROR:-ghcr.io}"
read -r -a retry_delays <<< "${SUPABASE_IMAGES_RETRY_DELAYS:-10 30}"
ecr_prefix="public.ecr.aws/supabase/"

log() { printf 'supabase-images: %s\n' "$*" >&2; }

usage() {
  echo "usage: $0 list | seed | save <file> | load <file> | verify [project_id]" >&2
  exit 2
}

images() {
  # Strip comments and whitespace; one image per line.
  sed -e 's/#.*//' -e 's/[[:space:]]//g' -e '/^$/d' "$manifest"
}

validate() {
  local image bad=0
  while IFS= read -r image; do
    if [[ "$image" != "$ecr_prefix"* || "$image" != *:* ]]; then
      log "manifest entry '$image' is not a tagged ${ecr_prefix}<name>:<tag> image"
      bad=1
    fi
  done < <(images)
  return "$bad"
}

pull_with_retry() {
  local ref="$1" attempt=1 delay
  if docker pull "$ref" >/dev/null; then
    return 0
  fi
  for delay in "${retry_delays[@]}"; do
    attempt=$((attempt + 1))
    log "pull of $ref failed; retrying in ${delay}s (attempt $attempt of $((${#retry_delays[@]} + 1)))"
    sleep "$delay"
    if docker pull "$ref" >/dev/null; then
      return 0
    fi
  done
  return 1
}

seed() {
  validate
  local image mirror_ref seeded=0 present=0
  while IFS= read -r image; do
    if docker image inspect "$image" >/dev/null 2>&1; then
      present=$((present + 1))
      continue
    fi
    mirror_ref="$mirror/supabase/${image#"$ecr_prefix"}"
    log "seeding $image from $mirror_ref"
    if ! pull_with_retry "$mirror_ref"; then
      log "could not pull $mirror_ref for $image"
      return 1
    fi
    docker tag "$mirror_ref" "$image"
    # Drop the mirror name so the image has exactly one name, the one the CLI
    # resolves. Only the tag goes; the layers stay under $image.
    docker rmi "$mirror_ref" >/dev/null
    seeded=$((seeded + 1))
  done < <(images)
  log "seed done: $present already present, $seeded pulled from $mirror"
}

verify() {
  local project="$1" image unlisted=0 seen=0
  local listed
  listed="$(images)"
  while IFS= read -r image; do
    [ -n "$image" ] || continue
    seen=$((seen + 1))
    if ! grep -qxF -- "$image" <<< "$listed"; then
      log "container image $image is not in $manifest - update it for the pinned Supabase CLI"
      unlisted=1
    fi
  done < <(docker ps --filter "label=com.supabase.cli.project=$project" --format '{{.Image}}')
  if [ "$seen" = 0 ]; then
    log "no running containers for Supabase project '$project'"
    return 1
  fi
  [ "$unlisted" = 0 ] && log "all $seen running container images are in the manifest"
  return "$unlisted"
}

cmd="${1:-}"
case "$cmd" in
  list) images ;;
  seed) seed ;;
  save)
    [ $# -eq 2 ] || usage
    validate
    mkdir -p "$(dirname -- "$2")"
    # shellcheck disable=SC2046 # one argument per image is the point
    docker save -o "$2" $(images)
    ;;
  load)
    [ $# -eq 2 ] || usage
    docker load -i "$2"
    ;;
  verify)
    [ $# -le 2 ] || usage
    project="${2:-$(sed -n 's/^project_id[[:space:]]*=[[:space:]]*"\(.*\)"/\1/p' "$repo_root/supabase/config.toml")}"
    [ -n "$project" ] || { log "no project_id given or found in supabase/config.toml"; exit 2; }
    verify "$project"
    ;;
  *) usage ;;
esac
