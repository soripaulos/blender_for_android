#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Prepare a hosted GitHub Ubuntu runner for the Android build: disk space, host
# packages, and the SDK/NDK components env.sh and package.sh expect.

set -euo pipefail

echo "::group::free disk space"
df -h /
# Toolchains this build never uses. The Android SDK stays: it is reused below.
sudo rm -rf /usr/share/dotnet /opt/ghc /usr/local/.ghcup /usr/local/share/boost \
  /opt/hostedtoolcache/CodeQL /usr/local/share/powershell /usr/share/swift \
  /usr/local/julia* /opt/az /usr/local/share/chromium /usr/local/lib/node_modules || true
sudo docker image prune --all --force >/dev/null 2>&1 || true
df -h /
echo "::endgroup::"

echo "::group::host packages"
sudo apt-get update -qq
# gcc-14: Blender's host code generators need GCC 14+. The -dev libraries are for the
# host CPython that deps/build.sh builds before cross-compiling the target one.
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \
  build-essential gcc-14 g++-14 ninja-build meson pkg-config autoconf automake libtool \
  autotools-dev autopoint gettext nasm yasm patchelf zip unzip git-lfs ccache \
  libffi-dev zlib1g-dev libssl-dev libbz2-dev liblzma-dev libsqlite3-dev libreadline-dev \
  libncurses-dev uuid-dev libgdbm-dev tk-dev xz-utils gperf bison flex python3-venv
cmake --version | head -1
echo "::endgroup::"

echo "::group::android sdk"
SDK="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-/usr/local/lib/android/sdk}}"
SDKMANAGER="$SDK/cmdline-tools/latest/bin/sdkmanager"
NDK_VERSION="$(sed -nE 's/.*ANDROID_NDK_VERSION:-([0-9.]+).*/\1/p' build_files/android/env.sh)"
yes | "$SDKMANAGER" --licenses >/dev/null 2>&1 || true
"$SDKMANAGER" --install "ndk;$NDK_VERSION" "build-tools;35.0.1" "platforms;android-35" "platform-tools" \
  | grep -v '^\[' || true
# build_apk.sh unsets ANDROID_HOME so env.sh falls back to its default, $HOME/android-sdk.
ln -sfn "$SDK" "$HOME/android-sdk"
ls "$HOME/android-sdk/ndk"
echo "::endgroup::"

if [ -n "${GITHUB_ENV:-}" ]; then
  {
    echo "ANDROID_HOST_CC=gcc-14"
    echo "ANDROID_HOST_CXX=g++-14"
    echo "JAVA_HOME=${JAVA_HOME_17_X64:-/usr/lib/jvm/temurin-17-jdk-amd64}"
    # The runner image presets these to its own default NDK, and env.sh honours them
    # over its pinned version. Point them at the pinned one.
    echo "ANDROID_NDK_ROOT=$SDK/ndk/$NDK_VERSION"
    echo "ANDROID_NDK_HOME=$SDK/ndk/$NDK_VERSION"
    echo "ANDROID_NDK=$SDK/ndk/$NDK_VERSION"
  } >> "$GITHUB_ENV"
fi
