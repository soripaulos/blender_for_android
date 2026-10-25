#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Delete this ref's GitHub Actions caches whose key starts with PREFIX, except KEEP.
#
#   build_files/android/ci/prune_caches.sh PREFIX [KEEP]
#
# A repository gets 10 GB of cache. The dependency prefix and the compiler cache are
# several GB each and are saved under a new key every stage, so without pruning the
# stale copies would evict the one the next job needs. Needs GH_TOKEN with actions:write;
# failures only warn, since a cache that could not be deleted costs space, not a build.

set -uo pipefail

PREFIX="$1"
KEEP="${2:-}"
REF="${GITHUB_REF:-}"

keys="$(gh cache list --limit 100 --key "$PREFIX" ${REF:+--ref "$REF"} --json id,key \
  --jq '.[] | "\(.id) \(.key)"' 2>/dev/null)" || { echo "::warning::cannot list caches"; exit 0; }

while read -r id key; do
  [ -n "$id" ] || continue
  [ "$key" = "$KEEP" ] && continue
  echo "[ci] deleting cache $key"
  gh cache delete "$id" >/dev/null 2>&1 || echo "::warning::cannot delete cache $key"
done <<< "$keys"
exit 0
