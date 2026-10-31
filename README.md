# tmux-roam

tmux-roam shows local tmux sessions and sessions on explicitly configured SSH hosts in one fzf popup. Selecting a remote session creates a managed local tmux session that holds an SSH attachment to the existing remote session. It never runs `new-session -A` remotely.

This checkout is version **0.1.0**. It requires Linux, Python 3.11+, OpenSSH, tmux with `display-popup` and session `key-table`, and fzf with `--id-nth`, `--track`, `--read0` and `--listen`. The isolated integration run covered local tmux 3.7c, remote tmux 3.6 and 3.7c, and fzf 0.74.4. The intended 3.8 local matrix and the actual Omni host still need acceptance.

## Install

Copy [examples/config.toml](examples/config.toml) to `$XDG_CONFIG_HOME/tmux-roam/config.toml` (or `~/.config/tmux-roam/config.toml`) and edit the explicit `[[hosts]]` entries. Host `ssh` values are OpenSSH aliases, so existing `~/.ssh/config` handles users, keys, ports and jump hosts. The plugin uses batch key/agent authentication. It does not modify host-key policy.

In `~/.tmux.conf`, load the plugin **after** your chooser bindings:

```tmux
run-shell '/absolute/path/to/tmux-roam/tmux-roam.tmux'
```

TPM can load the checkout as a normal plugin using its `tmux-roam.tmux` entry point. The entry point uses `/usr/bin/python3` by default, independent of an active project virtualenv. Set `TMUX_ROAM_PYTHON` to another Python 3.11+ executable if needed. `bin/tmux-roam doctor` reports actual dependencies and host status.

For a first workstation rollout, load under a spare key and query only one host before replacing an existing chooser. Set `@tmux-roam-chooser-keys` before loading the plugin, then change it to `s ы` and reload after acceptance:

```tmux
set -g @tmux-roam-chooser-keys 'F9'
run-shell '/absolute/path/to/tmux-roam/tmux-roam.tmux'
```

By default the installer binds `prefix s` and `prefix ы` to the combined picker, and `prefix S` and `prefix Ы` to the native local chooser. `@tmux-roam-native-keys` and `@tmux-roam-extra-tables` are also configurable. If an `off-prefix` table exists, it binds the chooser keys there without altering `v`, `м` or `Any`. It captures previous bindings on the first load. Reload is idempotent, removes an obsolete spare binding when promoted, and preserves a binding edited afterward.

```sh
# Roll back bindings without killing local or remote sessions:
bin/tmux-roam integrate unload
```

Unloading removes the plugin's bindings only when they still match what it installed. Existing managed connections keep running; switch away or close them explicitly when ready.

## Keys and navigation

`Alt+a s` and its Russian physical-key sequence `Alt+ф ы` open the picker when those are your local prefix keys. `Enter` opens, `Escape` cancels, `Ctrl-r` refreshes, `Ctrl-f` cycles All/Local/Remote, `Ctrl-h` shows hidden local sessions, `Tab` toggles details, `F1` shows help and `F2` shows actions. The picker marks cached observations and open connections with text.

Within a managed remote connection, `Alt+a s` opens the combined picker and `Alt+a S` opens the native **local** chooser. `Alt+a` followed by another tmux command key forwards the remote server's detected prefix and that key. Pressing `Alt+a` twice forwards one remote prefix; the next key goes straight to the remote tmux client. This makes `Alt+a Alt+a s` the remote native chooser. F12 goes to the remote terminal in managed sessions. Ordinary local sessions retain their own key behavior.

The remote prefix is read when opening or reusing a connection and may be overridden per host with `remote_prefix = "M-a"`, `"C-b"` or `"None"`. Reopen a connection from the picker after changing the remote prefix so the managed key table updates.

If SSH disconnects, the managed pane shows the reason. Press Enter to revalidate and retry the **same** remote session. If that session ended or the remote server restarted, choose a fresh entry from the picker. Closing an owned local connection ends only its local supervisor and SSH process; remote tmux lifecycle settings still govern remote persistence.

## CLI

```text
tmux-roam pick --client /dev/pts/N
tmux-roam list --json [--cached]
tmux-roam refresh [host-id]
tmux-roam open <opaque-id> --client /dev/pts/N
tmux-roam doctor
tmux-roam config check
```

`list --json` has schema version 1 with `sessions` and `errors`. Each session has an opaque `id`, separate host/server/session fields, metadata, observation time, source and state. Exit 0 means success; exit 2 means a configuration, dependency, host or action error. Host failures can coexist with usable local results. `list --cached` does not make SSH calls.

The cache at `$XDG_CACHE_HOME/tmux-roam` contains metadata only. Runtime sockets, locks and connection status files are under `$XDG_RUNTIME_DIR/tmux-roam`. Nothing is installed on remote hosts. tmuxp remains independent: sessions it starts appear through discovery.

## Verification and limits

Run `python3 -m unittest discover -s tests -v`. The test suite uses isolated tmux servers. A disposable local sshd integration run verified exact attachment, reuse, remote-client session reconciliation, both `C-b` and `M-a` forwarding, disconnect and retry, deletion detection, Unicode names, and switching only the invoking local client. It did not measure the stated 95th percentile performance budget or exercise full-screen applications, mouse, clipboard and all physical keyboard layouts. The workstation rollout and user keyboard acceptance remain separate from these automated checks.
