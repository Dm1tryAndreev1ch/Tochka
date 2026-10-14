#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python scripts/launch.py "$@"
fi
for python in python3.12 python3.13 python3; do
    if command -v "$python" >/dev/null 2>&1; then
        exec "$python" scripts/launch.py "$@"
    fi
done
echo "Install Python 3.12 or 3.13. See docs/LAUNCHERS.md." >&2
exit 1
