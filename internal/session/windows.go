package session

import (
	"context"
	"fmt"
	"strconv"
	"strings"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

type mirror struct{ Window, Index, Pane, Owner, Remote, Original, Reserved, Renumber string }

func windows(ctx context.Context, sid string) []mirror {
	out, _ := tmux.Run(ctx, "list-windows", "-t", sid, "-F", "#{window_id}|#{window_index}|#{pane_id}|#{@tmux-roam-window-owner}|#{@tmux-roam-window-id}|#{@tmux-roam-carrier-index}|#{@tmux-roam-carrier-reserved}|#{@tmux-roam-windows-renumber}")
	var result []mirror
	for _, line := range strings.Split(out, "\n") {
		p := strings.SplitN(line, "|", 8)
		if len(p) == 8 {
			result = append(result, mirror{p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7]})
		}
	}
	return result
}
func windowOption(ctx context.Context, target, name string) string {
	out, _ := tmux.Run(ctx, "show-options", "-wqv", "-t", target, name)
	return out
}
func removeWindow(ctx context.Context, sid string, entry mirror) error {
	guard := tmux.Guard([][2]string{{"session_id", sid}, {"window_id", entry.Window}, {"pane_id", entry.Pane}, {"window_panes", "1"}, {"window_linked", "0"}, {"pane_dead", "1"}, {"@tmux-roam-window-owner", entry.Owner}, {"@tmux-roam-window-id", entry.Remote}})
	for _, extra := range []string{"#{==:#{window_name},#{@tmux-roam-window-name}}", "#{==:#{pane_start_command},#{@tmux-roam-window-command}}", "#{==:#{pane_dead_time},#{@tmux-roam-window-exit}}", "#{||:#{==:#{session_attached},0},#{==:#{window_active},0}}"} {
		guard = "#{&&:" + guard + "," + extra + "}"
	}
	_, err := tmux.Run(ctx, "if-shell", "-F", "-t", entry.Pane, guard, "kill-window -t "+entry.Window)
	return err
}
func RemoveWindows(ctx context.Context, sid string) error {
	entries := windows(ctx, sid)
	changed := false
	for _, entry := range entries {
		if entry.Owner != "" && strings.HasPrefix(entry.Remote, "@") {
			if err := removeWindow(ctx, sid, entry); err != nil {
				return err
			}
			changed = true
		}
	}
	if changed {
		entries = windows(ctx, sid)
	}
	occupied := map[string]bool{}
	for _, entry := range entries {
		occupied[entry.Index] = true
	}
	hasMirrors := false
	previous := ""
	for _, entry := range entries {
		previous = entry.Renumber
		if entry.Owner != "" {
			hasMirrors = true
		}
		original := entry.Original
		if original == "" {
			continue
		}
		_, _ = tmux.Run(ctx, "set-option", "-wu", "-t", entry.Window, "@tmux-roam-window-carrier")
		reserved := entry.Reserved
		if entry.Index == reserved && occupied[original] {
			continue
		}
		if entry.Index == reserved && original != entry.Index {
			_, _ = tmux.Run(ctx, "move-window", "-s", sid+":"+entry.Window, "-t", sid+":"+original)
		}
		for _, name := range []string{"@tmux-roam-carrier-index", "@tmux-roam-carrier-reserved"} {
			_, _ = tmux.Run(ctx, "set-option", "-wu", "-t", entry.Window, name)
		}
	}
	if previous != "" && !hasMirrors {
		args := []string{"set-option", "-t", sid, "renumber-windows", previous}
		if previous == "inherit" {
			args = []string{"set-option", "-u", "-t", sid, "renumber-windows"}
		}
		_, _ = tmux.Run(ctx, args...)
		_, _ = tmux.Run(ctx, "set-option", "-u", "-t", sid, "@tmux-roam-windows-renumber")
	}
	return nil
}
func SyncWindows(ctx context.Context, sid string, r model.Record) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if tmux.Option(ctx, "", "@tmux-roam-native-windows") != "on" {
		return RemoveWindows(ctx, sid)
	}
	if len(r.WindowDetails) == 0 {
		return nil
	}
	entries := windows(ctx, sid)
	var carriers []mirror
	for _, entry := range entries {
		if entry.Owner == "" {
			carriers = append(carriers, entry)
		}
	}
	if len(carriers) != 1 {
		return nil
	}
	carrier := carriers[0]
	if windowOption(ctx, carrier.Window, "@tmux-roam-carrier-index") == "" {
		if err := func() error {
			// Finish the local index reservation before honoring picker closure.
			ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 200*time.Millisecond)
			defer cancel()
			reserved := 999999
			for _, w := range r.WindowDetails {
				index := intValue(w["index"])
				if index >= reserved {
					reserved = index + 1
				}
			}
			previous := tmux.Option(ctx, sid, "renumber-windows")
			if previous == "" {
				previous = "inherit"
			}
			_, err := tmux.Run(ctx,
				"set-option", "-t", sid, "@tmux-roam-windows-renumber", previous,
				";", "set-option", "-t", sid, "renumber-windows", "off",
				";", "set-option", "-w", "-t", carrier.Window, "@tmux-roam-carrier-index", carrier.Index,
				";", "set-option", "-w", "-t", carrier.Window, "@tmux-roam-carrier-reserved", strconv.Itoa(reserved),
				";", "move-window", "-s", sid+":"+carrier.Window, "-t", sid+":"+strconv.Itoa(reserved))
			if err != nil {
				return err
			}
			return nil
		}(); err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
	}
	wanted := map[string]map[string]any{}
	for _, w := range r.WindowDetails {
		wanted[fmt.Sprint(w["id"])] = w
	}
	for _, entry := range entries {
		w, ok := wanted[entry.Remote]
		if entry.Owner != "" && (entry.Owner != r.ID || !ok || entry.Index != strconv.Itoa(intValue(w["index"]))) {
			_ = removeWindow(ctx, sid, entry)
		}
	}
	entries = windows(ctx, sid)
	occupied := map[string]bool{}
	existing := map[string]mirror{}
	for _, entry := range entries {
		occupied[entry.Index] = true
		if entry.Owner == r.ID {
			existing[entry.Remote] = entry
		}
	}
	var updates []string
	for _, w := range r.WindowDetails {
		if err := ctx.Err(); err != nil {
			return err
		}
		remote := fmt.Sprint(w["id"])
		name := strings.ReplaceAll(tmux.Clean(fmt.Sprint(w["name"])), "#", "＃")
		entry, ok := existing[remote]
		if !ok {
			index := strconv.Itoa(intValue(w["index"]))
			if occupied[index] {
				continue
			}
			if err := func() error {
				// A cancelled response must not leave an unowned live mirror.
				ctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 200*time.Millisecond)
				defer cancel()
				out, err := tmux.Run(ctx, "new-window", "-d", "-P", "-F", "#{window_id}|#{pane_id}", "-t", sid+":"+index, "-n", name, "/bin/sleep", "infinity")
				if err != nil {
					return err
				}
				p := strings.SplitN(out, "|", 2)
				if len(p) != 2 {
					return fmt.Errorf("invalid mirror creation response")
				}
				entry = mirror{Window: p[0], Pane: p[1]}
				// Publish this mirror's settings in one bounded command queue,
				// rather than launching a tmux client for every rendered field.
				var setup []string
				for _, setting := range [][2]string{{"@tmux-roam-window-owner", r.ID}, {"@tmux-roam-window-id", remote}, {"@tmux-roam-window-name", name}, {"remain-on-exit", "on"}, {"remain-on-exit-format", ""}, {"automatic-rename", "off"}} {
					if len(setup) > 0 {
						setup = append(setup, ";")
					}
					setup = append(setup, "set-option", "-w", "-t", entry.Window, setting[0], setting[1])
				}
				setup = append(setup, ";", "respawn-pane", "-k", "-t", entry.Pane, "/usr/bin/printf", "%s", "Remote window\n\nSelect this window to open its terminal.\n", ";", "set-option", "-wF", "-t", entry.Window, "@tmux-roam-window-command", "#{pane_start_command}")
				_, err = tmux.Run(ctx, setup...)
				if err != nil {
					return err
				}
				var stamp string
				for range 20 {
					stamp, _ = tmux.Run(ctx, "display-message", "-p", "-t", entry.Pane, "#{pane_dead_time}")
					if stamp != "" {
						break
					}
					select {
					case <-ctx.Done():
						return ctx.Err()
					case <-time.After(10 * time.Millisecond):
					}
				}
				_, err = tmux.Run(ctx, "set-option", "-w", "-t", entry.Window, "@tmux-roam-window-exit", stamp)
				return err
			}(); err != nil {
				return err
			}
			if err := ctx.Err(); err != nil {
				return err
			}
			occupied[index] = true
		} else {
			if len(updates) > 0 {
				updates = append(updates, ";")
			}
			guard := "#{&&:#{==:#{window_name},#{@tmux-roam-window-name}},#{!:" + tmux.Guard([][2]string{{"window_name", name}}) + "}}"
			rename := ssh.Join([]string{"rename-window", "-t", entry.Window, name}) + " ; " + ssh.Join([]string{"set-option", "-w", "-t", entry.Window, "@tmux-roam-window-name", name})
			updates = append(updates, "if-shell", "-F", "-t", entry.Window, guard, rename)
		}
		active := ""
		if w["active"] == true {
			active = "1"
		}
		if len(updates) > 0 {
			updates = append(updates, ";")
		}
		guard := "#{!:" + tmux.Guard([][2]string{{"@tmux-roam-window-active", active}}) + "}"
		updates = append(updates, "if-shell", "-F", "-t", entry.Window, guard, ssh.Join([]string{"set-option", "-w", "-t", entry.Window, "@tmux-roam-window-active", active}))
	}
	if len(updates) > 0 {
		updates = append(updates, ";")
	}
	guard := "#{!:" + tmux.Guard([][2]string{{"@tmux-roam-window-carrier", "1"}}) + "}"
	updates = append(updates, "if-shell", "-F", "-t", carrier.Window, guard, ssh.Join([]string{"set-option", "-w", "-t", carrier.Window, "@tmux-roam-window-carrier", "1"}))
	_, err := tmux.Run(ctx, updates...)
	return err
}
func intValue(value any) int {
	switch n := value.(type) {
	case int:
		return n
	case float64:
		return int(n)
	default:
		return 0
	}
}
