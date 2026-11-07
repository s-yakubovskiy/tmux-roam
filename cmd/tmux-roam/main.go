package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/inventory"
	"code.talos.lowlife.us/baza/tmux-roam/internal/process"
)

const version = "0.1.0"

func main() { os.Exit(run(os.Args[1:])) }

func run(args []string) int {
	if len(args) == 1 && args[0] == "--version" {
		fmt.Println(version)
		return 0
	}
	c, err := config.Load("")
	if err != nil {
		fmt.Fprintln(os.Stderr, "tmux-roam:", err)
		return 2
	}
	if len(args) == 2 && args[0] == "config" && args[1] == "check" {
		fmt.Printf("OK: %s (%d hosts)\n", c.Path, len(c.Hosts))
		return 0
	}
	if len(args) == 1 && args[0] == "doctor" {
		return doctor(c)
	}
	if len(args) > 0 && args[0] == "list" {
		jsonFlag, cached := false, false
		for _, arg := range args[1:] {
			switch arg {
			case "--json":
				jsonFlag = true
			case "--cached":
				cached = true
			default:
				fmt.Fprintln(os.Stderr, "tmux-roam: unknown list option:", arg)
				return 2
			}
		}
		rows, errors := inventory.All(context.Background(), c, !cached)
		if jsonFlag {
			if e := json.NewEncoder(os.Stdout).Encode(struct {
				SchemaVersion int               `json:"schema_version"`
				Sessions      any               `json:"sessions"`
				Errors        map[string]string `json:"errors"`
			}{1, rows, errors}); e != nil {
				fmt.Fprintln(os.Stderr, "tmux-roam:", e)
				return 2
			}
		} else {
			for _, r := range rows {
				fmt.Printf("%-16s %-32s %3dw %-7s %s\n", r.HostLabel, r.Name, r.Windows, r.Source, r.ID)
			}
			for h, e := range errors {
				fmt.Fprintln(os.Stderr, h+":", e)
			}
		}
		if len(errors) > 0 {
			return 2
		}
		return 0
	}
	if len(args) > 0 && args[0] == "refresh" {
		if len(args) > 2 {
			return 2
		}
		target := ""
		if len(args) == 2 {
			target = args[1]
		}
		found := target == ""
		bad := false
		for _, host := range c.Hosts {
			if !host.Enabled || target != "" && host.ID != target {
				continue
			}
			found = true
			rows, err := inventory.RemoteWithUpdate(context.Background(), host, c, true)
			if err != nil {
				fmt.Fprintln(os.Stderr, host.ID+":", err)
				bad = true
				continue
			}
			if err = inventory.Save(host, rows); err != nil {
				fmt.Fprintln(os.Stderr, host.ID+":", err)
				bad = true
				continue
			}
			fmt.Printf("%s: %d sessions\n", host.ID, len(rows))
		}
		if !found {
			fmt.Fprintln(os.Stderr, "tmux-roam: unknown or disabled host:", target)
			return 2
		}
		if bad {
			return 2
		}
		return 0
	}
	fmt.Fprintln(os.Stderr, "tmux-roam: Go backend currently supports --version, config check, doctor, list, and refresh; use the installed Python backend for other commands")
	return 2
}

func doctor(c config.Config) int {
	fmt.Printf("tmux-roam %s; config: %s\n", version, c.Path)
	bad := false
	for _, name := range []string{"tmux", "fzf", "ssh"} {
		path, err := exec.LookPath(name)
		if err != nil {
			path = "MISSING"
			bad = true
		}
		fmt.Printf("%s: %s\n", name, path)
	}
	fmt.Printf("runtime Go: %s\n", os.Args[0])
	if _, err := exec.LookPath("tmux"); err == nil {
		r, e := process.Capture(context.Background(), 5*time.Second, 1<<20, "tmux", "-V")
		if e == nil {
			fmt.Println("tmux version:", strings.TrimSpace(string(r.Stdout)))
		}
	}
	if _, err := exec.LookPath("fzf"); err == nil {
		versionResult, e := process.Capture(context.Background(), 5*time.Second, 1<<20, "fzf", "--version")
		if e == nil {
			fmt.Println("fzf version:", strings.TrimSpace(string(versionResult.Stdout)))
		}
		help, e := process.Capture(context.Background(), 5*time.Second, 1<<20, "fzf", "--help")
		if e != nil || help.Code != 0 {
			bad = true
		} else {
			for _, option := range []string{"--id-nth", "--track", "--listen", "--read0", "--no-input", "--footer"} {
				if !strings.Contains(string(help.Stdout), option) {
					fmt.Println("fzf missing required option:", option)
					bad = true
				}
			}
		}
	}
	var hosts []string
	for _, h := range c.Hosts {
		if h.Enabled {
			hosts = append(hosts, h.ID)
		}
	}
	if len(hosts) == 0 {
		fmt.Println("hosts: (none)")
	} else {
		fmt.Println("hosts:", strings.Join(hosts, ", "))
	}
	local, e := inventory.Local(context.Background())
	if e != nil {
		fmt.Println("local:", e)
	} else {
		fmt.Printf("local sessions: %d\n", len(local))
	}
	for _, h := range c.Hosts {
		if !h.Enabled {
			continue
		}
		rows, e := inventory.Remote(context.Background(), h, c)
		if e != nil {
			fmt.Printf("%s: %s\n", h.ID, e)
			bad = true
		} else {
			fmt.Printf("%s: %d sessions\n", h.ID, len(rows))
		}
	}
	if bad {
		return 2
	}
	return 0
}
