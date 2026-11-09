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
if [ -n "${TMUX_ROAM_TEST_OLD_TMUX:-}" ]; then
  python3 tests/acceptance/local_version.py
fi
python3 tests/acceptance/compare_ssh_inventory.py
python3 tests/acceptance/cli.py
python3 tests/acceptance/session_lifecycle.py
python3 tests/acceptance/actions.py
python3 tests/acceptance/pickers.py
python3 tests/acceptance/terminal.py
python3 tests/acceptance/terminal_stream.py
python3 tests/acceptance/config_sync.py
if [ -n "${TMUX_ROAM_TEST_CONTAINER_IMAGE:-}" ]; then
  python3 tests/acceptance/config_sync_mounts.py
fi
sh scripts/release-go.sh
python3 tests/acceptance/release.py
python3 tests/acceptance/coexist.py
TMUX_ROAM_BACKEND=python python3 -m unittest discover -s tests -v
