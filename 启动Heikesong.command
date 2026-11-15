#!/bin/bash
project_dir="$(cd -- "$(dirname -- "$0")" && pwd)" || exit 1
if [ ! -f "$project_dir/P4/start.command" ]; then
  echo 'Cannot find P4/start.command. Keep this launcher in the project root.'
  read -r -p 'Press Enter to close.'
  exit 2
fi
exec /bin/bash "$project_dir/P4/start.command" "$@"
