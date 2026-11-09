package native

import (
	"context"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func TestStaleSearchCleanupPreservesNewerTree(t *testing.T) {
	if _, err := exec.LookPath("tmux"); err != nil {
		t.Skip("tmux unavailable")
	}
	t.Setenv("TMUX", filepath.Join(t.TempDir(), "s")+",0,0")
	ctx := context.Background()
	pane, err := tmux.Run(ctx, "-f", "/dev/null", "new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "keep", "sleep 60")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = tmux.Run(ctx, "kill-server") })
	args := append(tmux.Base(), "-C", "attach-session", "-t", "keep")
	client := exec.Command(args[0], args[1:]...)
	input, err := client.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err = client.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { input.Close(); client.Process.Kill(); client.Wait() })
	for range 100 {
		clients, _ := tmux.Run(ctx, "list-clients")
		if clients != "" {
			break
		}
		time.Sleep(time.Millisecond)
	}
	_, err = tmux.Run(ctx, "choose-tree", "-s", "-t", pane, ";", "set-option", "-g", "@tmux-roam-navigation-client", "new")
	if err != nil {
		t.Fatal(err)
	}
	Close(ctx, pane, "client", "old")
	mode, _ := tmux.Run(ctx, "display-message", "-p", "-t", pane, "#{pane_mode}")
	if mode != "tree-mode" {
		t.Fatal("stale cleanup closed newer tree")
	}
	Close(ctx, pane, "client", "new")
	mode, _ = tmux.Run(ctx, "display-message", "-p", "-t", pane, "#{pane_mode}")
	if mode != "" {
		t.Fatal("current cleanup did not close tree")
	}
	name, _ := tmux.Run(ctx, "list-clients", "-F", "#{client_name}")
	_, err = tmux.Run(ctx, "choose-tree", "-s", "-t", pane, ";", "set-option", "-g", "@tmux-roam-navigation-"+name, "refresh")
	if err != nil {
		t.Fatal(err)
	}
	refreshCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- Refresh(refreshCtx, config.Config{RefreshSeconds: 60}, name, pane, "refresh") }()
	registered := false
	for range 100 {
		hooks, _ := tmux.Run(ctx, "show-hooks", "-p", "-t", pane)
		if strings.Contains(hooks, "pane-mode-changed[") {
			registered = true
			break
		}
		time.Sleep(time.Millisecond)
	}
	if !registered {
		t.Fatal("refresh hook did not register")
	}
	_, err = tmux.Run(ctx, "new-session", "-d", "-s", "other", "sleep 60", ";", "switch-client", "-c", name, "-t", "other")
	if err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("native refresh survived invoking client switching away")
	}
}
