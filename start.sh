#!/usr/bin/env bash
# StockSage one-command launcher (Mac / Linux).
#
#   ./start.sh              -> open StockSage in its own app window
#   ./start.sh install      -> put a StockSage icon on your desktop/dock
#   ./start.sh phone        -> share to your phone over home Wi-Fi (QR code)
#   ./start.sh web          -> open in a normal browser tab instead
#   ./start.sh update       -> pull the latest code improvements from your repo
#   ./start.sh doctor       -> check everything that can go wrong, with fixes
#   ./start.sh security     -> who has signed in to your broker account, and how
#                              exposed the stored login is (--signin 8:30am)
#   ./start.sh leave        -> done with this computer: remove every scheduled
#                              job, credential, token and log stored on it
#   ./start.sh autopilot    -> learn automatically every weekday (off|status)
#   ./start.sh daily        -> run the daily learn+scan cycle in the terminal
#   ./start.sh <anything>   -> passed through to the CLI (suggest, sectors, ...)
#
# First run bootstraps everything: virtualenv, dependencies, .env template.

set -euo pipefail
cd "$(dirname "$0")"

say()  { printf '\033[1;36m[stocksage]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[stocksage]\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. Python ---------------------------------------------------------------
PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || fail "Python 3 is required. Install it from https://www.python.org/downloads/ and re-run."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || fail "Python 3.10+ is required (found $($PYTHON --version 2>&1))."

# --- 2. Virtualenv -----------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  say "First run: creating virtual environment..."
  "$PYTHON" -m venv .venv
fi
VENV_PY=".venv/bin/python"

# --- 3. Dependencies (reinstalled only when requirements.txt changes) --------
STAMP=".venv/.requirements.stamp"
if [ ! -f "$STAMP" ] || ! cmp -s requirements.txt "$STAMP"; then
  say "Installing dependencies (a few minutes on first run)..."
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet -r requirements.txt
  cp requirements.txt "$STAMP"
  say "Dependencies ready."
fi

# --- 4. .env template ---------------------------------------------------------
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  say "Created .env — link Robinhood from the dashboard's Portfolio tab, or edit .env."
fi

# --- 5. Run -------------------------------------------------------------------
case "${1:-app}" in
  app|web|phone|install)
    exec "$VENV_PY" -m stocksage.desktop "${1:-app}"
    ;;
  autopilot)
    shift
    exec "$VENV_PY" -m stocksage.autopilot "${@:-on}"
    ;;
  update)
    exec "$VENV_PY" -m stocksage.update
    ;;
  doctor)
    exec "$VENV_PY" -m stocksage.doctor
    ;;
  *)
    exec "$VENV_PY" -m stocksage "$@"
    ;;
esac
