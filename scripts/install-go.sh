#!/bin/sh
# Explicit, offline installation. The expected hash must come from a trusted
# release channel or the local build output, independently of the archive.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ "$#" -ne 2 ]; then
    printf '%s\n' 'usage: scripts/install-go.sh BUNDLE.tar.gz EXPECTED_SHA256' >&2
    exit 2
fi
archive=$1
expected=$2
case "$expected" in *[!a-fA-F0-9]*|'') printf '%s\n' 'invalid SHA256' >&2; exit 2;; esac
[ "${#expected}" -eq 64 ] || exit 2
actual=$(sha256sum -- "$archive")
actual=${actual%% *}
[ "$actual" = "$expected" ] || { printf '%s\n' 'bundle checksum mismatch; activation unchanged' >&2; exit 2; }
stage=$(mktemp -d "${TMPDIR:-/tmp}/tmux-roam-install.XXXXXXXX")
trap 'rm -rf -- "$stage"' 0 HUP INT TERM
members=$(tar -tzf "$archive" | LC_ALL=C sort)
allowed=$(printf '%s\n' agents/linux_amd64 agents/linux_arm64 manifest.json tmux-roam)
[ "$members" = "$allowed" ] || { printf '%s\n' 'unexpected archive members' >&2; exit 2; }
tar --no-same-owner --no-same-permissions -xzf "$archive" -C "$stage"
for member in tmux-roam agents/linux_amd64 agents/linux_arm64 manifest.json; do
    [ -f "$stage/$member" ] && [ ! -L "$stage/$member" ] || exit 2
done
expected_version=$(cat "$root/VERSION")
"$stage/tmux-roam" release install "$stage" "$root/.runtime" "$expected_version"
