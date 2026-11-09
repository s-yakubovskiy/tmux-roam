package configsync

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
)

func TestTransactionPreservesSessionSymlinkBackupAndRollback(t *testing.T) {
	binary, err := exec.LookPath("tmux")
	if err != nil {
		t.Skip("tmux unavailable")
	}
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_STATE_HOME", filepath.Join(home, "state"))
	socket := filepath.Join(home, "live.sock")
	base := []string{binary, "-S", socket}
	run := func(args ...string) string {
		t.Helper()
		output, err := exec.Command(base[0], append(base[1:], args...)...).CombinedOutput()
		if err != nil {
			t.Fatalf("tmux: %s: %v", output, err)
		}
		return strings.TrimSpace(string(output))
	}
	run("-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 60")
	t.Cleanup(func() { _ = exec.Command(binary, "-S", socket, "kill-server").Run() })
	original := []byte("set -g @sync_test before\n")
	path := filepath.Join(home, ".tmux.conf")
	target := filepath.Join(home, "dotfiles.conf")
	if err = os.WriteFile(target, original, 0640); err != nil {
		t.Fatal(err)
	}
	if err = os.Symlink(target, path); err != nil {
		t.Fatal(err)
	}
	run("source-file", path)
	request := Request{Protocol: Protocol, Action: "inspect", Tmux: base, Timeout: 3}
	inspected, err := Handle(context.Background(), request)
	if err != nil || inspected.SHA256 == nil || string(inspected.Content) != string(original) {
		t.Fatalf("inspect: %+v %v", inspected, err)
	}
	request.Action = "sync"
	request.Content = []byte("set -g @sync_test 'after | quoted'\n")
	request.Expected = inspected.SHA256
	request.CheckExpected = true
	response, err := Handle(context.Background(), request)
	if err != nil || response.Status != "synced" {
		t.Fatalf("sync: %+v %v", response, err)
	}
	if run("show-options", "-gqv", "@sync_test") != "after | quoted" || run("list-sessions", "-F", "#{session_name}") != "keep" {
		t.Fatal("live reload lost state or session")
	}
	backup, err := os.ReadFile(response.Backup)
	if err != nil || string(backup) != string(original) {
		t.Fatal("backup lost original", err)
	}
	info, _ := os.Lstat(path)
	if info.Mode()&os.ModeSymlink == 0 {
		t.Fatal("config symlink replaced")
	}
	info, _ = os.Stat(target)
	if info.Mode().Perm() != 0640 {
		t.Fatal("permissions changed")
	}
	request.CheckExpected = false
	response, err = Handle(context.Background(), request)
	if err != nil || response.Status != "unchanged" {
		t.Fatalf("unchanged: %+v %v", response, err)
	}
	before := request.Content
	request.Content = []byte("not-a-tmux-command\n")
	if _, err = Handle(context.Background(), request); err == nil {
		t.Fatal("invalid syntax installed")
	}
	current, _ := os.ReadFile(path)
	if string(current) != string(before) {
		t.Fatal("invalid syntax changed config")
	}
	request.Content = []byte("set -g @sync_test partial\nset -g not-a-tmux-option invalid\n")
	if _, err = Handle(context.Background(), request); err == nil || !strings.Contains(err.Error(), "previous file restored and reloaded") {
		t.Fatal("reload did not report recovery", err)
	}
	current, _ = os.ReadFile(path)
	if string(current) != string(before) || run("show-options", "-gqv", "@sync_test") != "after | quoted" {
		t.Fatal("reload rollback failed")
	}
	request.CheckExpected = true
	request.Expected = inspected.SHA256
	request.Content = original
	if _, err = Handle(context.Background(), request); err == nil || !strings.Contains(err.Error(), "changed since the preview") {
		t.Fatal("stale preview committed", err)
	}
	request.CheckExpected = false
	run("kill-server")
	response, err = Handle(context.Background(), request)
	if err != nil || response.Status != "saved" {
		t.Fatalf("stopped-server sync: %+v %v", response, err)
	}
	leftovers, _ := filepath.Glob(filepath.Join(home, ".tmux-roam-sync-*"))
	if len(leftovers) != 0 {
		t.Fatal("validation staging leaked", leftovers)
	}
	request.Protocol = Protocol + 1
	if _, err = Handle(context.Background(), request); err == nil {
		t.Fatal("accepted incompatible protocol")
	}
}

func TestValidationRefusesConcurrentContentPermissionAndTargetChanges(t *testing.T) {
	binary, err := exec.LookPath("tmux")
	if err != nil {
		t.Skip("tmux unavailable")
	}
	for name, mutation := range map[string]string{
		"content":     "printf '# user edit\\n' > \"$HOME/.tmux.conf\"",
		"permissions": "chmod 0644 \"$HOME/.tmux.conf\"",
		"target":      "rm \"$HOME/.tmux.conf\"; ln -s other.conf \"$HOME/.tmux.conf\"",
	} {
		t.Run(name, func(t *testing.T) {
			home := t.TempDir()
			t.Setenv("HOME", home)
			t.Setenv("XDG_STATE_HOME", filepath.Join(home, "state"))
			original := []byte("# before\n")
			for _, file := range []string{".tmux.conf", "other.conf"} {
				if err := os.WriteFile(filepath.Join(home, file), original, 0600); err != nil {
					t.Fatal(err)
				}
			}
			wrapper := filepath.Join(home, "tmux-wrapper")
			script := "#!/bin/sh\ncase \"$*\" in *source-file*) " + mutation + ";; esac\nexec " + ssh.Quote(binary) + " \"$@\"\n"
			if err := os.WriteFile(wrapper, []byte(script), 0700); err != nil {
				t.Fatal(err)
			}
			_, err := Handle(context.Background(), Request{Protocol: Protocol, Action: "sync", Tmux: []string{wrapper, "-S", filepath.Join(home, "live.sock")}, Timeout: 3, Content: []byte("# replacement\n")})
			if err == nil || !strings.Contains(err.Error(), "changed during validation") {
				t.Fatalf("accepted concurrent %s change: %v", name, err)
			}
			current, _ := os.ReadFile(filepath.Join(home, ".tmux.conf"))
			if string(current) == "# replacement\n" {
				t.Fatal("overwrote concurrent edit")
			}
		})
	}
}

func TestConcurrentSyncAllowsOnlyOnePreviewCommit(t *testing.T) {
	binary, err := exec.LookPath("tmux")
	if err != nil {
		t.Skip("tmux unavailable")
	}
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_STATE_HOME", filepath.Join(home, "state"))
	original := []byte("# original\n")
	path := filepath.Join(home, ".tmux.conf")
	if err := os.WriteFile(path, original, 0600); err != nil {
		t.Fatal(err)
	}
	results := make(chan error, 2)
	for _, content := range []string{"# first\n", "# second\n"} {
		go func() {
			_, err := Handle(context.Background(), Request{Protocol: Protocol, Action: "sync", Tmux: []string{binary, "-S", filepath.Join(home, "live.sock")}, Timeout: 3, Content: []byte(content), Expected: digest(original), CheckExpected: true})
			results <- err
		}()
	}
	accepted := 0
	for range 2 {
		if err := <-results; err == nil {
			accepted++
		} else if !strings.Contains(err.Error(), "changed since the preview") {
			t.Fatal(err)
		}
	}
	if accepted != 1 {
		t.Fatalf("committed %d requests from one preview", accepted)
	}
	backups, _ := filepath.Glob(filepath.Join(home, "state/tmux-roam/backups/*/tmux.conf"))
	if len(backups) != 1 {
		t.Fatal("unexpected backups", backups)
	}
}

func TestDanglingSymlinkChainsAndPreviewTargetChanges(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_STATE_HOME", filepath.Join(home, "state"))
	path, chain, final := filepath.Join(home, ".tmux.conf"), filepath.Join(home, "link"), filepath.Join(home, "missing.conf")
	if err := os.Symlink("link", path); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("missing.conf", chain); err != nil {
		t.Fatal(err)
	}
	resolved, err := destination(path)
	if err != nil || resolved != final {
		t.Fatalf("resolve: %q %v", resolved, err)
	}
	if err = os.Remove(chain); err != nil {
		t.Fatal(err)
	}
	if err = os.Symlink("other.conf", chain); err != nil {
		t.Fatal(err)
	}
	_, err = Handle(context.Background(), Request{Protocol: Protocol, Action: "sync", Tmux: []string{"tmux"}, Timeout: 3, CheckExpected: true, ExpectedPath: final})
	if err == nil || !strings.Contains(err.Error(), "target changed") {
		t.Fatalf("preview target race: %v", err)
	}
	if _, err = os.Lstat(final); !os.IsNotExist(err) {
		t.Fatal("unexpected file mutation", err)
	}
}
