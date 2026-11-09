package main

import (
	"fmt"
	"strings"
)

type commandSpec struct {
	min, max                   int
	values, switches, required string
}

// Validate every public callback before config loading or external work. The
// command handlers remain small, but typos can never become ignored options.
func arguments(args []string) ([]string, error) {
	if len(args) == 0 {
		return nil, fmt.Errorf("a command is required; use --help")
	}
	specs := map[string]commandSpec{
		"pick":           {0, 0, "--client --pane", "--search", ""},
		"popup":          {0, 0, "--client --pane", "--search", "--client"},
		"native":         {0, 0, "--client", "", ""},
		"native-select":  {0, 0, "--client --navigation --target", "", "--client --navigation --target"},
		"native-refresh": {0, 0, "--client --navigation --pane", "", "--client --navigation --pane"},
		"picker-state":   {2, 2, "", "", ""}, "picker-header": {1, 1, "", "", ""},
		"picker-select": {2, 2, "", "", ""}, "details": {2, 2, "", "", ""},
		"picker-connect": {2, 2, "", "", ""},
		"picker-restore": {2, 2, "", "", ""},
		"actions":        {2, 2, "", "", ""}, "new-session": {1, 2, "", "", ""},
		"delete-session": {2, 2, "", "", ""}, "help": {0, 0, "", "", ""},
		"list": {0, 0, "", "--json --cached", ""}, "refresh": {0, 1, "", "", ""},
		"open":      {1, 1, "--client --navigation --window", "", ""},
		"supervise": {3, 3, "", "", ""}, "doctor": {0, 0, "", "", ""},
		"integrate": {1, 1, "", "", ""}, "config": {1, 2, "--source", "--all", ""},
	}
	spec, ok := specs[args[0]]
	if !ok {
		return nil, fmt.Errorf("unknown command: %s", args[0])
	}
	has := func(list, key string) bool { return strings.Contains(" "+list+" ", " "+key+" ") }
	out := []string{args[0]}
	var positional []string
	flags := []string{}
	seen := map[string]bool{}
	literal := false
	for i := 1; i < len(args); i++ {
		arg := args[i]
		if arg == "--" {
			literal = true
			continue
		}
		if !literal && strings.HasPrefix(arg, "-") {
			key, value, equals := strings.Cut(arg, "=")
			if has(spec.switches, key) && !equals {
				flags = append(flags, key)
			} else if has(spec.values, key) {
				if !equals {
					i++
					if i >= len(args) {
						return nil, fmt.Errorf("missing value for %s", key)
					}
					value = args[i]
				}
				flags = append(flags, key, value)
			} else {
				return nil, fmt.Errorf("unknown %s option: %s", args[0], key)
			}
			seen[key] = true
		} else {
			positional = append(positional, arg)
		}
	}
	if len(positional) < spec.min || len(positional) > spec.max {
		return nil, fmt.Errorf("invalid %s arguments; use %s --help", args[0], args[0])
	}
	for _, key := range strings.Fields(spec.required) {
		if !seen[key] {
			return nil, fmt.Errorf("%s requires %s", args[0], key)
		}
	}
	if args[0] == "integrate" && positional[0] != "install" && positional[0] != "unload" {
		return nil, fmt.Errorf("unknown integration action")
	}
	if args[0] == "picker-state" && !has("filter hidden refresh resize", positional[1]) {
		return nil, fmt.Errorf("unknown picker state action")
	}
	if args[0] == "config" {
		if !has("check diff sync", positional[0]) {
			return nil, fmt.Errorf("unknown config action")
		}
		if positional[0] == "check" && (len(positional) != 1 || len(flags) != 0) {
			return nil, fmt.Errorf("config check takes no options")
		}
		if seen["--all"] && len(positional) > 1 {
			return nil, fmt.Errorf("choose a host or --all")
		}
	}
	// Handlers accept positional arguments followed by options.
	out = append(out, positional...)
	return append(out, flags...), nil
}
