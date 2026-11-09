package ssh

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
)

// Quote and Join quote POSIX shell words, never tmux formats.
func Quote(s string) string { return "'" + strings.ReplaceAll(s, "'", "'\\''") + "'" }
func Join(args []string) string {
	parts := make([]string, len(args))
	for i, a := range args {
		parts[i] = Quote(a)
	}
	return strings.Join(parts, " ")
}

func Args(h config.Host, c config.Config, tty bool) ([]string, error) {
	if err := store.PrivateDir(store.RuntimeDir()); err != nil {
		return nil, err
	}
	args := []string{"ssh"}
	cfg := os.Getenv("TMUX_ROAM_SSH_CONFIG")
	if cfg != "" {
		args = append(args, "-F", cfg)
	}
	hash := sha256.Sum256([]byte(h.SSH + "\x00" + cfg))
	control := filepath.Join(store.RuntimeDir(), "ssh-"+hex.EncodeToString(hash[:])[:8]+"-%C")
	if len(strings.ReplaceAll(control, "%C", strings.Repeat("0", 40))) <= 90 {
		args = append(args, "-o", "ControlMaster=auto", "-o", "ControlPersist=60", "-o", "ControlPath="+control)
	}
	args = append(args, "-o", "BatchMode=yes", "-o", fmt.Sprintf("ConnectTimeout=%d", c.ConnectTimeoutSeconds), "-o", "ConnectionAttempts=1", "-o", "RemoteCommand=none", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3")
	if tty {
		args = append(args, "-tt")
	} else {
		args = append(args, "-T")
	}
	return append(args, "--", h.SSH), nil
}
