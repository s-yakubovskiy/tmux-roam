package picker

import (
	"bytes"
	"context"
	"fmt"
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
	"code.talos.lowlife.us/baza/tmux-roam/internal/inventory"
	"code.talos.lowlife.us/baza/tmux-roam/internal/model"
	"code.talos.lowlife.us/baza/tmux-roam/internal/native"
	"code.talos.lowlife.us/baza/tmux-roam/internal/session"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/store"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func post(ctx context.Context, socket, action string) {
	transport := &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", socket)
	}}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: time.Second}
	request, err := http.NewRequestWithContext(ctx, "POST", "http://localhost/", strings.NewReader(action))
	if err != nil {
		return
	}
	response, err := client.Do(request)
	if err == nil {
		response.Body.Close()
	}
}
func worker(ctx context.Context, c config.Config, root string, data Metadata) {
	socket := filepath.Join(root, "fzf.sock")
	for {
		if _, err := os.Stat(socket); err == nil {
			break
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(10 * time.Millisecond):
		}
	}
	replace := func(host string, rows []model.Record, failure error) {
		var remaining, old []model.Record
		for _, r := range data.Records {
			if r.HostID == host {
				old = append(old, r)
			} else {
				remaining = append(remaining, r)
			}
		}
		known := map[string]bool{}
		for _, r := range rows {
			known[r.ID] = true
		}
		for _, r := range old {
			if !known[r.ID] {
				if failure == nil {
					r.Source = "ended"
					r.State = "ended"
				}
				rows = append(rows, r)
			}
		}
		if failure == nil {
			delete(data.Errors, host)
		} else {
			data.Errors[host] = failure.Error()
		}
		data.Records = append(remaining, rows...)
		if changed, err := snapshot(ctx, root, data, c); err == nil && changed && ctx.Err() == nil {
			post(ctx, socket, reloadAction(root, "cat "+ssh.Quote(filepath.Join(root, "rows"))))
		}
	}
	// Run already captured fresh local records. Do not repeat that subprocess
	// while fzf is initializing; later periodic/explicit refreshes read again.
	refreshLocal := false
	for {
		if refreshLocal {
			rows, err := inventory.Local(ctx)
			replace("local", rows, err)
		}
		refreshLocal = true
		inventory.RefreshHosts(ctx, c, true, func(result inventory.HostResult) { replace(result.Host.ID, result.Records, result.Err) })
		next := time.NewTimer(time.Duration(c.RefreshSeconds) * time.Second)
		poll := time.NewTicker(100 * time.Millisecond)
		refresh := false
		for !refresh {
			select {
			case <-ctx.Done():
				next.Stop()
				poll.Stop()
				return
			case <-next.C:
				refresh = true
			case <-poll.C:
				if _, err := os.Stat(filepath.Join(root, "refresh.request")); err == nil {
					_ = os.Remove(filepath.Join(root, "refresh.request"))
					refresh = true
				}
			}
		}
		next.Stop()
		poll.Stop()
	}
}
func Run(ctx context.Context, c config.Config, binary, client string, popup, search bool, pane string) error {
	// Cached remote metadata is independent of the live local snapshot. Join
	// this bounded read on every return; it never creates a discovery worker.
	var cached []model.Record
	cacheReady := make(chan struct{})
	go func() {
		defer close(cacheReady)
		for _, host := range c.Hosts {
			if host.Enabled {
				cached = append(cached, inventory.Cached(host)...)
			}
		}
	}()
	defer func() { <-cacheReady }()
	token := session.Token()
	var captured tmux.Client
	var rows []model.Record
	var err error
	navigation := token
	if search {
		navigation = ""
	}
	captured, rows, err = inventory.LocalClient(ctx, client, navigation)
	if err != nil {
		return err
	}
	if pane == "" {
		pane = captured.Pane
	}
	width, _ := strconv.Atoi(captured.Width)
	height, _ := strconv.Atoi(captured.Height)
	if search && pane == captured.Pane {
		width, _ = strconv.Atoi(captured.PaneWidth)
		height, _ = strconv.Atoi(captured.PaneHeight)
	} else if search {
		dimension, _ := tmux.Run(ctx, "display-message", "-p", "-t", pane, "#{pane_width}|#{pane_height}")
		p := strings.Split(dimension, "|")
		if len(p) == 2 {
			width, _ = strconv.Atoi(p[0])
			height, _ = strconv.Atoi(p[1])
		}
	}
	if popup && (width < 45 || height < 14) {
		if !search {
			tmux.EndNavigation(ctx, client, token)
		}
		_, err = tmux.Run(ctx, "choose-tree", "-Zs", "-t", pane)
		return err
	}
	treeToken := ""
	if search {
		mode := captured.PaneMode
		if pane != captured.Pane {
			mode, _ = tmux.Run(ctx, "display-message", "-p", "-t", pane, "#{pane_mode}")
		}
		if mode == "tree-mode" {
			treeToken = captured.Navigation
			if treeToken != "" {
				token = treeToken
			}
		}
	}
	if search && treeToken == "" {
		if _, err = tmux.Run(ctx, "set-option", "-g", "@tmux-roam-navigation-"+client, token); err != nil {
			return err
		}
	}
	keepTree := false
	defer func() {
		if !keepTree && tmux.Option(context.Background(), "", "@tmux-roam-navigation-"+client) == token {
			if search {
				native.Close(context.Background(), pane, client, token)
			}
			tmux.EndNavigation(context.Background(), client, token)
		}
	}()
	if err = store.PrivateDir(store.RuntimeDir()); err != nil {
		return err
	}
	root, err := os.MkdirTemp(store.RuntimeDir(), "picker-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(root)
	failures := map[string]string{}
	<-cacheReady
	rows = append(rows, cached...)
	current := captured.Session
	popupWidth := min(width-2, 96, max(42, width*85/100))
	popupHeight := min(height-2, max(12, height*75/100), max(16, min(24, len(rows)+12)))
	if p, err := strconv.Atoi(strings.TrimSuffix(c.Width, "%")); err == nil {
		popupWidth = min(width-2, 96, max(42, width*p/100))
	}
	if p, err := strconv.Atoi(strings.TrimSuffix(c.Height, "%")); err == nil {
		popupHeight = min(height-2, max(12, height*p/100), max(16, min(24, len(rows)+12)))
	}
	if search {
		popupWidth = width
		popupHeight = min(height-3, max(8, (height+1)/2))
	}
	columns := width
	if popup {
		columns = popupWidth - 2
	}
	data := Metadata{Records: rows, Errors: failures, CurrentSession: current, CurrentRemote: captured.Remote, Columns: columns}
	if err = store.WriteJSONTransient(ctx, filepath.Join(root, "mode.json"), Mode{Filter: "All"}); err != nil {
		return err
	}
	if err = store.WriteTransient(ctx, filepath.Join(root, "columns"), []byte(strconv.Itoa(columns)+"\n"), 0600); err != nil {
		return err
	}
	if _, err = snapshot(ctx, root, data, c); err != nil {
		return err
	}
	callback := func(action string) string { return ssh.Join([]string{binary, action, root}) }
	deleteCommand := ""
	if !search {
		deleteCommand = callback("delete-session") + " {1}"
	}
	args := []string{"--sync", "--read0", "--print0", "--print-query", "--delimiter=\t", "--with-nth=2..", "--track", "--no-multi", "--no-clear", "--listen=" + filepath.Join(root, "fzf.sock"), "--header=" + Header(root)}
	if !search {
		args = append(args, "--no-sort")
	}
	keys := captured.ChooserKeys
	if search {
		keys = captured.NativeKeys
	}
	args = append(args, Options(ctx, "Sessions", false, search, deleteCommand, columns, keys)...)
	if !c.Icons {
		args = append(args, "--pointer=>")
	}
	preview := "down:40%:hidden"
	if c.Details && !search {
		preview = "down:40%"
	}
	initialPosition := 1
	currentIDs := map[string]bool{data.CurrentRemote: true}
	for _, r := range rows {
		if r.HostID == "local" && r.SessionID == current {
			currentIDs[r.ID] = true
		}
	}
	raw, _ := os.ReadFile(filepath.Join(root, "rows"))
	for i, line := range bytes.Split(raw, []byte{0}) {
		id := strings.SplitN(string(line), "\t", 2)[0]
		if id != "" && currentIDs[id] {
			initialPosition = i + 1
		}
	}
	// Only observe movement while a reload is pending. The load action runs
	// before fzf renders the replacement list, so it restores the latest user
	// choice. Steady-state movement launches no selection callback.
	pending := ssh.Quote(filepath.Join(root, "reload-selection"))
	args = append(args, "--preview="+callback("details")+" {1}", "--preview-window="+preview, "--bind=start:+unbind(focus,load)+pos("+strconv.Itoa(initialPosition)+"),focus:execute-silent(test ! -f "+pending+" || printf '%s\\0%s' {q} {1} > "+pending+"),load:unbind(focus,load)+transform(test ! -f "+pending+" || "+callback("picker-restore")+" {*f1}),enter:transform("+callback("picker-select")+" {1}),tab:toggle-preview,ctrl-r:execute-silent("+callback("picker-state")+" refresh)")
	resize := reloadAction(root, callback("picker-state")+" resize") + "+transform-header(" + callback("picker-header") + ")"
	// Read the literal action from a private file: its placeholders belong to
	// the returned action and must not expand inside the outer shell transform.
	resizePath := filepath.Join(root, "resize.action")
	if err = store.WriteTransient(ctx, resizePath, []byte(resize), 0600); err != nil {
		return err
	}
	// fzf emits resize during initialization too. Avoid reloading an already
	// correctly sized first frame, and remember each later effective width.
	args = append(args, "--bind=resize:transform(read -r columns < "+ssh.Quote(filepath.Join(root, "columns"))+"; if [ \"$FZF_COLUMNS\" != \"$columns\" ]; then cat "+ssh.Quote(resizePath)+"; fi)")
	if !search {
		created := "+transform(test ! -f " + ssh.Quote(filepath.Join(root, "created.json")) + " || " + callback("picker-select") + " created)"
		args = append(args, "--bind=ctrl-f:"+reloadAction(root, callback("picker-state")+" filter")+"+transform-header("+callback("picker-header")+"),ctrl-h:"+reloadAction(root, callback("picker-state")+" hidden")+"+transform-header("+callback("picker-header")+"),f1:execute("+ssh.Join([]string{binary, "help"})+"),ctrl-n:execute("+callback("new-session")+")"+created+",f2:execute("+callback("actions")+" {1})"+created)
	}
	scope, cancel := context.WithCancel(ctx)
	opened := make(chan string, 1)
	stopConnections, err := serveConnections(scope, c, root, client, token, "", opened)
	if err != nil {
		cancel()
		return err
	}
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); worker(scope, c, root, data) }()
	defer func() { cancel(); stopConnections(); wg.Wait() }()
	extra := []string{"TMUX_ROAM_CLIENT=" + client, "TMUX_ROAM_COLUMNS=" + strconv.Itoa(columns), "TMUX_ROAM_CONFIG=" + c.Path}
	attemptArgs := args
	for {
		var output []byte
		var child *exec.Cmd
		if popup {
			selected := filepath.Join(root, "selected")
			launch := "exec " + ssh.Join(append([]string{"env", "-u", "FZF_DEFAULT_OPTS", "-u", "FZF_DEFAULT_OPTS_FILE", "-u", "FZF_DEFAULT_COMMAND", "ESCDELAY=10", "fzf"}, attemptArgs...)) + " < " + ssh.Quote(filepath.Join(root, "rows")) + " > " + ssh.Quote(selected)
			popupArgs := []string{"display-popup", "-c", client, "-E", "-w", strconv.Itoa(popupWidth), "-h", strconv.Itoa(popupHeight), "-T", " Sessions ", "-b", "rounded", "-s", "bg=#24273a,fg=#cad3f5", "-S", "fg=#494d64"}
			if search {
				popupArgs = append(popupArgs, "-t", pane, "-x", "P", "-y", "P")
			}
			for _, entry := range extra {
				popupArgs = append(popupArgs, "-e", entry)
			}
			if sshConfig := os.Getenv("TMUX_ROAM_SSH_CONFIG"); sshConfig != "" {
				popupArgs = append(popupArgs, "-e", "TMUX_ROAM_SSH_CONFIG="+sshConfig)
			}
			commands := []string{}
			if search {
				commands = append(commands, "switch-client", "-c", client, "-T", native.BrowseTable, ";")
			}
			argv := append(tmux.Base(), append(commands, append(popupArgs, "/bin/sh", "-c", launch)...)...)
			child = exec.CommandContext(ctx, argv[0], argv[1:]...)
			child.Stdout = os.Stdout
			child.Stderr = os.Stderr
			err = child.Run()
			output, _ = os.ReadFile(selected)
		} else {
			child = exec.CommandContext(ctx, "fzf", attemptArgs...)
			child.Stdin = bytes.NewReader(raw)
			child.Stderr = os.Stderr
			child.Env = environment(extra...)
			output, err = child.Output()
		}
		code := 0
		if child.ProcessState != nil {
			code = child.ProcessState.ExitCode()
		}
		select {
		case <-opened:
			if search {
				native.Close(ctx, pane, client, token)
			}
			return nil
		default:
		}
		if err != nil && code != 1 && code != 130 && code != 129 {
			return fmt.Errorf("picker failed: %w", err)
		}
		if code == 129 {
			treeToken = ""
			return nil
		}
		query, selection, _ := bytes.Cut(output, []byte{0})
		output = selection
		var created model.Record
		if store.ReadJSON(filepath.Join(root, "created.json"), 1<<20, &created) == nil {
			output = []byte(created.ID + "\tcreated\x00")
			_ = os.Remove(filepath.Join(root, "created.json"))
		}
		if code == 0 && len(output) > 0 {
			id := strings.SplitN(strings.SplitN(string(output), "\x00", 2)[0], "\t", 2)[0]
			_, err = session.OpenID(ctx, c, id, client, token, "")
			if err == nil {
				if search {
					native.Close(ctx, pane, client, token)
				}
				return nil
			}
			if ctx.Err() != nil || !captured.Valid(ctx, token) || native.ClientPane(ctx, client) != pane {
				return err
			}
			// Reopen the same query and matched-row position after a recoverable
			// failure. The tree and invocation's refresh scope remain alive.
			position := "1"
			_ = store.ReadJSON(filepath.Join(root, "selection.json"), 128, &position)
			if n, parseErr := strconv.Atoi(position); parseErr != nil || n < 1 {
				position = "1"
			}
			attemptArgs = append(append([]string{}, args...), "--query="+string(query), "--header="+tmux.Clean(err.Error())+"\n"+Header(root), "--bind=start:+pos("+position+")")
			continue
		}
		break
	}
	if search && treeToken != "" && captured.Valid(ctx, token) && native.ClientPane(ctx, client) == pane {
		mode, _ := tmux.Run(ctx, "display-message", "-p", "-t", pane, "#{pane_mode}")
		if mode == "tree-mode" {
			_, _ = tmux.Run(ctx, "switch-client", "-c", client, "-T", native.BrowseTable)
			keepTree = true
		}
	}
	return nil
}
