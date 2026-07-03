#!/usr/bin/env bash
set -uo pipefail

say_step() { printf "\n==> %s\n" "$1"; }
say_ok() { printf "OK: %s\n" "$1"; }
say_err() { printf "ERR: %s\n" "$1"; }

PYTHON_BIN="${PYTHON_BIN:-./venv/bin/python}"
LOCALFORGE_BIN="${LOCALFORGE_BIN:-./venv/bin/localforge}"
CONFIG="${CONFIG:-localforge.yaml}"
export LOCALFORGE_SKIP_SETUP="${LOCALFORGE_SKIP_SETUP:-1}"

failures=0

run_check() {
  desc="$1"
  shift
  say_step "$desc"
  if "$@"; then
    say_ok "$desc"
  else
    say_err "$desc"
    failures=$((failures + 1))
  fi
}

run_check "compile" "$PYTHON_BIN" -m compileall localforge tests
run_check "unit tests" "$PYTHON_BIN" -m unittest discover -s tests
run_check "doctor" "$LOCALFORGE_BIN" doctor --config "$CONFIG"
run_check "mcp smoke" "$LOCALFORGE_BIN" mcp-smoke --config "$CONFIG"

if [ "$failures" -ne 0 ]; then
  say_err "$failures smoke check(s) failed"
  exit 1
fi

say_ok "all smoke checks passed"
