#!/bin/bash
cd "$(dirname "$0")/.." || exit 1
bash P4/scripts/bootstrap.sh "$@"
result=$?
if [ "$result" -ne 0 ]; then
  echo '自动准备或启动未完成，请查看上面的错误。重新打开可重试。'
  read -r -p '按回车退出'
fi
exit "$result"
