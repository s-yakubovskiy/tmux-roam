package picker

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/session"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
)

func menu(ctx context.Context, prompt, header string, items []string) (string, error) {
	args := append([]string{"--no-sort", "--header=" + header}, Options(ctx, prompt, true, false, "", 96)...)
	child := exec.CommandContext(ctx, "fzf", args...)
	child.Stdin = strings.NewReader(strings.Join(items, "\n") + "\n")
	child.Stderr = os.Stderr
	child.Env = environment()
	output, err := child.Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(output)), nil
}
func input(ctx context.Context, prompt string) (string, error) {
	args := append([]string{"--disabled", "--height=8"}, Options(ctx, prompt, false, false, "", 96)...)
	args = append(args, "--bind=enter:print-query,esc:abort")
	child := exec.CommandContext(ctx, "fzf", args...)
	child.Stdin = strings.NewReader("")
	child.Stderr = os.Stderr
	child.Env = environment()
	output, err := child.Output()
	return strings.TrimRight(string(output), "\r\n"), err
}
func selected(root, id string) (model.Record, error) {
	if _, err := os.Stat(filepath.Join(root, "reload-selection")); err == nil {
		return model.Record{}, errors.New("list is refreshing; choose the session again when it finishes")
	}
	data, err := readMetadata(root)
	if err != nil {
		return model.Record{}, err
	}
	for _, r := range data.Records {
		if r.ID == id && r.Source != "ended" {
			return r, nil
		}
	}
	return model.Record{}, errors.New("session is no longer available; refresh the picker")
}
func NewSession(ctx context.Context, c config.Config, root, id string) error {
	preferred := strings.TrimPrefix(id, "error:")
	if r, err := selected(root, id); err == nil {
		preferred = r.HostID
	}
	var hosts []config.Host
	for _, h := range c.Hosts {
		if h.Enabled {
			hosts = append(hosts, h)
		}
	}
	sort.SliceStable(hosts, func(i, j int) bool { return hosts[i].ID == preferred && hosts[j].ID != preferred })
	if len(hosts) == 0 {
		return errors.New("no enabled hosts configured")
	}
	var items []string
	for _, h := range hosts {
		items = append(items, h.ID+"\t"+h.Label)
	}
	choice, err := menu(ctx, "Create on host", "Choose a host for the new session", items)
	if err != nil {
		return nil
	}
	var host config.Host
	for _, h := range hosts {
		if h.ID == strings.SplitN(choice, "\t", 2)[0] {
			host = h
		}
	}
	name, err := input(ctx, "Session name (empty cancels)")
	name = strings.TrimSpace(name)
	if err != nil || name == "" {
		return nil
	}
	directory, err := input(ctx, "Remote project directory [~]")
	if err != nil {
		return nil
	}
	record, err := session.Create(ctx, host, c, name, strings.TrimSpace(directory))
	if err != nil {
		return err
	}
	if err = store.WriteJSONTransient(ctx, filepath.Join(root, "created.json"), record); err != nil {
		return err
	}
	return StateAction(ctx, c, root, "refresh")
}
func DeleteSession(ctx context.Context, c config.Config, root, id string) error {
	r, err := selected(root, id)
	if err != nil {
		return err
	}
	header := fmt.Sprintf("Delete session?\n\n%s / %s\n\nCloses all %d windows and stops their programs.\n ", r.HostLabel, r.Name, r.Windows)
	choice, err := menu(ctx, "Confirm deletion", header, []string{"Cancel", "Delete session"})
	if err != nil || choice != "Delete session" {
		return nil
	}
	if err = session.Delete(ctx, c, r); err != nil {
		return err
	}
	file, err := os.OpenFile(filepath.Join(root, "deleted"), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(file, id)
	file.Close()
	if err != nil {
		return err
	}
	return StateAction(ctx, c, root, "refresh")
}

// Config sync is composed by the CLI. The picker does not own
// the config-sync transport or transaction.
func Actions(ctx context.Context, c config.Config, root, id string, syncConfig func(context.Context, config.Config, string) error) error {
	choice, err := menu(ctx, "Action", "Session actions", []string{"Open/retry", "New remote session", "Delete session", "Refresh host", "Sync tmux config", "Close owned local connection", "Diagnostics"})
	if err != nil {
		return nil
	}
	switch choice {
	case "New remote session":
		return NewSession(ctx, c, root, id)
	case "Delete session":
		return DeleteSession(ctx, c, root, id)
	case "Open/retry":
		post(ctx, filepath.Join(root, "fzf.sock"), SelectAction(ctx, root, id))
		return nil
	case "Sync tmux config":
		return syncConfig(ctx, c, id)
	case "Refresh host":
		return StateAction(ctx, c, root, "refresh")
	case "Close owned local connection":
		choice, err := menu(ctx, "Disconnect", "Close an unused local connection; remote work remains", []string{"Cancel", "Close local connection"})
		if err != nil || choice != "Close local connection" {
			return nil
		}
		for _, carrier := range session.Carriers(ctx) {
			if carrier.ID == id {
				if _, err = session.Disconnect(ctx, carrier); err != nil {
					return err
				}
			}
		}
		return StateAction(ctx, c, root, "refresh")
	case "Diagnostics":
		_ = Details(root, id)
		_, err = input(ctx, "Press Enter to return")
		return err
	}
	return nil
}
func Help(ctx context.Context) {
	fmt.Println("Ctrl-o navigation · j/k move · g/G first/last · Ctrl-u/d half page\n/ or i search · Escape closes Sessions · Ctrl-n new · dd delete (navigation only)\nCtrl-r refresh · Ctrl-f scope · Ctrl-h hidden · Tab details · F2 actions\nRussian physical-key aliases work; letters remain literal while searching.")
	_, _ = input(ctx, "Press Enter to return")
}
