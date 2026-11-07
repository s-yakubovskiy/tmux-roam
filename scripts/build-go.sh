#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$root/build"
cd "$root"
CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o build/tmux-roam-go ./cmd/tmux-roam
