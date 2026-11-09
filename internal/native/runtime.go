package native

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"sync"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/inventory"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/session"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func Refresh(parent context.Context, c config.Config, client, pane, token string) error {
	captured, err := tmux.CaptureClient(parent, client)
	if err != nil {
		return err
	}
	if err := tmux.RequireLocalVersion(captured.Version); err != nil {
		return err
	}
	ctx, cancel := context.WithCancel(parent)
	defer cancel()
	hook := fmt.Sprintf("pane-mode-changed[%d]", os.Getpid())
	changed := fmt.Sprintf("client-session-changed[%d]", os.Getpid())
	detached := fmt.Sprintf("client-detached[%d]", os.Getpid())
	channel := "roam-native-" + session.Token()
	signal := "wait-for -S " + channel
	onClient := ssh.Join([]string{"if-shell", "-F", "#{==:#{hook_client}," + tmux.EscapeFormat(client) + "}", signal})
	defer func() {
		_, _ = tmux.Run(context.Background(), "set-hook", "-pu", "-t", pane, hook, ";", "set-hook", "-gu", changed, ";", "set-hook", "-gu", detached)
	}()
	if _, err := tmux.Run(ctx, "set-hook", "-p", "-t", pane, hook, signal, ";", "set-hook", "-g", changed, onClient, ";", "set-hook", "-g", detached, onClient); err != nil {
		return err
	}
	args := append(tmux.Base(), "wait-for", channel)
	waiter := exec.CommandContext(ctx, args[0], args[1:]...)
	if err := waiter.Start(); err != nil {
		return err
	}
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); _ = waiter.Wait(); cancel() }()
	defer func() { cancel(); wg.Wait() }()
	active := func() bool {
		if ctx.Err() != nil || tmux.Option(ctx, "", "@tmux-roam-navigation-"+client) != token || ClientPane(ctx, client) != pane {
			return false
		}
		mode, _ := tmux.Run(ctx, "display-message", "-p", "-t", pane, "#{pane_mode}")
		return mode == "tree-mode" || mode == "switch-mode"
	}
	publish := func(h config.Host, rows []model.Record, fresh bool, failure error) {
		if !active() {
			cancel()
			return
		}
		changed, err := SyncHost(ctx, h, rows, fresh, failure)
		if err == nil && changed && active() {
			_, _ = tmux.Run(ctx, "refresh-client", "-t", client)
		}
	}
	if !active() {
		return nil
	}
	for _, host := range c.Hosts {
		if host.Enabled {
			publish(host, inventory.Cached(host), false, nil)
		}
	}
	for active() {
		inventory.RefreshHosts(ctx, c, true, func(result inventory.HostResult) { publish(result.Host, result.Records, result.Err == nil, result.Err) })
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(time.Duration(c.RefreshSeconds) * time.Second):
		}
	}
	return nil
}

var paneID = regexp.MustCompile(`^%[0-9]+$`)

func Select(ctx context.Context, c config.Config, client, target, token string, connect func(context.Context, config.Config, model.Record, string, string, string) (string, error)) error {
	if !paneID.MatchString(target) {
		return errors.New("invalid native target")
	}
	if tmux.Option(ctx, "", "@tmux-roam-navigation-"+client) != token {
		return nil
	}
	captured, err := tmux.CaptureClient(ctx, client)
	if err != nil {
		return err
	}
	sid, err := tmux.Run(ctx, "display-message", "-p", "-t", target, "#{session_id}")
	if err != nil {
		return err
	}
	if tmux.Option(ctx, sid, session.Owner) != "1" {
		return captured.Switch(ctx, sid, token)
	}
	defer func() {
		tmux.EndNavigation(ctx, client, token)
	}()
	var r model.Record
	if json.Unmarshal([]byte(tmux.Option(ctx, sid, "@tmux-roam-record")), &r) != nil || r.ID != tmux.Option(ctx, sid, session.Identity) {
		return errors.New("remote metadata changed or unavailable; refresh the picker")
	}
	window, _ := tmux.Run(ctx, "show-options", "-wqv", "-t", target, "@tmux-roam-window-id")
	if window != "" {
		owner, _ := tmux.Run(ctx, "show-options", "-wqv", "-t", target, "@tmux-roam-window-owner")
		if owner != r.ID {
			return errors.New("remote window changed")
		}
	}
	opened, err := connect(ctx, c, r, client, token, window)
	if err == nil && opened != "" {
		_ = Decorate(ctx, opened, r, "connected")
	}
	return err
}

// This is a user-visible native capability test, not a version-number guess.
func SupportsSearch(ctx context.Context) bool {
	commands, _ := tmux.Run(ctx, "list-commands")
	return strings.Contains(commands, "switch-mode ")
}
