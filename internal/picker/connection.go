package picker

import (
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/session"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

// fzf may SIGKILL a background transform. The callback only waits on this
// invocation's controller: leases and cancelled-attempt cleanup stay here.
func serveConnections(ctx context.Context, c config.Config, root, client, token, window string, opened chan<- string) (func(), error) {
	listener, err := net.Listen("unix", filepath.Join(root, "controller.sock"))
	if err != nil {
		return nil, err
	}
	var opening sync.Mutex
	server := &http.Server{ReadTimeout: time.Second, MaxHeaderBytes: 4096,
		BaseContext: func(net.Listener) context.Context { return ctx },
		Handler: http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
			if request.Method != "POST" || request.URL.Path != "/open" {
				http.NotFound(w, request)
				return
			}
			id, err := io.ReadAll(http.MaxBytesReader(w, request.Body, 16<<10))
			if err != nil {
				http.Error(w, "invalid connection request", http.StatusBadRequest)
				return
			}
			if !opening.TryLock() {
				http.Error(w, "a connection is already in progress", http.StatusConflict)
				return
			}
			defer opening.Unlock()
			sessionID, err := session.OpenID(request.Context(), c, string(id), client, token, window)
			if err != nil {
				http.Error(w, err.Error(), http.StatusConflict)
				return
			}
			select {
			case opened <- sessionID:
			case <-ctx.Done():
			}
			w.WriteHeader(http.StatusNoContent)
		})}
	done := make(chan struct{})
	go func() { defer close(done); _ = server.Serve(listener) }()
	return func() {
		// The caller cancels ctx first. Shutdown joins handlers, including the
		// opener's deferred lease cleanup, before the private directory is removed.
		_ = server.Shutdown(context.Background())
		<-done
	}, nil
}

// Connect gives native-tree selections the same cancellable, parent-owned
// connection operation as fuzzy selections, including exact remote windows.
func Connect(ctx context.Context, c config.Config, record model.Record, client, token, window string) (string, error) {
	captured, err := tmux.CaptureClient(ctx, client)
	if err != nil {
		return "", err
	}
	width, _ := strconv.Atoi(captured.Width)
	height, _ := strconv.Atoi(captured.Height)
	if width < 30 || height < 8 {
		return "", fmt.Errorf("terminal too small to show connection progress")
	}
	if err = store.PrivateDir(store.RuntimeDir()); err != nil {
		return "", err
	}
	root, err := os.MkdirTemp(store.RuntimeDir(), "picker-")
	if err != nil {
		return "", err
	}
	defer os.RemoveAll(root)
	width = min(width-2, 96)
	data := Metadata{Records: []model.Record{record}, Columns: width - 2}
	if _, err = snapshot(ctx, root, data, c); err != nil {
		return "", err
	}
	scope, cancel := context.WithCancel(ctx)
	opened := make(chan string, 1)
	stop, err := serveConnections(scope, c, root, client, token, window, opened)
	if err != nil {
		cancel()
		return "", err
	}
	defer func() { cancel(); stop() }()
	binary, err := os.Executable()
	if err != nil {
		return "", err
	}
	connecting := "Connecting to " + tmux.Clean(record.HostLabel+"/"+record.Name) + "…  Esc cancels"
	command := ssh.Join([]string{binary, "picker-connect", root, record.ID})
	action := "unbind(enter)+" + headerAction(connecting) + "+bg-transform(" + command + ")"
	args := []string{"--disabled", "--no-input", "--read0", "--no-sort", "--listen=" + filepath.Join(root, "fzf.sock"), "--header=" + connecting}
	args = append(args, Options(ctx, "Connection", false, true, "", width-2, captured.ChooserKeys+" "+captured.NativeKeys)...)
	args = append(args, "--no-input", "--footer=Enter Retry    Esc Cancel", "--bind=start:"+action+",enter:"+action)
	launch := ssh.Join(append([]string{"env", "-u", "FZF_DEFAULT_OPTS", "-u", "FZF_DEFAULT_OPTS_FILE", "-u", "FZF_DEFAULT_COMMAND", "ESCDELAY=10", "fzf"}, args...)) + " < " + ssh.Quote(filepath.Join(root, "rows")) + " > /dev/null"
	popup := []string{"display-popup", "-c", client, "-E", "-w", strconv.Itoa(width), "-h", strconv.Itoa(min(height-2, 12)), "-T", " Connection ", "-e", "TMUX_ROAM_CLIENT=" + client, "-e", "TMUX_ROAM_CONFIG=" + c.Path}
	if sshConfig := os.Getenv("TMUX_ROAM_SSH_CONFIG"); sshConfig != "" {
		popup = append(popup, "-e", "TMUX_ROAM_SSH_CONFIG="+sshConfig)
	}
	argv := append(tmux.Base(), append(popup, launch)...)
	child := exec.CommandContext(ctx, argv[0], argv[1:]...)
	child.Stdout, child.Stderr = os.Stdout, os.Stderr
	err = child.Run()
	select {
	case sessionID := <-opened:
		return sessionID, nil
	default:
	}
	if child.ProcessState != nil {
		switch child.ProcessState.ExitCode() {
		case 0, 1, 129, 130:
			return "", nil
		}
	}
	return "", err
}

func headerAction(text string) string {
	// fzf parses transform output as actions. Remote names/errors may contain
	// parentheses or action separators; keep them inside one literal argument.
	lines := strings.Split(text, "\n")
	for i := range lines {
		lines[i] = tmux.Clean(lines[i])
	}
	text = strings.NewReplacer("~", "∼", "\\", "＼").Replace(strings.Join(lines, "\n"))
	return "change-header~" + text + "~"
}

func ConnectAction(ctx context.Context, root, id string) string {
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", filepath.Join(root, "controller.sock"))
	}}
	defer transport.CloseIdleConnections()
	request, err := http.NewRequestWithContext(ctx, "POST", "http://localhost/open", strings.NewReader(id))
	if err == nil {
		var response *http.Response
		response, err = (&http.Client{Transport: transport}).Do(request)
		if err == nil {
			defer response.Body.Close()
			if response.StatusCode == http.StatusNoContent {
				return "accept"
			}
			body, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
			err = fmt.Errorf("%s", strings.TrimSpace(string(body)))
		}
	}
	_ = os.WriteFile(filepath.Join(root, "connection-error"), []byte(err.Error()), 0600)
	message := strings.Join(strings.Fields(err.Error()), " ")
	if runes := []rune(message); len(runes) > 180 {
		message = string(runes[:179]) + "…"
	}
	return headerAction(message+"\n"+Header(root)) + "+rebind(enter,ctrl-n,f2)"
}
