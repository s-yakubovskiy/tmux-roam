#!/bin/sh
TMUX_ROAM_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
"$TMUX_ROAM_ROOT/bin/tmux-roam" integrate install
