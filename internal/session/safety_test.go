package session

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func isolated(t *testing.T) (context.Context, string) {
	t.Helper()
	binary, err := exec.LookPath("tmux")
	if err != nil {
		t.Skip("tmux unavailable")
	}
	root, err := os.MkdirTemp("", "tr-safety-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	socket := filepath.Join(root, "s")
	t.Setenv("TMUX", socket+",0,0")
	t.Setenv("XDG_RUNTIME_DIR", root)
	ctx := context.Background()
	if _, err = tmux.Run(ctx, "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 120"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = exec.Command(binary, "-S", socket, "kill-server").Run() })
	return ctx, root
}

func TestCancelledMirrorRetainsCompleteOwnership(t *testing.T) {
	for _, phase := range []string{"reservation", "mirror"} {
		t.Run(phase, func(t *testing.T) {
			background, root := isolated(t)
			binary, _ := exec.LookPath("tmux")
			_, _ = tmux.Run(background, "set-option", "-g", "@tmux-roam-native-windows", "on")
			sid, _ := tmux.Run(background, "display-message", "-p", "#{session_id}")
			marker := filepath.Join(root, "created")
			wrapper := filepath.Join(root, "tmux")
			pattern := "new-window "
			if phase == "reservation" {
				pattern = "@tmux-roam-carrier-index "
			}
			script := "#!/bin/sh\n" + ssh.Quote(binary) + " \"$@\" || exit $?\ncase \" $* \" in *'" + pattern + "'*) : > " + ssh.Quote(marker) + "; sleep .06;; esac\n"
			if err := os.WriteFile(wrapper, []byte(script), 0700); err != nil {
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
			err := SyncWindows(ctx, sid, model.Record{ID: "mirror-owner", WindowDetails: []map[string]any{{"id": "@1", "index": 0, "name": "remote"}}})
			cancel()
			<-done
			if !errors.Is(err, context.Canceled) {
				t.Fatalf("expected cancellation, got %v", err)
			}
			entries := windows(background, sid)
			if phase == "reservation" {
				if len(entries) != 1 || entries[0].Original != "0" || entries[0].Index != entries[0].Reserved {
					t.Fatalf("cancelled reservation is incomplete: %+v", entries)
				}
			} else if len(entries) != 2 {
				t.Fatalf("missing cancelled mirror: %+v", entries)
			}
			for _, entry := range entries {
				if entry.Index == "0" && (entry.Owner != "mirror-owner" || entry.Remote != "@1") {
					t.Fatalf("cancelled mirror lost ownership: %+v", entry)
				}
			}
			if err := RemoveWindows(background, sid); err != nil {
				t.Fatal(err)
			}
			if entries = windows(background, sid); len(entries) != 1 || entries[0].Index != "0" {
				t.Fatalf("cancelled mirror cannot be safely removed: %+v", entries)
			}
		})
	}
}

func TestCleanupPreservesUserChanges(t *testing.T) {
	ctx, _ := isolated(t)
	id := model.Identity{Host: "remote", Socket: "/tmp/remote.sock", Generation: "12/34", Session: "$1", Created: 100}.Encode()
	for _, change := range []string{"untouched", "renamed", "split", "linked", "repurposed", "attached"} {
		t.Run(change, func(t *testing.T) {
			token := Token()
			command := ssh.Join([]string{"sh", "-c", "while :; do sleep 30; done", "supervise", token, "remote", id})
			sid, err := tmux.Run(ctx, "new-session", "-d", "-P", "-F", "#{session_id}", "-s", change, command)
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _, _ = tmux.Run(ctx, "kill-session", "-t", sid) })
			name, _ := tmux.Run(ctx, "display-message", "-p", "-t", sid, "#{session_name}")
			for _, pair := range [][2]string{{Owner, "1"}, {Identity, id}, {StateOption, token}, {"@tmux-roam-name", name}} {
				if _, err = tmux.Run(ctx, "set-option", "-t", sid, pair[0], pair[1]); err != nil {
					t.Fatal(err)
				}
			}
			actual, _ := tmux.Run(ctx, "display-message", "-p", "-t", sid, "#{pane_start_command}")
			if err = WriteState(ctx, token, State{Status: Disconnected, Command: actual}); err != nil {
				t.Fatal(err)
			}
			c := Carrier{Session: sid, ID: id, Token: token, State: ReadState(token)}
			switch change {
			case "renamed":
				_, err = tmux.Run(ctx, "rename-session", "-t", sid, "user-renamed")
			case "split":
				_, err = tmux.Run(ctx, "split-window", "-d", "-t", sid, "sleep 120")
			case "linked":
				_, err = tmux.Run(ctx, "link-window", "-s", sid+":0", "-t", "keep:9")
			case "repurposed":
				_, err = tmux.Run(ctx, "respawn-pane", "-k", "-t", sid, "sleep 120")
			case "attached":
				args := append(tmux.Base(), "-C", "attach-session", "-t", sid)
				cmd := exec.Command(args[0], args[1:]...)
				input, pipeErr := cmd.StdinPipe()
				if pipeErr != nil {
					t.Fatal(pipeErr)
				}
				if err = cmd.Start(); err != nil {
					t.Fatal(err)
				}
				t.Cleanup(func() { input.Close(); cmd.Process.Kill(); cmd.Wait() })
				// Queueing a control command establishes attachment before cleanup.
				_, err = input.Write([]byte("display-message -p ready\n"))
				for range 100 {
					count, _ := tmux.Run(ctx, "display-message", "-p", "-t", sid, "#{session_attached}")
					if count != "0" {
						break
					}
				}
			}
			if err != nil {
				t.Fatal(err)
			}
			removed, err := Disconnect(ctx, c)
			if err != nil || removed != (change == "untouched") {
				t.Fatalf("cleanup %s: removed=%v err=%v", change, removed, err)
			}
		})
	}
}

func TestCreateDeleteExactIdentityAndShellBoundaries(t *testing.T) {
	ctx, root := isolated(t)
	actual, err := exec.LookPath("tmux")
	if err != nil {
		t.Fatal(err)
	}
	bin := filepath.Join(root, "bin")
	os.Mkdir(bin, 0700)
	script := filepath.Join(bin, "ssh")
	// A test adapter executes the exact remote command against a private server.
	err = os.WriteFile(script, []byte("#!/bin/sh\nwhile [ \"$1\" != -- ]; do shift; done\nshift; shift\nexec sh -c \"$1\"\n"), 0700)
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+":"+os.Getenv("PATH"))
	socket := strings.Split(os.Getenv("TMUX"), ",")[0]
	h := config.Host{ID: "remote", Label: "Remote", SSH: "test", Enabled: true, TmuxPath: actual, SocketPath: socket}
	c := config.Config{Hosts: []config.Host{h}, ConnectTimeoutSeconds: 2, CommandTimeoutSeconds: 3, CacheSeconds: 30}
	directory := filepath.Join(root, "project '$ literal")
	if err = os.Mkdir(directory, 0700); err != nil {
		t.Fatal(err)
	}
	first, err := Create(ctx, h, c, "--leading-$(literal)", directory)
	if err != nil {
		t.Fatal(err)
	}
	second, err := Create(ctx, h, c, "independent", directory)
	if err != nil {
		t.Fatal(err)
	}
	if first.ID == second.ID {
		t.Fatal("independent projects share identity")
	}
	if _, err = Create(ctx, h, c, first.Name, directory); err == nil {
		t.Fatal("duplicate overwrote existing session")
	}
	for _, name := range []string{"bad.name", "bad:name", "bad;name", "bad\nname", " edge"} {
		if _, err = Create(ctx, h, c, name, directory); err == nil {
			t.Fatal("invalid name accepted", name)
		}
	}
	if _, err = Create(ctx, h, c, "missing", filepath.Join(root, "does not exist")); err == nil {
		t.Fatal("missing directory accepted")
	}
	if err = Delete(ctx, c, first); err != nil {
		t.Fatal(err)
	}
	if err = Delete(ctx, c, first); err == nil {
		t.Fatal("stale identity accepted")
	}
	alive, err := tmux.Run(ctx, "display-message", "-p", "-t", second.SessionID, "#{session_name}")
	if err != nil || alive != "independent" {
		t.Fatal("unrelated project changed", alive, err)
	}
}

func TestWindowMirrorsPreserveEditsAndRestoreCarrierIndex(t *testing.T) {
	ctx, _ := isolated(t)
	sid, err := tmux.Run(ctx, "display-message", "-p", "-t", "keep", "#{session_id}")
	if err != nil {
		t.Fatal(err)
	}
	original, err := tmux.Run(ctx, "display-message", "-p", "-t", sid, "#{window_id}|#{window_index}")
	if err != nil {
		t.Fatal(err)
	}
	_, err = tmux.Run(ctx, "set-option", "-g", "@tmux-roam-native-windows", "on")
	if err != nil {
		t.Fatal(err)
	}
	r := model.Record{ID: "mirror-owner", WindowDetails: []map[string]any{
		{"id": "@101", "index": 0, "name": "editor 👩‍💻", "active": true},
		{"id": "@102", "index": 3, "name": "logs", "active": false},
	}}
	if err = SyncWindows(ctx, sid, r); err != nil {
		t.Fatal(err)
	}
	entries := windows(ctx, sid)
	if len(entries) != 3 {
		t.Fatal("missing mirrors", entries)
	}
	var edited mirror
	for _, w := range entries {
		if w.Owner == r.ID && w.Remote == "@101" {
			edited = w
		}
	}
	if edited.Window == "" {
		t.Fatal("mirror not found")
	}
	if _, err = tmux.Run(ctx, "rename-window", "-t", edited.Window, "user-edited"); err != nil {
		t.Fatal(err)
	}
	r.WindowDetails[0]["name"] = "remote-renamed"
	r.WindowDetails[1]["name"] = "new logs"
	if err = SyncWindows(ctx, sid, r); err != nil {
		t.Fatal(err)
	}
	for _, entry := range windows(ctx, sid) {
		name, _ := tmux.Run(ctx, "display-message", "-p", "-t", entry.Window, "#{window_name}")
		if entry.Remote == "@101" && name != "user-edited" || entry.Remote == "@102" && name != "new logs" {
			t.Fatalf("mirror rename guard failed: %+v %s", entry, name)
		}
	}
	if err = RemoveWindows(ctx, sid); err != nil {
		t.Fatal(err)
	}
	if len(windows(ctx, sid)) != 2 {
		t.Fatal("modified mirror removed or untouched mirror retained")
	}
	if _, err = tmux.Run(ctx, "rename-window", "-t", edited.Window, "editor 👩‍💻"); err != nil {
		t.Fatal(err)
	}
	if err = RemoveWindows(ctx, sid); err != nil {
		t.Fatal(err)
	}
	restored, err := tmux.Run(ctx, "display-message", "-p", "-t", sid, "#{window_id}|#{window_index}")
	if err != nil || restored != original || len(windows(ctx, sid)) != 1 {
		t.Fatal("carrier index not restored", original, restored, err)
	}
}
