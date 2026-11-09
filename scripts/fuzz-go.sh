#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
duration=${TMUX_ROAM_FUZZ_TIME:-15s}
go test ./internal/config -run '^$' -fuzz '^FuzzConfigLoad$' -fuzztime "$duration" -parallel 2
go test ./internal/model -run '^$' -fuzz '^FuzzIdentityDecode$' -fuzztime "$duration" -parallel 2
go test ./internal/inventory -run '^$' -fuzz '^FuzzSessionLine$' -fuzztime "$duration" -parallel 2
go test ./internal/ssh -run '^$' -fuzz '^FuzzCommandFields$' -fuzztime "$duration" -parallel 2
