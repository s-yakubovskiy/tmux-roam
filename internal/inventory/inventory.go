package inventory

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/process"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

const sessionFormat = "#{session_id}|#{session_created}|#{session_name}|#{session_windows}|#{session_attached}"
const windowFormat = "#{session_id}|#{window_id}|#{window_index}|#{window_active}|#{window_name}"
const clientFormat = "#{client_pid}|#{client_tty}|#{session_id}|#{client_name}"
const serverFormat = "#{pid}|#{start_time}|#{socket_path}"
const clientMarker = "TMUX_ROAM_CLIENTS_V1"
const windowMarker = "TMUX_ROAM_WINDOWS_V1"

func tmuxBase() []string { return tmux.Base() }
func command(ctx context.Context, seconds int, args ...string) (process.Result, error) {
	return process.Capture(ctx, time.Duration(seconds)*time.Second, 1<<20, args...)
}
func clean(s string) string {
	return strings.Map(func(r rune) rune {
		if r < 32 || r >= 127 && r <= 159 {
			return '?'
		}
		return r
	}, s)
}
func fields(line string, right int) ([]string, error) {
	head := strings.SplitN(line, "|", 3)
	if len(head) != 3 {
		return nil, fmt.Errorf("malformed session record")
	}
	tail := strings.Split(head[2], "|")
	if len(tail) < right+1 {
		return nil, fmt.Errorf("malformed session record")
	}
	return append([]string{head[0], head[1], strings.Join(tail[:len(tail)-right], "|")}, tail[len(tail)-right:]...), nil
}
func sessionLine(line string, remote bool) (model.Record, string, error) {
	var rec model.Record
	right := 2
	if remote {
		right = 3
	}
	p, err := fields(line, right)
	if err != nil {
		return rec, "", err
	}
	if len(p) != right+3 || !numericID(p[0], '$') || !decimal(p[1]) || !decimal(p[3]) || !decimal(p[4]) {
		return rec, "", fmt.Errorf("malformed session record")
	}
	created, e1 := strconv.ParseInt(p[1], 10, 64)
	windows, e2 := strconv.Atoi(p[3])
	clients, e3 := strconv.Atoi(p[4])
	if e1 != nil || e2 != nil || e3 != nil || created < 0 || windows < 0 || clients < 0 {
		return rec, "", fmt.Errorf("malformed session record")
	}
	rec.SessionID = p[0]
	rec.Created = created
	rec.Name = clean(p[2])
	rec.Windows = windows
	rec.Clients = clients
	if remote {
		return rec, p[5], nil
	}
	return rec, "", nil
}
func numericID(s string, marker byte) bool {
	return len(s) >= 2 && s[0] == marker && decimal(s[1:])
}
func decimal(s string) bool {
	if s == "" {
		return false
	}
	for i := range s {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}
func records(host, label string, server []string, entries []model.Record, source string) []model.Record {
	stamp := float64(time.Now().UnixNano()) / 1e9
	generation := server[0] + "/" + server[1]
	for i := range entries {
		r := &entries[i]
		r.ID = (model.Identity{Host: host, Socket: server[2], Generation: generation, Session: r.SessionID, Created: r.Created}).Encode()
		r.HostID = host
		r.HostLabel = label
		r.Socket = server[2]
		r.Generation = generation
		r.Observed = stamp
		r.Source = source
	}
	return entries
}
func Local(ctx context.Context) ([]model.Record, error) {
	result, err := command(ctx, 5, append(tmuxBase(), "display-message", "-p", serverFormat, ";", "list-sessions", "-F", sessionFormat+"|#{@tmux-roam-owner}")...)
	if err != nil {
		return nil, err
	}
	if result.Code != 0 {
		return nil, fmt.Errorf("%s", strings.TrimSpace(string(result.Stderr)))
	}
	return localRecords(string(result.Stdout))
}

// LocalClient captures the client/navigation and local inventory together so
// opening a picker needs only one foreground snapshot subprocess.
func LocalClient(ctx context.Context, name, token string) (client tmux.Client, rows []model.Record, err error) {
	defer func() {
		if err != nil && token != "" {
			tmux.EndNavigation(context.Background(), name, token)
		}
	}()
	var commands []string
	if token != "" {
		commands = append(commands, ";", "set-option", "-g", "@tmux-roam-navigation-"+name, token)
	}
	commands = append(commands, ";", "display-message", "-p", serverFormat, ";", "list-sessions", "-F", sessionFormat+"|#{@tmux-roam-owner}")
	var out string
	client, out, err = tmux.CaptureClientSnapshot(ctx, name, commands)
	if err != nil {
		return
	}
	rows, err = localRecords(out)
	return
}

func localRecords(raw string) ([]model.Record, error) {
	lines := strings.Split(strings.TrimSuffix(raw, "\n"), "\n")
	server := strings.SplitN(lines[0], "|", 3)
	if len(server) != 3 || !decimal(server[0]) || !decimal(server[1]) || !strings.HasPrefix(server[2], "/") {
		return nil, fmt.Errorf("malformed tmux server identity")
	}
	var entries []model.Record
	for _, line := range lines[1:] {
		if line == "" {
			continue
		}
		end := strings.LastIndexByte(line, '|')
		if end < 0 {
			return nil, fmt.Errorf("malformed local ownership record")
		}
		r, _, err := sessionLine(line[:end], false)
		if err != nil {
			return nil, err
		}
		if line[end+1:] != "1" {
			entries = append(entries, r)
		}
	}
	return records("local", "local", server, entries, "live"), nil
}

type cacheFile struct {
	Schema   int                         `json:"schema"`
	HostID   string                      `json:"host_id"`
	Captured float64                     `json:"captured,omitempty"`
	Records  []model.Record              `json:"records"`
	Windows  map[string][]map[string]any `json:"windows"`
}

func CacheDir() string {
	base := os.Getenv("XDG_CACHE_HOME")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, ".cache")
	}
	return filepath.Join(base, "tmux-roam")
}
func Cached(host config.Host) []model.Record {
	b, err := readCache(host)
	if err != nil {
		return nil
	}
	var data cacheFile
	if json.Unmarshal(b, &data) != nil || data.Schema != 1 || data.HostID != host.ID {
		return nil
	}
	for i := range data.Records {
		r := &data.Records[i]
		id, err := model.Decode(r.ID)
		if err != nil || id.Host != host.ID || id.Session != r.SessionID || id.Socket != r.Socket || id.Generation != r.Generation || id.Created != r.Created {
			return nil
		}
		r.Source = "cached"
		r.State = ""
		if w, ok := data.Windows[r.ID]; ok {
			r.WindowDetails = w
		}
	}
	return data.Records
}

func readCache(host config.Host) ([]byte, error) {
	f, err := os.Open(filepath.Join(CacheDir(), host.ID+".json"))
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, (16<<20)+1))
	if len(b) > 16<<20 {
		return nil, fmt.Errorf("cache exceeded size limit")
	}
	return b, err
}

func Save(host config.Host, rows []model.Record) error {
	return SaveContext(context.Background(), host, rows)
}

// SaveContext holds only a per-host publication lock, never a lock across SSH.
// Polling LOCK_NB keeps a different process's writer cancellable.
func SaveContext(ctx context.Context, host config.Host, rows []model.Record) error {
	dir := CacheDir()
	if err := ensurePrivateDir(dir); err != nil {
		return err
	}
	lock, err := os.OpenFile(filepath.Join(dir, host.ID+".lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	for {
		if err = ctx.Err(); err != nil {
			return err
		}
		err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) && !errors.Is(err, syscall.EINTR) {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(10 * time.Millisecond):
		}
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	now := float64(time.Now().UnixNano()) / 1e9
	stamp := now
	if len(rows) > 0 {
		stamp = rows[0].Observed
	}
	if previous, readErr := readCache(host); readErr == nil {
		var old cacheFile
		if json.Unmarshal(previous, &old) == nil && old.Schema == 1 && old.HostID == host.ID {
			oldStamp := old.Captured
			if oldStamp == 0 && len(old.Records) > 0 {
				oldStamp = old.Records[0].Observed
			}
			// A future observation indicates a wall-clock step backwards. Do not
			// freeze cache publication until the old clock catches up.
			if oldStamp > stamp && oldStamp <= now {
				return nil
			}
		}
	}
	data := cacheFile{Schema: 1, HostID: host.ID, Captured: stamp, Windows: map[string][]map[string]any{}}
	for _, r := range rows {
		if r.WindowDetails != nil {
			data.Windows[r.ID] = r.WindowDetails
		}
		r.WindowDetails = nil
		data.Records = append(data.Records, r)
	}
	b, err := json.Marshal(data)
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, host.ID+".*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if err = f.Chmod(0600); err != nil {
		return err
	}
	if _, err = f.Write(b); err != nil {
		return err
	}
	if err = f.Sync(); err != nil {
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	return os.Rename(f.Name(), filepath.Join(dir, host.ID+".json"))
}

func quote(s string) string      { return ssh.Quote(s) }
func shell(args []string) string { return ssh.Join(args) }
func sshBase(host config.Host, c config.Config) []string {
	args, _ := ssh.Args(host, c, false)
	return args
}
func runtimeDir() string                { return store.RuntimeDir() }
func ensurePrivateDir(dir string) error { return store.PrivateDir(dir) }

type Snapshot struct {
	Records  []model.Record
	Clients  map[string]string
	Prefixes map[string]string
}

func Remote(ctx context.Context, host config.Host, c config.Config) ([]model.Record, error) {
	snapshot, err := RemoteSnapshot(ctx, host, c, false)
	return snapshot.Records, err
}

func RemoteWithUpdate(ctx context.Context, host config.Host, c config.Config, update bool) ([]model.Record, error) {
	snapshot, err := RemoteSnapshot(ctx, host, c, update)
	return snapshot.Records, err
}

func remoteSnapshot(ctx context.Context, host config.Host, c config.Config, update bool) (Snapshot, error) {
	var empty Snapshot
	if err := ensurePrivateDir(runtimeDir()); err != nil {
		return empty, err
	}
	commands := []string{}
	for _, a := range [][]string{append(host.TmuxArgs(), "display-message", "-p", serverFormat), append(host.TmuxArgs(), "list-sessions", "-F", sessionFormat+"|#{prefix}")} {
		commands = append(commands, "LC_ALL=C.UTF-8 "+shell(a))
	}
	commands = append(commands, "printf '%s\\n' "+quote(clientMarker), "LC_ALL=C.UTF-8 "+shell(append(host.TmuxArgs(), "list-clients", "-F", clientFormat)), "printf '%s\\n' "+quote(windowMarker), "LC_ALL=C.UTF-8 "+shell(append(host.TmuxArgs(), "list-windows", "-a", "-F", windowFormat)))
	remote := "sh -c " + quote(strings.Join(commands, " && "))
	args := append(sshBase(host, c), remote)
	result, err := command(ctx, c.CommandTimeoutSeconds, args...)
	if err != nil {
		return empty, err
	}
	if result.Code != 0 {
		msg := clean(strings.TrimSpace(string(result.Stderr)))
		if msg == "" {
			msg = fmt.Sprintf("SSH exit %d", result.Code)
		}
		if len(msg) > 500 {
			msg = msg[:500]
		}
		return empty, fmt.Errorf("%s", msg)
	}
	if !utf8.Valid(result.Stdout) {
		return empty, fmt.Errorf("malformed inventory from %s: invalid UTF-8", host.ID)
	}
	lines := strings.Split(strings.TrimSuffix(string(result.Stdout), "\n"), "\n")
	if len(lines) < 3 {
		return empty, fmt.Errorf("malformed inventory from %s: short response", host.ID)
	}
	server := strings.SplitN(lines[0], "|", 3)
	if len(server) != 3 || server[2] == "" || server[2][0] != '/' {
		return empty, fmt.Errorf("malformed inventory from %s: invalid server identity", host.ID)
	}
	for _, n := range server[:2] {
		if _, err := strconv.ParseUint(n, 10, 64); err != nil {
			return empty, fmt.Errorf("malformed inventory from %s: invalid server identity", host.ID)
		}
	}
	ci, wi := -1, -1
	for i, line := range lines {
		if line == clientMarker {
			ci = i
		}
		if line == windowMarker {
			wi = i
		}
	}
	if ci < 1 || wi < ci {
		return empty, fmt.Errorf("malformed inventory from %s: missing markers", host.ID)
	}
	var entries []model.Record
	prefixes := map[string]string{}
	for _, line := range lines[1:ci] {
		r, prefix, err := sessionLine(line, true)
		if err != nil {
			return empty, fmt.Errorf("malformed inventory from %s: %w", host.ID, err)
		}
		entries = append(entries, r)
		prefixes[r.SessionID] = prefix
	}
	clients := map[string]string{}
	for _, line := range lines[ci+1 : wi] {
		p := strings.SplitN(line, "|", 4)
		if len(p) != 4 || !numericID(p[2], '$') || !decimal(p[0]) {
			return empty, fmt.Errorf("malformed inventory from %s: invalid client record", host.ID)
		}
		if _, e := strconv.Atoi(p[0]); e != nil {
			return empty, fmt.Errorf("malformed inventory from %s: invalid client record", host.ID)
		}
		clients[p[0]+"|"+p[1]] = p[2]
	}
	rows := records(host.ID, host.Label, server, entries, "live")
	bySession := map[string]*model.Record{}
	for i := range rows {
		rows[i].WindowDetails = []map[string]any{}
		bySession[rows[i].SessionID] = &rows[i]
	}
	for _, line := range lines[wi+1:] {
		if line == "" {
			continue
		}
		p := strings.SplitN(line, "|", 5)
		if len(p) != 5 || !numericID(p[0], '$') || !numericID(p[1], '@') || !decimal(p[2]) || (p[3] != "0" && p[3] != "1") {
			return empty, fmt.Errorf("malformed inventory from %s: invalid window record", host.ID)
		}
		index, err := strconv.Atoi(p[2])
		if err != nil {
			return empty, err
		}
		if r := bySession[p[0]]; r != nil {
			r.WindowDetails = append(r.WindowDetails, map[string]any{"id": p[1], "index": index, "active": p[3] == "1", "name": clean(p[4])})
		}
	}
	reconcile(ctx, host, rows, clients, update)
	return Snapshot{Records: rows, Clients: clients, Prefixes: prefixes}, nil
}

func reconcile(ctx context.Context, host config.Host, rows []model.Record, clients map[string]string, update bool) {
	base := tmuxBase()
	result, err := command(ctx, 5, append(base, "list-sessions", "-F", "#{session_id}|#{@tmux-roam-owner}|#{@tmux-roam-identity}|#{@tmux-roam-state}")...)
	if err != nil || result.Code != 0 {
		return
	}
	bySession := map[string]*model.Record{}
	for i := range rows {
		bySession[rows[i].SessionID] = &rows[i]
	}
	counts := map[string]int{}
	for _, line := range strings.Split(strings.TrimSpace(string(result.Stdout)), "\n") {
		p := strings.SplitN(line, "|", 4)
		if len(p) != 4 || p[1] != "1" {
			continue
		}
		id, e := model.Decode(p[2])
		if e != nil || id.Host != host.ID || len(rows) > 0 && id.Generation != rows[0].Generation {
			continue
		}
		token := p[3]
		if token == "" || strings.ContainsAny(token, "/\\.") || len(token) > 128 {
			continue
		}
		b, e := os.ReadFile(filepath.Join(runtimeDir(), token+".json"))
		if e != nil || len(b) > 4096 {
			continue
		}
		var state struct {
			PID int    `json:"pid"`
			TTY string `json:"tty"`
		}
		if json.Unmarshal(b, &state) != nil || state.PID <= 0 || state.TTY == "" {
			continue
		}
		sid := clients[strconv.Itoa(state.PID)+"|"+state.TTY]
		target := bySession[sid]
		if target == nil {
			continue
		}
		counts[target.ID]++
		if update && target.ID != p[2] {
			_, _ = command(ctx, 5, append(base, "set-option", "-t", p[0], "@tmux-roam-identity", target.ID)...)
		}
	}
	for i := range rows {
		if n := counts[rows[i].ID]; n > 0 {
			rows[i].State = "open here"
			if n > 1 {
				rows[i].State += fmt.Sprintf(" (%d)", n)
			}
		}
	}
}

func All(ctx context.Context, c config.Config, refresh bool) ([]model.Record, map[string]string) {
	rows := []model.Record{}
	errors := map[string]string{}
	local, err := Local(ctx)
	if err != nil {
		errors["local"] = err.Error()
	} else {
		rows = append(rows, local...)
	}
	if !refresh {
		for _, host := range c.Hosts {
			if host.Enabled {
				rows = append(rows, Cached(host)...)
			}
		}
	} else {
		RefreshHosts(ctx, c, false, func(item HostResult) {
			if item.Err != nil {
				errors[item.Host.ID] = item.Err.Error()
			}
			rows = append(rows, item.Records...)
		})
	}
	if err := ctx.Err(); err != nil {
		errors["refresh"] = err.Error()
	}
	order := map[string]int{}
	for i, h := range c.Hosts {
		order[h.ID] = i + 1
	}
	sort.SliceStable(rows, func(i, j int) bool {
		a, b := rows[i], rows[j]
		if order[a.HostID] != order[b.HostID] {
			return order[a.HostID] < order[b.HostID]
		}
		an, bn := strings.ToLower(a.Name), strings.ToLower(b.Name)
		if an != bn {
			return an < bn
		}
		return a.SessionID < b.SessionID
	})
	return rows, errors
}
