#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$APP_DIR/启动.sh" --install-desktop
