package main

import (
	"context"
	"errors"
	"fmt"
	"os"

	"code.talos.lowlife.us/baza/tmux-roam/internal/bindings"
	"code.talos.lowlife.us/baza/tmux-roam/internal/config"
	"code.talos.lowlife.us/baza/tmux-roam/internal/configsync"
	"code.talos.lowlife.us/baza/tmux-roam/internal/native"
	"code.talos.lowlife.us/baza/tmux-roam/internal/picker"
	"code.talos.lowlife.us/baza/tmux-roam/internal/tmux"
)

func ui(ctx context.Context, c config.Config, args []string) (bool, int) {
	if len(args) == 0 {
		return false, 0
	}
	command := args[0]
	if command == "pick" || command == "popup" || command == "picker-state" || command == "picker-header" || command == "picker-select" || command == "picker-connect" || command == "picker-restore" || command == "details" || command == "help" || command == "actions" || command == "new-session" || command == "delete-session" {
		return true, pickerCommand(ctx, c, args)
	}
	if command != "integrate" && command != "native" && command != "native-select" && command != "native-refresh" {
		return false, 0
	}
	exe, err := os.Executable()
	options := map[string]string{}
	if command != "integrate" {
		for i := 1; i < len(args); i += 2 {
			if i+1 >= len(args) {
				err = errors.New("missing option value")
				break
			}
			options[args[i]] = args[i+1]
		}
		if options["--client"] == "" && command == "native" {
			options["--client"], err = tmux.Run(ctx, "display-message", "-p", "#{client_name}")
		}
	}
	if err == nil {
		switch command {
		case "integrate":
			if len(args) != 2 {
				err = errors.New("usage: integrate install|unload")
			} else if args[1] == "install" {
				_, err = bindings.Install(ctx, c, exe)
			} else if args[1] == "unload" {
				_, err = bindings.Unload(ctx)
			} else {
				err = errors.New("unknown integration action")
			}
		case "native":
			err = native.Open(ctx, c, exe, options["--client"])
		case "native-select":
			err = native.Select(ctx, c, options["--client"], options["--target"], options["--navigation"], picker.Connect)
		case "native-refresh":
			err = native.Refresh(ctx, c, options["--client"], options["--pane"], options["--navigation"])
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "tmux-roam:", err)
		return true, 2
	}
	return true, 0
}

func pickerCommand(ctx context.Context, c config.Config, args []string) int {
	command := args[0]
	var err error
	switch command {
	case "pick", "popup":
		options := map[string]string{}
		search := false
		for i := 1; i < len(args); i++ {
			if args[i] == "--search" {
				search = true
				continue
			}
			if i+1 >= len(args) {
				err = errors.New("missing option value")
				break
			}
			options[args[i]] = args[i+1]
			i++
		}
		if options["--client"] == "" {
			options["--client"], err = tmux.Run(ctx, "display-message", "-p", "#{client_name}")
		}
		exe, exeErr := os.Executable()
		if err == nil {
			err = exeErr
		}
		if err == nil {
			err = picker.Run(ctx, c, exe, options["--client"], command == "popup", search, options["--pane"])
		}
	case "help":
		picker.Help(ctx)
	default:
		if len(args) < 2 {
			err = errors.New("missing picker directory")
			break
		}
		root := args[1]
		id := ""
		if len(args) > 2 {
			id = args[2]
		}
		switch command {
		case "picker-state":
			err = picker.StateAction(ctx, c, root, id)
		case "picker-header":
			fmt.Println(picker.Header(root))
		case "picker-restore":
			var action string
			action, err = picker.RestoreSelection(root, id)
			fmt.Println(action)
		case "picker-select":
			fmt.Println(picker.SelectAction(ctx, root, id))
		case "picker-connect":
			fmt.Println(picker.ConnectAction(ctx, root, id))
		case "details":
			err = picker.Details(root, id)
		case "new-session":
			err = picker.NewSession(ctx, c, root, id)
		case "delete-session":
			err = picker.DeleteSession(ctx, c, root, id)
		case "actions":
			err = picker.Actions(ctx, c, root, id, picker.SyncMenu)
		}
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "tmux-roam:", err)
		return 2
	}
	return 0
}

func sharedConfig(ctx context.Context, c config.Config, args []string) (bool, int) {
	if len(args) < 2 || args[0] != "config" || args[1] != "diff" && args[1] != "sync" {
		return false, 0
	}
	id, source := "", ""
	all := false
	var err error
	for i := 2; i < len(args); i++ {
		switch args[i] {
		case "--all":
			all = true
		case "--source":
			if i+1 >= len(args) {
				err = errors.New("missing source")
				break
			}
			source = args[i+1]
			i++
		default:
			if id != "" {
				err = errors.New("choose one host")
			}
			id = args[i]
		}
	}
	code := 2
	if err == nil {
		code, err = configsync.Execute(ctx, c, args[1], id, source, all)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "tmux-roam:", err)
	}
	return true, code
}
