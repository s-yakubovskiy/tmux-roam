// Public CLI help preserved from tests/fixtures/python_cli_help.json.
package main

import "fmt"

var commandHelp = map[string]string{
	"pick":           "usage: tmux-roam pick [-h] [--client CLIENT] [--search] [--pane PANE]\n\noptions:\n  -h, --help       show this help message and exit\n  --client CLIENT\n  --search\n  --pane PANE\n",
	"native":         "usage: tmux-roam native [-h] [--client CLIENT]\n\noptions:\n  -h, --help       show this help message and exit\n  --client CLIENT\n",
	"native-select":  "usage: tmux-roam native-select [-h] --client CLIENT --navigation NAVIGATION\n                               --target TARGET\n\noptions:\n  -h, --help            show this help message and exit\n  --client CLIENT\n  --navigation NAVIGATION\n  --target TARGET\n",
	"native-refresh": "usage: tmux-roam native-refresh [-h] --client CLIENT --navigation NAVIGATION\n                                --pane PANE\n\noptions:\n  -h, --help            show this help message and exit\n  --client CLIENT\n  --navigation NAVIGATION\n  --pane PANE\n",
	"popup":          "usage: tmux-roam popup [-h] --client CLIENT [--search] [--pane PANE]\n\noptions:\n  -h, --help       show this help message and exit\n  --client CLIENT\n  --search\n  --pane PANE\n",
	"picker-state":   "usage: tmux-roam picker-state [-h] directory {filter,hidden,refresh}\n\npositional arguments:\n  directory\n  {filter,hidden,refresh}\n\noptions:\n  -h, --help            show this help message and exit\n",
	"picker-header":  "usage: tmux-roam picker-header [-h] directory\n\npositional arguments:\n  directory\n\noptions:\n  -h, --help  show this help message and exit\n",
	"picker-select":  "usage: tmux-roam picker-select [-h] directory opaque_id\n\npositional arguments:\n  directory\n  opaque_id\n\noptions:\n  -h, --help  show this help message and exit\n",
	"details":        "usage: tmux-roam details [-h] directory opaque_id\n\npositional arguments:\n  directory\n  opaque_id\n\noptions:\n  -h, --help  show this help message and exit\n",
	"help":           "usage: tmux-roam help [-h]\n\noptions:\n  -h, --help  show this help message and exit\n",
	"actions":        "usage: tmux-roam actions [-h] directory opaque_id\n\npositional arguments:\n  directory\n  opaque_id\n\noptions:\n  -h, --help  show this help message and exit\n",
	"new-session":    "usage: tmux-roam new-session [-h] directory [opaque_id]\n\npositional arguments:\n  directory\n  opaque_id\n\noptions:\n  -h, --help  show this help message and exit\n",
	"delete-session": "usage: tmux-roam delete-session [-h] directory opaque_id\n\npositional arguments:\n  directory\n  opaque_id\n\noptions:\n  -h, --help  show this help message and exit\n",
	"list":           "usage: tmux-roam list [-h] [--json] [--cached]\n\noptions:\n  -h, --help  show this help message and exit\n  --json\n  --cached\n",
	"refresh":        "usage: tmux-roam refresh [-h] [host_id]\n\npositional arguments:\n  host_id\n\noptions:\n  -h, --help  show this help message and exit\n",
	"open":           "usage: tmux-roam open [-h] [--client CLIENT] opaque_id\n\npositional arguments:\n  opaque_id\n\noptions:\n  -h, --help       show this help message and exit\n  --client CLIENT\n",
	"doctor":         "usage: tmux-roam doctor [-h]\n\noptions:\n  -h, --help  show this help message and exit\n",
	"integrate":      "usage: tmux-roam integrate [-h] {install,unload}\n\npositional arguments:\n  {install,unload}\n\noptions:\n  -h, --help        show this help message and exit\n",
	"config":         "usage: tmux-roam config [-h] [--source SOURCE]\n                        {check,diff,sync} [--all | host_id]\n\npositional arguments:\n  {check,diff,sync}\n  host_id\n\noptions:\n  -h, --help         show this help message and exit\n  --all\n  --source SOURCE    shared tmux config (default: ~/.tmux.conf)\n",
	"supervise":      "usage: tmux-roam supervise [-h] token host_id opaque_id\n\npositional arguments:\n  token\n  host_id\n  opaque_id\n\noptions:\n  -h, --help  show this help message and exit\n",
}

func help(args []string) bool {
	if len(args) == 1 && (args[0] == "--help" || args[0] == "-h") {
		fmt.Println("usage: tmux-roam {pick,native,list,refresh,open,doctor,config,integrate} ...\n\nUse COMMAND --help for command options.")
		return true
	}
	if len(args) > 1 {
		for _, arg := range args[1:] {
			if arg == "--help" || arg == "-h" {
				if text, ok := commandHelp[args[0]]; ok {
					fmt.Print(text)
					return true
				}
			}
		}
	}
	return false
}
