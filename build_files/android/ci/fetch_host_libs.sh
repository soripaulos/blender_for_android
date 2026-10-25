#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Fetch Blender's precompiled Linux libraries into lib/linux_x64. The host code
# generators (makesdna, makesrna, ...) are configured with the same feature set as the
# Android target and link against these. The submodule is not recorded in this fork,
# so clone the release branch that matches the source version directly.

set -euo pipefail

DEST="lib/linux_x64"
BRANCH="${1:-blender-v5.3-release}"
URL="https://projects.blender.org/blender/lib-linux_x64.git"

if [ -d "$DEST/python" ]; then
  echo "[ci] $DEST already present"
  exit 0
fi

# projects.blender.org asks for credentials on LFS even though the content is public;
# with no terminal that prompt hangs forever instead of failing. Hand it empty ones.
git config --global credential."https://projects.blender.org".helper '!f() { echo username=; echo password=; }; f'
git lfs install --skip-repo
rm -rf "$DEST"
git clone --depth 1 --branch "$BRANCH" "$URL" "$DEST"
du -sh "$DEST"
