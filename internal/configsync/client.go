package configsync

import (
	"archive/tar"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
	"unicode/utf8"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/process"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
)

func Source(path string) (string, []byte, error) {
	if path == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return "", nil, err
		}
		path = filepath.Join(home, ".tmux.conf")
	}
	if strings.HasPrefix(path, "~/") {
		home, _ := os.UserHomeDir()
		path = filepath.Join(home, path[2:])
	}
	content, err := read(path)
	if err != nil {
		return path, nil, err
	}
	if content == nil {
		return path, nil, os.ErrNotExist
	}
	if !utf8.Valid(content) {
		return path, nil, errors.New("config must contain UTF-8")
	}
	return path, content, nil
}
func Hosts(c config.Config, id string, all bool) ([]config.Host, error) {
	if (id != "") == all {
		return nil, errors.New("choose a host ID or --all")
	}
	var hosts []config.Host
	for _, host := range c.Hosts {
		if host.Enabled && (all || host.ID == id) {
			hosts = append(hosts, host)
		}
	}
	if len(hosts) == 0 {
		return nil, errors.New("unknown/disabled host or no enabled hosts")
	}
	return hosts, nil
}
func Send(ctx context.Context, h config.Host, c config.Config, action string, content []byte, expected *string, checkExpected bool, expectedPath ...string) (Response, error) {
	var response Response
	args, err := ssh.Args(h, c, false)
	if err != nil {
		return response, err
	}
	detected, err := process.Capture(ctx, time.Duration(c.CommandTimeoutSeconds)*time.Second, 4096, append(append([]string{}, args...), "uname -s -m")...)
	if err != nil {
		return response, err
	}
	if detected.Code != 0 {
		return response, fmt.Errorf("cannot detect destination platform: %s", detected.Stderr)
	}
	platform := strings.Fields(string(detected.Stdout))
	if len(platform) != 2 || platform[0] != "Linux" {
		return response, errors.New("config sync supports Linux AMD64/ARM64")
	}
	arch := map[string]string{"x86_64": "amd64", "aarch64": "arm64"}[platform[1]]
	if arch == "" {
		return response, errors.New("unsupported destination architecture")
	}
	exe, err := os.Executable()
	if err != nil {
		return response, err
	}
	helper := filepath.Join(filepath.Dir(exe), "agents", "linux_"+arch)
	file, err := os.Open(helper)
	if err != nil {
		return response, fmt.Errorf("config-sync helper is missing: %w", err)
	}
	defer file.Close()
	binary, err := io.ReadAll(io.LimitReader(file, (32<<20)+1))
	if err != nil {
		return response, err
	}
	if len(binary) > 32<<20 {
		return response, errors.New("config-sync helper exceeds size limit")
	}
	request := Request{Protocol: Protocol, Action: action, Tmux: h.TmuxArgs(), Timeout: max(10, c.CommandTimeoutSeconds), Content: content, Expected: expected, CheckExpected: checkExpected}
	if len(expectedPath) > 0 {
		request.ExpectedPath = expectedPath[0]
	}
	payload, err := json.Marshal(request)
	if err != nil {
		return response, err
	}
	var archive bytes.Buffer
	writer := tar.NewWriter(&archive)
	for _, member := range []struct {
		name string
		data []byte
		mode int64
	}{{"helper", binary, 0700}, {"request.json", payload, 0600}} {
		if err = writer.WriteHeader(&tar.Header{Name: member.name, Mode: member.mode, Size: int64(len(member.data))}); err != nil {
			return response, err
		}
		if _, err = writer.Write(member.data); err != nil {
			return response, err
		}
	}
	if err = writer.Close(); err != nil {
		return response, err
	}
	bootstrap := `umask 077; staging=$(mktemp -d "${TMPDIR:-/tmp}/tmux-roam-sync.XXXXXXXX") || exit 2; trap 'rm -rf -- "$staging"' 0; trap 'exit 2' HUP INT TERM; printf 'tmux-roam-sync-v1\n%s\n' "$$" > "$staging/owner" || exit 2; tar -xf - -C "$staging" helper request.json || exit 2; "$staging/helper" < "$staging/request.json"`
	result, err := process.CaptureInput(ctx, time.Duration(c.ConnectTimeoutSeconds+request.Timeout*5+5)*time.Second, 2*Limit, bytes.NewReader(archive.Bytes()), append(args, "sh -c "+ssh.Quote(bootstrap))...)
	if err != nil {
		return response, fmt.Errorf("%w; run config diff to check the remote file before retrying", err)
	}
	if err = json.Unmarshal(result.Stdout, &response); err != nil {
		return response, fmt.Errorf("invalid config-sync response: %s; run config diff to check the remote file before retrying", strings.TrimSpace(string(result.Stderr)))
	}
	if response.Protocol != Protocol {
		return response, errors.New("incompatible config-sync response")
	}
	if response.Error != "" {
		return response, errors.New(response.Error)
	}
	if result.Code != 0 {
		return response, fmt.Errorf("SSH exit %d; inspect remote config before retrying", result.Code)
	}
	return response, nil
}
func Compare(ctx context.Context, h config.Host, c config.Config, path string, content []byte) (Response, string, error) {
	response, err := Send(ctx, h, c, "inspect", nil, nil, false)
	if err != nil {
		return response, "", err
	}
	if bytes.Equal(response.Content, content) && response.SHA256 != nil {
		return response, "", nil
	}
	directory, err := os.MkdirTemp("", "tmux-roam-diff-")
	if err != nil {
		return response, "", err
	}
	defer os.RemoveAll(directory)
	before, after := filepath.Join(directory, "before"), filepath.Join(directory, "after")
	if err = os.WriteFile(before, response.Content, 0600); err != nil {
		return response, "", err
	}
	if err = os.WriteFile(after, content, 0600); err != nil {
		return response, "", err
	}
	result, err := process.Capture(ctx, 5*time.Second, 4*Limit, "diff", "-u", "--label", h.ID+":"+response.Path, "--label", path, before, after)
	if err != nil {
		return response, "", err
	}
	if result.Code > 1 {
		return response, "", fmt.Errorf("diff failed: %s", result.Stderr)
	}
	return response, string(result.Stdout), nil
}
func Describe(response Response) string {
	if response.Status == "unchanged" {
		return "already matches"
	}
	message := "installed; no running server, applies at next start"
	if response.Status == "synced" {
		message = "installed and reloaded"
	}
	if response.Backup != "" {
		message += "; backup: " + response.Backup
	}
	return message
}
func Execute(ctx context.Context, c config.Config, action, id, source string, all bool) (int, error) {
	hosts, err := Hosts(c, id, all)
	if err != nil {
		return 2, err
	}
	path, content, err := Source(source)
	if err != nil {
		return 2, err
	}
	code := 0
	for _, host := range hosts {
		fmt.Printf("%s: %s %s…\n", host.Label, action, path)
		if action == "diff" {
			response, diff, err := Compare(ctx, host, c, path, content)
			if err != nil {
				fmt.Printf("%s: %s\n", host.Label, err)
				code = 2
				continue
			}
			if diff != "" || response.SHA256 == nil {
				if code < 1 {
					code = 1
				}
				if diff == "" {
					fmt.Println("Remote file is missing")
				} else {
					fmt.Print(diff)
				}
			} else {
				fmt.Println("Already matches")
			}
		} else {
			response, err := Send(ctx, host, c, "sync", content, nil, false)
			if err != nil {
				fmt.Printf("%s: %s\n", host.Label, err)
				code = 2
				continue
			}
			fmt.Printf("%s: %s\n", host.Label, Describe(response))
		}
	}
	return code, nil
}
