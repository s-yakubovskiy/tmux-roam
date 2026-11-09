package config

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strings"

	"github.com/pelletier/go-toml/v2"
)

type Host struct {
	ID, Label, SSH                                 string
	Enabled                                        bool
	TmuxPath, RemotePrefix, SocketName, SocketPath string
}

func (h Host) TmuxArgs() []string {
	a := []string{h.TmuxPath}
	if h.SocketName != "" {
		a = append(a, "-L", h.SocketName)
	}
	if h.SocketPath != "" {
		a = append(a, "-S", h.SocketPath)
	}
	return a
}

type Config struct {
	Path                                                                                    string
	Hosts                                                                                   []Host
	Width, Height                                                                           string
	Details, Icons                                                                          bool
	HideLocal                                                                               []string
	CacheSeconds, RefreshSeconds, ConnectTimeoutSeconds, CommandTimeoutSeconds, Concurrency int
}

func DefaultPath() string {
	if p := os.Getenv("TMUX_ROAM_CONFIG"); p != "" {
		return p
	}
	base := os.Getenv("XDG_CONFIG_HOME")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, ".config")
	}
	return filepath.Join(base, "tmux-roam", "config.toml")
}

func Load(path string) (Config, error) {
	if path == "" {
		path = DefaultPath()
	}
	c := Config{Path: path, Width: "85%", Height: "75%", Icons: true, HideLocal: []string{"popup-*"}, CacheSeconds: 30, RefreshSeconds: 15, ConnectTimeoutSeconds: 3, CommandTimeoutSeconds: 5, Concurrency: 4}
	b, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return c, nil
	}
	if err != nil {
		return c, fmt.Errorf("%s: %w", path, err)
	}
	var root map[string]any
	if err := toml.Unmarshal(b, &root); err != nil {
		return c, fmt.Errorf("%s: %w", path, err)
	}
	if err := keys(root, path, "version", "picker", "discovery", "hosts"); err != nil {
		return c, err
	}
	if v, ok := root["version"]; ok {
		n, valid := integer(v)
		if !valid || n != 1 {
			return c, fmt.Errorf("%s: version must be 1", path)
		}
	}
	picker, err := table(root, "picker", path)
	if err != nil {
		return c, err
	}
	if err = keys(picker, path+" [picker]", "width", "height", "details", "icons", "hide_local"); err != nil {
		return c, err
	}
	discovery, err := table(root, "discovery", path)
	if err != nil {
		return c, err
	}
	if err = keys(discovery, path+" [discovery]", "cache_seconds", "refresh_seconds", "connect_timeout_seconds", "command_timeout_seconds", "concurrency"); err != nil {
		return c, err
	}
	if c.Width, err = stringValue(picker, "width", c.Width, path+" [picker].width", regexp.MustCompile(`^(?:[1-9][0-9]?%|100%)$`)); err != nil {
		return c, err
	}
	if c.Height, err = stringValue(picker, "height", c.Height, path+" [picker].height", regexp.MustCompile(`^(?:[1-9][0-9]?%|100%)$`)); err != nil {
		return c, err
	}
	if c.Details, err = boolValue(picker, "details", c.Details, path+" [picker].details"); err != nil {
		return c, err
	}
	if c.Icons, err = boolValue(picker, "icons", c.Icons, path+" [picker].icons"); err != nil {
		return c, err
	}
	if raw, ok := picker["hide_local"]; ok {
		values, ok := raw.([]any)
		if !ok {
			return c, fmt.Errorf("%s [picker].hide_local: expected array", path)
		}
		c.HideLocal = make([]string, 0, len(values))
		for _, value := range values {
			s, err := textValue(value, path+" [picker].hide_local", nil)
			if err != nil {
				return c, err
			}
			c.HideLocal = append(c.HideLocal, s)
		}
	}
	for _, item := range []struct {
		key    string
		dst    *int
		lo, hi int
	}{
		{"cache_seconds", &c.CacheSeconds, 1, 3600}, {"refresh_seconds", &c.RefreshSeconds, 1, 3600},
		{"connect_timeout_seconds", &c.ConnectTimeoutSeconds, 1, 60}, {"command_timeout_seconds", &c.CommandTimeoutSeconds, 1, 120}, {"concurrency", &c.Concurrency, 1, 32},
	} {
		if *item.dst, err = intValue(discovery, item.key, *item.dst, path+" [discovery]."+item.key, item.lo, item.hi); err != nil {
			return c, err
		}
	}
	if raw, ok := root["hosts"]; ok {
		list, ok := raw.([]any)
		if !ok {
			return c, fmt.Errorf("%s: hosts must be an array", path)
		}
		seen := make(map[string]bool)
		for i, value := range list {
			where := fmt.Sprintf("%s [[hosts]] #%d", path, i+1)
			m, ok := value.(map[string]any)
			if !ok {
				return c, fmt.Errorf("%s: expected table", where)
			}
			if err = keys(m, where, "id", "label", "ssh", "enabled", "tmux_path", "remote_prefix", "socket_name", "socket_path"); err != nil {
				return c, err
			}
			for _, key := range []string{"id", "ssh"} {
				if _, ok := m[key]; !ok {
					return c, fmt.Errorf("%s: missing %s", where, key)
				}
			}
			h := Host{Enabled: true, TmuxPath: "tmux", RemotePrefix: "auto"}
			if h.ID, err = stringValue(m, "id", "", where+".id", regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)); err != nil {
				return c, err
			}
			if seen[h.ID] {
				return c, fmt.Errorf("%s: duplicate id %s", where, h.ID)
			}
			seen[h.ID] = true
			if h.SSH, err = stringValue(m, "ssh", "", where+".ssh", regexp.MustCompile(`^[A-Za-z0-9_.@-]+$`)); err != nil {
				return c, err
			}
			if h.Label, err = stringValue(m, "label", h.ID, where+".label", nil); err != nil {
				return c, err
			}
			if h.Enabled, err = boolValue(m, "enabled", h.Enabled, where+".enabled"); err != nil {
				return c, err
			}
			if h.TmuxPath, err = stringValue(m, "tmux_path", h.TmuxPath, where+".tmux_path", nil); err != nil {
				return c, err
			}
			if strings.HasPrefix(h.TmuxPath, "-") {
				return c, fmt.Errorf("%s.tmux_path: invalid executable", where)
			}
			if h.RemotePrefix, err = stringValue(m, "remote_prefix", h.RemotePrefix, where+".remote_prefix", nil); err != nil {
				return c, err
			}
			switch h.RemotePrefix {
			case "auto", "M-a", "C-b", "None":
			default:
				return c, fmt.Errorf("%s.remote_prefix: supported values are auto, M-a, C-b, None", where)
			}
			if raw, ok := m["socket_name"]; ok && raw != "" {
				if h.SocketName, err = textValue(raw, where+".socket_name", regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)); err != nil {
					return c, err
				}
			}
			if raw, ok := m["socket_path"]; ok && raw != "" {
				s, ok := raw.(string)
				if !ok || !filepath.IsAbs(s) {
					return c, fmt.Errorf("%s.socket_path: expected absolute path", where)
				}
				h.SocketPath = s
			}
			if h.SocketName != "" && h.SocketPath != "" {
				return c, fmt.Errorf("%s: socket_name and socket_path are mutually exclusive", where)
			}
			c.Hosts = append(c.Hosts, h)
		}
	}
	return c, nil
}

func keys(m map[string]any, where string, allowed ...string) error {
	known := make(map[string]bool, len(allowed))
	for _, k := range allowed {
		known[k] = true
	}
	var unknown []string
	for k := range m {
		if !known[k] {
			unknown = append(unknown, k)
		}
	}
	sort.Strings(unknown)
	if len(unknown) > 0 {
		return fmt.Errorf("%s: unknown key %s", where, unknown[0])
	}
	return nil
}
func table(m map[string]any, key, where string) (map[string]any, error) {
	if v, ok := m[key]; ok {
		t, ok := v.(map[string]any)
		if !ok {
			return nil, fmt.Errorf("%s: picker and discovery must be tables", where)
		}
		return t, nil
	}
	return map[string]any{}, nil
}
func textValue(v any, where string, pattern *regexp.Regexp) (string, error) {
	s, ok := v.(string)
	if !ok || s == "" {
		return "", fmt.Errorf("%s: expected nonempty text without control characters", where)
	}
	for _, r := range s {
		if r < 32 || r >= 127 && r <= 159 {
			return "", fmt.Errorf("%s: expected nonempty text without control characters", where)
		}
	}
	if pattern != nil && !pattern.MatchString(s) {
		return "", fmt.Errorf("%s: invalid value %q", where, s)
	}
	return s, nil
}
func stringValue(m map[string]any, key, def, where string, pattern *regexp.Regexp) (string, error) {
	if v, ok := m[key]; ok {
		return textValue(v, where, pattern)
	}
	return def, nil
}
func boolValue(m map[string]any, key string, def bool, where string) (bool, error) {
	if v, ok := m[key]; ok {
		b, ok := v.(bool)
		if !ok {
			return false, fmt.Errorf("%s: expected boolean", where)
		}
		return b, nil
	}
	return def, nil
}
func integer(v any) (int, bool) {
	switch n := v.(type) {
	case int64:
		return int(n), true
	case int:
		return n, true
	default:
		return 0, false
	}
}
func intValue(m map[string]any, key string, def int, where string, lo, hi int) (int, error) {
	if v, ok := m[key]; ok {
		n, valid := integer(v)
		if !valid || n < lo || n > hi {
			return 0, fmt.Errorf("%s: expected integer from %d to %d", where, lo, hi)
		}
		return n, nil
	}
	return def, nil
}
