package picker

import (
	"context"
	"os"
	"strconv"
	"strings"

	"code.talos.lowlife.us/baza/tmux-roam/internal/ssh"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func environment(extra ...string) []string {
	var env []string
	for _, entry := range os.Environ() {
		key := strings.SplitN(entry, "=", 2)[0]
		if key != "FZF_DEFAULT_OPTS" && key != "FZF_DEFAULT_OPTS_FILE" && key != "FZF_DEFAULT_COMMAND" && key != "ESCDELAY" {
			env = append(env, entry)
		}
	}
	return append(append(env, "ESCDELAY=10"), extra...)
}
func Options(ctx context.Context, prompt string, navigation, searchOnly bool, deleteCommand string, columns int, capturedKeys ...string) []string {
	option := "@tmux-roam-chooser-keys"
	if searchOnly {
		option = "@tmux-roam-native-keys"
		navigation = false
	}
	var keys string
	if len(capturedKeys) > 0 {
		keys = capturedKeys[0]
	} else {
		keys = tmux.Option(ctx, "", option)
	}
	chooser := strings.Fields(keys)
	if len(chooser) == 0 {
		chooser = []string{"s", "ы"}
	}
	toggle := make([]string, len(chooser))
	for i, key := range chooser {
		if strings.HasPrefix(key, "F") {
			if _, err := strconv.Atoi(key[1:]); err == nil {
				key = strings.ToLower(key)
			}
		}
		toggle[i] = key
	}
	toggleKeys := strings.Join(toggle, ",")
	disarm := "unbind(" + toggleKeys + ")"
	normalPrompt, searchPrompt := prompt+" [vi]> ", prompt+"> "
	if prompt == "Sessions" {
		searchPrompt = "Search> "
	}
	footer := "j/k Move   g/G First/last   / Search\nEnter Select   Esc Close   Alt+a " + chooser[0] + " Close"
	if prompt == "Sessions" {
		footer = "j/k Move   / Search   Enter Open   Esc Close\nCtrl-n New   F2 Actions   Ctrl-r Refresh\nCtrl-f Scope   Ctrl-h Hidden   Tab Details   F1 Help"
	}
	if searchOnly {
		footer = "↑/↓ Move   Enter Open   Esc Back to tree\nTab Details   Ctrl-r Refresh   Alt+a " + chooser[0] + " Close"
		if columns < 70 {
			footer = "↑/↓ Move  Enter Open  Esc Back\nTab Details  Alt+a " + chooser[0] + " Close"
		}
	}
	movement := [][2]string{{"j", "down"}, {"о", "down"}, {"k", "up"}, {"л", "up"}, {"g", "first"}, {"п", "first"}, {"G", "last"}, {"П", "last"}, {"ctrl-d", "half-page-down"}, {"ctrl-u", "half-page-up"}}
	var modeKeys []string
	for _, pair := range movement {
		modeKeys = append(modeKeys, pair[0])
	}
	modeKeys = append(modeKeys, "i", "ш", "/", ".", "q", "й")
	if deleteCommand != "" {
		modeKeys = append(modeKeys, "d", "в")
	}
	modes := strings.Join(modeKeys, ",")
	sessionPicker := prompt == "Sessions" && !searchOnly
	searchFooter := "Enter Select    Esc Back to navigation"
	if sessionPicker {
		searchFooter = "Enter Open    Ctrl-o Navigate    Esc Close"
	}
	if searchOnly {
		searchFooter = footer
	}
	empty := "No matches\n"
	newHint := ""
	if prompt == "Sessions" {
		empty = "No matching sessions\n"
		if !searchOnly {
			newHint = "  Ctrl-n New"
		}
	}
	emptySearch := "Edit search  Esc Back"
	if sessionPicker {
		emptySearch = "Edit search  Esc Close"
	}
	footerCommand := "if [ -n \"$FZF_QUERY\" ]; then printf 'Filter: %s\\n' \"$FZF_QUERY\"; fi; printf %s " + ssh.Quote(footer)
	resultFooter := "if [ \"${FZF_MATCH_COUNT:-1}\" = 0 ]; then if [ \"$FZF_PROMPT\" = " + ssh.Quote(searchPrompt) + " ]; then printf %s " + ssh.Quote(empty+emptySearch+newHint) + "; else printf %s " + ssh.Quote(empty+"/ Search  Esc Close"+newHint) + "; fi; elif [ \"$FZF_PROMPT\" = " + ssh.Quote(searchPrompt) + " ]; then printf %s " + ssh.Quote(searchFooter) + "; else " + footerCommand + "; fi"
	normal := "hide-input+change-list-label()+change-prompt(" + normalPrompt + ")+rebind(" + modes + ")+" + disarm + "+transform-footer~" + resultFooter + "~"
	search := "show-input+change-prompt(" + searchPrompt + ")+unbind(" + modes + ")+" + disarm + "+transform-footer~" + resultFooter + "~"
	closeCommand := ssh.Join(append(tmux.Base(), "display-popup", "-C", "-c")) + " \"$TMUX_ROAM_CLIENT\""
	var bindings []string
	for _, pair := range movement {
		bindings = append(bindings, pair[0]+":"+pair[1]+"+"+disarm)
	}
	for _, key := range []string{"i", "ш", "/", "."} {
		bindings = append(bindings, key+":"+search)
	}
	bindings = append(bindings, "q:abort", "й:abort", "alt-a:rebind("+toggleKeys+")", "alt-ф:rebind("+toggleKeys+")", "change:"+disarm)
	for _, key := range toggle {
		bindings = append(bindings, key+":execute-silent("+closeCommand+")+abort")
	}
	escape := "if [ \"$FZF_PROMPT\" = " + ssh.Quote(searchPrompt) + " ]; then printf %s " + ssh.Quote(normal) + "; else printf abort; fi"
	if deleteCommand != "" {
		deleteAction := "if [ \"$FZF_ACTION\" = change-list-label ]; then printf %s " + ssh.Quote("execute("+deleteCommand+")+"+normal) + "; else printf %s 'change-list-label(dd)'; fi"
		for _, key := range []string{"d", "в"} {
			bindings = append(bindings, key+":transform@"+deleteAction+"@")
		}
		escape = "if [ \"$FZF_ACTION\" = change-list-label ]; then printf %s " + ssh.Quote(normal) + "; else " + escape + "; fi"
	}
	if sessionPicker {
		bindings = append(bindings, "ctrl-o:"+normal)
	}
	if searchOnly || sessionPicker {
		bindings = append(bindings, "esc:abort")
	} else {
		bindings = append(bindings, "esc:transform@"+escape+"@")
	}
	start := disarm
	// Vi navigation starts with its movement bindings active; search starts literal.
	if navigation {
		start = disarm
	} else {
		start = "unbind(" + modes + ")+" + disarm
	}
	bindings = append(bindings, "start:"+start)
	if navigation || prompt == "Sessions" {
		bindings = append(bindings, "result:bg-transform-footer~"+resultFooter+"~")
	}
	chosenPrompt := searchPrompt
	if navigation {
		chosenPrompt = normalPrompt
	}
	chosenFooter := footer
	if prompt == "Sessions" && !navigation {
		chosenFooter = searchFooter
	}
	padding := "1,2"
	if searchOnly {
		padding = "0,2"
	}
	options := []string{"--with-shell=/bin/sh -c", "--reverse", "--prompt=" + chosenPrompt, "--bind=" + strings.Join(bindings, ","), "--ansi", "--padding=" + padding, "--pointer=›", "--gutter= ", "--info=hidden", "--no-separator", "--header-first", "--footer=" + chosenFooter, "--footer-border=top", "--color=fg:#cad3f5,bg:#24273a,fg+:#ffffff:bold,bg+:#24273a,hl:#f5a97f,hl+:#f5a97f,header:#939ab7,footer:#939ab7,border:#494d64,pointer:#ffffff,prompt:#cad3f5,spinner:#a6da95"}
	if navigation {
		options = append(options, "--no-input")
	}
	return options
}
