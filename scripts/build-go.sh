#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$root/build"
cd "$root"
CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o build/tmux-roam-go ./cmd/tmux-roam
mkdir -p build/agents
for arch in amd64 arm64; do
    CGO_ENABLED=0 GOOS=linux GOARCH="$arch" go build -trimpath -ldflags='-s -w' -o "build/agents/linux_$arch" ./cmd/tmux-roam-agent
done
