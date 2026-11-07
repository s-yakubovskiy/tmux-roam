package inventory

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
)

func TestReconcileTracksRemoteClientAndUpdatesIdentity(t *testing.T) {
	if _, err := exec.LookPath("tmux"); err != nil {
		t.Skip("tmux unavailable")
	}
	dir := t.TempDir()
	sock := filepath.Join(dir, "local.sock")
	t.Setenv("TMUX", sock+",0,0")
	t.Setenv("XDG_RUNTIME_DIR", dir)
	run := func(args ...string) string {
		t.Helper()
		out, err := exec.Command("tmux", append([]string{"-S", sock}, args...)...).CombinedOutput()
		if err != nil {
			t.Fatalf("tmux %v: %v: %s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	run("-f", "/dev/null", "new-session", "-d", "-s", "carrier")
	t.Cleanup(func() { _ = exec.Command("tmux", "-S", sock, "kill-server").Run() })
	localID := run("display-message", "-p", "-t", "carrier", "#{session_id}")
	old := (model.Identity{Host: "remote", Socket: "/tmp/remote.sock", Generation: "12/34", Session: "$1", Created: 100}).Encode()
	newID := (model.Identity{Host: "remote", Socket: "/tmp/remote.sock", Generation: "12/34", Session: "$2", Created: 101}).Encode()
	run("set-option", "-t", localID, "@tmux-roam-owner", "1")
	run("set-option", "-t", localID, "@tmux-roam-identity", old)
	run("set-option", "-t", localID, "@tmux-roam-state", "test-token")
	if err := os.MkdirAll(runtimeDir(), 0700); err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(map[string]any{"pid": 123, "tty": "/dev/pts/9", "status": "open"})
	if err := os.WriteFile(filepath.Join(runtimeDir(), "test-token.json"), b, 0600); err != nil {
		t.Fatal(err)
	}
	rows := []model.Record{{ID: newID, HostID: "remote", Socket: "/tmp/remote.sock", Generation: "12/34", SessionID: "$2", Created: 101}}
	clients := map[string]string{"123|/dev/pts/9": "$2"}
	reconcile(context.Background(), config.Host{ID: "remote"}, rows, clients, false)
	if rows[0].State != "open here" {
		t.Fatalf("state %q", rows[0].State)
	}
	if got := run("show-options", "-v", "-t", localID, "@tmux-roam-identity"); got != old {
		t.Fatalf("read-only reconcile changed identity: %q", got)
	}
	reconcile(context.Background(), config.Host{ID: "remote"}, rows, clients, true)
	if got := run("show-options", "-v", "-t", localID, "@tmux-roam-identity"); got != newID {
		t.Fatalf("update did not track remote move: %q", got)
	}
}
