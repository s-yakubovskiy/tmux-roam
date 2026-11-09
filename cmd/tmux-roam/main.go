package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/inventory"
	"code.talos.lowlife.us/baza/tmux-roam/internal/native"
	"code.talos.lowlife.us/baza/tmux-roam/internal/process"
	"code.talos.lowlife.us/baza/tmux-roam/internal/release"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

const version = "0.1.0"

var buildID = "development"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx, os.Args[1:])
	stop()
	os.Exit(code)
}

func run(ctx context.Context, args []string) int {
	if help(args) {
		return 0
	}
	if len(args) == 1 && args[0] == "--version" {
		fmt.Println(version)
		return 0
	}
	if len(args) > 0 && args[0] == "release" {
		return releaseCommand(ctx, args[1:])
	}
	args, err := arguments(args)
	if err != nil {
		fmt.Fprintln(os.Stderr, "tmux-roam:", err)
		return 2
	}
	c, err := config.Load("")
	if err != nil {
		fmt.Fprintln(os.Stderr, "tmux-roam:", err)
		return 2
	}
	if handled, code := lifecycle(ctx, c, args); handled {
		return code
	}
	if handled, code := ui(ctx, c, args); handled {
		return code
	}
	if handled, code := sharedConfig(ctx, c, args); handled {
		return code
	}
	if len(args) == 2 && args[0] == "config" && args[1] == "check" {
		fmt.Printf("OK: %s (%d hosts)\n", c.Path, len(c.Hosts))
		return 0
	}
	if len(args) == 1 && args[0] == "doctor" {
		return doctor(ctx, c)
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
		rows, errors := inventory.All(ctx, c, !cached)
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
		selected := c
		selected.Hosts = nil
		for _, host := range c.Hosts {
			if host.Enabled && (target == "" || host.ID == target) {
				selected.Hosts = append(selected.Hosts, host)
			}
		}
		if target != "" && len(selected.Hosts) == 0 {
			fmt.Fprintln(os.Stderr, "tmux-roam: unknown or disabled host:", target)
			return 2
		}
		bad := false
		inventory.RefreshHosts(ctx, selected, true, func(result inventory.HostResult) {
			if result.Err != nil {
				fmt.Fprintln(os.Stderr, result.Host.ID+":", result.Err)
				bad = true
			} else {
				fmt.Printf("%s: %d sessions\n", result.Host.ID, len(result.Records))
			}
		})
		if bad || ctx.Err() != nil {
			return 2
		}
		return 0
	}
	fmt.Fprintln(os.Stderr, "tmux-roam: unknown command; use list, refresh, open, pick, native, doctor, config, or integrate")
	return 2
}

func doctor(ctx context.Context, c config.Config) int {
	started := time.Now()
	timing := func(phase string, since time.Time) {
		if os.Getenv("TMUX_ROAM_TIMING") == "1" {
			fmt.Fprintf(os.Stderr, "timing %s: %.2f ms\n", phase, float64(time.Since(since))/float64(time.Millisecond))
		}
	}
	defer timing("doctor total", started)
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
	exe, _ := os.Executable()
	fmt.Printf("runtime Go: %s; build: %s\n", exe, buildID)
	fmt.Printf("bundled agents: %s\n", filepath.Join(filepath.Dir(exe), "agents"))
	if _, err := os.Stat(filepath.Join(filepath.Dir(exe), "manifest.json")); err == nil {
		manifest, err := release.Inspect(filepath.Dir(exe), version)
		if err != nil {
			fmt.Println("release verification:", err)
			bad = true
		} else {
			fmt.Println("verified release:", manifest.Build)
		}
	}
	if _, err := exec.LookPath("tmux"); err == nil {
		r, e := process.Capture(ctx, 5*time.Second, 1<<20, "tmux", "-V")
		if e == nil {
			fmt.Println("tmux version:", strings.TrimSpace(string(r.Stdout)))
		}
		serverVersion, serverErr := tmux.Run(ctx, "display-message", "-p", "#{version}")
		if serverErr == nil {
			fmt.Println("tmux server version:", serverVersion)
			if err := tmux.RequireLocalVersion(serverVersion); err != nil {
				fmt.Println(err)
				bad = true
			}
		} else {
			fmt.Println("tmux server version: unavailable")
			if os.Getenv("TMUX") != "" || e != nil || r.Code != 0 || tmux.RequireLocalVersion(strings.TrimPrefix(strings.TrimSpace(string(r.Stdout)), "tmux ")) != nil {
				bad = true
			}
		}
		commands, e := process.Capture(ctx, 5*time.Second, 1<<20, "tmux", "list-commands")
		if e != nil || commands.Code != 0 {
			fmt.Println("tmux capabilities: cannot list commands")
			bad = true
		} else {
			for _, command := range []string{"choose-tree", "display-popup"} {
				if !strings.Contains(string(commands.Stdout), command+" ") {
					fmt.Println("tmux missing required command:", command)
					bad = true
				}
			}
			fmt.Printf("tmux native fuzzy search: %t (fzf search available separately)\n", strings.Contains(string(commands.Stdout), "switch-mode "))
		}
	}
	timing("local dependency probes", started)
	if _, err := exec.LookPath("fzf"); err == nil {
		versionResult, e := process.Capture(ctx, 5*time.Second, 1<<20, "fzf", "--version")
		if e == nil {
			fmt.Println("fzf version:", strings.TrimSpace(string(versionResult.Stdout)))
		}
		help, e := process.Capture(ctx, 5*time.Second, 1<<20, "fzf", "--help")
		if e != nil || help.Code != 0 {
			bad = true
		} else {
			for _, option := range []string{"--track", "--listen", "--read0", "--no-input", "--footer"} {
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
	localStarted := time.Now()
	local, e := inventory.Local(ctx)
	timing("local inventory", localStarted)
	if e != nil {
		fmt.Println("local:", e)
	} else {
		fmt.Printf("local sessions: %d\n", len(local))
	}
	for _, h := range c.Hosts {
		if !h.Enabled {
			continue
		}
		remoteStarted := time.Now()
		rows, e := inventory.Remote(ctx, h, c)
		timing("host "+h.ID+" observation including SSH", remoteStarted)
		if e != nil {
			fmt.Printf("%s: %s\n", h.ID, e)
			bad = true
		} else {
			fmt.Printf("%s: %d sessions\n", h.ID, len(rows))
			if count := native.WindowMirrorCount(rows); count > native.WindowMirrorLimit && tmux.Option(ctx, "", "@tmux-roam-native-windows") == "on" {
				fmt.Printf("%s: %d remote windows exceed native mirror limit %d; showing session entries\n", h.ID, count, native.WindowMirrorLimit)
			}
		}
	}
	if bad || ctx.Err() != nil {
		return 2
	}
	return 0
}
