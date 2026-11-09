package session

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

const Owner = "@tmux-roam-owner"
const Identity = "@tmux-roam-identity"
const StateOption = "@tmux-roam-state"

type Status string

const (
	Idle         Status = "idle"
	Preparing    Status = "preparing"
	Attaching    Status = "attaching"
	Open         Status = "open"
	Disconnected Status = "disconnected"
	Failed       Status = "error"
)

type State struct {
	Status  Status `json:"status"`
	PID     int    `json:"pid,omitempty"`
	TTY     string `json:"tty,omitempty"`
	SSHPID  int    `json:"ssh_pid,omitempty"`
	Error   string `json:"error,omitempty"`
	Pane    string `json:"pane,omitempty"`
	Command string `json:"command,omitempty"`
}

func Token() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b[:])
}
func validToken(token string) bool {
	return len(token) > 0 && len(token) <= 128 && !strings.ContainsAny(token, "/\\.") && tmux.Clean(token) == token
}
func statePath(token string) string { return filepath.Join(store.RuntimeDir(), token+".json") }
func ReadState(token string) State {
	var state State
	if validToken(token) {
		_ = store.ReadJSON(statePath(token), 4096, &state)
	}
	return state
}
func WriteState(ctx context.Context, token string, state State) error {
	if !validToken(token) {
		return errors.New("invalid connection state token")
	}
	if state.Command == "" {
		state.Command = ReadState(token).Command
	}
	return store.WriteJSON(ctx, statePath(token), state)
}

type Carrier struct {
	Session, ID, Token       string
	State                    State
	Configuration            string
	Windows, WindowsRenumber string
}

func Carriers(ctx context.Context) []Carrier {
	out, err := tmux.Run(ctx, "list-sessions", "-F", "#{session_id}|#{@tmux-roam-owner}|#{@tmux-roam-identity}|#{@tmux-roam-state}|#{@tmux-roam-prefix}|#{prefix}|#{key-table}|#{status}|#{session_windows}|#{@tmux-roam-windows-renumber}")
	if err != nil {
		return nil
	}
	var found []Carrier
	for _, line := range strings.Split(out, "\n") {
		p := strings.SplitN(line, "|", 10)
		if len(p) == 10 && p[1] == "1" {
			if _, err := model.Decode(p[2]); err == nil {
				found = append(found, Carrier{p[0], p[2], p[3], ReadState(p[3]), strings.Join(p[4:8], "|"), p[8], p[9]})
			}
		}
	}
	return found
}

type Pane struct{ ID, PID, Dead, DeadTime, Attached, Session, Token, Identity, StartCommand string }

func ManagedPane(ctx context.Context, c Carrier) (Pane, error) {
	out, err := tmux.Run(ctx, "list-panes", "-s", "-t", c.Session, "-F", "#{session_id}|#{session_attached}|#{session_windows}|#{window_panes}|#{pane_id}|#{pane_pid}|#{pane_dead}|#{pane_dead_time}|#{@tmux-roam-owner}|#{@tmux-roam-identity}|#{@tmux-roam-state}|#{window_linked}|#{pane_start_command}")
	if err != nil {
		return Pane{}, err
	}
	p := strings.SplitN(out, "|", 13)
	if len(p) != 13 || p[0] != c.Session || p[2] != "1" || p[3] != "1" || p[8] != "1" || p[9] != c.ID || p[10] != c.Token || p[11] == "1" {
		return Pane{}, errors.New("connection pane was split, linked, or repurposed")
	}
	if c.State.Command != "" && p[12] != c.State.Command {
		return Pane{}, errors.New("connection command changed")
	}
	if p[6] != "0" && c.State.Command == "" {
		args, parseErr := ssh.Fields(p[12])
		if parseErr == nil && len(args) == 1 {
			args, parseErr = ssh.Fields(args[0])
		}
		owned := false
		if parseErr == nil && c.State.Status == Idle && c.State.Pane == p[4] && len(args) == 3 {
			var record model.Record
			if json.Unmarshal([]byte(tmux.Option(ctx, c.Session, "@tmux-roam-record")), &record) == nil && record.ID == c.ID {
				preview := tmux.Clean(record.HostLabel+" / "+record.Name) + "\n\nRemote session available.\nSelect this entry in the session picker to connect.\nThe terminal preview appears after connecting.\n"
				owned = args[0] == "/usr/bin/printf" && args[1] == "%s" && args[2] == preview
			}
		}
		if parseErr == nil && len(args) >= 4 && args[len(args)-4] == "supervise" && args[len(args)-3] == c.Token {
			started, err := model.Decode(args[len(args)-1])
			identity, _ := model.Decode(c.ID)
			owned = err == nil && started.Host == identity.Host && started.Generation == identity.Generation && started.Socket == identity.Socket && args[len(args)-2] == identity.Host
		}
		if !owned {
			return Pane{}, errors.New("cannot prove ownership of ended legacy pane")
		}
	}
	if p[6] == "0" {
		pid, err := strconv.Atoi(p[5])
		if err != nil || pid <= 0 {
			return Pane{}, errors.New("invalid supervisor PID")
		}
		b, err := os.ReadFile(filepath.Join("/proc", p[5], "cmdline"))
		if err != nil {
			return Pane{}, err
		}
		args := strings.Split(strings.TrimRight(string(b), "\x00"), "\x00")
		id, _ := model.Decode(c.ID)
		if len(args) < 4 || args[len(args)-4] != "supervise" || args[len(args)-3] != c.Token || args[len(args)-2] != id.Host {
			return Pane{}, errors.New("pane process is no longer its supervisor")
		}
		started, err := model.Decode(args[len(args)-1])
		if err != nil || started.Host != id.Host || started.Generation != id.Generation || started.Socket != id.Socket {
			return Pane{}, errors.New("supervisor identity changed")
		}
	}
	return Pane{ID: p[4], PID: p[5], Dead: p[6], DeadTime: p[7], Attached: p[1], Session: p[0], Token: p[10], Identity: p[9], StartCommand: p[12]}, nil
}
func (p Pane) Guard() string {
	return tmux.Guard([][2]string{{"session_id", p.Session}, {"session_attached", p.Attached}, {"session_windows", "1"}, {"window_panes", "1"}, {"pane_id", p.ID}, {"pane_pid", p.PID}, {"pane_dead", p.Dead}, {"pane_dead_time", p.DeadTime}, {"pane_start_command", p.StartCommand}, {Owner, "1"}, {Identity, p.Identity}, {StateOption, p.Token}, {"window_linked", "0"}})
}
func (p Pane) Command(ctx context.Context, command string) error {
	out, err := tmux.Run(ctx, "if-shell", "-F", "-t", p.ID, p.Guard(), command, "display-message -p TMUX_ROAM_PANE_CHANGED")
	if err == nil && strings.Contains(out, "TMUX_ROAM_PANE_CHANGED") {
		err = errors.New("connection pane changed")
	}
	return err
}
