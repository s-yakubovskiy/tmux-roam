// Package release installs verified, immutable controller/helper bundles.
package release

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
)

var Files = []string{"tmux-roam", "agents/linux_amd64", "agents/linux_arm64"}

type Manifest struct {
	Version  string            `json:"version"`
	Build    string            `json:"build"`
	OS       string            `json:"os"`
	Arch     string            `json:"arch"`
	Protocol int               `json:"agent_protocol"`
	SHA256   map[string]string `json:"sha256"`
}

func Inspect(dir, version string) (Manifest, error) {
	var m Manifest
	if err := store.ReadJSON(filepath.Join(dir, "manifest.json"), 16384, &m); err != nil {
		return m, err
	}
	if m.Version != version || m.OS != runtime.GOOS || m.Arch != runtime.GOARCH || m.Protocol != 1 {
		return m, errors.New("bundle version, platform, or helper protocol mismatch")
	}
	if len(m.Build) != 64 {
		return m, errors.New("invalid build identity")
	}
	if _, err := hex.DecodeString(m.Build); err != nil {
		return m, err
	}
	if len(m.SHA256) != len(Files) {
		return m, errors.New("unexpected bundle file set")
	}
	for _, name := range append(append([]string{}, Files...), "manifest.json") {
		path := filepath.Join(dir, name)
		info, err := os.Lstat(path)
		if err != nil {
			return m, err
		}
		if !info.Mode().IsRegular() || info.Mode().Perm()&0022 != 0 || info.Size() > 32<<20 {
			return m, fmt.Errorf("unsafe release member: %s", name)
		}
		if name == "manifest.json" {
			continue
		}
		f, err := os.Open(path)
		if err != nil {
			return m, err
		}
		digest := sha256.New()
		_, err = io.Copy(digest, io.LimitReader(f, (32<<20)+1))
		f.Close()
		if err != nil {
			return m, err
		}
		if hex.EncodeToString(digest.Sum(nil)) != m.SHA256[name] {
			return m, fmt.Errorf("checksum mismatch: %s", name)
		}
	}
	return m, nil
}

func activate(root, target string) error {
	path := filepath.Join(root, ".activate-"+filepath.Base(target))
	_ = os.Remove(path)
	if err := os.Symlink(target, path); err != nil {
		return err
	}
	defer os.Remove(path)
	return os.Rename(path, filepath.Join(root, "current"))
}
func target(root, link string) string {
	value, _ := os.Readlink(filepath.Join(root, link))
	if !strings.HasPrefix(value, "releases/") || filepath.Clean(value) != value || strings.Count(value, "/") != 1 {
		return ""
	}
	return value
}
func previous(root, value string) error {
	temp := filepath.Join(root, ".previous")
	_ = os.Remove(temp)
	if err := os.Symlink(value, temp); err != nil {
		return err
	}
	defer os.Remove(temp)
	return os.Rename(temp, filepath.Join(root, "previous"))
}

func Install(ctx context.Context, root, source, version string) (string, error) {
	m, err := Inspect(source, version)
	if err != nil {
		return "", err
	}
	root, err = filepath.Abs(root)
	if err != nil {
		return "", err
	}
	if err = store.PrivateDir(root); err != nil {
		return "", err
	}
	unlock, err := store.Acquire(ctx, filepath.Join(root, "install.lock"))
	if err != nil {
		return "", err
	}
	defer unlock()
	releaseDir := filepath.Join(root, "releases")
	if err = store.PrivateDir(releaseDir); err != nil {
		return "", err
	}
	name := version + "-" + m.Build[:16] + "-" + m.Arch
	destination := filepath.Join(releaseDir, name)
	if _, err = os.Lstat(destination); os.IsNotExist(err) {
		stage, err := os.MkdirTemp(releaseDir, ".install-")
		if err != nil {
			return "", err
		}
		defer os.RemoveAll(stage)
		if err = os.Mkdir(filepath.Join(stage, "agents"), 0700); err != nil {
			return "", err
		}
		for _, file := range append(append([]string{}, Files...), "manifest.json") {
			content, err := os.ReadFile(filepath.Join(source, file))
			if err != nil {
				return "", err
			}
			mode := os.FileMode(0500)
			if file == "manifest.json" {
				mode = 0400
			}
			if err = os.WriteFile(filepath.Join(stage, file), content, mode); err != nil {
				return "", err
			}
		}
		if _, err = Inspect(stage, version); err != nil {
			return "", err
		}
		if err = ctx.Err(); err != nil {
			return "", err
		}
		if err = os.Rename(stage, destination); err != nil {
			return "", err
		}
	} else if err != nil {
		return "", err
	}
	if _, err = Inspect(destination, version); err != nil {
		return "", err
	}
	next := filepath.Join("releases", name)
	if current := target(root, "current"); current != "" && current != next {
		if err = previous(root, current); err != nil {
			return "", err
		}
	}
	if err = ctx.Err(); err != nil {
		return "", err
	}
	return destination, activate(root, next)
}

func Rollback(ctx context.Context, root, version string) (string, error) {
	unlock, err := store.Acquire(ctx, filepath.Join(root, "install.lock"))
	if err != nil {
		return "", err
	}
	defer unlock()
	next, old := target(root, "previous"), target(root, "current")
	if next == "" || next == old {
		return "", errors.New("no previous verified release")
	}
	// Rollback permits an earlier version, but still validates its architecture,
	// protocol and every payload checksum before changing activation.
	var m Manifest
	if err = store.ReadJSON(filepath.Join(root, next, "manifest.json"), 16384, &m); err != nil {
		return "", err
	}
	if _, err = Inspect(filepath.Join(root, next), m.Version); err != nil {
		return "", err
	}
	if err = ctx.Err(); err != nil {
		return "", err
	}
	if err = activate(root, next); err != nil {
		return "", err
	}
	if old != "" {
		err = previous(root, old)
	}
	return filepath.Join(root, next), err
}

func WriteManifest(dir, version, build, arch string) error {
	m := Manifest{Version: version, Build: build, OS: "linux", Arch: arch, Protocol: 1, SHA256: map[string]string{}}
	for _, name := range Files {
		content, err := os.ReadFile(filepath.Join(dir, name))
		if err != nil {
			return err
		}
		sum := sha256.Sum256(content)
		m.SHA256[name] = hex.EncodeToString(sum[:])
	}
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dir, "manifest.json"), append(data, '\n'), 0600)
}
