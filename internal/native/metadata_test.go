package native

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/session"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func TestLargeHostRetainsSessionsAndRemovesUntouchedMirrors(t *testing.T) {
	if _, err := exec.LookPath("tmux"); err != nil {
		t.Skip("tmux unavailable")
	}
	root := t.TempDir()
	t.Setenv("TMUX", filepath.Join(root, "s")+",0,0")
	t.Setenv("XDG_RUNTIME_DIR", filepath.Join(root, "r"))
	ctx := context.Background()
	if _, err := tmux.Run(ctx, "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 60"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = tmux.Run(ctx, "kill-server") })
	_, _ = tmux.Run(ctx, "set-option", "-g", "@tmux-roam-native-windows", "on")
	r := model.Record{ID: (model.Identity{Host: "remote", Socket: "/tmp/remote", Generation: "1/2", Session: "$0", Created: 3}).Encode(), HostID: "remote", HostLabel: "Remote", Name: "project", Windows: 1,
		WindowDetails: []map[string]any{{"id": "@0", "index": 0, "name": "shell"}}}
	if _, err := SyncHost(ctx, config.Host{ID: "remote"}, []model.Record{r}, true, nil); err != nil {
		t.Fatal(err)
	}
	carriers := session.Carriers(ctx)
	if len(carriers) != 1 || carriers[0].Windows != "2" {
		t.Fatalf("small inventory did not create mirror: %+v", carriers)
	}
	r.WindowDetails = nil
	for i := range WindowMirrorLimit + 1 {
		r.WindowDetails = append(r.WindowDetails, map[string]any{"id": fmt.Sprintf("@%d", i), "index": i, "name": fmt.Sprintf("window-%d", i)})
	}
	r.Windows = len(r.WindowDetails)
	if WindowMirrorCount([]model.Record{r}) != WindowMirrorLimit+1 || WindowMirrorCount([]model.Record{{WindowDetails: r.WindowDetails[:50]}, {WindowDetails: r.WindowDetails[50:100]}}) != WindowMirrorLimit {
		t.Fatal("mirror count did not aggregate the host inventory")
	}
	if _, err := SyncHost(ctx, config.Host{ID: "remote"}, []model.Record{r}, true, nil); err != nil {
		t.Fatal(err)
	}
	carriers = session.Carriers(ctx)
	if len(carriers) != 1 || carriers[0].Windows != "1" || carriers[0].ID != r.ID {
		t.Fatalf("large inventory lost session or retained untouched mirror: %+v", carriers)
	}
	if tmux.Option(ctx, carriers[0].Session, "@tmux-roam-windows") != "101" {
		t.Fatal("large inventory lost the real remote window count")
	}
	index, _ := tmux.Run(ctx, "display-message", "-p", "-t", carriers[0].Session, "#{window_index}")
	if index != "0" {
		t.Fatalf("large inventory did not restore carrier index: %s", index)
	}
}

func TestSyncHostRejectsUnsafeServerBeforeMutation(t *testing.T) {
	root := t.TempDir()
	log := filepath.Join(root, "calls")
	script := "#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + ssh.Quote(log) + "\nprintf '1|2|/tmp/test\\n3.6a\\n'\n"
	if err := os.WriteFile(filepath.Join(root, "tmux"), []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", root+":"+os.Getenv("PATH"))
	changed, err := SyncHost(context.Background(), config.Host{ID: "remote"}, []model.Record{{ID: "remote-entry"}}, true, nil)
	if changed || err == nil || !strings.Contains(err.Error(), "local tmux 3.8+") {
		t.Fatalf("unsafe server accepted: changed=%t, err=%v", changed, err)
	}
	calls, err := os.ReadFile(log)
	if err != nil || string(calls) != strings.Join(append(tmux.Base(), "display-message", "-p", "#{pid}|#{start_time}|#{socket_path}\n#{version}")[1:], " ")+"\n" {
		t.Fatalf("unsupported metadata refresh mutated server: %s (%v)", calls, err)
	}
}

func TestCachedLegacyEntryInitializesStatusColour(t *testing.T) {
	if _, err := exec.LookPath("tmux"); err != nil {
		t.Skip("tmux unavailable")
	}
	root := t.TempDir()
	t.Setenv("TMUX", filepath.Join(root, "s")+",0,0")
	t.Setenv("XDG_RUNTIME_DIR", filepath.Join(root, "r"))
	ctx := context.Background()
	if _, err := tmux.Run(ctx, "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 60"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = tmux.Run(ctx, "kill-server") })
	r := model.Record{ID: (model.Identity{Host: "remote", Socket: "/tmp/remote", Generation: "1/2", Session: "$0", Created: 3}).Encode(), HostID: "remote", HostLabel: "Remote", Name: "project"}
	sid, err := placeholder(ctx, r)
	if err != nil {
		t.Fatal(err)
	}
	for _, status := range []string{"connected", "disconnected", "available", "cached", "connecting", "ended", "available · host unavailable"} {
		if err := Decorate(ctx, sid, r, status); err != nil {
			t.Fatal(err)
		}
		if got := tmux.Option(ctx, sid, "@tmux-roam-status-colour"); got != StatusColour(Hint(r, status)) {
			t.Fatalf("%s colour: %s", status, got)
		}
	}
	// Python carriers can have Checking… without the Go colour option.
	_, _ = tmux.Run(ctx, "set-option", "-t", sid, "@tmux-roam-hint", "Checking…", ";", "set-option", "-u", "-t", sid, "@tmux-roam-status-colour")
	if _, err := SyncHost(ctx, config.Host{ID: "remote"}, []model.Record{r}, false, nil); err != nil {
		t.Fatal(err)
	}
	if got := tmux.Option(ctx, sid, "@tmux-roam-status-colour"); got != "#8aadf4" {
		t.Fatalf("cached legacy colour: %s", got)
	}
}

func TestCancelledPlaceholderRetainsCompleteOwnership(t *testing.T) {
	binary, err := exec.LookPath("tmux")
	if err != nil {
		t.Skip("tmux unavailable")
	}
	root := t.TempDir()
	t.Setenv("TMUX", filepath.Join(root, "s")+",0,0")
	t.Setenv("XDG_RUNTIME_DIR", filepath.Join(root, "r"))
	background := context.Background()
	if _, err = tmux.Run(background, "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 60"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = tmux.Run(background, "kill-server") })
	marker := filepath.Join(root, "created")
	wrapper := filepath.Join(root, "tmux")
	script := "#!/bin/sh\n" + ssh.Quote(binary) + " \"$@\" || exit $?\ncase \" $* \" in *' new-session '*) : > " + ssh.Quote(marker) + "; sleep .06;; esac\n"
	if err = os.WriteFile(wrapper, []byte(script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", root+":"+os.Getenv("PATH"))
	ctx, cancel := context.WithCancel(background)
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		for {
			if _, err := os.Stat(marker); err == nil {
				cancel()
				return
			}
			select {
			case <-ctx.Done():
				return
			case <-time.After(time.Millisecond):
			}
		}
	}()
	id := (model.Identity{Host: "remote", Socket: "/tmp/remote", Generation: "1/2", Session: "$0", Created: 3}).Encode()
	_, err = placeholder(ctx, model.Record{ID: id, HostID: "remote", HostLabel: "Remote", Name: "project"})
	cancel()
	<-done
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("expected cancellation, got %v", err)
	}
	carriers := session.Carriers(background)
	if len(carriers) != 1 || carriers[0].ID != id || carriers[0].State.Status != session.Idle {
		t.Fatalf("cancelled creation lost ownership: %+v", carriers)
	}
	if _, err = session.ManagedPane(background, carriers[0]); err != nil {
		t.Fatalf("cancelled placeholder cannot be safely reused: %v", err)
	}
}
