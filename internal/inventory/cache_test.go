package inventory

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"testing"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
)

func TestCacheConcurrentPublicationCancellationAndClockStep(t *testing.T) {
	t.Setenv("XDG_CACHE_HOME", t.TempDir())
	host := config.Host{ID: "remote"}
	id := model.Identity{Host: host.ID, Socket: "/tmp/remote.sock", Generation: "12/34", Session: "$0", Created: 100}
	row := model.Record{ID: id.Encode(), HostID: host.ID, Socket: id.Socket, Generation: id.Generation,
		SessionID: id.Session, Created: id.Created, WindowDetails: []map[string]any{{"id": "@0"}}}
	stamp := float64(time.Now().UnixNano())/1e9 - 60
	var wg sync.WaitGroup
	for i := range 20 {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			r := row
			r.Observed = stamp + float64(i)
			if err := Save(host, []model.Record{r}); err != nil {
				t.Error(err)
			}
			if got := Cached(host); len(got) != 1 || len(got[0].WindowDetails) != 1 {
				t.Errorf("reader saw incomplete cache: %+v", got)
			}
		}(i)
	}
	wg.Wait()
	if got := Cached(host); len(got) != 1 || got[0].Observed != stamp+19 {
		t.Fatalf("older writer replaced newer observation: %+v", got)
	}
	lock, err := os.OpenFile(filepath.Join(CacheDir(), host.ID+".lock"), os.O_RDWR, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer lock.Close()
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_EX); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	start := time.Now()
	if err := SaveContext(ctx, host, nil); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("blocked publication did not cancel: %v", err)
	}
	if time.Since(start) > time.Second {
		t.Fatal("publication lock was not promptly cancellable")
	}
	if err := syscall.Flock(int(lock.Fd()), syscall.LOCK_UN); err != nil {
		t.Fatal(err)
	}

	// A prior observation in the future must not freeze all later refreshes.
	row.Observed = float64(time.Now().Add(time.Hour).Unix())
	if err := Save(host, []model.Record{row}); err != nil {
		t.Fatal(err)
	}
	row.Observed = float64(time.Now().Unix())
	if err := Save(host, []model.Record{row}); err != nil {
		t.Fatal(err)
	}
	if got := Cached(host); len(got) != 1 || got[0].Observed != row.Observed {
		t.Fatalf("cache froze after wall-clock step: %+v", got)
	}
	// An empty successful inventory is a newer observation too.
	if err := Save(host, nil); err != nil {
		t.Fatal(err)
	}
	if err := Save(host, []model.Record{row}); err != nil {
		t.Fatal(err)
	}
	if got := Cached(host); len(got) != 0 {
		t.Fatalf("older writer resurrected empty inventory: %+v", got)
	}
	data, err := readCache(host)
	if err != nil {
		t.Fatal(err)
	}
	var cached cacheFile
	if json.Unmarshal(data, &cached) != nil || cached.Captured == 0 {
		t.Fatal("empty inventory lost its observation time")
	}
}
