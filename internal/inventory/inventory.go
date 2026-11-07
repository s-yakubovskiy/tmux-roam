package inventory

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/process"
)

const sessionFormat = "#{session_id}|#{session_created}|#{session_name}|#{session_windows}|#{session_attached}"
const windowFormat = "#{session_id}|#{window_id}|#{window_index}|#{window_active}|#{window_name}"
const clientFormat = "#{client_pid}|#{client_tty}|#{session_id}|#{client_name}"
const serverFormat = "#{pid}|#{start_time}|#{socket_path}"
const clientMarker = "TMUX_ROAM_CLIENTS_V1"
const windowMarker = "TMUX_ROAM_WINDOWS_V1"

func tmuxBase() []string {
	if s := strings.SplitN(os.Getenv("TMUX"), ",", 2)[0]; s != "" {
		return []string{"tmux", "-S", s}
	}
	return []string{"tmux"}
}
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
	if len(p) != right+3 || !numericID(p[0], '$') {
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
	if len(s) < 2 || s[0] != marker {
		return false
	}
	for i := 1; i < len(s); i++ {
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
	base := tmuxBase()
	serverResult, err := command(ctx, 5, append(base, "display-message", "-p", serverFormat)...)
	if err != nil {
		return nil, err
	}
	if serverResult.Code != 0 {
		return nil, fmt.Errorf("%s", strings.TrimSpace(string(serverResult.Stderr)))
	}
	server := strings.SplitN(strings.TrimSpace(string(serverResult.Stdout)), "|", 3)
	if len(server) != 3 || server[0] == "" || server[1] == "" {
		return nil, fmt.Errorf("malformed tmux server identity")
	}
	sessions, err := command(ctx, 5, append(base, "list-sessions", "-F", sessionFormat)...)
	if err != nil {
		return nil, err
	}
	if sessions.Code != 0 {
		return nil, fmt.Errorf("%s", strings.TrimSpace(string(sessions.Stderr)))
	}
	owners, err := command(ctx, 5, append(base, "list-sessions", "-F", "#{session_id}|#{@tmux-roam-owner}")...)
	if err != nil {
		return nil, err
	}
	owned := map[string]bool{}
	for _, line := range strings.Split(string(owners.Stdout), "\n") {
		if strings.HasSuffix(line, "|1") {
			owned[strings.SplitN(line, "|", 2)[0]] = true
		}
	}
	var entries []model.Record
	for _, line := range strings.Split(strings.TrimSuffix(string(sessions.Stdout), "\n"), "\n") {
		if line == "" {
			continue
		}
		r, _, err := sessionLine(line, false)
		if err != nil {
			return nil, err
		}
		if !owned[r.SessionID] {
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
	b, err := os.ReadFile(filepath.Join(CacheDir(), host.ID+".json"))
	if err != nil || len(b) > 16<<20 {
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
func Save(host config.Host, rows []model.Record) error {
	dir := CacheDir()
	if err := ensurePrivateDir(dir); err != nil {
		return err
	}
	lock, err := os.OpenFile(filepath.Join(dir, host.ID+".lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer lock.Close()
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		return err
	}
	defer syscall.Flock(int(lock.Fd()), syscall.LOCK_UN)
	stamp := float64(time.Now().UnixNano()) / 1e9
	if len(rows) > 0 {
		stamp = rows[0].Observed
	}
	if previous, readErr := os.ReadFile(filepath.Join(dir, host.ID+".json")); readErr == nil {
		var old cacheFile
		if json.Unmarshal(previous, &old) == nil && old.Schema == 1 && old.HostID == host.ID {
			oldStamp := old.Captured
			if oldStamp == 0 && len(old.Records) > 0 {
				oldStamp = old.Records[0].Observed
			}
			if oldStamp > stamp {
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
	return os.Rename(f.Name(), filepath.Join(dir, host.ID+".json"))
}

func quote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'" }
func shell(args []string) string {
	parts := make([]string, len(args))
	for i, a := range args {
		parts[i] = quote(a)
	}
	return strings.Join(parts, " ")
}
func sshBase(host config.Host, c config.Config) []string {
	args := []string{"ssh"}
	sshConfig := os.Getenv("TMUX_ROAM_SSH_CONFIG")
	if sshConfig != "" {
		args = append(args, "-F", sshConfig)
	}
	hash := sha256.Sum256([]byte(host.SSH + "\x00" + sshConfig))
	control := filepath.Join(runtimeDir(), "ssh-"+hex.EncodeToString(hash[:])[:8]+"-%C")
	if len([]byte(strings.ReplaceAll(control, "%C", strings.Repeat("0", 40)))) <= 90 {
		args = append(args, "-o", "ControlMaster=auto", "-o", "ControlPersist=60", "-o", "ControlPath="+control)
	}
	return append(args, "-o", "BatchMode=yes", "-o", fmt.Sprintf("ConnectTimeout=%d", c.ConnectTimeoutSeconds), "-o", "ConnectionAttempts=1", "-o", "RemoteCommand=none", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3", "-T", "--", host.SSH)
}
func runtimeDir() string {
	base := os.Getenv("XDG_RUNTIME_DIR")
	if base == "" {
		base = fmt.Sprintf("/tmp/tmux-roam-%d", os.Getuid())
	}
	return filepath.Join(base, "tmux-roam")
}

func ensurePrivateDir(dir string) error {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.IsDir() || info.Mode().Perm()&0077 != 0 || stat.Uid != uint32(os.Getuid()) {
		return fmt.Errorf("unsafe private directory: %s", dir)
	}
	return nil
}

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

func RemoteSnapshot(ctx context.Context, host config.Host, c config.Config, update bool) (Snapshot, error) {
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
		if len(p) != 4 || !numericID(p[2], '$') {
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
		if len(p) != 5 || !numericID(p[0], '$') || !numericID(p[1], '@') || (p[3] != "0" && p[3] != "1") {
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
	type result struct {
		host config.Host
		rows []model.Record
		err  error
	}
	ch := make(chan result, len(c.Hosts))
	sem := make(chan struct{}, c.Concurrency)
	var wg sync.WaitGroup
	for _, host := range c.Hosts {
		if !host.Enabled {
			continue
		}
		if !refresh {
			rows = append(rows, Cached(host)...)
			continue
		}
		wg.Add(1)
		go func(host config.Host) {
			defer wg.Done()
			select {
			case sem <- struct{}{}:
			case <-ctx.Done():
				ch <- result{host: host, err: ctx.Err()}
				return
			}
			defer func() { <-sem }()
			r, e := Remote(ctx, host, c)
			ch <- result{host: host, rows: r, err: e}
		}(host)
	}
	if refresh {
		go func() { wg.Wait(); close(ch) }()
		for item := range ch {
			if item.err != nil {
				errors[item.host.ID] = item.err.Error()
				rows = append(rows, Cached(item.host)...)
			} else {
				if e := Save(item.host, item.rows); e != nil {
					errors[item.host.ID] = e.Error()
				}
				rows = append(rows, item.rows...)
			}
		}
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
