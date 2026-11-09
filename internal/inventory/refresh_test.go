package inventory

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
)

func refreshFixture(t *testing.T, script string) (string, config.Config) {
	t.Helper()
	dir := t.TempDir()
	t.Setenv("XDG_RUNTIME_DIR", filepath.Join(dir, "run"))
	t.Setenv("XDG_CACHE_HOME", filepath.Join(dir, "cache"))
	t.Setenv("TMUX", filepath.Join(dir, "absent.sock")+",0,0")
	t.Setenv("PATH", dir+":"+os.Getenv("PATH"))
	t.Setenv("TEST_REFRESH_DIR", dir)
	output := "12|34|/tmp/remote.sock\n$0|100|project|1|0|C-b\n" + clientMarker + "\n" + windowMarker + "\n$0|@0|0|1|shell\n"
	if err := os.WriteFile(filepath.Join(dir, "output"), []byte(output), 0600); err != nil {
		t.Fatal(err)
	}
	content := `#!/bin/sh
while [ "$1" != -- ]; do shift; done
shift
host=$1
cd "$TEST_REFRESH_DIR" || exit 1
echo "$host" >> calls
echo $$ > "$host.started"
` + script + "\ncat output\n"
	if err := os.WriteFile(filepath.Join(dir, "ssh"), []byte(content), 0700); err != nil {
		t.Fatal(err)
	}
	c := config.Config{Concurrency: 2, CommandTimeoutSeconds: 5, ConnectTimeoutSeconds: 1}
	return dir, c
}

func waitForFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if b, err := os.ReadFile(path); err == nil && len(b) > 0 {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", path)
}

func TestRefresherCoalescesAndCancelledWaiterPreservesOwner(t *testing.T) {
	dir, c := refreshFixture(t, `while [ ! -f release ]; do sleep 0.01; done`)
	r := NewRefresher(context.Background(), c, false)
	defer r.Close()
	host := config.Host{ID: "one", SSH: "one", Label: "one", TmuxPath: "tmux"}
	owner := make(chan HostResult, 1)
	go func() { owner <- r.Refresh(context.Background(), host) }()
	waitForFile(t, filepath.Join(dir, "one.started"))
	ctx, cancel := context.WithTimeout(context.Background(), 80*time.Millisecond)
	defer cancel()
	if result := r.Refresh(ctx, host); !errors.Is(result.Err, context.DeadlineExceeded) {
		t.Fatalf("cancelled waiter: %v", result.Err)
	}
	if err := os.WriteFile(filepath.Join(dir, "release"), []byte("go"), 0600); err != nil {
		t.Fatal(err)
	}
	result := <-owner
	if result.Err != nil || len(result.Records) != 1 || len(Cached(host)) != 1 {
		t.Fatalf("owner lost its result: %+v", result)
	}
	calls, err := os.ReadFile(filepath.Join(dir, "calls"))
	if err != nil || string(calls) != "one\n" {
		t.Fatalf("overlapping refreshes invoked extra SSH: %q %v", calls, err)
	}
}

func TestRefreshPublishesFastHostAndCancelsSlowChild(t *testing.T) {
	dir, c := refreshFixture(t, `if [ "$host" = slow ]; then exec sleep 30; fi
while [ ! -f slow.started ]; do sleep 0.01; done`)
	c.Hosts = []config.Host{{ID: "slow", SSH: "slow", Enabled: true}, {ID: "fast", SSH: "fast", Enabled: true}}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	start := time.Now()
	var published []string
	RefreshHosts(ctx, c, false, func(result HostResult) {
		published = append(published, result.Host.ID)
		if result.Err != nil || len(Cached(result.Host)) != 1 {
			t.Errorf("fast host not independently published: %+v", result)
		}
		cancel()
	})
	if strings.Join(published, ",") != "fast" {
		t.Fatalf("published after cancellation: %v", published)
	}
	if elapsed := time.Since(start); elapsed > time.Second {
		t.Fatalf("slow host delayed fast host/cancellation: %v", elapsed)
	}
	pid, err := os.ReadFile(filepath.Join(dir, "slow.started"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join("/proc", strings.TrimSpace(string(pid)))); !os.IsNotExist(err) {
		t.Fatalf("slow SSH child still exists: %s %v", pid, err)
	}
	if _, err := os.Stat(filepath.Join(CacheDir(), "slow.json")); !os.IsNotExist(err) {
		t.Fatalf("cancelled host published a cache: %v", err)
	}
}

func TestRefresherBoundsHostConcurrency(t *testing.T) {
	dir, c := refreshFixture(t, `while [ ! -f release ]; do sleep 0.01; done`)
	r := NewRefresher(context.Background(), c, false)
	defer r.Close()
	results := make(chan HostResult, 8)
	for i := range 8 {
		host := config.Host{ID: fmt.Sprint(i), SSH: fmt.Sprint(i)}
		go func() { results <- r.Refresh(context.Background(), host) }()
	}
	deadline := time.Now().Add(3 * time.Second)
	for {
		started, _ := filepath.Glob(filepath.Join(dir, "*.started"))
		if len(started) >= c.Concurrency {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("refresh workers did not start")
		}
		time.Sleep(5 * time.Millisecond)
	}
	time.Sleep(50 * time.Millisecond)
	started, _ := filepath.Glob(filepath.Join(dir, "*.started"))
	if len(started) != c.Concurrency {
		t.Fatalf("started %d commands for concurrency %d", len(started), c.Concurrency)
	}
	if err := os.WriteFile(filepath.Join(dir, "release"), []byte("go"), 0600); err != nil {
		t.Fatal(err)
	}
	for range 8 {
		if result := <-results; result.Err != nil {
			t.Fatal(result.Err)
		}
	}
	// Also verify a closed invocation cannot start new SSH work.
	r.Close()
	if result := r.Refresh(context.Background(), config.Host{ID: "9"}); !errors.Is(result.Err, context.Canceled) {
		t.Fatalf("closed refresher accepted work: %v", result.Err)
	}
}
