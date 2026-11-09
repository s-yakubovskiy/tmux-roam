package inventory

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"sync"
	"syscall"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
)

type observation struct {
	Snapshot  Snapshot `json:"snapshot"`
	Error     string   `json:"error,omitempty"`
	Cancelled bool     `json:"cancelled,omitempty"`
}

// RemoteSnapshot shares only an overlapping observation. The listening process
// exits with its invocation; no daemon or inventory timer is introduced. The
// short ownership lock is released before SSH or waiting for a peer response.
func RemoteSnapshot(ctx context.Context, h config.Host, c config.Config, update bool) (Snapshot, error) {
	if err := store.PrivateDir(store.RuntimeDir()); err != nil {
		return Snapshot{}, err
	}
	key, _ := json.Marshal(struct {
		Host             config.Host
		SSHConfig, Local string
		Update           bool
	}{h, os.Getenv("TMUX_ROAM_SSH_CONFIG"), os.Getenv("TMUX"), update})
	sum := sha256.Sum256(key)
	path := filepath.Join(store.RuntimeDir(), "inventory-"+hex.EncodeToString(sum[:])[:20]+".sock")
	if len(path) > 100 {
		return remoteSnapshot(ctx, h, c, update)
	}
	for range 8 {
		release, err := store.Acquire(ctx, path+".lock")
		if err != nil {
			return Snapshot{}, err
		}
		conn, dialErr := (&net.Dialer{Timeout: 50 * time.Millisecond}).DialContext(ctx, "unix", path)
		if dialErr == nil {
			release()
			stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
			_ = conn.SetReadDeadline(time.Now().Add(time.Duration(c.CommandTimeoutSeconds+6) * time.Second))
			var response observation
			err = json.NewDecoder(io.LimitReader(conn, 4<<20)).Decode(&response)
			stop()
			_ = conn.Close()
			if ctx.Err() != nil {
				return Snapshot{}, ctx.Err()
			}
			if err != nil || response.Cancelled {
				// The owner's cancellation closes peers before its SSH child has
				// necessarily exited and its listener has been removed. Give that
				// bounded teardown time instead of reconnecting to it in a tight loop.
				select {
				case <-ctx.Done():
					return Snapshot{}, ctx.Err()
				case <-time.After(20 * time.Millisecond):
				}
				continue
			}
			if response.Error != "" {
				return Snapshot{}, errors.New(response.Error)
			}
			return response.Snapshot, nil
		}
		if ctx.Err() != nil {
			release()
			return Snapshot{}, ctx.Err()
		}
		if !errors.Is(dialErr, syscall.ECONNREFUSED) && !errors.Is(dialErr, os.ErrNotExist) {
			release()
			return Snapshot{}, dialErr
		}
		_ = os.Remove(path)
		listener, err := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
		release()
		if err != nil {
			continue
		}
		if err = os.Chmod(path, 0600); err != nil {
			listener.Close()
			return Snapshot{}, err
		}
		ready := make(chan struct{})
		var response observation
		var peers sync.WaitGroup
		acceptDone := make(chan struct{})
		go func() {
			defer close(acceptDone)
			for {
				peer, err := listener.AcceptUnix()
				if err != nil {
					return
				}
				peers.Add(1)
				go func() {
					defer peers.Done()
					defer peer.Close()
					stop := context.AfterFunc(ctx, func() { _ = peer.Close() })
					defer stop()
					select {
					case <-ctx.Done():
						return
					case <-ready:
					}
					_ = peer.SetWriteDeadline(time.Now().Add(200 * time.Millisecond))
					_ = json.NewEncoder(peer).Encode(response)
				}()
			}
		}()
		rows, observeErr := remoteSnapshot(ctx, h, c, update)
		response = observation{Snapshot: rows}
		if observeErr != nil {
			response.Error = observeErr.Error()
			response.Cancelled = errors.Is(observeErr, context.Canceled) || errors.Is(observeErr, context.DeadlineExceeded)
		}
		close(ready)
		_ = listener.Close()
		<-acceptDone
		peers.Wait()
		return rows, observeErr
	}
	return Snapshot{}, fmt.Errorf("concurrent inventory owner changed repeatedly; retry refresh")
}
