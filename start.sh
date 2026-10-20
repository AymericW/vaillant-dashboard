#!/usr/bin/env sh
# Start the dashboard on macOS / Linux. First run installs everything (needs Python 3.11+).
cd "$(dirname "$0")"
# update to the latest version from GitHub
if [ -d .git ] && command -v git >/dev/null; then
  git pull --ff-only || echo "Could not update - starting the current version."
fi
if [ ! -d .venv ]; then
  python3 -m venv .venv || exit 1
fi
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt || exit 1
if [ ! -f config.toml ] && [ "$1" != "--demo" ]; then
  cp config.example.toml config.toml
  echo "Created config.toml - fill in your login details (see README), then run ./start.sh again (or ./start.sh --demo)."
  exit 0
fi
exec .venv/bin/python app.py "$@"
