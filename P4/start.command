#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
for p4_python in P4/.venv/bin/python P1/.venv/bin/python /usr/bin/python3 python3 /opt/homebrew/bin/python3; do
  if "$p4_python" -c 'import sys; assert sys.version_info >= (3,9)' >/dev/null 2>&1; then
    exec "$p4_python" P4/scripts/control.py
  fi
done
echo '请先安装 Python 3.9 或以上，再打开本控制台。'
read -r -p '按回车退出'
