#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
go test ./...
go vet ./...
go test -race ./...
sh scripts/build-go.sh
python3 tests/acceptance/compare_config.py
python3 tests/acceptance/compare_inventory.py
