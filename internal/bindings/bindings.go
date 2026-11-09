package bindings

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/native"
	"code.talos.lowlife.us/baza/tmux-roam/internal/session"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

type entry struct {
	Table     string `json:"table"`
	Key       string `json:"key"`
	Original  string `json:"original"`
	Installed string `json:"installed"`
}
type state struct {
	Bindings []entry `json:"bindings"`
}

func statePath(ctx context.Context) (string, error) {
	generation, err := tmux.Run(ctx, "display-message", "-p", "#{pid}|#{start_time}|#{socket_path}")
	if err != nil {
		return "", err
	}
	digest := sha256.Sum256([]byte(generation))
	return filepath.Join(store.RuntimeDir(), "bindings-"+hex.EncodeToString(digest[:])[:24]+".json"), nil
}
func current(ctx context.Context, table, key string) string {
	out, _ := tmux.Run(ctx, "list-keys", "-T", table)
	for _, line := range strings.Split(out, "\n") {
		f := strings.Fields(line)
		i := 1
		if len(f) > 1 && f[1] == "-r" {
			i++
		}
		if len(f) > i+2 && f[i] == "-T" && f[i+1] == table && f[i+2] == key {
			return line
		}
	}
	return ""
}

var alignment = regexp.MustCompile(`^(bind-key\s+(?:-r\s+)?-T\s+\S+\s+\S+)\s+`)

func canonical(line string) string {
	return alignment.ReplaceAllStringFunc(line, func(prefix string) string { return strings.Join(strings.Fields(prefix), " ") + " " })
}
func restore(ctx context.Context, item entry) (bool, error) {
	if canonical(current(ctx, item.Table, item.Key)) != canonical(item.Installed) {
		return false, nil
	}
	if item.Original == "" {
		_, err := tmux.Run(ctx, "unbind-key", "-T", item.Table, item.Key)
		return err == nil, err
	}
	if err := store.PrivateDir(store.RuntimeDir()); err != nil {
		return false, err
	}
	f, err := os.CreateTemp(store.RuntimeDir(), "restore-*.tmux")
	if err != nil {
		return false, err
	}
	defer os.Remove(f.Name())
	_, err = f.WriteString(item.Original + "\n")
	f.Close()
	if err != nil {
		return false, err
	}
	_, err = tmux.Run(ctx, "source-file", f.Name())
	return err == nil, err
}
func option(ctx context.Context, name, fallback string) string {
	value := tmux.Option(ctx, "", name)
	if value == "" {
		return fallback
	}
	return value
}
func keys(ctx context.Context, name, fallback string) ([]string, error) {
	value := strings.Fields(option(ctx, name, fallback))
	if len(value) == 0 || len(value) > 8 {
		return nil, errors.New("invalid " + name)
	}
	for _, key := range value {
		if strings.ContainsAny(key, "\r\n\t") {
			return nil, errors.New("invalid " + name)
		}
	}
	return value, nil
}
func Install(ctx context.Context, c config.Config, binary string) (int, error) {
	path, err := statePath(ctx)
	if err != nil {
		return 0, err
	}
	release, err := store.Acquire(ctx, path+".lock")
	if err != nil {
		return 0, err
	}
	defer release()
	if option(ctx, "@tmux-roam-native", "off") != "on" || option(ctx, "@tmux-roam-native-windows", "off") != "on" {
		for _, carrier := range session.Carriers(ctx) {
			if err := session.RemoveWindows(ctx, carrier.Session); err != nil {
				return 0, err
			}
		}
	}
	old := state{}
	if err = store.ReadJSON(path, 1<<20, &old); err != nil && !os.IsNotExist(err) {
		return 0, err
	}
	remembered := map[string]entry{}
	for _, item := range old.Bindings {
		remembered[item.Table+"\x00"+item.Key] = item
	}
	chooser, err := keys(ctx, "@tmux-roam-chooser-keys", "s ы")
	if err != nil {
		return 0, err
	}
	nativeKeys, err := keys(ctx, "@tmux-roam-native-keys", "S Ы")
	if err != nil {
		return 0, err
	}
	fuzzy := strings.Fields(option(ctx, "@tmux-roam-native-fuzzy-keys", ""))
	fuzzySet := map[string]bool{}
	for _, key := range fuzzy {
		fuzzySet[key] = true
	}
	if option(ctx, "@tmux-roam-native-search", "native") != "fzf" {
		commands, _ := tmux.Run(ctx, "list-commands")
		if !strings.Contains(commands, "switch-mode ") {
			fuzzySet = map[string]bool{}
		}
	}
	popup := native.Popup(c, binary, "popup")
	nativeCommands := map[string][]string{}
	enabled := option(ctx, "@tmux-roam-native", "off") == "on"
	var desired []native.KeyBinding
	for _, key := range chooser {
		desired = append(desired, native.KeyBinding{Table: "prefix", Key: key, Command: popup})
	}
	for _, key := range nativeKeys {
		cmd := []string{"choose-tree", "-Zs"}
		if enabled {
			cmd = native.Binding(c, binary, "", "", false, fuzzySet[key], true)
		}
		nativeCommands[key] = cmd
		desired = append(desired, native.KeyBinding{Table: "prefix", Key: key, Command: cmd})
	}
	for _, key := range []string{"L", "Д"} {
		desired = append(desired, native.KeyBinding{Table: "prefix", Key: key, Command: []string{"switch-client", "-l"}})
	}
	extra, err := keys(ctx, "@tmux-roam-extra-tables", "off-prefix")
	if err != nil {
		return 0, err
	}
	for _, table := range extra {
		out, _ := tmux.Run(ctx, "list-keys", "-T", table)
		if out == "" {
			continue
		}
		for _, key := range chooser {
			desired = append(desired, native.KeyBinding{Table: table, Key: key, Command: popup})
		}
		for _, key := range []string{"L", "Д"} {
			desired = append(desired, native.KeyBinding{Table: table, Key: key, Command: []string{"switch-client", "-l"}})
		}
	}
	for _, pair := range [][2]string{{"cb", "C-b"}, {"ma", "M-a"}, {"none", ""}} {
		root, routed := "roam-root-"+pair[0], "roam-prefix-"+pair[0]
		prefix := pair[1]
		for _, key := range []string{"M-a", "M-ф"} {
			desired = append(desired, native.KeyBinding{Table: root, Key: key, Command: []string{"switch-client", "-T", routed}})
			cmd := []string{"switch-client", "-T", root}
			if prefix != "" {
				cmd = []string{"send-keys", prefix, prefix}
			}
			desired = append(desired, native.KeyBinding{Table: routed, Key: key, Command: cmd})
		}
		if prefix != "" {
			desired = append(desired, native.KeyBinding{Table: routed, Key: "F12", Command: []string{"send-keys", prefix, "\\;", "switch-client", "-T", "roam-escape"}})
		}
		desired = append(desired, native.KeyBinding{Table: root, Key: "Any", Command: []string{"if-shell", "-F", "#{mouse_pane}", "send-keys -M", "send-keys"}})
		for _, key := range []string{"L", "Д"} {
			desired = append(desired, native.KeyBinding{Table: routed, Key: key, Command: []string{"switch-client", "-l"}})
		}
		for _, key := range chooser {
			desired = append(desired, native.KeyBinding{Table: routed, Key: key, Command: popup})
		}
		for _, key := range nativeKeys {
			desired = append(desired, native.KeyBinding{Table: routed, Key: key, Command: nativeCommands[key]})
		}
		cmd := []string{"send-keys"}
		if prefix != "" {
			cmd = []string{"send-keys", prefix, "\\;", "send-keys"}
		}
		desired = append(desired, native.KeyBinding{Table: routed, Key: "Any", Command: cmd})
	}
	desired = append(desired, native.KeyBinding{Table: "roam-escape", Key: "Any", Command: []string{"send-keys"}})
	if len(fuzzySet) > 0 && enabled {
		desired = append(desired, native.NavigationBindings(ctx, c, binary)...)
	}
	wanted := map[string]bool{}
	for _, item := range desired {
		wanted[item.Table+"\x00"+item.Key] = true
	}
	for pair, item := range remembered {
		if !wanted[pair] {
			if _, err := restore(ctx, item); err != nil {
				return 0, err
			}
			delete(remembered, pair)
		}
	}
	count := 0
	for _, binding := range desired {
		pair := binding.Table + "\x00" + binding.Key
		previous := current(ctx, binding.Table, binding.Key)
		item, exists := remembered[pair]
		if !exists {
			item = entry{Table: binding.Table, Key: binding.Key, Original: previous}
		} else if canonical(previous) != canonical(item.Installed) {
			continue
		}
		if _, err = tmux.Run(ctx, append([]string{"bind-key", "-T", binding.Table, binding.Key}, binding.Command...)...); err != nil {
			return count, err
		}
		item.Installed = current(ctx, binding.Table, binding.Key)
		remembered[pair] = item
		count++
	}
	next := state{}
	for _, item := range remembered {
		next.Bindings = append(next.Bindings, item)
	}
	return count, store.WriteJSON(ctx, path, next)
}
func Unload(ctx context.Context) (int, error) {
	path, err := statePath(ctx)
	if err != nil {
		return 0, err
	}
	release, err := store.Acquire(ctx, path+".lock")
	if err != nil {
		return 0, err
	}
	defer release()
	var saved state
	if err = store.ReadJSON(path, 1<<20, &saved); os.IsNotExist(err) {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	count := 0
	for _, item := range saved.Bindings {
		changed, err := restore(ctx, item)
		if err != nil {
			return count, err
		}
		if changed {
			count++
		}
	}
	if err = os.Remove(path); err != nil {
		return count, err
	}
	for _, carrier := range session.Carriers(ctx) {
		if carrier.State.Status == session.Idle {
			if _, err = session.Disconnect(ctx, carrier); err != nil {
				return count, err
			}
		}
	}
	return count, nil
}
