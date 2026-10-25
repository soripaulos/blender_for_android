#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# One time-boxed attempt at building the APK, for CI.
#
#   build_files/android/ci/build_stage.sh MINUTES CONFIG OUT_DIR
#
# Compiling Blender for arm64 on a hosted runner can take longer than one job may run.
# Every compile goes through ccache, which the workflow keeps between jobs, so a stage
# that runs out of time is not wasted: the next one replays the finished objects from
# the cache in minutes and carries on where this one stopped.
#
# Writes built=true|false to $GITHUB_OUTPUT (when set). Exits non-zero only when the
# build actually failed.

set -euo pipefail

BUDGET_MIN="${1:-320}"
CONFIG="${2:-full}"
OUT_DIR="${3:-dist}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
: "${BUILD_BASE:?BUILD_BASE must be set}"

set_output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1" >> "$GITHUB_OUTPUT"
  fi
  echo "[ci] $1"
}

# CMake reads these from the environment for every project it configures, the host
# tools included, so nothing in build_apk.sh has to know about ccache.
export CMAKE_C_COMPILER_LAUNCHER=ccache
export CMAKE_CXX_COMPILER_LAUNCHER=ccache
export CCACHE_DIR="${CCACHE_DIR:-$BUILD_BASE/ccache}"
export CCACHE_BASEDIR="$REPO_ROOT"
export CCACHE_MAXSIZE="${CCACHE_MAXSIZE:-6G}"
export CCACHE_COMPRESS=1
# A fresh checkout gives every file a new mtime; compare contents instead.
export CCACHE_SLOPPINESS="include_file_mtime,include_file_ctime,time_macros,pch_defines,file_stat_matches"
ccache --zero-stats >/dev/null

set +e
timeout --signal=TERM --kill-after=120 "$(( BUDGET_MIN * 60 ))" \
  bash "$REPO_ROOT/build_files/android/build_apk.sh" "$CONFIG"
status=$?
set -e
ccache --show-stats || true

if [ "$status" -eq 124 ] || [ "$status" -eq 137 ]; then
  echo "[ci] out of time; the next stage resumes from the compiler cache"
  set_output "built=false"
  exit 0
fi
if [ "$status" -ne 0 ]; then
  echo "::error::the APK build failed (exit $status)"
  exit "$status"
fi

APK="$BUILD_BASE/android_apk_stage_$CONFIG/blender-$CONFIG.apk"
[ -f "$APK" ] || { echo "::error::build reported success but $APK is missing"; exit 1; }

mkdir -p "$OUT_DIR"
SHORT="$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
cp "$APK" "$OUT_DIR/blender-android-$CONFIG-$SHORT.apk"
# The Termux client, for installing without a running Blender to fetch it from.
cp "$REPO_ROOT/scripts/modules/_bpy_internal/remote_control/blender_cli.py" "$OUT_DIR/blender-cli"
( cd "$OUT_DIR" && sha256sum ./* > SHA256SUMS )
ls -lh "$OUT_DIR"
set_output "built=true"
