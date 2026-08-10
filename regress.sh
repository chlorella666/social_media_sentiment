#!/usr/bin/env sh
# 一键回归（POSIX 入口；Windows 用 regress.bat）
cd "$(dirname "$0")" || exit 2
exec python3 tests/run_regression.py "$@"
