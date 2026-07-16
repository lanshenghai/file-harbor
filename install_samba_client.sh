#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install_root="$project_root/.tools/samba"
binary="$install_root/usr/bin/smbclient"

if [[ -x "$binary" ]]; then
    "$binary" --version
    exit 0
fi

for command in dnf rpm2cpio cpio; do
    command -v "$command" >/dev/null || {
        echo "Required command not found: $command" >&2
        exit 1
    }
done

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

dnf download \
    --disablerepo='*' \
    --enablerepo=rocky8-baseos \
    --enablerepo=rocky8-baseos-updates \
    --enablerepo=rocky8-appstream \
    --enablerepo=rocky8-appstream-updates \
    --destdir "$tmp" \
    samba-client

rpm="$(printf '%s\n' "$tmp"/samba-client-*.rpm | head -n 1)"
mkdir -p "$install_root"
(cd "$install_root" && rpm2cpio "$rpm" | cpio -idm --quiet)

"$binary" --version
