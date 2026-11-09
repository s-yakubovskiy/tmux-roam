package native

import (
	"context"
	"os"
	"strings"

	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

const BrowseTable = "roam-native-browse"
const SearchTable = "roam-native-search"

func launch(c config.Config, binary string) []string {
	args := []string{"env", "TMUX_ROAM_CONFIG=" + c.Path}
	if value := os.Getenv("TMUX_ROAM_SSH_CONFIG"); value != "" {
		args = append(args, "TMUX_ROAM_SSH_CONFIG="+value)
	}
	return append(args, binary)
}
func Popup(c config.Config, binary, command string) []string {
	return []string{"run-shell", "-b", ssh.Join(launch(c, binary)) + " " + command + " --client '#{client_name}'"}
}
func queue(args []string) string {
	parts := make([]string, len(args))
	for i, a := range args {
		if a == "\\;" {
			parts[i] = ";"
		} else {
			parts[i] = ssh.Quote(a)
		}
	}
	return strings.Join(parts, " ")
}
func Binding(c config.Config, binary, client, pane string, fuzzy, searchable, toggle bool) []string {
	command := launch(c, binary)
	selectCommand := ssh.Join(append(append([]string{}, command...), "native-select")) + " --client '@CLIENT@' --navigation '@TOKEN@' --target '#{pane_id}'"
	remote := "run-shell -b -t \"%1%\" " + ssh.Quote(selectCommand)
	template := "if-shell -F -t '%%' '#{@tmux-roam-owner}' { " + remote + " } { switch-client -c '@CLIENT@' -t \"%1%\" }"
	refresh := ssh.Join(append(append([]string{}, command...), "native-refresh")) + " --client '@CLIENT@' --navigation '@TOKEN@' --pane '@PANE@'"
	chooser := []string{"choose-tree", "-Zs", "-O", "index", "-t", "@PANE@", "-f", treeFilter, "-F", treeFormat, template}
	if fuzzy {
		chooser = []string{"switch-mode", "-Zs", "-t", "@PANE@", "-F", searchFormat, template}
	}
	active := "#{||:#{==:#{pane_mode},tree-mode},#{==:#{pane_mode},switch-mode}}"
	commands := ssh.Join([]string{"if-shell", "-F", "-t", "@PANE@", active, "send-keys -t @PANE@ Escape"}) + " ; " + ssh.Join(chooser)
	if searchable {
		table := BrowseTable
		if fuzzy {
			table = SearchTable
		}
		commands += " ; " + ssh.Join([]string{"switch-client", "-c", "@CLIENT@", "-T", table})
	}
	// Discovery must never replace the native tree with run-shell's error view
	// when the controller is unavailable. Explicit diagnostics remain available.
	commands += " ; run-shell -b " + ssh.Quote(refresh+" >/dev/null 2>&1 || :")
	commands = strings.ReplaceAll(strings.ReplaceAll(commands, "#", "##"), "##[", "#[")
	clientTarget := client
	if clientTarget == "" {
		clientTarget = "#{client_name}"
	}
	paneTarget := pane
	if paneTarget == "" {
		paneTarget = "#{pane_id}"
	}
	commands = strings.NewReplacer("@CLIENT@", clientTarget, "@TOKEN@", "#{@tmux-roam-generation}", "@PANE@", paneTarget, "@CURRENT@", "#{session_id}").Replace(commands)
	opening := []string{"set-option", "-gF", "@tmux-roam-generation", "#{e|+|:#{?@tmux-roam-generation,#{@tmux-roam-generation},0},1}", "\\;", "set-option", "-gF", "@tmux-roam-navigation-" + clientTarget, "#{@tmux-roam-generation}", "\\;", "run-shell", "-C", "-t", paneTarget, commands}
	if toggle {
		return []string{"if-shell", "-F", active, "send-keys Escape", queue(opening)}
	}
	return opening
}
func ClientPane(ctx context.Context, client string) string {
	out, _ := tmux.Run(ctx, "list-clients", "-F", "#{pane_id}|#{client_name}")
	for _, line := range strings.Split(out, "\n") {
		p := strings.SplitN(line, "|", 2)
		if len(p) == 2 && p[1] == client {
			return p[0]
		}
	}
	return ""
}
func Open(ctx context.Context, c config.Config, binary, client string) error {
	pane := ClientPane(ctx, client)
	if pane == "" {
		return nil
	}
	args := Binding(c, binary, client, pane, false, true, false)
	for i := range args {
		if args[i] == "\\;" {
			args[i] = ";"
		}
	}
	_, err := tmux.Run(ctx, args...)
	return err
}
func Close(ctx context.Context, pane, client, token string) {
	guard := "#{&&:#{==:#{pane_mode},tree-mode},#{==:#{@tmux-roam-navigation-" + client + "}," + tmux.EscapeFormat(token) + "}}"
	_, _ = tmux.Run(ctx, "if-shell", "-F", "-t", pane, guard, ssh.Join([]string{"send-keys", "-t", pane, "Escape"}))
}

type KeyBinding struct {
	Table, Key string
	Command    []string
}

func NavigationBindings(ctx context.Context, c config.Config, binary string) []KeyBinding {
	var desired []KeyBinding
	fallback := "send-keys -K"
	for _, pair := range [][2]string{{BrowseTable, "tree-mode"}, {SearchTable, "switch-mode"}} {
		active := "#{==:#{pane_mode}," + pair[1] + "}"
		forward := "if-shell -F '#{mouse_pane}' 'send-keys -M' 'send-keys' ; if-shell -F " + ssh.Quote(active) + " " + ssh.Quote("switch-client -T "+pair[0])
		desired = append(desired, KeyBinding{pair[0], "Any", []string{"if-shell", "-F", active, forward, fallback}})
		for _, key := range []string{"M-a", "M-ф"} {
			desired = append(desired, KeyBinding{pair[0], key, []string{"send-keys", "-K", key}})
		}
	}
	for _, pair := range [][2]string{{"о", "Down"}, {"л", "Up"}} {
		desired = append(desired, KeyBinding{BrowseTable, pair[0], []string{"if-shell", "-F", "#{==:#{pane_mode},tree-mode}", "send-keys " + pair[1] + " ; switch-client -T " + BrowseTable, fallback}})
	}
	for _, key := range []string{"/", "."} {
		opening := Binding(c, binary, "", "", true, true, false)
		if tmux.Option(ctx, "", "@tmux-roam-native-search") == "fzf" {
			opening = Popup(c, binary, "popup --search --pane '#{pane_id}'")
		}
		desired = append(desired, KeyBinding{BrowseTable, key, []string{"if-shell", "-F", "#{==:#{pane_mode},tree-mode}", queue(opening), fallback}})
	}
	desired = append(desired, KeyBinding{SearchTable, "Escape", []string{"if-shell", "-F", "#{==:#{pane_mode},switch-mode}", queue(Binding(c, binary, "", "", false, true, false)), fallback}})
	return desired
}
