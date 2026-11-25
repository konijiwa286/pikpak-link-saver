#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' '需要 Python 3。CachyOS：sudo pacman -S --needed python。' >&2
  exit 1
fi
exec python3 "$APP_DIR/main.py" "$@"
