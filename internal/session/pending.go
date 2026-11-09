package session

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
)

// Each waiting opener holds a lease. Cancellation may stop an unused attempt
// only after the last waiter leaves; another client's open keeps its attempt.
func pending(ctx context.Context, carrier Carrier, created bool) (func(bool), error) {
	prefix := "opening-" + carrier.Token + "-"
	creation := filepath.Join(store.RuntimeDir(), "opening-created-"+carrier.Token)
	if created {
		if err := store.Write(ctx, creation, nil, 0600); err != nil {
			return nil, err
		}
	}
	lease := filepath.Join(store.RuntimeDir(), prefix+strconv.Itoa(os.Getpid())+"-"+Token())
	if err := store.Write(ctx, lease, nil, 0600); err != nil {
		return nil, err
	}
	return func(success bool) {
		cleanup, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		release, err := store.Acquire(cleanup, targetLock(carrier.ID))
		if err != nil {
			_ = os.Remove(lease)
			return
		}
		defer release()
		_ = os.Remove(lease)
		if success {
			_ = os.Remove(creation)
			return
		}
		files, err := os.ReadDir(store.RuntimeDir())
		if err != nil {
			return
		}
		for _, f := range files {
			if !strings.HasPrefix(f.Name(), prefix) {
				continue
			}
			fields := strings.Split(strings.TrimPrefix(f.Name(), prefix), "-")
			pid, err := strconv.Atoi(fields[0])
			if err != nil || pid <= 0 {
				return
			}
			if syscall.Kill(pid, 0) == nil {
				return
			}
			_ = os.Remove(filepath.Join(store.RuntimeDir(), f.Name()))
		}
		carrier.State = ReadState(carrier.Token)
		pane, err := ManagedPane(cleanup, carrier)
		if err != nil || pane.Attached != "0" {
			return
		}
		if _, err := os.Stat(creation); err == nil {
			// Guard against all ownership changes in the same tmux command queue.
			_ = pane.Command(cleanup, "kill-session -t "+carrier.Session)
			_ = os.Remove(creation)
			return
		}
		// Preserve the existing pane. Terminate only an SSH child still parented
		// by its proven supervisor, so it returns to the retry prompt.
		pid := carrier.State.SSHPID
		if pid <= 0 {
			return
		}
		stat, err := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
		if err != nil {
			return
		}
		end := strings.LastIndex(string(stat), ")")
		if end < 0 {
			return
		}
		fields := strings.Fields(string(stat)[end+1:])
		if len(fields) > 1 && fields[1] == pane.PID {
			_ = syscall.Kill(pid, syscall.SIGTERM)
		}
	}, nil
}
