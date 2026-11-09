package tmux

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

func TestSwitchChecksClientServerAndNavigationInQueue(t *testing.T) {
	if _, err := exec.LookPath("tmux"); err != nil {
		t.Skip("tmux unavailable")
	}
	socket := filepath.Join(t.TempDir(), "s")
	t.Setenv("TMUX", socket+",0,0")
	ctx := context.Background()
	if _, err := Run(ctx, "-f", "/dev/null", "new-session", "-d", "-s", "origin", "sleep 60"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = Run(ctx, "kill-server") })
	target, err := Run(ctx, "new-session", "-d", "-P", "-F", "#{session_id}", "-s", "target", "sleep 60")
	if err != nil {
		t.Fatal(err)
	}
	args := append(Base(), "-C", "attach-session", "-t", "origin")
	child := exec.Command(args[0], args[1:]...)
	input, err := child.StdinPipe()
	if err != nil {
		t.Fatal(err)
	}
	child.Stdout = os.Stderr
	if err = child.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { input.Close(); child.Process.Kill(); child.Wait() })
	var client Client
	for range 100 {
		name, _ := Run(ctx, "list-clients", "-F", "#{client_name}")
		client, err = CaptureClient(ctx, name)
		if err == nil {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if err != nil {
		t.Fatal(err)
	}
	version, err := Run(ctx, "display-message", "-p", "#{version}")
	if err != nil || client.Version != version || version == "" {
		t.Fatalf("client captured wrong server version: %q, want %q (%v)", client.Version, version, err)
	}
	_, _ = Run(ctx, "set-option", "-g", "@tmux-roam-navigation-"+client.Name, "current")
	for _, change := range []string{"token", "pid", "generation"} {
		stale, token := client, "current"
		switch change {
		case "token":
			token = "older"
		case "pid":
			stale.PID = "0"
		case "generation":
			stale.Generation = "0|0|old-socket"
		}
		if err := stale.Switch(ctx, target, token); err == nil {
			t.Fatalf("accepted stale %s", change)
		}
		actual, _ := CaptureClient(ctx, client.Name)
		if actual.Session != client.Session {
			t.Fatal("stale switch moved client")
		}
	}
	if err := client.Switch(ctx, target, "current", [2]string{"session_created", "0"}); err == nil {
		t.Fatal("accepted wrong target incarnation")
	}
	if err := client.Switch(ctx, target, "current"); err != nil {
		t.Fatal(err)
	}
	actual, _ := CaptureClient(ctx, client.Name)
	if actual.Session != target {
		t.Fatal("valid switch did not move client")
	}
	EndNavigation(ctx, client.Name, "older")
	if Option(ctx, "", "@tmux-roam-navigation-"+client.Name) != "current" {
		t.Fatal("stale cleanup cleared current navigation")
	}
}

func TestRequireLocalVersion(t *testing.T) {
	for _, version := range []string{"3.8", "3.8a", "3.9", "3.10", "4.0"} {
		if err := RequireLocalVersion(version); err != nil {
			t.Errorf("rejected supported version %q: %v", version, err)
		}
	}
	// Unknown/development versions need explicit validation before support.
	for _, version := range []string{"3.6", "3.6a", "3.7", "2.10", "", "next-3.9", "3.8-rc", "3.8.1", "3", "4.-1"} {
		if err := RequireLocalVersion(version); err == nil {
			t.Errorf("accepted unsupported version %q", version)
		}
	}
}
