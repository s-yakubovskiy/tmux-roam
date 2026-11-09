package configsync

import (
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func TestStaleCleanupProtectsLiveUnmarkedAndSymlinkedDirectories(t *testing.T) {
	parent := t.TempDir()
	old := time.Now().Add(-48 * time.Hour)
	for _, name := range []string{"dead", "live", "new", "unmarked", "public"} {
		directory := filepath.Join(parent, "tmux-roam-sync."+name)
		if err := os.Mkdir(directory, 0700); err != nil {
			t.Fatal(err)
		}
		pid := "2147483647" // Linux PID maximum is smaller; never probe PID 0/-1.
		if name == "live" {
			pid = strconv.Itoa(os.Getpid())
		}
		if name != "unmarked" {
			marker := filepath.Join(directory, "owner")
			if err := os.WriteFile(marker, []byte(stageOwner+pid+"\n"), 0600); err != nil {
				t.Fatal(err)
			}
			if name != "new" {
				if err := os.Chtimes(marker, old, old); err != nil {
					t.Fatal(err)
				}
			}
		}
		if name == "public" {
			os.Chmod(directory, 0755)
		}
	}
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(parent, "tmux-roam-sync.link")); err != nil {
		t.Fatal(err)
	}
	cleanupStale(parent, "tmux-roam-sync.")
	for _, name := range []string{"dead", "live", "new", "unmarked", "public", "link"} {
		_, err := os.Lstat(filepath.Join(parent, "tmux-roam-sync."+name))
		if os.IsNotExist(err) != (name == "dead") {
			t.Fatalf("cleanup %s: %v", name, err)
		}
	}
	if _, err := os.Stat(outside); err != nil {
		t.Fatal("symlink target changed", err)
	}
}
