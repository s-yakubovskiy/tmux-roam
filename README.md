# tmux-roam

The Go runtime and offline release bundles are implemented. See [GO_MIGRATION_PROGRESS.md](GO_MIGRATION_PROGRESS.md) for validation and the remaining production gates. This checkout defaults to Go; the workstation TPM clone has not been changed.

tmux-roam shows local tmux sessions and sessions on explicitly configured SSH hosts in tmux's native session tree or an fzf popup. Selecting a remote session opens a managed local tmux session that holds an SSH attachment to the existing remote session. It never runs `new-session -A` remotely.

Development roadmap: [Go migration plan](GO_MIGRATION_PLAN.md), including full feature parity, performance targets, package boundaries, and verification checklists for every stage.

This checkout is version **0.1.0**, with an unpublished Go release candidate. The Go runtime requires Linux, OpenSSH, local tmux **3.8+** and fzf; Python and Go are development tools only. Validation covers local tmux 3.8/fzf 0.74.5 and remote tmux 3.6 on Omni and 3.6a on DevDocker. Linux AMD64 runs on both targets. Native Linux AMD64 and ARM64 hosted CI passes the full acceptance, release and fuzz checks; the oldest supported kernel remains unverified.

Local servers older than 3.8 can crash when respawning a pane with control clients attached ([upstream fix](https://github.com/tmux/tmux/commit/4714f478fed87ae2c510ecd035aa89c5a90dedf8)). Remote opens and native metadata refresh refuse before creating or respawning panes on those servers. Local session switching remains available. The guard checks the running server version; updating only the client binary does not upgrade an existing server. Remote tmux 3.6/3.6a remains supported.

Config sync additionally uses local GNU `diff` and remote `sh`, `tar`, `mktemp`, `uname`, and `rm`. The bundle includes static temporary helpers for Linux AMD64/ARM64. No remote Python, Go compiler, permanent helper installation, or internet download is required.

## Install

Copy [examples/config.toml](examples/config.toml) to `$XDG_CONFIG_HOME/tmux-roam/config.toml` (or `~/.config/tmux-roam/config.toml`) and edit the explicit `[[hosts]]` entries. Host `ssh` values are OpenSSH aliases, so existing `~/.ssh/config` handles users, keys, ports and jump hosts. The plugin uses batch key/agent authentication. It does not modify host-key policy.

Install a trusted release bundle explicitly before loading the plugin:

```sh
sh scripts/install-go.sh /path/to/tmux-roam-0.1.0-linux_amd64.tar.gz EXPECTED_SHA256
```

Obtain the expected SHA256 from a trusted release channel or your own build output. A checksum supplied alongside an untrusted archive does not authenticate it. Installation checks the checkout's `VERSION`, architecture, helper protocol, and each payload digest, then atomically activates an immutable directory under `.runtime/releases/`. The previous verified bundle remains available for rollback. Interrupted or failed verification preserves the current activation.

For a development checkout, build explicitly with `sh scripts/build-go.sh` (Go 1.27.2), or create both architecture bundles and `SHA256SUMS` with `sh scripts/release-go.sh`. The launcher resolves `.runtime/current/tmux-roam`, then the explicit local `build/tmux-roam-go`; it never searches PATH for the controller. A source update requires another explicit build or bundle install. TPM reloads and picker startup perform no downloads or builds. If no binary exists, loading reports the install command and preserves existing bindings.

In `~/.tmux.conf`, load the plugin **after** your chooser bindings:

```tmux
run-shell '/absolute/path/to/tmux-roam/tmux-roam.tmux'
```

This candidate is unpublished: use its matching source checkout and the explicit build/bundle steps above. For **TPM**, the matching plugin source and installed bundle use the declaration below in place of the direct `run-shell`. Put it before your existing TPM initialization:

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

`bin/tmux-roam doctor` reports the executable path/build identity, verifies an installed release manifest, checks tmux/fzf capabilities, and reports configured hosts and live host errors. `TMUX_ROAM_TIMING=1 bin/tmux-roam doctor` adds dependency, local-inventory and per-host observation durations to stderr; host durations include SSH and remote work. `bin/tmux-roam --version` does not load configuration or contact hosts.

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

The fzf popup prepares cached rows once and starts fzf directly, avoiding a second controller startup inside the window. The tree stays visible while the search overlay occupies its lower preview area. The first list is rendered together, and background discovery reloads it only when visible rows change. Remote discovery starts after fzf is ready, so an offline host does not delay opening. No persistent picker process is required.

Remote selection shows connection progress while SSH validation and attachment run. Escape or either layout's popup toggle cancels the attempt; closing lower search returns to its tree. A failed attempt keeps the fuzzy query and selected identity available for retry, with the full error under Tab details. Native-tree remote selections use a small progress popup with Enter to retry and Escape to cancel. A cancelled attempt preserves the invoking client's session and cleans up its unused connection.

For the previous tmux-native search view, set `@tmux-roam-native-search native` and reload. This uses tmux 3.8's built-in fuzzy matcher; older local tmux versions without `switch-mode` retain the tree. If the option is unset, the native search backend is used.

Press the native picker shortcut again to close it, from either browse or fuzzy search mode. This toggle also works inside managed remote sessions and with the Russian shortcuts.

The native tree shows friendly names such as `Omni / 🤖omni`, remote window counts, and connection state. Opening the tree and selecting a local session run entirely inside tmux. Existing entries appear immediately; a background worker publishes cached entries and refreshes discovery while the tree is open. It redraws only when the visible metadata changes and uses a tmux mode-change notification to stop on exit, without frequent polling. Entries are retained local panes with no running process until selected. Remote selection invokes Go to validate the exact remote identity and connect or reuse its terminal. Native navigation, search, mouse selection and previews remain available.

To expand remote sessions into their windows, enable this option before TPM initialization and reload:

```tmux
set -g @tmux-roam-native-windows on
```

In `Alt+a s`, select a remote session and press **Right** or **l** to expand it. Window indexes, names and the remote active-window `*` come from the same SSH discovery request as session metadata. **Enter** on a window opens that exact remote window through the existing attachment; Enter on the session opens its current window. Session-only fuzzy search remains on `/`. Window names are cached and retained during outages. Refresh runs on opening and every `discovery.refresh_seconds` while the picker is open; it requires no daemon or per-window SSH connections.

Native tmux needs local windows to render child rows. Roam uses exited metadata-only windows, hides the SSH carrier from the tree, and removes those rows before connecting or reconnecting. Their preview identifies a remote window rather than streaming its contents; the remote terminal appears when opened. User-modified, running, linked or currently viewed rows are preserved. To disable the extra rows, set the option to `off` and reload; the plugin removes its untouched metadata windows and restores the carrier's original index. Without this option, expanded remote children represent the local SSH terminal.

Native window mirrors are limited to **100 remote windows per host**. Larger inventories retain session entries and their real remote window counts; selecting a session still attaches normally. The plugin removes its untouched mirrors when a host grows beyond the limit, preserving edited, running, linked or viewed windows. `doctor` reports this fallback when mirrors are enabled. Initial creation of 2,500 mirrors exceeded ten minutes, so large native mirroring is deferred from this release; see the [performance evidence](GO_MIGRATION_PROGRESS.md#performance-evidence).

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

## Rollback

To reactivate the previous verified Go bundle, run `bin/tmux-roam release rollback /absolute/path/to/tmux-roam/.runtime`, then `bin/tmux-roam integrate install`. To restore Python callbacks, run `bin/tmux-roam-python integrate install` (Python 3.11+). `TMUX_ROAM_BACKEND=python bin/tmux-roam ...` is also supported. The explicit Python launcher pins legacy callbacks to Python, independent of the checkout's default backend.

Existing supervisors keep their original executable and paired helper files. Updating bindings does not migrate or close them. Retain release directories and the Python package while any live supervisor uses them. Python/Go binding journals, caches, exact live-carrier reuse, and reconnect in the original pane are covered by the coexistence acceptance test. Unload restores only unchanged owned bindings, preserving subsequent user edits and active connections.

## Keys and navigation

The popup fits the cached session count, up to 96 columns and 24 rows, within the configured width/height budgets and terminal bounds. Session rows are compact, with extra space below the headings and around the aligned controls. It keeps a minimum usable size and scrolls if discovery adds more sessions. Each session is shown as a host/project path, such as `Local/dev` or `DD/gitlab`. A muted slash separates the host folder from the project. Window counts (`3w`) and status stay beside the path instead of stretching to the terminal edge; narrow terminals omit the count. Fuzzy search matches the whole path, so `dd/git` narrows to a host and project together. Host colors follow configuration order (orange, green), while local sessions use gray. Names are measured in terminal cells so emoji and combining characters keep the columns aligned. The current session is marked with `*` and selected initially. Setting `picker.icons = false` uses a plain ASCII `>` row pointer. Brighter bold text and a slim `›` mark the selection on the same plain background; the `*` stays on the session you are using. Host colours are text accents, without filled badges; connection states use the same semantic colours as the native tree. Search filters remain visible after returning to vi navigation; technical details are available with Tab.

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

Use the plugin's `bin/tmux-roam` path if it is not on your PATH. `config check` continues to validate the plugin's TOML configuration. `config diff` compares file contents without changing the remote config; its exit status is 0 for matching files, 1 for differences, or 2 for errors. Sync returns 2 if any host fails, while continuing with the other selected hosts. A host ID or `--all` is required; disabled hosts are excluded. Offline hosts are reported and can be retried later. These commands use the bundle's static helper temporarily through SSH; remote Python and Go are not required. The helper is removed after each operation.

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

Run `sh scripts/check-go.sh` after installing the development dependencies (Go, Python, tmux 3.8, fzf 0.74.5, openssh-server, Vim and zsh). Set `TMUX_ROAM_TEST_SSHD` to an absolute sshd path if necessary. The suite runs Go tests/vet/race checks, public CLI/config/inventory comparisons, real loopback SSH, attachment/reconnect, uncertain create/delete outcomes, live resize, input/selection during delayed fzf refreshes, terminal streaming and real Vim editing, Python-free config sync, offline release installation/rollback, backend coexistence and the 34 legacy Python baseline tests. Run `sh scripts/fuzz-go.sh` for the bounded configuration, identity, inventory and quoting fuzz checks.

To include actual noexec/full-filesystem config-sync checks, set `TMUX_ROAM_TEST_CONTAINER_IMAGE` to an existing Linux AMD64 Docker image containing standard `sh`/`tar` tools. The fixtures use isolated containers with no network, private tmpfs mounts and a read-only build bind mount; they pull no image and change no host mount.

Actual Omni and DevDocker canaries use private remote homes and sockets, preserve existing session identities, and clean up their own resources. Run them explicitly with `python3 tests/acceptance/real_hosts.py omni devdocker`. They do not constitute a production TPM cutover. The optional UI benchmark uses the development-only dependencies in `bench/requirements.txt`. Current evidence, benchmark commands and outstanding production gates are tracked in [GO_MIGRATION_PROGRESS.md](GO_MIGRATION_PROGRESS.md). Physical keyboard, touchpad, clipboard and suspend/resume checks and the production soak remain separate from generated PTY input. Earlier workstation rollout evidence describes the Python deployment.

For physical acceptance in your terminal, run `python3 tests/acceptance/real_hosts.py --interactive devdocker`. After its automatic checks it attaches a private Go canary with native/manager Latin and Cyrillic bindings, local shells and a remote shell. Check both layouts, mouse clicks/double-click/drag/scroll, clipboard in both directions, resize, and normal terminal applications. Return to `Local/local` and press `Alt+a d` to leave and clean up the fixture. Record actual results in the migration progress report; starting the canary alone does not establish acceptance.

The canary accepts `--command-timeout 30` to test a slower link using the existing discovery/validation timeout setting in its private configuration. An earlier November 9 Omni canary needed this larger transfer budget; its config sync took 32 seconds, after the default-budget canary failed and a plain OpenSSH helper upload also timed out after 15 seconds. Later builds passed with the default fixture timeout; the latest sync took 1.66 seconds. Reproduce physical Omni checks with `python3 tests/acceptance/real_hosts.py --interactive omni`; use the larger fixture timeout if the link slows again. See the progress report for the latest host results. The flag changes no production configuration.

## Developing the Go runtime

Build with `sh scripts/build-go.sh`; package with `sh scripts/release-go.sh`. There are two runtime dependencies: TOML parsing and Unicode grapheme/cell widths. SSH, process handling, storage, JSON, terminal byte forwarding and release installation use the standard library and system executables.

`cmd/tmux-roam` validates CLI arguments and dispatches operations. `internal/config` and `model` define configuration and opaque identity contracts; `inventory` owns discovery/cache/coalescing; `session` owns exact attachment, actions, supervisors and cleanup. `bindings`, `native` and `picker` compose tmux/fzf views. `process`, `ssh`, `tmux` and `store` are shared system adapters. `configsync` is shared by the controller and the small `cmd/tmux-roam-agent`; `release` verifies and activates paired bundles.

For example, F2 deletion runs through `cmd/tmux-roam/ui.go` → `picker.DeleteSession` (explicit Cancel-first confirmation) → `session.Delete` (exact identity validation and deletion in one tmux command queue). SSH quoting and bounded process capture are shared adapters. The PTY deletion scenario is in `tests/acceptance/pickers.py`, and stale identity/quoting checks are in `internal/session/safety_test.go`. Add a new action through the existing dispatcher/domain operation rather than creating a second transport path. Retain JSON stdout, cancellation and ownership guards.

To trace an isolated command, use `strace -f -tt -o /tmp/roam.trace build/tmux-roam-go list --cached --json` with the disposable test environment. Trace output may contain session/config metadata; do not publish it as a benchmark report. The launcher and installed tmux callbacks resolve a known controller path, so changes require an explicit rebuild or verified bundle installation.
