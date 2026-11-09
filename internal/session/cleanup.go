package session

import (
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"context"
	"os"

	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

// Disconnect removes only a proven unused owned carrier. Remote work is never
// deleted. Renamed, linked, attached, split, or repurposed resources survive.
func Disconnect(ctx context.Context, c Carrier) (bool, error) {
	release, err := store.Acquire(ctx, targetLock(c.ID))
	if err != nil {
		return false, err
	}
	defer release()
	return removeCarrier(ctx, c)
}

func removeCarrier(ctx context.Context, c Carrier) (bool, error) {
	c.State = ReadState(c.Token)
	if c.State.Status == Preparing || c.State.Status == Attaching {
		return false, nil
	}
	if err := RemoveWindows(ctx, c.Session); err != nil {
		return false, err
	}
	p, err := ManagedPane(ctx, c)
	if err != nil {
		return false, nil
	}
	if p.Attached != "0" {
		return false, nil
	}
	name, err := tmux.Run(ctx, "display-message", "-p", "-t", c.Session, "#{session_name}")
	if err != nil {
		return false, err
	}
	ownedName := tmux.Option(ctx, c.Session, "@tmux-roam-name")
	if ownedName == "" || name != ownedName {
		return false, nil
	}
	guard := p.Guard()
	guard = "#{&&:" + guard + ",#{==:#{session_name},#{@tmux-roam-name}}}"
	_, err = tmux.Run(ctx, "if-shell", "-F", "-t", p.ID, guard, "kill-session -t "+c.Session)
	if err != nil {
		return false, err
	}
	result, err := tmux.Capture(ctx, "has-session", "-t", c.Session)
	if err == nil && result.Code != 0 {
		_ = os.Remove(statePath(c.Token))
	}
	return err == nil && result.Code != 0, err
}

func Cleanup(ctx context.Context, host string, present map[string]bool, healthy map[string]bool) {
	for _, carrier := range Carriers(ctx) {
		id, err := model.Decode(carrier.ID)
		if err != nil || id.Host != host {
			continue
		}
		ended := !present[carrier.ID]
		if !ended && !healthy[carrier.ID] {
			continue
		}
		status := carrier.State.Status
		if status != Idle && status != Failed && status != Disconnected {
			continue
		}
		_, _ = Disconnect(ctx, carrier)
	}
}
