package configsync

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"code.talos.lowlife.us/baza/tmux-roam/internal/process"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
)

const Protocol = 1
const Limit = 1 << 20

type Request struct {
	Protocol      int      `json:"protocol"`
	Action        string   `json:"action"`
	Tmux          []string `json:"tmux"`
	Timeout       int      `json:"timeout"`
	Content       []byte   `json:"content,omitempty"`
	Expected      *string  `json:"expected,omitempty"`
	ExpectedPath  string   `json:"expected_path,omitempty"`
	CheckExpected bool     `json:"check_expected,omitempty"`
}
type Response struct {
	Protocol int     `json:"protocol"`
	Path     string  `json:"path,omitempty"`
	SHA256   *string `json:"sha256"`
	Content  []byte  `json:"content,omitempty"`
	Status   string  `json:"status,omitempty"`
	Backup   string  `json:"backup,omitempty"`
	Error    string  `json:"error,omitempty"`
}

func digest(data []byte) *string {
	if data == nil {
		return nil
	}
	sum := sha256.Sum256(data)
	value := hex.EncodeToString(sum[:])
	return &value
}
func read(path string) ([]byte, error) {
	f, err := os.Open(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	defer f.Close()
	stat, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if !stat.Mode().IsRegular() {
		return nil, errors.New("config destination is not a regular file")
	}
	content, err := io.ReadAll(io.LimitReader(f, Limit+1))
	if len(content) > Limit {
		return nil, errors.New("config exceeds 1 MiB")
	}
	return content, err
}
func run(ctx context.Context, timeout int, args ...string) error {
	result, err := process.Capture(ctx, time.Duration(timeout)*time.Second, Limit, args...)
	if err != nil {
		return err
	}
	if result.Code != 0 {
		return fmt.Errorf("tmux: %s", strings.TrimSpace(string(result.Stderr)+string(result.Stdout)))
	}
	return nil
}

// Resolve dangling symlink chains without ever replacing a link itself. Only
// the final file may be missing; its parent must already exist.
func destination(path string) (string, error) {
	for range 40 {
		parent, err := filepath.EvalSymlinks(filepath.Dir(path))
		if err != nil {
			return "", err
		}
		path = filepath.Join(parent, filepath.Base(path))
		info, err := os.Lstat(path)
		if os.IsNotExist(err) {
			return path, nil
		}
		if err != nil {
			return "", err
		}
		if info.Mode()&os.ModeSymlink == 0 {
			return path, nil
		}
		link, err := os.Readlink(path)
		if err != nil {
			return "", err
		}
		if !filepath.IsAbs(link) {
			link = filepath.Join(filepath.Dir(path), link)
		}
		path = filepath.Clean(link)
	}
	return "", errors.New("config symlink chain is too deep")
}
func Handle(ctx context.Context, request Request) (response Response, err error) {
	response.Protocol = Protocol
	if request.Protocol != Protocol {
		return response, errors.New("incompatible config-sync protocol")
	}
	if request.Action != "inspect" && request.Action != "sync" {
		return response, errors.New("unknown config-sync action")
	}
	if len(request.Tmux) == 0 || request.Tmux[0] == "" || strings.HasPrefix(request.Tmux[0], "-") || request.Timeout < 1 || request.Timeout > 120 {
		return response, errors.New("invalid tmux command or timeout")
	}
	cleanupStale(os.TempDir(), "tmux-roam-sync.")
	home, err := os.UserHomeDir()
	if err != nil {
		return response, err
	}
	configPath := filepath.Join(home, ".tmux.conf")
	path, err := destination(configPath)
	if err != nil {
		return response, err
	}
	original, err := read(path)
	if err != nil {
		return response, err
	}
	response.Path = path
	response.SHA256 = digest(original)
	if request.Action == "inspect" {
		response.Content = original
		return response, nil
	}
	if len(request.Content) > Limit || !utf8.Valid(request.Content) {
		return response, errors.New("config must be UTF-8 and at most 1 MiB")
	}
	stateBase := os.Getenv("XDG_STATE_HOME")
	if stateBase == "" {
		stateBase = filepath.Join(home, ".local", "state")
	}
	state := filepath.Join(stateBase, "tmux-roam")
	release, err := store.Acquire(ctx, filepath.Join(state, "config-sync.lock"))
	if err != nil {
		return response, err
	}
	defer release()
	cleanupStale(filepath.Dir(path), ".tmux-roam-sync-")
	resolved, err := destination(configPath)
	if err != nil || resolved != path || request.CheckExpected && request.ExpectedPath != "" && request.ExpectedPath != path {
		return response, errors.New("config symlink target changed; compare it again")
	}
	original, err = read(path)
	if err != nil {
		return response, err
	}
	current := digest(original)
	if request.CheckExpected && ((current == nil) != (request.Expected == nil) || current != nil && *current != *request.Expected) {
		return response, errors.New("remote config changed since the preview; compare it again")
	}
	if bytes.Equal(original, request.Content) && original != nil {
		response.Status = "unchanged"
		return response, nil
	}
	directory, err := os.MkdirTemp(filepath.Dir(path), ".tmux-roam-sync-")
	if err != nil {
		return response, err
	}
	defer os.RemoveAll(directory)
	if err = markStage(directory); err != nil {
		return response, err
	}
	mode := os.FileMode(0600)
	if original != nil {
		info, err := os.Stat(path)
		if err != nil {
			return response, err
		}
		mode = info.Mode().Perm()
	}
	stage := filepath.Join(directory, "tmux.conf")
	if err = os.WriteFile(stage, request.Content, mode); err != nil {
		return response, err
	}
	if err = os.Chmod(stage, mode); err != nil {
		return response, err
	}
	validationDir, err := os.MkdirTemp("", "tr-validate-")
	if err != nil {
		return response, err
	}
	defer os.RemoveAll(validationDir)
	validator := []string{request.Tmux[0], "-S", filepath.Join(validationDir, "socket"), "-f", "/dev/null"}
	validationErr := func() error {
		defer func() {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), time.Duration(request.Timeout)*time.Second)
			defer cancel()
			_ = run(cleanupCtx, request.Timeout, append(append([]string{}, validator...), "kill-server")...)
		}()
		if err := run(ctx, request.Timeout, append(append([]string{}, validator...), "start-server", ";", "set-option", "-s", "exit-empty", "off")...); err != nil {
			return err
		}
		return run(ctx, request.Timeout, append(append([]string{}, validator...), "source-file", "-n", stage)...)
	}()
	if validationErr != nil {
		return response, validationErr
	}
	probe, err := process.Capture(ctx, time.Duration(request.Timeout)*time.Second, Limit, append(append([]string{}, request.Tmux...), "-N", "list-sessions")...)
	if err != nil {
		return response, err
	}
	running := probe.Code == 0
	if !running {
		message := strings.ToLower(string(probe.Stderr))
		expected := false
		for _, reason := range []string{"no server running", "no sessions", "no such file", "connection refused"} {
			expected = expected || strings.Contains(message, reason)
		}
		if !expected {
			return response, fmt.Errorf("cannot inspect destination tmux: %s", probe.Stderr)
		}
	}
	latest, err := read(path)
	if err != nil {
		return response, err
	}
	if !bytes.Equal(latest, original) || (latest == nil) != (original == nil) {
		return response, errors.New("remote config changed during validation")
	}
	if original != nil {
		info, statErr := os.Stat(path)
		if statErr != nil || info.Mode().Perm() != mode {
			return response, errors.New("remote config permissions changed during validation")
		}
	}
	resolved, err = destination(configPath)
	if err != nil || resolved != path {
		return response, errors.New("config symlink target changed during validation")
	}
	if original != nil {
		backupDir, err := os.MkdirTemp(filepath.Join(state, "backups"), time.Now().Format("20060102-150405")+"-config-")
		if os.IsNotExist(err) {
			if err = os.MkdirAll(filepath.Join(state, "backups"), 0700); err == nil {
				backupDir, err = os.MkdirTemp(filepath.Join(state, "backups"), time.Now().Format("20060102-150405")+"-config-")
			}
		}
		if err != nil {
			return response, err
		}
		response.Backup = filepath.Join(backupDir, "tmux.conf")
		if err = os.WriteFile(response.Backup, original, mode); err != nil {
			return response, err
		}
		if err = os.Chmod(response.Backup, mode); err != nil {
			return response, err
		}
	}
	if err = ctx.Err(); err != nil {
		return response, err
	}
	if err = os.Rename(stage, path); err != nil {
		return response, err
	}
	if running {
		if reloadErr := run(ctx, request.Timeout, append(append([]string{}, request.Tmux...), "-N", "source-file", path)...); reloadErr != nil {
			recoveryCtx, cancel := context.WithTimeout(context.Background(), time.Duration(request.Timeout*2)*time.Second)
			defer cancel()
			recovery := "new file removed; no previous config existed"
			if original != nil {
				if err = os.WriteFile(stage, original, mode); err == nil {
					err = os.Chmod(stage, mode)
				}
				if err == nil {
					err = os.Rename(stage, path)
				}
				if err != nil {
					return response, fmt.Errorf("reload failed: %v; file restore failed: %v; backup: %s", reloadErr, err, response.Backup)
				}
				recovery = "previous file restored and reloaded"
				if restoreErr := run(recoveryCtx, request.Timeout, append(append([]string{}, request.Tmux...), "-N", "source-file", path)...); restoreErr != nil {
					recovery = "previous file restored; its reload also failed: " + restoreErr.Error()
				}
			} else {
				_ = os.Remove(path)
			}
			return response, fmt.Errorf("reload failed: %v; %s. Live changes may remain. Backup: %s", reloadErr, recovery, response.Backup)
		}
	}
	response.Status = "saved"
	if running {
		response.Status = "synced"
	}
	return response, nil
}
