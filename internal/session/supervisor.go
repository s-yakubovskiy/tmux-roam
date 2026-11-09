package session

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/inventory"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func terminalState() func() {
	var state syscall.Termios
	_, _, err := syscall.Syscall(syscall.SYS_IOCTL, os.Stdin.Fd(), syscall.TCGETS, uintptr(unsafe.Pointer(&state)))
	if err != 0 {
		return func() {}
	}
	return func() {
		_, _, _ = syscall.Syscall(syscall.SYS_IOCTL, os.Stdin.Fd(), syscall.TCSETS, uintptr(unsafe.Pointer(&state)))
	}
}

func exact(rows []model.Record, id string) (model.Record, bool) {
	for _, r := range rows {
		if r.ID == id {
			return r, true
		}
	}
	return model.Record{}, false
}

func attach(ctx context.Context, token string, h config.Host, c config.Config, record model.Record) error {
	rows, err := inventory.Remote(ctx, h, c)
	if err != nil {
		return err
	}
	if _, ok := exact(rows, record.ID); !ok {
		return errors.New("session ended or server restarted; choose a fresh session")
	}
	nonce := Token()
	remote := "printf '%s:%s:%s\\n' " + ssh.Quote("TMUX_ROAM_"+nonce) + " \"$$\" \"$(tty)\"; export LC_ALL=C.UTF-8; exec " + ssh.Join(append(h.TmuxArgs(), "attach-session", "-t", record.SessionID))
	args, err := ssh.Args(h, c, true)
	if err != nil {
		return err
	}
	args = append(args, "sh -c "+ssh.Quote(remote))
	child := exec.CommandContext(ctx, args[0], args[1:]...)
	child.Stdin = os.Stdin
	child.Stderr = os.Stderr
	stdout, err := child.StdoutPipe()
	if err != nil {
		return err
	}
	restore := terminalState()
	defer restore()
	if err = child.Start(); err != nil {
		return err
	}
	defer func() { _ = child.Process.Kill(); _ = child.Wait() }()
	if err = WriteState(ctx, token, State{Status: Attaching, SSHPID: child.Process.Pid}); err != nil {
		return err
	}
	handshakeTimer := time.AfterFunc(time.Duration(c.CommandTimeoutSeconds+c.ConnectTimeoutSeconds)*time.Second, func() { _ = child.Process.Kill() })
	reader := bufio.NewReaderSize(stdout, 512)
	line, err := reader.ReadSlice('\n')
	handshakeTimer.Stop()
	if err != nil {
		return fmt.Errorf("remote attachment handshake: %w", err)
	}
	parts := strings.Split(strings.TrimRight(string(line), "\r\n"), ":")
	if len(parts) != 3 || parts[0] != "TMUX_ROAM_"+nonce || !strings.HasPrefix(parts[2], "/dev/") || tmux.Clean(parts[2]) != parts[2] {
		return errors.New("remote attachment handshake failed")
	}
	pid, err := strconv.Atoi(parts[1])
	if err != nil || pid <= 0 {
		return errors.New("invalid remote client PID")
	}
	if err = WriteState(ctx, token, State{Status: Open, PID: pid, TTY: parts[2], SSHPID: child.Process.Pid}); err != nil {
		return err
	}
	if _, err = io.Copy(os.Stdout, reader); err != nil {
		return err
	}
	if err = child.Wait(); err != nil {
		return fmt.Errorf("SSH disconnected: %w", err)
	}
	return errors.New("SSH disconnected (exit 0)")
}

func Supervise(ctx context.Context, token string, h config.Host, c config.Config, id string) error {
	identity, err := model.Decode(id)
	if err != nil || identity.Host != h.ID {
		return errors.New("invalid supervisor identity")
	}
	record := model.Record{ID: id, HostID: identity.Host, Socket: identity.Socket, Generation: identity.Generation, SessionID: identity.Session, Created: identity.Created}
	input := bufio.NewReader(os.Stdin)
	startCommand, _ := tmux.Run(ctx, "display-message", "-p", "#{pane_start_command}")
	for {
		if err = WriteState(ctx, token, State{Status: Preparing, Command: startCommand}); err != nil {
			return err
		}
		err = attach(ctx, token, h, c, record)
		if ctx.Err() != nil {
			return ctx.Err()
		}
		message := tmux.Clean(err.Error())
		_ = WriteState(ctx, token, State{Status: Disconnected, Error: message})
		fmt.Printf("\r\n%s: %s\r\nEnter: retry exact session · Alt+a s: choose another session\r\n", h.Label, message)
		entered := make(chan error, 1)
		go func() { _, readErr := input.ReadString('\n'); entered <- readErr }()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err = <-entered:
			if err != nil {
				return err
			}
		}
	}
}
