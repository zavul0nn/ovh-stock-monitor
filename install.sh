#!/usr/bin/env bash
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
  exec sudo -- bash "$0" "$@"
fi
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  # Ubuntu normally includes Python. Do not upgrade/remove packages to bootstrap it.
  . /etc/os-release
  [[ ${ID:-} == ubuntu ]] || { echo 'Only Ubuntu is supported.' >&2; exit 1; }
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
  apt-get update
  package_plan="$(LC_ALL=C apt-get -s --no-remove --no-upgrade install python3)"
  if printf '%s\n' "$package_plan" | grep -Eq '^Remv |^Inst [^ ]+ \['; then
    echo 'Python installation would modify existing packages; stopped.' >&2
    exit 1
  fi
  apt-get -y --no-remove --no-upgrade install python3
fi
exec python3 "$source_dir/installer.py" "$@"
