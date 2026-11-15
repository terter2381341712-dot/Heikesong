#!/bin/bash
# No system Python, shell activation, or global package installation required.
set -euo pipefail
cd "$(dirname "$0")/../.."
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) target=osx-arm64 ;;
  Darwin-x86_64) target=osx-64 ;;
  Linux-x86_64) target=linux-64 ;;
  Linux-aarch64) target=linux-aarch64 ;;
  *) echo '此系统架构尚无可用的应用运行包。'; exit 1 ;;
esac
# Keep prepared environments outside the checkout so they survive branch
# switches, new worktrees and moving the project directory.
if [ -n "${P4_RUNTIME_DIR:-}" ]; then
  root="$P4_RUNTIME_DIR"
elif [ "$(uname -s)" = "Darwin" ]; then
  root="$HOME/Library/Application Support/EdgeFabric/runtime"
else
  root="${XDG_DATA_HOME:-$HOME/.local/share}/EdgeFabric/runtime"
fi
mkdir -p "$root"
lock="$root/.bootstrap-lock"
# PID-based lock: a crashed launcher leaves a stale PID, which the next start
# reclaims automatically instead of asking the user to delete a directory.
acquire_lock() {
  if mkdir "$lock" 2>/dev/null; then
    echo $$ > "$lock/pid" 2>/dev/null || true
    return 0
  fi
  holder=$(cat "$lock/pid" 2>/dev/null || true)
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    return 1
  fi
  # Stale lock from an interrupted run (or one killed before writing its PID).
  rm -rf "$lock" 2>/dev/null || true
  if mkdir "$lock" 2>/dev/null; then
    echo $$ > "$lock/pid" 2>/dev/null || true
    return 0
  fi
  return 1
}
if ! acquire_lock; then
  echo '另一个启动器正在准备环境，请等待它完成后再试。'
  exit 1
fi
trap 'rm -f "$root/micromamba.download"; rm -rf "$lock"' EXIT
hash_file() {
  if command -v shasum >/dev/null; then shasum -a 256 "$1" | awk '{print $1}';
  else sha256sum "$1" | awk '{print $1}'; fi
}
IFS=$'\t' read -r _ url digest < <(awk -F '\t' -v p="$target" '$1==p {print}' P4/runtime/installers.tsv)
if [ ! -f "$root/micromamba" ] || [ "$(hash_file "$root/micromamba")" != "$digest" ]; then
  echo '正在下载并校验应用环境管理器…'
  curl --fail --location --retry 3 --connect-timeout 20 --max-time 600 "$url" -o "$root/micromamba.download"
  [ "$(hash_file "$root/micromamba.download")" = "$digest" ] || { echo '下载校验失败，请重新启动。'; exit 1; }
  chmod u+x "$root/micromamba.download"
  mv "$root/micromamba.download" "$root/micromamba"
fi
echo '正在检查本机应用专用环境…'
if ! "$root/bootstrap/bin/python" -I -c 'import sys,ssl; assert sys.version_info[:3]==(3,12,15)' 2>/dev/null; then
  action=create
  repair=(-y)
  if [ -d "$root/bootstrap/conda-meta" ]; then action=install; repair=(-y --force-reinstall); fi
  "$root/micromamba" --no-rc -r "$root/cache" "$action" "${repair[@]}" -p "$root/bootstrap" --override-channels -c conda-forge python=3.12.15 pip
fi
# Release the shell lock before Python takes its own cross-platform preparation lock.
rm -rf "$lock"
trap - EXIT
exec "$root/bootstrap/bin/python" -I P4/scripts/managed_runtime.py "$@"
