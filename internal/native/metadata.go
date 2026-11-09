package native

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/session"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func Hint(r model.Record, status string) string {
	if strings.Contains(status, "unavailable") {
		age := max(0, int(time.Since(time.Unix(0, int64(r.Observed*1e9))).Seconds()))
		seen := "just now"
		switch {
		case age >= 86400:
			seen = fmt.Sprintf("%dd ago", age/86400)
		case age >= 3600:
			seen = fmt.Sprintf("%dh ago", age/3600)
		case age >= 60:
			seen = fmt.Sprintf("%dm ago", age/60)
		}
		return "Unavailable · seen " + seen
	}
	if value := map[string]string{"connected": "Connected", "connecting": "Connecting…", "disconnected": "Reconnect", "available": "Ready", "cached": "Checking…", "ended": "Ended"}[status]; value != "" {
		return value
	}
	return "Checking…"
}

// StatusColour is shared by native metadata and manager rows. Resolve it once
// when publishing metadata, rather than nesting status tests in every redraw.
func StatusColour(label string) string {
	status := strings.TrimSuffix(strings.SplitN(label, " ", 2)[0], "…")
	if colour := map[string]string{"Connected": "#a6da95", "Reconnect": "#eed49f", "Checking": "#8aadf4", "Connecting": "#8aadf4", "Ready": "#939ab7", "Current": "#cad3f5", "Unavailable": "#8087a2", "Offline": "#8087a2", "Ended": "#8087a2"}[status]; colour != "" {
		return colour
	}
	return "#939ab7"
}

var colour = regexp.MustCompile(`^#[0-9a-fA-F]{6}$`)

// Larger inventories retain session entries; optional window mirrors exceeded
// the bounded setup/cancellation budget in the 500-session stress workload.
const WindowMirrorLimit = 100

func WindowMirrorCount(rows []model.Record) int {
	count := 0
	for _, r := range rows {
		count += len(r.WindowDetails)
	}
	return count
}

func Decorate(ctx context.Context, sid string, r model.Record, status string) error {
	label := strings.ReplaceAll(tmux.Clean(r.HostLabel+" / "+r.Name), "#", "＃")
	current, err := tmux.Run(ctx, "display-message", "-p", "-t", sid, "#{session_name}")
	if err != nil {
		return err
	}
	previous := tmux.Option(ctx, sid, "@tmux-roam-name")
	if current == previous || previous == "" && strings.HasPrefix(current, "roam-") {
		base := strings.NewReplacer(".", "_", ":", "_").Replace(label)
		names := map[string]bool{}
		all, _ := tmux.Run(ctx, "list-sessions", "-F", "#{session_id}|#{session_name}")
		for _, line := range strings.Split(all, "\n") {
			p := strings.SplitN(line, "|", 2)
			if len(p) == 2 && p[0] != sid {
				names[p[1]] = true
			}
		}
		name := base
		for number := 2; names[name]; number++ {
			name = fmt.Sprintf("%s (%d)", base, number)
		}
		if current != name {
			if _, err := tmux.Run(ctx, "rename-session", "-t", sid, name); err != nil {
				return err
			}
		}
		if previous != name {
			_, _ = tmux.Run(ctx, "set-option", "-t", sid, "@tmux-roam-name", name)
		}
	}
	accent := tmux.Option(ctx, "", "@tmux-roam-native-colour-"+r.HostID)
	if !colour.MatchString(accent) {
		accent = "#8aadf4"
	}
	raw, _ := json.Marshal(r)
	var record map[string]any
	_ = json.Unmarshal(raw, &record)
	delete(record, "window_details")
	raw, _ = json.Marshal(record)
	var updates []string
	for _, setting := range [][2]string{{"@tmux-roam-record", string(raw)}, {"@tmux-roam-label", label}, {"@tmux-roam-windows", strconv.Itoa(r.Windows)}, {"@tmux-roam-status", status}, {"@tmux-roam-hint", Hint(r, status)}, {"@tmux-roam-status-colour", StatusColour(Hint(r, status))}, {"@tmux-roam-accent", accent}} {
		old := ""
		if setting[0] == "@tmux-roam-record" {
			old = tmux.Option(ctx, sid, setting[0])
		}
		if setting[0] == "@tmux-roam-record" && old != "" {
			var previous map[string]any
			if json.Unmarshal([]byte(old), &previous) == nil {
				for _, field := range []string{"observed", "source", "state"} {
					delete(previous, field)
					delete(record, field)
				}
				a, _ := json.Marshal(previous)
				b, _ := json.Marshal(record)
				if string(a) == string(b) {
					continue
				}
			}
		}
		if len(updates) > 0 {
			updates = append(updates, ";")
		}
		guard := "#{!:" + tmux.Guard([][2]string{setting}) + "}"
		updates = append(updates, "if-shell", "-F", "-t", sid, guard, ssh.Join([]string{"set-option", "-t", sid, setting[0], setting[1]}))
	}
	_, err = tmux.Run(ctx, updates...)
	return err
}
func placeholder(ctx context.Context, r model.Record) (string, error) {
	if err := ctx.Err(); err != nil {
		return "", err
	}
	// Finish this small local transaction even if the picker closes. Otherwise
	// cancellation between creation and ownership publication exposes an orphan
	// as an ordinary local session and leaves it unsafe to reuse or remove.
	initializing, cancel := context.WithTimeout(context.WithoutCancel(ctx), 200*time.Millisecond)
	defer cancel()
	token := session.Token()
	name := "roam-entry-" + token[:12]
	target := name + ":"
	preview := tmux.Clean(r.HostLabel+" / "+r.Name) + "\n\nRemote session available.\nSelect this entry in the session picker to connect.\nThe terminal preview appears after connecting.\n"
	args := []string{"new-session", "-d", "-P", "-F", "#{session_id}|#{pane_id}", "-s", name, "-n", "remote", "/bin/sleep", "infinity"}
	for _, setting := range [][2]string{{session.Owner, "1"}, {session.Identity, r.ID}, {session.StateOption, token}} {
		args = append(args, ";", "set-option", "-t", name, setting[0], setting[1])
	}
	args = append(args, ";", "set-option", "-w", "-t", target, "remain-on-exit", "on", ";", "set-option", "-w", "-t", target, "remain-on-exit-format", "", ";", "respawn-pane", "-k", "-t", target, "/usr/bin/printf", "%s", preview, ";", "display-message", "-p", "-t", target, "#{pane_start_command}")
	out, err := tmux.Run(initializing, args...)
	if err != nil {
		return "", err
	}
	identity, command, ok := strings.Cut(out, "\n")
	p := strings.SplitN(identity, "|", 2)
	if len(p) != 2 || !ok {
		return "", fmt.Errorf("invalid placeholder identity")
	}
	sid, pane := p[0], p[1]
	if err = session.WriteState(initializing, token, session.State{Status: session.Idle, Pane: pane, Command: command}); err != nil {
		return sid, err
	}
	return sid, ctx.Err()
}
func view(ctx context.Context) string {
	out, _ := tmux.Run(ctx, "list-windows", "-a", "-F", "#{session_id}|#{session_name}|#{@tmux-roam-windows}|#{@tmux-roam-status}|#{@tmux-roam-hint}|#{@tmux-roam-accent}|#{window_id}|#{window_name}|#{@tmux-roam-window-active}")
	return out
}
func SyncHost(ctx context.Context, h config.Host, rows []model.Record, fresh bool, failure error) (bool, error) {
	server, err := tmux.Run(ctx, "display-message", "-p", "#{pid}|#{start_time}|#{socket_path}\n#{version}")
	if err != nil {
		return false, err
	}
	generation, version, _ := strings.Cut(server, "\n")
	if err := tmux.RequireLocalVersion(version); err != nil {
		return false, err
	}
	digest := sha256.Sum256([]byte(generation))
	release, err := store.Acquire(ctx, filepath.Join(store.RuntimeDir(), "native-"+hex.EncodeToString(digest[:])[:24]+".lock"))
	if err != nil {
		return false, err
	}
	defer release()
	before := view(ctx)
	limited := WindowMirrorCount(rows) > WindowMirrorLimit
	present, healthy := map[string]bool{}, map[string]bool{}
	for _, r := range rows {
		present[r.ID] = true
		healthy[r.ID] = strings.HasPrefix(r.State, "open here")
	}
	if fresh && failure == nil {
		session.Cleanup(ctx, h.ID, present, healthy)
	}
	carriers := session.Carriers(ctx)
	indexed := map[string][]session.Carrier{}
	for _, carrier := range carriers {
		indexed[carrier.ID] = append(indexed[carrier.ID], carrier)
	}
	for _, r := range rows {
		owned := indexed[r.ID]
		if len(owned) == 0 {
			sid, err := placeholder(ctx, r)
			if err != nil {
				return false, err
			}
			owned = []session.Carrier{{Session: sid, ID: r.ID, Token: tmux.Option(ctx, sid, session.StateOption)}}
		}
		for _, carrier := range owned {
			if !fresh && failure == nil && tmux.Option(ctx, carrier.Session, "@tmux-roam-record") != "" {
				guard := "#{!:" + tmux.Guard([][2]string{{"@tmux-roam-hint", "Checking…"}, {"@tmux-roam-status-colour", StatusColour("Checking")}}) + "}"
				_, _ = tmux.Run(ctx, "if-shell", "-F", "-t", carrier.Session, guard, ssh.Join([]string{"set-option", "-t", carrier.Session, "@tmux-roam-hint", "Checking…"})+" ; "+ssh.Join([]string{"set-option", "-t", carrier.Session, "@tmux-roam-status-colour", StatusColour("Checking")}))
			} else {
				state := session.ReadState(carrier.Token)
				status := "cached"
				if fresh {
					status = "available"
				}
				switch state.Status {
				case session.Open:
					status = "connected"
				case session.Preparing, session.Attaching:
					status = "connecting"
				case session.Failed, session.Disconnected:
					status = "disconnected"
				}
				if failure != nil {
					status += " · host unavailable"
				} else if fresh && state.Status == session.Open && !healthy[r.ID] {
					status = "disconnected"
				}
				if err := Decorate(ctx, carrier.Session, r, status); err != nil {
					return false, err
				}
			}
			var err error
			if limited {
				err = session.RemoveWindows(ctx, carrier.Session)
			} else {
				err = session.SyncWindows(ctx, carrier.Session, r)
			}
			if err != nil {
				return false, err
			}
		}
	}
	if fresh || failure != nil {
		for _, carrier := range session.Carriers(ctx) {
			identity, err := model.Decode(carrier.ID)
			if err != nil || identity.Host != h.ID || present[carrier.ID] {
				continue
			}
			status, hint := "ended", "Ended"
			if failure != nil {
				status, hint = "host unavailable", "Unavailable"
			}
			for _, setting := range [][2]string{{"@tmux-roam-status", status}, {"@tmux-roam-hint", hint}, {"@tmux-roam-status-colour", StatusColour(hint)}} {
				if tmux.Option(ctx, carrier.Session, setting[0]) != setting[1] {
					_, _ = tmux.Run(ctx, "set-option", "-t", carrier.Session, setting[0], setting[1])
				}
			}
		}
	}
	return before != view(ctx), ctx.Err()
}
