# tmux-roam

The Go migration is in progress. See [GO_MIGRATION_PROGRESS.md](GO_MIGRATION_PROGRESS.md) for implemented commands, verification, and remaining gates. The installed launcher still uses Python.

tmux-roam shows local tmux sessions and sessions on explicitly configured SSH hosts in tmux's native session tree or an fzf popup. Selecting a remote session opens a managed local tmux session that holds an SSH attachment to the existing remote session. It never runs `new-session -A` remotely.

Development roadmap: [Go migration plan](GO_MIGRATION_PLAN.md), including full feature parity, performance targets, package boundaries, and verification checklists for every stage.

This checkout is version **0.1.0**. It requires Linux, Python 3.11+, OpenSSH, tmux with `display-popup` and session `key-table`, and fzf with `--id-nth`, `--track`, `--read0`, `--listen`, `--no-input` and `--footer`. The isolated integration run covered local tmux 3.7c, remote tmux 3.6 and 3.7c, and fzf 0.74.4. A workstation rollout also verified tmux 3.8, fzf 0.74.5, zsh, and attachment to the actual Omni host; physical keyboard acceptance remains manual.

## Install

Copy [examples/config.toml](examples/config.toml) to `$XDG_CONFIG_HOME/tmux-roam/config.toml` (or `~/.config/tmux-roam/config.toml`) and edit the explicit `[[hosts]]` entries. Host `ssh` values are OpenSSH aliases, so existing `~/.ssh/config` handles users, keys, ports and jump hosts. The plugin uses batch key/agent authentication. It does not modify host-key policy.

In `~/.tmux.conf`, load the plugin **after** your chooser bindings:

```tmux
run-shell '/absolute/path/to/tmux-roam/tmux-roam.tmux'
```

For **TPM**, use this declaration instead of the direct `run-shell` above. Put it before your existing TPM initialization:

```tmux
set -g @tmux-roam-chooser-keys 's ы'
set -g @tmux-roam-native-keys 'S Ы'
set -g @plugin 's-yakubovskiy/tmux-roam'

run '~/.tmux/plugins/tpm/tpm'
```

Keep only one TPM initialization. Remove separate bindings for `s` / `ы` from your config when tmux-roam owns those keys: reloading an earlier native binding counts as a user edit, which the plugin preserves. Reload with `tmux source-file ~/.tmux.conf`, then press **prefix + I** (uppercase) to install. With `Alt+a` as prefix, that is `Alt+a`, then `Shift+i`. The command-line equivalent is:

```sh
~/.tmux/plugins/tpm/bin/install_plugins
tmux source-file ~/.tmux.conf
~/.tmux/plugins/tmux-roam/bin/tmux-roam config check
~/.tmux/plugins/tmux-roam/bin/tmux-roam doctor
```

TPM installs its own clone under `~/.tmux/plugins/tmux-roam`; editing a separate development checkout does not update that clone. Use **prefix + U** to update the plugin, preserving any local modifications first.

The entry point uses `/usr/bin/python3` by default, independent of an active project virtualenv and the current working directory. Set `TMUX_ROAM_PYTHON` to another Python 3.11+ executable if needed. `bin/tmux-roam doctor` reports actual dependencies and host status.

To show remote sessions in the native chooser on `s` / `ы` and keep the fzf picker on prefix + b (Russian и), set these options before TPM initialization (or the direct entry point). They apply inside managed remote sessions too:

```tmux
set -g @tmux-roam-chooser-keys 'b и'
set -g @tmux-roam-native-keys 's ы S Ы'
set -g @tmux-roam-native 'on'
```

Enable slash-activated fuzzy filtering on selected keys:

```tmux
set -g @tmux-roam-native-fuzzy-keys 's ы'
set -g @tmux-roam-native-search fzf
```

Reload the plugin after changing these options. `Alt+a s` / `Alt+ф ы` opens the tree in navigation mode with a bottom terminal preview. Use `j`/`k` with `mode-keys vi`, Russian `о`/`л` (down/up), or the arrow keys to browse. Press `/` (Russian physical equivalent `.`) to open fzf search over the bottom preview area, then type. The session tree remains visible above it. It shares the compact manager's folder-style project paths (`DD/gitlab`, `OMNI/project`), subtle host text colours, current-session `*`, selection pointer and padding. Search starts in text input mode and ranks matches; fzf's extended query syntax works too. Use arrows to select a result and Enter to open it. Escape restores the terminal preview and the same tree selection and expanded rows; Escape again closes the picker. Tab toggles session details, and Ctrl-r refreshes discovery. Search temporarily occupies the terminal preview area. `Alt+a S` / `Alt+ф Ы` keeps the plain tree shortcuts, and `Alt+a b` keeps the compact manager with creation, deletion and other actions.

The fzf popup prepares cached rows once and starts fzf directly, avoiding a second Python startup inside the window. The tree stays visible while the search overlay occupies its lower preview area. The first list is rendered together, and background discovery reloads it only when visible rows change. Remote discovery starts after fzf is ready, so an offline host does not delay opening. No persistent picker process is required.

For the previous tmux-native search view, set `@tmux-roam-native-search native` and reload. This uses tmux 3.8's built-in fuzzy matcher; older local tmux versions without `switch-mode` retain the tree. If the option is unset, the native search backend is used.

Press the native picker shortcut again to close it, from either browse or fuzzy search mode. This toggle also works inside managed remote sessions and with the Russian shortcuts.

The native tree shows friendly names such as `Omni / 🤖omni`, remote window counts, and connection state. Opening the tree and selecting a local session run entirely inside tmux. Existing entries appear immediately; a background worker publishes cached entries and refreshes discovery while the tree is open. It redraws only when the visible metadata changes and uses a tmux mode-change notification to stop on exit, without frequent polling. Entries are retained local panes with no running process until selected. Remote selection invokes Python to validate the exact remote identity and connect or reuse its terminal. Native navigation, search, mouse selection and previews remain available.

To expand remote sessions into their windows, enable this option before TPM initialization and reload:

```tmux
set -g @tmux-roam-native-windows on
```

In `Alt+a s`, select a remote session and press **Right** or **l** to expand it. Window indexes, names and the remote active-window `*` come from the same SSH discovery request as session metadata. **Enter** on a window opens that exact remote window through the existing attachment; Enter on the session opens its current window. Session-only fuzzy search remains on `/`. Window names are cached and retained during outages. Refresh runs on opening and every `discovery.refresh_seconds` while the picker is open; it requires no daemon or per-window SSH connections.

Native tmux needs local windows to render child rows. Roam uses exited metadata-only windows, hides the SSH carrier from the tree, and removes those rows before connecting or reconnecting. Their preview identifies a remote window rather than streaming its contents; the remote terminal appears when opened. User-modified, running, linked or currently viewed rows are preserved. To disable the extra rows, set the option to `off` and reload; the plugin removes its untouched metadata windows and restores the carrier's original index. Without this option, expanded remote children represent the local SSH terminal.

The compact native layout starts collapsed, with a bottom preview (`v` toggles it) and stable session-index order. Details align after short names, including Unicode names; long names retain their full native label. Rows show counts such as `3w`, and `*` marks the invoking client's current session independently of the selection highlight. Remote states are **Ready**, **Connected**, **Reconnect**, **Checking…**, or **Unavailable** with the last observation age. Status colours are consistent across hosts: green for **Connected**, amber for **Reconnect**, blue for **Checking/Connecting**, neutral grey for **Ready**, and dim grey for **Unavailable/Ended**. Window counts stay neutral. Unavailable counts are dimmed and remain selectable for retry. The tree's native `Ctrl-s` search remains available too.

The optional tmux-native search view uses three-cell side gutters and host-coloured session names. Its selection and matching characters use tmux's `mode-style` and `switch-mode-match-style`. To soften that search view's colours while keeping existing copy-mode highlighting, place these overrides after your theme (the fzf view uses the manager's palette):

```tmux
set -gwF copy-mode-selection-style '#{E:copy-mode-selection-style}'
set -gwF copy-mode-position-style '#{E:copy-mode-position-style}'
set -gw mode-style 'fg=#cad3f5,bg=#494d64'
set -gw switch-mode-match-style 'fg=#f5a97f,bg=#363a4f,bold'
```

Optional host accents use the configured host ID; unconfigured hosts use blue. The separate selection style below is available in local tmux 3.8 and does not change copy-mode highlighting:

```tmux
set -g @tmux-roam-native-colour-omni '#f5a97f'
set -g @tmux-roam-native-colour-devdocker '#a6da95'
set -gw tree-mode-selection-style 'fg=#cad3f5,bg=#494d64,bold'
```

To restore the plain native tree, change the existing option **before TPM initialization** to `set -g @tmux-roam-native 'off'`, then run `tmux source-file ~/.tmux.conf`. Edit the existing setting rather than appending a second one after TPM. Existing remote entries remain local sessions. To remove untouched dormant entries as well, run `integrate unload`; active connections stay running. Remove the plugin declaration before reloading if disabling the whole plugin persistently.

To put the fzf picker on the usual chooser keys instead, change `@tmux-roam-chooser-keys` to `s ы` and `@tmux-roam-native-keys` to `S Ы`. Reload removes the obsolete b / и bindings.

By default the installer binds `prefix s` and `prefix ы` to the combined picker, and `prefix S` and `prefix Ы` to the native local chooser. `@tmux-roam-native-keys` and `@tmux-roam-extra-tables` are also configurable. If an `off-prefix` table exists, it binds the chooser keys there without altering `v`, `м` or `Any`. It captures previous bindings on the first load. Reload is idempotent, removes an obsolete spare binding when promoted, and preserves a binding edited afterward.

```sh
# Roll back bindings and remove untouched dormant entries:
bin/tmux-roam integrate unload
```

Unloading removes the plugin's bindings only when they still match what it installed. Existing managed connections keep running; switch away or close them explicitly when ready.

## Keys and navigation

The popup fits the cached session count, up to 96 columns and 24 rows, within the configured width/height budgets and terminal bounds. Session rows are compact, with extra space below the headings and around the aligned controls. It keeps a minimum usable size and scrolls if discovery adds more sessions. Each session is shown as a host/project path, such as `Local/dev` or `DD/gitlab`. A muted slash separates the host folder from the project. Window counts (`3w`) and status stay beside the path instead of stretching to the terminal edge; narrow terminals omit the count. Fuzzy search matches the whole path, so `dd/git` narrows to a host and project together. Host colors follow configuration order (orange, green), while local sessions use gray. Names are measured in terminal cells so emoji and combining characters keep the columns aligned. The current session is marked with `*` and selected initially. Brighter bold text and a slim `›` mark the selection on the same plain background; the `*` stays on the session you are using. Host colours are text accents, without filled badges; connection states use the same semantic colours as the native tree. Search filters remain visible after returning to vi navigation; technical details are available with Tab.

With the native configuration above, `Alt+a s` and its Russian physical-key sequence `Alt+ф ы` open the enhanced native tree. Use its usual arrow keys, `Enter`, numeric shortcuts and `Escape`. `Alt+a b` / `Alt+ф и` toggles the fzf picker, including while an action menu or project form is open. In fzf, `Enter` opens, `Ctrl-r` refreshes, `Ctrl-f` cycles All/Local/Remote, `Ctrl-h` shows hidden local sessions, `Tab` toggles details, `F1` shows help and `F2` shows actions. The picker marks cached observations and open connections with text.

The session list starts in search mode, ready for typing. Escape immediately closes the session picker, including with a filter or no matches. Use arrows to move while searching, or `Ctrl-o` to enter vi navigation and keep the filter: `j`/`k` move, `g`/`G` choose the first/last item, and `Ctrl-u`/`Ctrl-d` move half a page. Press `/` or `i` to search again. The action menu and host chooser start in vi navigation; Escape returns from their search to navigation. Escape or `q` in navigation closes the current menu. Russian physical-key equivalents work too (`о`/`л`, `п`/`П`, `ш`, `.` and `й`). Project name and directory fields accept text immediately; Escape or Ctrl-c cancels them.

To delete a local or remote session, select it and press `dd` in vi navigation mode (`вв` on the Russian layout), or use `F2` → **Delete session**. The confirmation shows the host and session name and starts on **Cancel**; choose **Delete session** and press Enter to close all its windows and stop their programs. Escape cancels. Search treats `dd` as ordinary text. The plugin rechecks the server/session identity before deletion; offline hosts and stale records cannot delete another session. A failed connection is never treated as successful deletion; refresh to check the outcome before retrying. To disconnect without deleting remote work, use **Close owned local connection** instead.

To create a project session, open `Alt+a b` and press `Ctrl-n`, or use `F2` → **New remote session**. Choose a configured host, enter a session name, then an existing remote project directory (`~/work/project`, an absolute path, or Enter for the remote home directory). Submitting the directory creates and opens the session; it also appears in `Alt+a s`. The F2 action puts the selected session's host first in the host list. Ctrl-n works even when the search has no matches. Escape cancels host selection; Ctrl-c or an empty session name cancels the form before creation.

Each project is an independent session on that host's configured tmux server, with its own windows and panes. Names cannot contain dots, colons, semicolons or control characters. Duplicate names and missing directories return an error without replacing anything. Creation is explicit; ordinary session selection still never recreates an ended session. If a connection drops during creation, refresh the picker to check the outcome before retrying.

`Alt+a Shift+l` (`Alt+ф Shift+д` on the Russian layout) switches to the previous local or roaming session. Repeat it to toggle between two sessions. It works in ordinary sessions, managed remote connections and configured extra prefix tables, including `off-prefix`. The plugin restores the previous bindings when unloaded.

Within a managed remote connection, the configured native and fzf keys keep opening the local pickers. Apart from the global previous-session shortcut above, `Alt+a` followed by another tmux command key forwards the remote server's detected prefix and that key. Pressing `Alt+a` twice forwards two remote prefixes, preserving the remote double-prefix binding (for example, `last-window` on Omni). The Russian `Alt+ф` equivalent works too. To send a reserved command directly to the remote tmux, press `Alt+a`, then `F12`, then the command key: `Alt+a F12 s` opens the remote native chooser, and `Alt+a F12 Shift+l` switches to the remote server's previous session. F12 on its own goes to the remote terminal in managed sessions.

Managed connections hide the local carrier's status bar so only the remote tmux bar takes screen space. The local session picker still identifies each connection by host and session name.

Mouse events are forwarded through managed sessions to remote tmux, including touchpad scrolling, clicks, double-clicks and dragging. The remote tmux mouse bindings control pane selection, word selection on double-click, copying a dragged selection, and mouse input in full-screen applications. A plain shell does not move its text cursor on a single click. Both tmux layers need mouse support enabled.

`Alt+a [` enters the remote server's copy mode, whose key style is independent of the local server. For vi navigation and copying back to your local terminal, put these settings in the remote `~/.tmux.conf`:

```tmux
set -gw mode-keys vi
set -s set-clipboard on
bind-key -T copy-mode-vi y send-keys -X copy-selection-and-cancel
```

Use `j`/`k` to navigate, `Space` to start a selection, and `y` to copy and exit. Clipboard transfer uses OSC 52 through SSH; the local tmux also needs `set-clipboard on` and the terminal must support clipboard writes. A remote `xclip` command targets the remote display rather than this terminal connection.

The remote prefix is read when opening or reusing a connection and may be overridden per host with `remote_prefix = "M-a"`, `"C-b"` or `"None"`. Reopen a connection from the picker after changing the remote prefix so the managed key table updates.

SSH discovery, validation, and attachment share an OpenSSH connection through a private runtime control socket (`ControlMaster=auto`, `ControlPersist=60`). An idle transport exits after 60 seconds. Returning to an existing connection uses one fresh request containing server/session identities, session prefixes, and remote clients; a new attachment still gets separate confirmation. Closing the picker cancels its outstanding refresh commands. Custom runtime paths too long for an OpenSSH control socket fall back to ordinary SSH connections.

If SSH disconnects, the managed pane shows the reason. Press Enter to revalidate and retry the **same** remote session. If that session ended or the remote server restarted, choose a fresh entry from the picker. Closing an owned local connection ends only its local supervisor and SSH process; remote tmux lifecycle settings still govern remote persistence.

A temporary network failure keeps cached entries and marks the host unavailable. Discovery resumes while the picker is open, but a dropped SSH terminal requires Enter or a new picker selection; there is no automatic reconnect loop. A short interruption may recover without breaking the SSH connection. The remote tmux session keeps running when the host/server remains alive and its lifecycle settings allow detached sessions.

Selecting a disconnected entry reconnects in its existing local session and pane. If there are multiple local entries for the same remote session, selection prefers a confirmed working connection. Fresh native-picker discovery removes old, failed duplicates only when a working connection exists; attached, renamed or repurposed entries are preserved. Offline discovery never removes these entries.

After successful discovery confirms a remote session is gone, the native picker removes its unused placeholder or failed local connection, including a supervisor waiting at the retry prompt. Successful deletion also attempts this cleanup immediately; discovery handles connections that finish disconnecting afterward. It only removes an owned, unattached entry with one unmodified pane and window. Attached clients, renamed entries and user-modified connection panes are retained. Network errors never trigger this cleanup.

## Shared tmux configuration

Keep one canonical `~/.tmux.conf` on the workstation and explicitly distribute it to the hosts in `config.toml`:

```sh
tmux-roam config diff omni
tmux-roam config diff --all
tmux-roam config sync omni
tmux-roam config sync --all
# Review or deploy a different source file:
tmux-roam config diff --all --source ./shared.tmux.conf
```

Use the plugin's `bin/tmux-roam` path if it is not on your PATH. `config check` continues to validate the plugin's TOML configuration. `config diff` compares file contents without changing the remote config; its exit status is 0 for matching files, 1 for differences, or 2 for errors. Sync returns 2 if any host fails, while continuing with the other selected hosts. A host ID or `--all` is required; disabled hosts are excluded. Offline hosts are reported and can be retried later. These commands require Python 3 on the remote host; no helper is installed there.

The picker also provides **F2 → Sync tmux config**. Choose a host or all enabled hosts, review the diff with **Ctrl-u/d**, then select **Sync config**. Cancel is selected initially; Escape or the popup toggle cancels the preview. Sync refuses to replace a remote file edited since that preview.

Only `~/.tmux.conf` is copied, to the SSH user's home directory. Identical files are skipped. A changed file is parsed with that host's tmux binary on a temporary isolated server before installation. The previous file is backed up under `$XDG_STATE_HOME/tmux-roam/backups/` (default `~/.local/state/tmux-roam/backups/`), and replacement is atomic. Existing config symlinks are preserved by updating their target. The configured tmux socket is reloaded without restarting it or its sessions. If no server is running, the file is saved for the next start.

Syntax validation does not execute shell commands or check every option's runtime validity. A reported reload failure restores the previous file and attempts to reload it; this cannot undo arbitrary shell commands or every live tmux change. The command reports this distinction and the backup path. If SSH drops mid-sync, use `config diff` to check the remote file before retrying.

Shared files should use host conditions for colors, local clipboard programs, and workstation scripts. Use remote OSC 52 clipboard support rather than a workstation's `xclip` command. Script files, plugins, and `config.toml` are not distributed. TPM scans literal `@plugin` lines even inside conditional blocks; its runtime `@tpm_plugins` list supports a host-specific plugin set. This is useful for loading tmux-roam only on the workstation while sharing the rest of the file.

## CLI

```text
tmux-roam pick --client /dev/pts/N
tmux-roam native --client /dev/pts/N
tmux-roam list --json [--cached]
tmux-roam refresh [host-id]
tmux-roam open <opaque-id> --client /dev/pts/N
tmux-roam doctor
tmux-roam config check
tmux-roam config diff <host-id> | --all [--source FILE]
tmux-roam config sync <host-id> | --all [--source FILE]
```

`list --json` has schema version 1 with `sessions` and `errors`. Each session has an opaque `id`, separate host/server/session fields, metadata, observation time, source and state. Exit 0 means success; exit 2 means a configuration, dependency, host or action error. Host failures can coexist with usable local results. `list --cached` does not make SSH calls.

The cache at `$XDG_CACHE_HOME/tmux-roam` contains metadata only. Runtime sockets, locks and connection status files are under `$XDG_RUNTIME_DIR/tmux-roam`. Ordinary discovery and attachment install nothing on remote hosts; explicit config sync writes only the shared config and its backup/lock files. tmuxp remains independent: sessions it starts appear through discovery.

## Verification and limits

Run `python3 -m unittest discover -s tests -v`. The test suite uses isolated tmux servers. A disposable local sshd integration run verified exact attachment, reuse, remote-client session reconciliation, both `C-b` and `M-a` forwarding, disconnect and retry, deletion detection, Unicode names, and switching only the invoking local client. The workstation rollout verified repeated TPM reloads, popup cancellation, Cyrillic chooser input, local selection, exact Omni attachment and reuse, and returning to a local session, using a separate terminal client and temporary local server. A native-tree run against an isolated server on Omni also checked dormant entries, background additions/removals, quoted Unicode names, in-place activation, reuse with two local clients, ended-session rejection, and F9 fallback. Cleanup preserved existing remote sessions. Additional isolated terminal checks verified wheel forwarding, mouse input in a full-screen application, and vi copy-mode navigation and selection. SSH checks against Omni and DevDocker verified that copied text reaches the local tmux buffer and produces an OSC 52 clipboard write at the terminal. Remote creation checks cover directory handling, shell quoting, duplicate names and preserving existing sessions; isolated SSH popup checks exercise Ctrl-n from an empty search, host-selection cancellation, and F2 creation from an existing managed connection. These checks did not measure the stated 95th percentile performance budget or exercise all physical keyboard layouts. User keyboard and clipboard acceptance remains separate from these automated checks.

Vi-mode checks use a real fzf terminal to verify navigation, literal search input, mode changes and selection. Isolated popup checks verify the English/Russian toggle from the list, search, action menu, host chooser and project fields, plus creation and reopening from a managed remote connection. Slash-search checks also verify literal input, returning to the native tree with Escape, session selection, and closing both the bottom search panel and tree with either keyboard layout through local and managed routing.
