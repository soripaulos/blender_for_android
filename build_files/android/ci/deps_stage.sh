#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# One time-boxed stage of the dependency build, for CI.
#
#   build_files/android/ci/deps_stage.sh MINUTES
#
# The full dependency build takes longer than a hosted GitHub runner may run a job (six
# hours), so the workflow chains several stages, each restoring the prefix the previous
# one left in the cache. This builds dependencies one at a time, in build.sh's order,
# until the budget runs out. A dependency that would be cut off is killed and its install
# directory removed: build.sh counts any non-empty install directory as built, so a
# half-installed one would otherwise be skipped forever.
#
# Writes complete=true|false to $GITHUB_OUTPUT (when set) and exits non-zero only on a
# real build failure.

set -euo pipefail

BUDGET_MIN="${1:-320}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
BUILD_SH="$REPO_ROOT/build_files/android/deps/build.sh"

: "${BUILD_BASE:?BUILD_BASE must be set}"
LIBDIR="$BUILD_BASE/lib/android_arm64"
WORK_DIR="$BUILD_BASE/android_deps_build/work"
HOST_TOOLS_DIR="$BUILD_BASE/android_deps_build/host"

# The build order, read from build.sh so there is one list.
eval "$(sed -n '/^ALL_DEPS=(/,/^)/p' "$BUILD_SH")"

# Mirrors dep_install_dir() in build.sh.
install_dir() {
  case "$1" in
    numpy)          echo "$LIBDIR/python/lib/python3.13/site-packages/numpy" ;;
    certifi)        echo "$LIBDIR/python/lib/python3.13/site-packages/certifi" ;;
    pip)            echo "$LIBDIR/python/lib/python3.13/site-packages/pip" ;;
    vulkan_headers) echo "$LIBDIR/vulkan" ;;
    ispc)           echo "$HOST_TOOLS_DIR/ispc" ;;
    oidn)           echo "$LIBDIR/openimagedenoise" ;;
    *)              echo "$LIBDIR/$1" ;;
  esac
}

is_built() {
  local d; d="$(install_dir "$1")"
  [ -d "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ]
}

set_output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1" >> "$GITHUB_OUTPUT"
  fi
  echo "[ci] $1"
}

# Keep the disk from filling: a finished dependency's sources and build tree are not
# needed again. The host Python is, by numpy and USD in later stages.
prune_work_dir() {
  [ -d "$WORK_DIR" ] || return 0
  find "$WORK_DIR" -mindepth 1 -maxdepth 1 ! -name 'python-host-install' -exec rm -rf {} +
}

deadline=$(( $(date +%s) + BUDGET_MIN * 60 ))
built=0
for dep in "${ALL_DEPS[@]}"; do
  # remote_asset_python_deps installs into site-packages and never creates its own
  # directory, so it always runs; it is a quick pip install.
  if is_built "$dep"; then
    continue
  fi
  remaining=$(( deadline - $(date +%s) ))
  if [ "$remaining" -le 300 ]; then
    echo "[ci] budget spent before $dep; the next stage continues from here"
    set_output "complete=false"
    exit 0
  fi
  echo "::group::deps: $dep (${remaining}s of budget left)"
  set +e
  timeout --signal=TERM --kill-after=60 "$remaining" bash "$BUILD_SH" "$dep"
  status=$?
  set -e
  echo "::endgroup::"
  if [ "$status" -ne 0 ]; then
    # Whatever it managed to install is suspect. (remote_asset_python_deps has no
    # directory of its own: its install dir would name one that does not exist.)
    [ "$dep" = remote_asset_python_deps ] || rm -rf "$(install_dir "$dep")"
    if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
      echo "[ci] $dep was cut off by the time budget; the next stage rebuilds it"
      set_output "complete=false"
      exit 0
    fi
    echo "::error::dependency $dep failed to build (exit $status)"
    exit "$status"
  fi
  built=$((built + 1))
  prune_work_dir
done

echo "[ci] all dependencies present ($built built in this stage)"
set_output "complete=true"
