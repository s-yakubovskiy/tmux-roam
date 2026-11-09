package session

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/inventory"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/process"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

type UncertainOutcome struct{ Cause error }

func (e UncertainOutcome) Error() string {
	return e.Cause.Error() + "; refresh to check the outcome before retrying"
}
func (e UncertainOutcome) Unwrap() error { return e.Cause }
func remoteCommand(ctx context.Context, h config.Host, c config.Config, command string, mutating bool) (string, error) {
	args, err := ssh.Args(h, c, false)
	if err != nil {
		return "", err
	}
	result, err := process.Capture(ctx, time.Duration(c.CommandTimeoutSeconds)*time.Second, 1<<20, append(args, command)...)
	if err != nil {
		if mutating {
			return "", UncertainOutcome{err}
		}
		return "", err
	}
	if result.Code != 0 {
		err = fmt.Errorf("%s: %s", h.Label, tmux.Clean(strings.TrimSpace(string(result.Stderr))))
		if mutating && result.Code == 255 {
			return "", UncertainOutcome{err}
		}
		return "", err
	}
	return string(result.Stdout), nil
}
func Create(ctx context.Context, h config.Host, c config.Config, name, directory string) (model.Record, error) {
	if name == "" || name != strings.TrimSpace(name) || tmux.Clean(name) != name || strings.ContainsAny(name, ".:;") {
		return model.Record{}, errors.New("use a session name without edge spaces, dots, colons, semicolons or control characters")
	}
	if directory == "" {
		directory = "~"
	}
	if tmux.Clean(directory) != directory || !(strings.HasPrefix(directory, "/") || strings.HasPrefix(directory, "~/") || directory == "~") {
		return model.Record{}, errors.New("use an absolute remote directory or ~/path")
	}
	command := "set -eu; directory=" + ssh.Quote(directory) + "; case \"$directory\" in '~') directory=$HOME;; '~/'*) directory=$HOME/${directory#'~/'};; esac; cd -- \"$directory\"; export LC_ALL=C.UTF-8; exec " + ssh.Join(append(h.TmuxArgs(), "new-session", "-d", "-P", "-F", "#{pid}|#{start_time}|#{session_id}|#{session_created}|#{socket_path}", "-s", name))
	output, err := remoteCommand(ctx, h, c, "sh -c "+ssh.Quote(command), true)
	if err != nil {
		return model.Record{}, err
	}
	p := strings.SplitN(strings.TrimSpace(output), "|", 5)
	invalid := func() (model.Record, error) {
		return model.Record{}, UncertainOutcome{errors.New("creation response was incomplete")}
	}
	if len(p) != 5 || !strings.HasPrefix(p[2], "$") || !strings.HasPrefix(p[4], "/") {
		return invalid()
	}
	for _, field := range p[:4] {
		if field == p[2] {
			field = strings.TrimPrefix(field, "$")
		}
		if _, err := strconv.ParseUint(field, 10, 64); err != nil {
			return invalid()
		}
	}
	created, err := strconv.ParseInt(p[3], 10, 64)
	if err != nil {
		return invalid()
	}
	identity := model.Identity{Host: h.ID, Socket: p[4], Generation: p[0] + "/" + p[1], Session: p[2], Created: created}
	return model.Record{ID: identity.Encode(), HostID: h.ID, HostLabel: h.Label, Name: name, Socket: identity.Socket, Generation: identity.Generation, SessionID: identity.Session, Created: created, Windows: 1, Observed: float64(time.Now().UnixNano()) / 1e9, Source: "live"}, nil
}

// Delete is called only after explicit confirmation. Guard evaluation and the
// destructive command share one tmux queue, with no intervening shell lookup.
func Delete(ctx context.Context, c config.Config, r model.Record) error {
	var fresh []model.Record
	var err error
	var host config.Host
	if r.HostID == "local" {
		fresh, err = inventory.Local(ctx)
	} else {
		host, err = hostFor(c, r.HostID)
		if err == nil {
			fresh, err = inventory.Remote(ctx, host, c)
		}
	}
	if err != nil {
		return err
	}
	current, ok := exact(fresh, r.ID)
	if !ok {
		return errors.New("session ended or server restarted; refresh the picker")
	}
	generation := strings.Split(current.Generation, "/")
	if len(generation) != 2 {
		return errors.New("invalid server identity")
	}
	guard := tmux.Guard([][2]string{{"pid", generation[0]}, {"start_time", generation[1]}, {"socket_path", current.Socket}, {"session_id", current.SessionID}, {"session_created", strconv.FormatInt(current.Created, 10)}})
	deleted := "TMUX_ROAM_DELETED_SESSION"
	command := ssh.Join([]string{"kill-session", "-t", current.SessionID}) + " ; " + ssh.Join([]string{"display-message", "-p", deleted})
	args := []string{"if-shell", "-F", "-t", current.SessionID, guard, command, "display-message -p TMUX_ROAM_STALE_SESSION"}
	var output string
	if r.HostID == "local" {
		output, err = tmux.Run(ctx, args...)
	} else {
		output, err = remoteCommand(ctx, host, c, ssh.Join(append(host.TmuxArgs(), args...)), true)
	}
	if err != nil {
		return err
	}
	if strings.Contains(output, "TMUX_ROAM_STALE_SESSION") {
		return errors.New("session changed while deleting; refresh the picker")
	}
	if strings.TrimSpace(output) != deleted {
		return UncertainOutcome{errors.New("deletion response was incomplete")}
	}
	if r.HostID != "local" {
		remaining := []model.Record{}
		present, healthy := map[string]bool{}, map[string]bool{}
		for _, row := range fresh {
			if row.ID != current.ID {
				remaining = append(remaining, row)
				present[row.ID] = true
				healthy[row.ID] = strings.HasPrefix(row.State, "open here")
			}
		}
		if err = inventory.SaveContext(ctx, host, remaining); err != nil {
			return err
		}
		Cleanup(ctx, host.ID, present, healthy)
	}
	return nil
}
