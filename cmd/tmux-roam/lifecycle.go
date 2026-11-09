package main

import (
	"context"
	"errors"
	"fmt"
	"os"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/session"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func lifecycle(ctx context.Context, c config.Config, args []string) (bool, int) {
	if len(args) == 0 || args[0] != "open" && args[0] != "supervise" {
		return false, 0
	}
	var err error
	if args[0] == "supervise" {
		if len(args) != 4 {
			err = errors.New("usage: supervise TOKEN HOST OPAQUE_ID")
		} else {
			var host config.Host
			found := false
			for _, h := range c.Hosts {
				if h.Enabled && h.ID == args[2] {
					host = h
					found = true
				}
			}
			if !found {
				err = errors.New("unknown or disabled host")
			} else {
				err = session.Supervise(ctx, args[1], host, c, args[3])
			}
		}
	} else {
		options := map[string]string{}
		var positionals []string
		for i := 1; i < len(args); i++ {
			switch args[i] {
			case "--client", "--navigation", "--window":
				if i+1 >= len(args) {
					err = errors.New("missing option value")
					break
				}
				options[args[i]] = args[i+1]
				i++
			default:
				positionals = append(positionals, args[i])
			}
		}
		if len(positionals) != 1 {
			err = errors.New("usage: open OPAQUE_ID [--client CLIENT]")
		}
		if err == nil {
			client := options["--client"]
			if client == "" {
				client, err = tmux.Run(ctx, "display-message", "-p", "#{client_name}")
			}
			if err == nil {
				_, err = session.OpenID(ctx, c, positionals[0], client, options["--navigation"], options["--window"])
			}
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "tmux-roam:", err)
		return true, 2
	}
	return true, 0
}
