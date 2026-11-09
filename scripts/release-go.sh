#!/bin/sh
set -eu
umask 022
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
version=$(cat VERSION)
# Include untracked Go sources: a development bundle identifies the actual tree.
build_id=$(find cmd internal -type f -name '*.go' -print | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d ' ' -f 1)
build_id=$(printf '%s\n' "$build_id" "$(sha256sum go.mod go.sum)" | sha256sum | cut -d ' ' -f 1)
sh scripts/build-go.sh
mkdir -p build/releases
for arch in amd64 arm64; do
    stage=$(mktemp -d "$root/build/.release.XXXXXXXX")
    trap 'rm -rf -- "$stage"' 0 HUP INT TERM
    mkdir "$stage/agents"
    CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -trimpath -ldflags="-s -w -X main.buildID=$build_id" -o "$stage/tmux-roam" ./cmd/tmux-roam
    cp build/agents/linux_amd64 build/agents/linux_arm64 "$stage/agents/"
    build/tmux-roam-go release manifest "$stage" "$version" "$build_id" "$arch"
    chmod 0500 "$stage/tmux-roam" "$stage/agents/linux_amd64" "$stage/agents/linux_arm64"
    tar --sort=name --mtime=@0 --owner=0 --group=0 --numeric-owner -C "$stage" -czf "build/releases/tmux-roam-$version-linux_$arch.tar.gz" tmux-roam agents/linux_amd64 agents/linux_arm64 manifest.json
    rm -rf -- "$stage"
    trap - 0 HUP INT TERM
done
cd build/releases
sha256sum tmux-roam-*.tar.gz > SHA256SUMS
cat SHA256SUMS
