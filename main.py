#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 konijiwa286
"""Portable application entry point."""
import sys

sys.dont_write_bytecode = True

from saver.bootstrap import main

if __name__ == '__main__':
    raise SystemExit(main())
