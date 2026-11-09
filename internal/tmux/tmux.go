package tmux

import (
	"context"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/process"
)

func Base() []string {
	if socket := strings.SplitN(os.Getenv("TMUX"), ",", 2)[0]; socket != "" {
		return []string{"tmux", "-S", socket}
	}
	return []string{"tmux"}
}
func Capture(ctx context.Context, args ...string) (process.Result, error) {
	return process.Capture(ctx, 5*time.Second, 1<<20, append(Base(), args...)...)
}
func Run(ctx context.Context, args ...string) (string, error) {
	r, err := Capture(ctx, args...)
	if err != nil {
		return "", err
	}
	if r.Code != 0 {
		return "", fmt.Errorf("tmux: %s", strings.TrimSpace(string(r.Stderr)))
	}
	return strings.TrimSuffix(string(r.Stdout), "\n"), nil
}
func Option(ctx context.Context, target, name string) string {
	args := []string{"show-options", "-qv"}
	if target == "" {
		args = append(args, "-g")
	} else {
		args = append(args, "-t", target)
	}
	value, _ := Run(ctx, append(args, name)...)
	return value
}
func Clean(s string) string {
	return strings.Map(func(r rune) rune {
		if r < 32 || r >= 127 && r <= 159 {
			return '?'
		}
		return r
	}, s)
}
func EscapeFormat(s string) string {
	return strings.NewReplacer("#", "##", ",", "#,", "}", "#}").Replace(s)
}
func Guard(fields [][2]string) string {
	guard := "1"
	for _, f := range fields {
		guard = "#{&&:" + guard + ",#{==:#{" + f[0] + "}," + EscapeFormat(f[1]) + "}}"
	}
	return guard
}

// RequireLocalVersion excludes servers affected by tmux's pane-offset respawn
// crash with control clients (upstream fix 4714f478, first released in 3.8).
func RequireLocalVersion(version string) error {
	major, minor, _ := strings.Cut(version, ".")
	majorNumber, majorErr := strconv.Atoi(major)
	minorNumber, minorErr := strconv.Atoi(strings.TrimRight(minor, "abcdefghijklmnopqrstuvwxyz"))
	if majorErr == nil && minorErr == nil && minorNumber >= 0 && (majorNumber > 3 || majorNumber == 3 && minorNumber >= 8) {
		return nil
	}
	return fmt.Errorf("local tmux 3.8+ required for safe pane respawn with control clients (running server %q)", version)
}

type Client struct{ Name, PID, TTY, Width, Height, Generation, Session, Pane, Remote, PaneWidth, PaneHeight, PaneMode, ChooserKeys, NativeKeys, Navigation, Version string }

func CaptureClient(ctx context.Context, name string) (Client, error) {
	return captureClient(ctx, name, nil)
}
func BeginNavigation(ctx context.Context, name, token string) (Client, error) {
	client, err := captureClient(ctx, name, []string{";", "set-option", "-g", "@tmux-roam-navigation-" + name, token})
	if err != nil {
		EndNavigation(context.Background(), name, token)
	}
	return client, err
}
func captureClient(ctx context.Context, name string, commands []string) (Client, error) {
	client, _, err := CaptureClientSnapshot(ctx, name, commands)
	return client, err
}

// CaptureClientSnapshot appends reads/mutations to the same tmux command queue
// and returns their output alongside the captured invoking client.
func CaptureClientSnapshot(ctx context.Context, name string, commands []string) (Client, string, error) {
	args := []string{"list-clients", "-F", "#{client_pid}|#{client_width}|#{client_height}|#{client_tty}|#{client_name}|#{session_id}|#{pane_id}|#{@tmux-roam-identity}|#{pid}|#{start_time}|#{socket_path}|#{pane_width}|#{pane_height}|#{pane_mode}|#{@tmux-roam-chooser-keys}|#{@tmux-roam-native-keys}|#{@tmux-roam-navigation-" + name + "}|#{version}"}
	if len(commands) > 0 {
		args = append(args, ";", "display-message", "-p", "TMUX_ROAM_CLIENT_SNAPSHOT")
	}
	out, err := Run(ctx, append(args, commands...)...)
	if err != nil {
		return Client{}, out, err
	}
	clients, snapshot := out, ""
	if len(commands) > 0 {
		var ok bool
		clients, snapshot, ok = strings.Cut(out, "\nTMUX_ROAM_CLIENT_SNAPSHOT")
		if !ok {
			return Client{}, "", errors.New("missing tmux client snapshot boundary")
		}
		snapshot = strings.TrimPrefix(snapshot, "\n")
	}
	for _, line := range strings.Split(clients, "\n") {
		p := strings.SplitN(line, "|", 18)
		if len(p) == 18 && p[4] == name {
			if p[1] == "0" || p[1] == "" {
				p[1] = "80"
			}
			if p[2] == "0" || p[2] == "" {
				p[2] = "24"
			}
			return Client{Name: p[4], PID: p[0], Width: p[1], Height: p[2], TTY: p[3], Generation: strings.Join(p[8:11], "|"), Session: p[5], Pane: p[6], Remote: p[7], PaneWidth: p[11], PaneHeight: p[12], PaneMode: p[13], ChooserKeys: p[14], NativeKeys: p[15], Navigation: p[16], Version: p[17]}, snapshot, nil
		}
	}
	return Client{}, out, errors.New("invoking tmux client disappeared")
}
func (c Client) Valid(ctx context.Context, token string) bool {
	actual, err := CaptureClient(ctx, c.Name)
	if err != nil || actual.PID != c.PID || actual.TTY != c.TTY || actual.Generation != c.Generation {
		return false
	}
	return token == "" || actual.Navigation == token
}
func (c Client) Switch(ctx context.Context, session, token string, targetFields ...[2]string) error {
	// The client tuple is checked again in the same tmux queue as the switch.
	guard := "#{m:*1*,#{L:#{?" + Guard([][2]string{{"client_pid", c.PID}, {"client_tty", c.TTY}, {"client_name", c.Name}}) + ",1,0}}}"
	guard = "#{&&:" + guard + ",#{==:#{pid}|#{start_time}|#{socket_path}," + EscapeFormat(c.Generation) + "}}"
	if len(targetFields) > 0 {
		guard = "#{&&:" + guard + "," + Guard(targetFields) + "}"
	}
	if token != "" {
		guard = "#{&&:" + guard + ",#{==:#{@tmux-roam-navigation-" + c.Name + "}," + EscapeFormat(token) + "}}"
	}
	out, err := Run(ctx, "if-shell", "-F", "-t", session, guard, "switch-client -c '"+strings.ReplaceAll(c.Name, "'", "'\\''")+"' -t "+session, "display-message -p TMUX_ROAM_CANCELLED")
	if err == nil && strings.Contains(out, "TMUX_ROAM_CANCELLED") {
		err = errors.New("navigation cancelled")
	}
	return err
}

func EndNavigation(ctx context.Context, client, token string) {
	option := "@tmux-roam-navigation-" + client
	_, _ = Run(ctx, "if-shell", "-F", "#{==:#{"+option+"},"+EscapeFormat(token)+"}", "set-option -gu '"+strings.ReplaceAll(option, "'", "'\\''")+"'")
}
