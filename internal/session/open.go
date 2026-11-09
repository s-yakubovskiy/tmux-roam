package session

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/inventory"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/process"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func targetLock(id string) string {
	digest := sha256.Sum256([]byte(id))
	return filepath.Join(store.RuntimeDir(), "target-"+hex.EncodeToString(digest[:])+".lock")
}
func configure(ctx context.Context, carrier Carrier, prefix string) error {
	suffix := map[string]string{"M-a": "ma", "C-b": "cb", "None": "none"}[prefix]
	if suffix == "" {
		return errors.New("unsupported remote prefix: " + prefix)
	}
	if carrier.Configuration == prefix+"|None|roam-root-"+suffix+"|off" {
		return nil
	}
	session := carrier.Session
	_, err := tmux.Run(ctx, "set-option", "-t", session, "@tmux-roam-prefix", prefix, ";", "set-option", "-t", session, "prefix", "None", ";", "set-option", "-t", session, "key-table", "roam-root-"+suffix, ";", "set-option", "-t", session, "status", "off")
	return err
}
func safeName(host, name string) string {
	name = strings.Map(func(r rune) rune {
		if unicode.IsLetter(r) || unicode.IsDigit(r) || r == '_' || r == '-' {
			return r
		}
		return '_'
	}, name)
	name = strings.Trim(name, "_")
	r := []rune(name)
	if len(r) > 32 {
		name = string(r[:32])
	}
	if name == "" {
		name = "session"
	}
	return "roam-" + host + "-" + name
}
func command(c config.Config, token, host, id string) (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	args := []string{"env", "TMUX_ROAM_CONFIG=" + c.Path, "TMUX_ROAM_SSH_CONFIG=" + os.Getenv("TMUX_ROAM_SSH_CONFIG"), exe, "supervise", token, host, id}
	return "exec " + ssh.Join(args), nil
}
func hostFor(c config.Config, id string) (config.Host, error) {
	for _, h := range c.Hosts {
		if h.ID == id && h.Enabled {
			return h, nil
		}
	}
	return config.Host{}, errors.New("host is disabled or no longer configured")
}
func OpenID(ctx context.Context, c config.Config, id, clientName, token, window string) (string, error) {
	identity, err := model.Decode(id)
	if err != nil {
		return "", err
	}
	client, err := tmux.CaptureClient(ctx, clientName)
	if err != nil {
		return "", err
	}
	if identity.Host == "local" {
		generation := strings.Split(identity.Generation, "/")
		if len(generation) != 2 || len(identity.Session) < 2 || identity.Session[0] != '$' {
			return "", errors.New("invalid local session identity")
		}
		if _, err := strconv.ParseUint(identity.Session[1:], 10, 64); err != nil {
			return "", errors.New("invalid local session identity")
		}
		// Validate the selected identity and switch in one tmux queue. Reading
		// every unrelated session adds work and leaves a validation/switch race.
		return identity.Session, client.Switch(ctx, identity.Session, token,
			[2]string{"pid", generation[0]}, [2]string{"start_time", generation[1]},
			[2]string{"socket_path", identity.Socket}, [2]string{"session_id", identity.Session},
			[2]string{"session_created", strconv.FormatInt(identity.Created, 10)},
			[2]string{"!=:#{@tmux-roam-owner},1", "1"})
	}
	if err := tmux.RequireLocalVersion(client.Version); err != nil {
		return "", err
	}
	if token != "" && client.Navigation != token {
		return "", errors.New("navigation cancelled or superseded")
	}
	h, err := hostFor(c, identity.Host)
	if err != nil {
		return "", err
	}
	snapshot, err := inventory.RemoteSnapshot(ctx, h, c, true)
	if err != nil {
		return "", err
	}
	record, ok := exact(snapshot.Records, id)
	if !ok {
		return "", errors.New("session ended or server restarted; refresh the picker")
	}
	if window != "" {
		found := false
		for _, w := range record.WindowDetails {
			if w["id"] == window {
				found = true
			}
		}
		if !found {
			return "", errors.New("remote window ended or moved")
		}
	}
	if err = inventory.SaveContext(ctx, h, snapshot.Records); err != nil {
		return "", err
	}
	prefix := h.RemotePrefix
	if prefix == "auto" {
		prefix = snapshot.Prefixes[record.SessionID]
	}
	if prefix != "M-a" && prefix != "C-b" && prefix != "None" {
		return "", errors.New("unsupported remote prefix: " + prefix)
	}
	release, err := store.Acquire(ctx, targetLock(id))
	if err != nil {
		return "", err
	}
	var candidate *Carrier
	for _, carrier := range Carriers(ctx) {
		if carrier.ID != id {
			continue
		}
		// A normal single-window carrier has no mirror state to restore.
		// Keep the guarded cleanup path for mirrors and user-modified layouts.
		if carrier.Windows != "1" || carrier.WindowsRenumber != "" {
			if err := RemoveWindows(ctx, carrier.Session); err != nil {
				release()
				return "", err
			}
		}
		state := carrier.State
		if state.Status == Open && snapshot.Clients[strconv.Itoa(state.PID)+"|"+state.TTY] == record.SessionID {
			if _, err := ManagedPane(ctx, carrier); err != nil {
				continue
			}
			copy := carrier
			candidate = &copy
			break
		}
		if candidate == nil || state.Status == Preparing || state.Status == Attaching {
			copy := carrier
			candidate = &copy
		}
	}
	created := candidate == nil
	var selected Carrier
	healthy := candidate != nil && candidate.State.Status == Open && snapshot.Clients[strconv.Itoa(candidate.State.PID)+"|"+candidate.State.TTY] == record.SessionID
	preparing := candidate != nil && (candidate.State.Status == Preparing || candidate.State.Status == Attaching)
	if created || !healthy && !preparing {
		if !client.Valid(ctx, token) {
			release()
			return "", errors.New("navigation cancelled")
		}
		selected = Carrier{ID: id, Token: Token()}
		launch, commandErr := command(c, selected.Token, h.ID, id)
		if commandErr != nil {
			release()
			return "", commandErr
		}
		if created {
			if err = WriteState(ctx, selected.Token, State{Status: Preparing, Command: launch}); err == nil {
				name := safeName(h.ID, record.Name) + "-" + selected.Token[:6]
				// Publish ownership in the creation queue, before a cancelled
				// client can leave the new carrier visible as a local project.
				selected.Session, err = tmux.Run(ctx, "new-session", "-d", "-P", "-F", "#{session_id}", "-x", client.Width, "-y", client.Height, "-s", name, launch,
					";", "set-option", "-t", name, Owner, "1", ";", "set-option", "-t", name, Identity, id, ";", "set-option", "-t", name, StateOption, selected.Token)
			}
		} else {
			selected.Session = candidate.Session
			pane, paneErr := ManagedPane(ctx, *candidate)
			if paneErr != nil {
				release()
				return "", paneErr
			}
			if err = WriteState(ctx, selected.Token, State{Status: Preparing, Command: launch}); err == nil {
				err = pane.Command(ctx, ssh.Join([]string{"respawn-pane", "-k", "-t", pane.ID, launch})+" ; "+ssh.Join([]string{"set-option", "-t", selected.Session, StateOption, selected.Token}))
			}
		}
		if err == nil {
			_, err = tmux.Run(ctx, "set-option", "-t", selected.Session, Owner, "1", ";", "set-option", "-t", selected.Session, Identity, id, ";", "set-option", "-t", selected.Session, StateOption, selected.Token)
			if created && err == nil {
				name, _ := tmux.Run(ctx, "display-message", "-p", "-t", selected.Session, "#{session_name}")
				_, err = tmux.Run(ctx, "set-option", "-t", selected.Session, "@tmux-roam-name", name)
			}
		}
	} else {
		selected = *candidate
	}
	if err == nil {
		err = configure(ctx, selected, prefix)
	}
	if err == nil && candidate != nil && candidate.Token != selected.Token {
		_ = os.Remove(statePath(candidate.Token))
	}
	var endPending func(bool)
	if err == nil && !healthy {
		endPending, err = pending(ctx, selected, created)
	}
	release()
	if err != nil {
		return "", err
	}
	success := false
	if endPending != nil {
		defer func() { endPending(success) }()
	}
	finish := func() (string, error) {
		if window != "" {
			if !client.Valid(ctx, token) {
				return "", errors.New("navigation cancelled")
			}
			if err := SelectWindow(ctx, h, c, record, window); err != nil {
				return "", err
			}
		}
		err := client.Switch(ctx, selected.Session, token)
		success = err == nil
		return selected.Session, err
	}
	if healthy {
		return finish()
	}
	deadline := time.NewTimer(time.Duration(c.CommandTimeoutSeconds+c.ConnectTimeoutSeconds+3) * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(40 * time.Millisecond)
	defer ticker.Stop()
	for {
		if !client.Valid(ctx, token) {
			return "", errors.New("navigation cancelled or client disappeared")
		}
		state := ReadState(selected.Token)
		if state.Status == Open {
			confirmed, checkErr := inventory.RemoteSnapshot(ctx, h, c, false)
			if checkErr == nil {
				if _, ok := exact(confirmed.Records, id); !ok {
					return "", errors.New("remote session ended during attachment")
				}
				if confirmed.Clients[strconv.Itoa(state.PID)+"|"+state.TTY] == record.SessionID {
					return finish()
				}
			}
		}
		if state.Status == Disconnected || state.Status == Failed {
			return "", fmt.Errorf("attachment failed: %s", state.Error)
		}
		select {
		case <-ctx.Done():
			return "", ctx.Err()
		case <-deadline.C:
			return "", errors.New("connection did not confirm exact remote attachment")
		case <-ticker.C:
		}
	}
}

func SelectWindow(ctx context.Context, h config.Host, c config.Config, r model.Record, window string) error {
	if len(window) < 2 || window[0] != '@' {
		return errors.New("invalid window ID")
	}
	if _, err := strconv.ParseUint(window[1:], 10, 64); err != nil {
		return err
	}
	generation := strings.Split(r.Generation, "/")
	if len(generation) != 2 {
		return errors.New("invalid server generation")
	}
	guard := tmux.Guard([][2]string{{"pid", generation[0]}, {"start_time", generation[1]}, {"socket_path", r.Socket}, {"session_id", r.SessionID}, {"session_created", strconv.FormatInt(r.Created, 10)}, {"window_id", window}})
	args := append(h.TmuxArgs(), "if-shell", "-F", "-t", r.SessionID+":"+window, guard, ssh.Join([]string{"select-window", "-t", r.SessionID + ":" + window}), "display-message -p TMUX_ROAM_STALE_WINDOW")
	base, err := ssh.Args(h, c, false)
	if err != nil {
		return err
	}
	result, err := process.Capture(ctx, time.Duration(c.CommandTimeoutSeconds)*time.Second, 1<<20, append(base, ssh.Join(args))...)
	if err != nil {
		return err
	}
	if result.Code != 0 || strings.Contains(string(result.Stdout), "TMUX_ROAM_STALE_WINDOW") {
		return errors.New("remote window ended or changed: " + tmux.Clean(string(result.Stderr)))
	}
	return nil
}
