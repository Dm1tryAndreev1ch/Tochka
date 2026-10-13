#!/bin/zsh
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  print "Сначала выполните python3.12 scripts/setup.py"
  exit 1
fi
.venv/bin/python scripts/run.py
