package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

func RuntimeDir() string {
	base := os.Getenv("XDG_RUNTIME_DIR")
	if base == "" {
		base = fmt.Sprintf("/tmp/tmux-roam-%d", os.Getuid())
	}
	return filepath.Join(base, "tmux-roam")
}

func PrivateDir(dir string) error {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	info, err := os.Lstat(dir)
	if err != nil {
		return err
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || !info.IsDir() || info.Mode().Perm()&0077 != 0 || stat.Uid != uint32(os.Getuid()) {
		return fmt.Errorf("unsafe private directory: %s", dir)
	}
	return nil
}

func ReadJSON(path string, limit int64, dst any) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return err
	}
	if int64(len(data)) > limit {
		return errors.New("JSON exceeds size limit")
	}
	return json.Unmarshal(data, dst)
}

func WriteJSON(ctx context.Context, path string, value any) error {
	return writeJSON(ctx, path, value, true)
}

// Transient files are atomically replaced but need not survive a power loss.
// Picker snapshots/sockets are discarded with their owning invocation.
func WriteJSONTransient(ctx context.Context, path string, value any) error {
	return writeJSON(ctx, path, value, false)
}

func writeJSON(ctx context.Context, path string, value any, durable bool) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return write(ctx, path, data, 0600, durable)
}

func Write(ctx context.Context, path string, data []byte, mode os.FileMode) error {
	return write(ctx, path, data, mode, true)
}

func WriteTransient(ctx context.Context, path string, data []byte, mode os.FileMode) error {
	return write(ctx, path, data, mode, false)
}

func write(ctx context.Context, path string, data []byte, mode os.FileMode, durable bool) error {
	if err := PrivateDir(filepath.Dir(path)); err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".roam-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if err = f.Chmod(mode); err != nil {
		return err
	}
	if _, err = f.Write(data); err != nil {
		return err
	}
	if durable {
		if err = f.Sync(); err != nil {
			return err
		}
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = ctx.Err(); err != nil {
		return err
	}
	return os.Rename(f.Name(), path)
}

// Acquire holds a short local ownership/publication lock. Never wait for SSH
// while holding it. Different targets use different lock files.
func Acquire(ctx context.Context, path string) (func(), error) {
	if err := PrivateDir(filepath.Dir(path)); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	for {
		if err = ctx.Err(); err != nil {
			f.Close()
			return nil, err
		}
		err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			return func() { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN); _ = f.Close() }, nil
		}
		if !errors.Is(err, syscall.EWOULDBLOCK) && !errors.Is(err, syscall.EINTR) {
			f.Close()
			return nil, err
		}
		select {
		case <-ctx.Done():
			f.Close()
			return nil, ctx.Err()
		case <-time.After(10 * time.Millisecond):
		}
	}
}
