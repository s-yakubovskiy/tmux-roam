package main

import (
	"code.talos.lowlife.us/baza/tmux-roam/internal/release"
	"context"
	"fmt"
	"os"
)

func releaseCommand(ctx context.Context, args []string) int {
	var path string
	var err error
	switch {
	case len(args) == 4 && args[0] == "install":
		if args[3] != version {
			fmt.Fprintln(os.Stderr, "tmux-roam: bundle does not match checkout VERSION")
			return 2
		}
		path, err = release.Install(ctx, args[2], args[1], version)
	case len(args) == 2 && args[0] == "rollback":
		path, err = release.Rollback(ctx, args[1], version)
	case len(args) == 5 && args[0] == "manifest":
		err = release.WriteManifest(args[1], args[2], args[3], args[4])
	default:
		err = fmt.Errorf("usage: release install BUNDLE_DIRECTORY INSTALL_ROOT | release rollback INSTALL_ROOT")
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "tmux-roam:", err)
		return 2
	}
	if path != "" {
		fmt.Println(path)
	}
	return 0
}
