package configsync

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const stageOwner = "tmux-roam-sync-v1\n"

// The bootstrap writes its PID before receiving the archive. Transactions use
// the helper PID. A reused/live PID is conservatively retained; age alone never
// authorizes removal of an operation that might still be running.
func markStage(directory string) error {
	return os.WriteFile(filepath.Join(directory, "owner"), []byte(fmt.Sprintf("%s%d\n", stageOwner, os.Getpid())), 0600)
}

func cleanupStale(parent, prefix string) {
	root, err := os.OpenRoot(parent)
	if err != nil {
		return
	}
	defer root.Close()
	entries, err := os.ReadDir(parent)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if !strings.HasPrefix(entry.Name(), prefix) {
			continue
		}
		info, err := root.Lstat(entry.Name())
		if err != nil || !info.IsDir() || info.Mode().Perm() != 0700 {
			continue
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || stat.Uid != uint32(os.Getuid()) {
			continue
		}
		marker := filepath.Join(entry.Name(), "owner")
		info, err = root.Lstat(marker)
		if err != nil || !info.Mode().IsRegular() || info.Size() > 80 || time.Since(info.ModTime()) < 24*time.Hour {
			continue
		}
		data, err := root.ReadFile(marker)
		if err != nil || !strings.HasPrefix(string(data), stageOwner) {
			continue
		}
		pid, err := strconv.Atoi(strings.TrimSpace(strings.TrimPrefix(string(data), stageOwner)))
		if err != nil || pid < 1 || !errors.Is(syscall.Kill(pid, 0), syscall.ESRCH) {
			continue
		}
		// Root confines traversal; RemoveAll does not follow directory symlinks.
		_ = root.RemoveAll(entry.Name())
	}
}
