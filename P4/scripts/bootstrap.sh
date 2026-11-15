#!/bin/bash
# No system Python, shell activation, or global package installation required.
set -euo pipefail
cd "$(dirname "$0")/../.."
root="$PWD/P4/var/runtime"
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64) target=osx-arm64 ;;
  Darwin-x86_64) target=osx-64 ;;
  Linux-x86_64) target=linux-64 ;;
  Linux-aarch64) target=linux-aarch64 ;;
  *) echo '此系统架构尚无可用的应用运行包。'; exit 1 ;;
esac
mkdir -p "$root"
if ! mkdir "$root/.bootstrap-lock" 2>/dev/null; then
  echo '另一个启动器正在准备环境。请等待它完成；若上次意外退出，请关闭启动器后删除 P4/var/runtime/.bootstrap-lock 再试。'
  exit 1
fi
trap 'rm -f "$root/micromamba.download"; rmdir "$root/.bootstrap-lock"' EXIT
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
if ! "$root/bootstrap/bin/python" -I -c 'import sys,ssl; assert sys.version_info[:3]==(3,9,6)' 2>/dev/null; then
  action=create
  repair=(-y)
  if [ -d "$root/bootstrap/conda-meta" ]; then action=install; repair=(-y --force-reinstall); fi
  "$root/micromamba" --no-rc -r "$root/cache" "$action" "${repair[@]}" -p "$root/bootstrap" --override-channels -c conda-forge python=3.9.6 pip
fi
# Release the shell lock before Python takes its own cross-platform preparation lock.
rmdir "$root/.bootstrap-lock"
trap - EXIT
exec "$root/bootstrap/bin/python" -I P4/scripts/managed_runtime.py "$@"
