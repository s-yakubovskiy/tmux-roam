# tmux-roam implementation plan

Status: implementation in progress. Core plugin and isolated local/SSH tests are implemented. Workstation rollout is at the spare `F9` binding; `home.omni` authentication and user physical-key acceptance are pending. Working name: **tmux-roam**.

Implementation checkpoint (2026-10-31): version 0.1.0 is in this checkout. Isolated tests covered local tmux 3.7c, remote tmux 3.6/3.7c, exact attachment and reuse, concurrent creation, prefix routing, remote session changes, disconnect/retry, deletion and server restart. The 3.8 local matrix, sustained full-screen application testing, performance budgets and the final workstation key flow remain to be verified. Do not mark the definition of done complete until those gates pass.

The deliverable is a tmux plugin that puts local and remote tmux sessions in one picker, opened with the user's existing `Alt+a s` shortcut. Selecting an entry switches the current local tmux client to the selected workspace. Remote sessions continue to run on their original machines.

## 1. Product contract

The normal workflow must be:

1. Press `Alt+a s` from a local workspace or a managed remote workspace.
2. Search sessions across the local server and configured SSH hosts.
3. Press Enter to open a session.
4. Work with the selected session's windows and panes.
5. Press `Alt+a s` again to go somewhere else.

The Russian physical-key equivalent, `Alt+ф ы`, must perform the same operation. Arrow keys, Enter, Escape and control-key UI actions must work without requiring a Latin layout.

This version is complete when this navigation workflow, connection lifecycle and recovery behavior work reliably. The first release includes discovery, filtering, switching, reconnecting, connection cleanup, configuration validation and diagnostics. Creating or destroying remote workloads is a separate concern; session creation remains available through tmux and tmuxp.

### Observable invariants

- Opening or cancelling the picker does not change the attached session.
- Selecting a local entry switches only the invoking local client.
- Selecting a remote entry attaches to that exact existing remote session. A missing session is never silently recreated under the same name.
- Repeated selection reuses a verified connection where possible.
- A remote host failure never prevents local session switching.
- A failed selection leaves the user in the previous workspace with an actionable error.
- The picker represents a remote session once, even when a local connection session exists for it.
- Host, server and session identity are kept separate from display names.
- Closing a managed local connection does not issue `kill-session` remotely.
- The UI distinguishes recent observations, cached observations and an open connection. A running SSH process alone is not proof of successful tmux attachment.

## 2. Current environment and constraints

Verified on 2026-10-31:

| Component | Current state | Design implication |
| --- | --- | --- |
| Local tmux | 3.8, one default server | Use tmux popups and named key tables |
| Omni tmux | 3.6, inspected earlier in this conversation | Test the actual local/remote version pair |
| fzf | 0.74.5 | Supports identity-based selection tracking, refresh actions and a Unix socket control endpoint |
| Python | System Python 3.12 | Standard library provides TOML, subprocesses, locks, JSON and concurrency |
| SSH | OpenSSH aliases, including `home.omni` | Use the existing SSH configuration and agent |
| Shell | Zsh, work-environment virtualenvs modify PATH | Resolve and validate runtime paths explicitly |
| Session picker | `prefix s` and `prefix ы` run `choose-tree -Zs` | Preserve these entry points |
| Existing nesting controls | F12 plus `prefix v/м`, using `off` and `off-prefix` tables | Integrate without breaking unmanaged sessions |
| Workspace profiles | tmuxp defines local sessions and launches SSH in some panes | Discover sessions independently of how they were created |

The repository is currently empty. Implementation starts here. This plan does not install a plugin or alter live tmux/SSH configuration.

The first supported platform is Linux with local tmux 3.8, remote tmux 3.6 or 3.8, Python 3.11+, OpenSSH and a recent fzf supporting the required features. Feature detection should check the actual executable; the installed fzf manual can lag behind the executable. Broader version support follows a tested compatibility matrix rather than speculative fallback code.

## 3. Architecture

```text
Local tmux client
  └─ Alt+a s
      └─ tmux popup: Python controller + fzf
          ├─ live local tmux inventory
          ├─ cached remote metadata
          └─ bounded SSH discovery of configured hosts

Selection
  ├─ local session → switch the invoking client
  └─ remote session → reuse/create a managed local connection session
                         └─ connection supervisor → SSH → remote tmux client
```

Use a small Python application, the installed fzf, the tmux CLI and OpenSSH. A shell `.tmux` entry point handles TPM/manual loading. There is no persistent inventory daemon, database, remote installation or custom terminal renderer.

One managed local session normally represents one remote session. Its pane holds the SSH connection. tmux remains responsible for terminal rendering, layouts, scrollback and session persistence on each host. The plugin provides discovery, navigation and connection supervision.

Two local clients deliberately opening the same managed session share that connection and its remote view, just as clients attached to the same ordinary tmux session share a workspace. The first release does not promise independent remote window selection inside one shared proxy. Switching either local client to a different local session still affects only that client.

Use explicit modules for configuration, inventory, picker, connection and tmux integration. These are implementation boundaries, not an extensible provider framework. Both local and SSH discovery produce the same record type; add further backends only when a concrete use case needs them.

### IDs and ownership

Remote session identity is `(configured host ID, configured socket selector, remote server PID/start time, tmux session ID)`. Include session creation time as an additional guard. A session name is a label, never a command target or unique key. Local records similarly include the local socket and server generation.

Managed sessions have a readable name such as `omni/🤖omni` plus namespaced tmux user options containing ownership, connection ID and remote identity. Names are sanitized using tmux's naming rules. Resolve name collisions with a short suffix; never reuse an unrelated session because its name looks right.

Local tmux options are authoritative for running connection ownership. On-disk state is a disposable metadata cache. A remote server restart invalidates old session IDs, even if a new session has the same name.

Always carry the invoking socket, server generation, client name and client PID through popup callbacks. Never infer the target client from whichever session happens to be active when a background task finishes.

## 4. Picker UI and interaction

Example, with illustrative remote sessions:

```text
 Sessions                         2 hosts checked · 1 refreshing
 Search: _

 HOST          SESSION        WINDOWS   CLIENTS   STATE
 local         🌲pnl               4         1   current
 local         🐱dev               4         0
 omni          🤖omni              3         1   open here
 devdocker     build               2         0   cached · 24s ago

 Enter open  Esc cancel  Ctrl-r refresh  Tab details  F1 help
```

### Display rules

- Open a centered popup using roughly 85% width and 75% height, bounded to the current client size.
- Show local sessions and the last remote snapshot immediately; fetch remote updates in the background.
- Search host labels and session names together. Preserve emoji and Unicode.
- Default to local sessions first, then configured host order, then stable name order. Fuzzy matching may rank results while a query is present.
- Keep the query and selected identity when metadata changes. A refresh must not move the cursor to a different session simply because a row's status changed.
- If the selected identity disappears, keep the picker open, explain that it ended, and require a new explicit selection before connecting elsewhere.
- Do not reset filters or sort order because an unreachable host responds later.
- Treat host-status rows as informational. Enter on one shows details; it never starts a shell or creates a remote session.
- Include ordinary local sessions by default. For this workstation, configure `popup-*` as hidden. Offer a visible filter to include hidden sessions when needed.
- Fold owned local connection sessions into their remote rows. Display multiple existing connections in details rather than duplicating a remote session in the main list.

### Actions

| Input | Action |
| --- | --- |
| Enter | Validate and open the selected session |
| Escape / Ctrl-c | Cancel; retain the current workspace |
| Up / Down / Ctrl-p / Ctrl-n | Move selection |
| Ctrl-r | Request refresh, with repeated requests coalesced |
| Tab | Toggle metadata details |
| Ctrl-f | Cycle All / Local / Remote, with the active filter shown |
| F1 | Show concise help and key-routing rules |
| F2 | Open actions for the selected entry |

Initial F2 actions: open/retry, refresh this host, close an owned local connection, and show diagnostics. Closing a local connection must clearly identify the host/session and say that remote work remains running under its existing tmux lifecycle policy. It must not be named “kill session”.

Details show host alias, session name/ID, windows, attached-client count, socket selector, remote prefix, last successful observation and the last connection error. Do not capture pane contents for previews in the first release.

At small terminal sizes, collapse columns and details before reducing the session list. At sizes too small for a usable popup, show a short message and retain a working native picker shortcut. Use text state labels as well as colors. Icons are optional; an ASCII presentation must remain usable.

### fzf implementation

Use hidden opaque row IDs with `--id-nth`, NUL-delimited records and `--track`. Use `reload-sync` to replace a completed snapshot without briefly clearing the list. The controller updates a private snapshot and sends fixed refresh actions to fzf's Unix socket. Network discovery never runs in fzf's search or focus callbacks.

Create the socket and snapshots in a per-invocation directory with mode 0700. Only generated local paths and opaque IDs enter action templates; never interpolate a hostname or remote session name into an fzf shell action. Sanitize control characters in all displayed metadata and errors.

The plugin supplies its own deterministic fzf options. Do not inherit behavioral settings from `FZF_DEFAULT_OPTS`, `FZF_DEFAULT_OPTS_FILE` or `FZF_DEFAULT_COMMAND`. Provide a small documented set of theme/layout options instead. Close the control socket and cancel discovery work when the popup closes.

## 5. Key routing and tmux integration

Local workspaces continue using their existing prefix and bindings. The plugin replaces only its explicitly configured chooser bindings and adds a documented native-chooser fallback.

Managed remote sessions automatically use a plugin-owned key table. Normal terminal input reaches SSH. The local prefix introduces the following dispatch:

| Sequence in a managed remote session | Result |
| --- | --- |
| `Alt+a s` / `Alt+ф ы` | Combined local/remote picker |
| `Alt+a S` / `Alt+ф Ы` | Native local tmux chooser |
| `Alt+a`, then another tmux command key | Forward the detected remote prefix followed by that key |
| Local prefix pressed twice | Send two remote prefixes, preserving the remote double-prefix binding |
| `Alt+a F12`, then a command key | Send one remote prefix, then forward the following key directly to the remote tmux |

For example, `Alt+a F12 s` opens the remote native session chooser. Double `Alt+a` retains Omni's `last-window` binding instead of consuming it as an escape sequence.

Read the selected remote session's actual `prefix` option; support remote `M-a` and `C-b` without changing remote configuration. Account explicitly for `prefix None` and for a prefix changed while connected. Allow an explicit per-host override when auto-detection is unsuitable.

This automatically routed mode applies only to plugin-owned connection sessions. The user's current double-prefix `last-window`, F12 and `v/м` behavior remains in ordinary local sessions. In managed remote sessions, double-prefix follows the remote binding; F12 on its own is forwarded to the remote terminal. Manual outer passthrough is unnecessary. Document this difference in help and the managed-session status indicator.

For this workstation, also bind the combined picker into the existing `off-prefix` table, so it remains reachable from legacy passthrough mode. Preserve the existing `v`, `м` and `Any` handlers there. Detect conflicts and record exactly which bindings the plugin owns.

Use session-scoped key-table settings for owned proxies, never global prefix changes. Keyboard mode changes must not leak into unrelated sessions or other clients. Test root, prefix, off and off-prefix transitions, cancellation after a prefix, held modifiers, copy mode and popup focus.

The chooser must explicitly target its invoking client. Client B must not move because client A chose a session. Reloading the plugin must be idempotent. Hook entries, if required, use dedicated indices and do not replace another plugin's hooks.

Preserve an existing binding before the first install/rebind. Unloading restores it only if the current binding is still the plugin's own binding, so a later user edit is not overwritten. Load the plugin after the user's chooser bindings are defined.

## 6. SSH discovery and caching

Configuration explicitly lists the hosts to query. Do not enumerate every host in `~/.ssh/config`; that can include production machines, wildcard entries and slow jump hosts. Existing SSH aliases resolve Include, ProxyJump, identities, users and ports through OpenSSH itself.

Each refresh uses one bounded batch command per configured server to obtain session metadata and the connected clients needed to reconcile owned connections. Run independent hosts concurrently, with a default limit of four.

Proposed defaults:

- SSH connect timeout: 3 seconds; one connection attempt.
- Whole discovery command timeout: 5 seconds, including a remote command that hangs after authentication.
- Metadata cache freshness: 30 seconds; always label older observations.
- Refresh on picker open, at most once every 15 seconds while open, and on explicit Ctrl-r.
- Coalesce concurrent requests for the same host/server. No periodic discovery when the picker is closed.
- Limit response size and validate every record before admitting it to inventory.

Discovery uses `BatchMode=yes` and `RequestTTY=no`, with stdin closed. Explicitly suppress configured `RemoteCommand` for these controlled invocations; rely on OpenSSH for destination and authentication settings. Preserve host-key policy. Do not add permissive host-key flags or take ownership of a user's existing control socket.

Launch local subprocesses with argument arrays and `shell=False`. Where SSH requires a remote shell command, quote each argument for the remote POSIX shell and use fixed bootstrap code. Display text must never become executable code, a tmux format expression or an option. Validate host IDs, socket selectors and configuration values before constructing commands.

Use tmux-generated IDs and a versioned record format. Session names need correct handling of spaces, emoji, punctuation, backslashes and leading dashes. Validate tmux's escaping and naming behavior against both supported versions. Do not parse `tmux ls`'s human-readable default output. Socket paths and other unrestricted text fields need an unambiguous encoding.

Inventory success with zero sessions differs from transport failure, authentication failure, missing tmux and malformed output. Keep useful cached metadata after a failed refresh and expose the failure. A later successful inventory is authoritative for that server generation.

Cache only metadata and timestamps, under `$XDG_CACHE_HOME/tmux-roam`. Use atomic file replacement. Corrupt or incompatible cache files are discarded and regenerated. Runtime locks/sockets use `$XDG_RUNTIME_DIR` or a private per-user temporary directory. Never store authentication data or terminal output.

## 7. Opening and tracking a remote connection

### Selection transaction

1. Capture the selected opaque identity and the invoking client's current session.
2. Revalidate that the remote server generation and session still match. Keep the picker visible during validation; Escape cancels pending navigation.
3. Acquire a short per-target creation lock and recheck for an owned connection. Never hold this lock during a slow network operation.
4. Reuse a connection only after confirming which remote tmux client it represents and which session that client currently displays.
5. Otherwise create a detached managed local session and supervisor with the invoking client's terminal dimensions. Mark its state as preparing and preserve an ownership token.
6. Establish SSH and attach by the exact remote session ID. Do not use `new-session -A` for an existing-session selection, and do not detach other remote clients.
7. Confirm attachment through the remote tmux client inventory. Recheck the caller is still valid and the navigation request has not been cancelled or superseded.
8. Close the popup and switch only that client to the prepared local session.
9. On failure or cancellation, clean up only resources newly created for this transaction. A previously open connection is never destroyed as incidental rollback.

Late responses must not steal focus after the user cancels or opens another picker. Pending navigation uses a generation/token per invoking client. Repeated Enter is idempotent.

A preparing connection has one creation owner. Other requests observe that attempt instead of creating a second SSH client. Cancelling a follower cancels only its navigation. Rollback must recheck ownership and whether another client has already committed to the connection before deleting anything; an aborted attempt returns a clear retryable result to remaining observers.

Owned attachments also use `BatchMode=yes`, with an explicit tty allocation. Version one assumes usable SSH keys/agent authentication, matching the current workstation. It must fail visibly rather than leave a password or host-key prompt waiting in an invisible detached pane. Interactive authentication, if added later, needs its own visible flow.

### Identifying the remote client

An SSH attachment can subsequently switch to another remote session. Therefore the plugin must identify the actual remote client, rather than assuming the initial target remains displayed.

The proposed bootstrap runs a small POSIX shell command over the attachment SSH connection. It validates the selected server/session, emits a bounded nonce-tagged prelude containing its process ID and terminal, then `exec`s the remote tmux client. The supervisor removes only that exact bootstrap prelude and records the client identity. Remote `list-clients` verifies it against the selected server.

Keep SSH stdin attached to the existing pane tty. Forward stdout bytes unchanged after bootstrap; inherit SSH stderr for authentication/transport errors. This is a small stream relay, with no terminal emulation or screen parsing. Signal propagation, tty restoration, resize behavior, partial writes, backpressure and throughput are mandatory proof-of-concept tests before relying on this mechanism.

If this mechanism cannot pass those tests, stop the connection implementation and revise client identification before building the rest of the UI. Do not ship guessed connection state or add a second custom PTY stack as an expedient fallback.

At refresh and before connection reuse, reconcile the remote client's current session. If the user changed sessions remotely, update the displayed association. Never switch an arbitrary remote client by targeting only a session name. If external changes produce two owned connections to one remote session, prefer the caller's connection, then the most recently used verified connection; show the count and permit explicit cleanup.

### Connection lifecycle

```text
discovered → validating → preparing → attaching → open
                 └──────── failure ─────────────→ error
open → clean detach / transport loss / remote session ended → disconnected
disconnected → explicit retry → validating
```

Remote reachability, local connection state and remote session existence are separate fields. An offline host can still have an SSH process that has not detected the outage yet.

The supervisor remains in its owned pane after SSH exits and displays a short reconnect screen: host/session, error or normal detach, Enter to retry and the combined-picker shortcut. The previous remote session is revalidated before retry. A server restart or ended session requires a fresh selection; never attach to a reused numeric ID accidentally.

Use application keepalives for owned attachment connections, initially 15 seconds with three missed replies. Reconnection is explicit in the first release. This avoids retry loops after deliberate detach, authentication errors or a completed session. A bounded automatic reconnect policy can be added later after transport classification is proven.

When the local connection is closed, terminate only its supervisor/SSH child. When the local tmux server restarts, remote workloads remain subject to the remote server's existing lifecycle settings and can be rediscovered. Diagnose unusual remote policies such as `destroy-unattached` rather than promising persistence that those policies disable.

## 8. Configuration and extension

Use one TOML file at `$XDG_CONFIG_HOME/tmux-roam/config.toml`. Keep operational settings here and tmux key bindings in the TPM integration. Validate unknown fields and report file/key-specific errors.

Example intended configuration:

```toml
version = 1

[picker]
width = "85%"
height = "75%"
details = false
icons = true
hide_local = ["popup-*"]

[discovery]
cache_seconds = 30
refresh_seconds = 15
connect_timeout_seconds = 3
command_timeout_seconds = 5
concurrency = 4

[[hosts]]
id = "omni"
label = "Omni"
ssh = "home.omni"
enabled = true
tmux_path = "/usr/bin/tmux"
remote_prefix = "auto"

[[hosts]]
id = "devdocker"
label = "Devdocker"
ssh = "devdocker"
enabled = true
tmux_path = "tmux"
remote_prefix = "auto"
```

Optional host fields include an explicit tmux socket name or absolute socket path, mutually exclusive. Another server on the same SSH host gets another stable host entry ID. Adding a host requires only a new configuration entry.

The integration exposes a small set of tmux options: config path, chooser keys, native-fallback keys, extra existing key tables to integrate with, and runtime executable overrides. Defaults follow the current tmux prefix; this workstation explicitly configures `s/ы` and `S/Ы`. Discover fzf/Python once at load and validate them; do not accidentally depend on whichever work virtualenv happens to be active.

CLI contract:

```text
tmux-roam pick                 Open the combined picker
tmux-roam list --json          Machine-readable inventory, no mutation
tmux-roam refresh [host-id]    Refresh inventory
tmux-roam open <opaque-id>     Open an exact listed session
tmux-roam doctor              Check config, binaries, bindings and host status
tmux-roam config check        Validate configuration without connecting
```

Document JSON schema versioning and exit codes. Structured errors and the JSON inventory make it possible to add a different UI later without rewriting discovery and connection logic. Avoid arbitrary executable hooks or user-defined provider classes in the first release.

tmuxp remains independent: any running session it creates appears automatically. A later explicit “workspace profiles” source can list configured tmuxp profiles and create sessions through `tmuxp load -d`. Such entries must be labelled as launchable profiles, distinct from running sessions.

## 9. Repository and delivery

Start with this layout, splitting files further only when necessary:

```text
tmux-roam.tmux                 TPM/manual entry point
bin/tmux-roam                  Python entry point
tmux_roam/
  cli.py                      Commands and orchestration
  config.py                   TOML validation and defaults
  tmux.py                     Explicit targeting, ownership, key installation
  inventory.py                Local/SSH discovery and cache
  picker.py                   fzf lifecycle and UI actions
  connection.py               Selection transaction and SSH supervision
tests/                        Contract and isolated integration tests
examples/config.toml
README.md                     Installation, keys, configuration, recovery
IMPLEMENTATION_PLAN.md
```

Install as a normal TPM plugin or source the `.tmux` entry point from a checkout. Python uses only its standard library, so installation does not require pip or a project virtualenv. No remote package is installed.

Document which local checkout provides the plugin, its version, configuration paths and update process. Plugin reload should not terminate existing SSH supervisors. Breaking state-format changes require an explicit compatibility decision; routine updates must not kill connection sessions.

## 10. Implementation stages and completion gates

### Stage 1 — prove tmux routing and connection mechanics

Create isolated local/remote tmux servers, a pseudo-terminal test client and a disposable SSH target. Prove chooser invocation and cancellation, invoking-client targeting, prefix forwarding, Russian aliases, attachment identity, streaming, terminal resizing and detection of remote client session changes.

Also prove fzf asynchronous reload preserves both the query and selected row identity when labels change and rows disappear. Exercise a slow host while selecting a local session.

Gate: the interaction and connection mechanisms work before building configuration breadth or visual polish. Production sessions are not test targets.

### Stage 2 — inventory, configuration and diagnostics

Implement schema validation, explicit hosts, local discovery, bounded remote discovery, server-generation identity, client metadata, cache and `list --json`. Add `config check` and initial `doctor` output.

Gate: one unavailable host cannot stall local inventory; zero sessions, stale data and failed queries are distinguishable; malformed metadata cannot become a command.

### Stage 3 — complete picker

Implement the popup, fuzzy search, stable refresh, hidden-session filter, metadata details, help and host-error display. Make local selection fully functional first. Test narrow dimensions, Unicode, colors and cancellation.

Gate: first local results appear promptly, filtering stays responsive during refresh, and cancelled/late work never switches a client.

### Stage 4 — managed remote sessions

Implement selection transactions, connection ownership, deduplicated creation, exact attachment, supervisor lifecycle, per-session routing, remote-client reconciliation, retry and local connection cleanup.

Gate: selecting/reselecting a remote session works from every supported local state, no unrelated client moves, and disconnect/restart scenarios retain an accessible picker and accurate state.

### Stage 5 — packaging and compatibility

Implement the TPM entry point, idempotent reload, ownership-aware key restoration, version checks, sample config, help, logging and documentation. Complete migration notes for existing tmuxp-created SSH panes.

Gate: a clean user can configure one SSH alias and use the complete workflow; a user with existing plugins can remove tmux-roam without losing their unrelated bindings.

### Stage 6 — workstation rollout and acceptance

1. Record the live relevant bindings and save a timestamped configuration backup.
2. Initially load the picker under a spare binding and configure only `home.omni`.
3. Verify local/remote discovery and connection against Omni with the user's actual tmux configuration.
4. Exercise the complete US and Russian keyboard flow with the user.
5. Promote it to `s/ы`, retaining the native fallback at `S/Ы`.
6. Add devdocker through configuration and repeat disconnect/reconnect acceptance.
7. Review the existing manual `🤖omni` SSH session separately; do not take ownership of it based on its name. The old and new paths can coexist during acceptance.
8. Provide the changed files, active version, verified behavior and rollback command.

Implementation authorization permits building and testing the plugin; the delivery report must distinguish automated validation from the user's actual keyboard acceptance. Do not call the latter complete until it has happened.

## 11. Tests that establish reliability

Use standard-library unit tests for the meaningful contracts and isolated tmux/SSH integration tests for real behavior. Do not substitute mocked command lists for end-to-end navigation tests.

| Area | Required cases |
| --- | --- |
| Identity | Same name on two hosts; local/remote name collision; rename; session deletion; server restart and ID reuse |
| Input | Emoji, Cyrillic, spaces, quotes, backslashes, leading dashes, hostile control characters and malformed responses |
| Discovery | No server, no sessions, auth failure, unreachable host, delayed command, missing binary, unexpected shell output, corrupted cache |
| Switching | Local → remote → local; remote A → remote B; repeated selection; double Enter; cancelling while connecting |
| Clients | Two local clients selecting different sessions; simultaneous creation of the same target; detached caller; a late result after a newer request |
| Remote tracking | Remote client switches session; remote prefix changes; multiple clients attached; unrelated clients remain attached |
| Keyboard | Normal prefix, legacy off/off-prefix, managed routing, repeated prefix escape, F12 behavior, US/Russian aliases, copy mode, popup focus |
| Terminal | Resize, sustained output, full-screen Neovim/btop, Ctrl-c/Ctrl-z, bracketed paste, mouse reporting, OSC clipboard and tty restoration |
| Recovery | SSH loss, network return, explicit remote detach, remote session ending, local proxy closure, local tmux restart, plugin reload |
| Packaging | Missing dependency, stale binary/manual mismatch, bad config, repeated load/unload and user-edited bindings |

Performance targets are acceptance budgets to measure on the workstation, not current benchmark claims:

- Local and cached rows visible within 200 ms at the 95th percentile across 30 warm opens.
- UI remains responsive throughout a five-second host timeout.
- Remote results appear independently as each host completes.
- Manual refresh preserves query and selection; coalesce bursts of updates to avoid flicker.
- No inventory polling or idle inventory process after the popup closes. One small supervisor remains per open remote connection.
- No duplicate SSH attachment from repeated Enter or concurrent selection of the same target.
- Connection forwarding must not introduce visible typing/resize lag relative to plain SSH; compare with the same remote full-screen applications and a sustained-output workload.

## 12. Definition of done

The plugin is ready when the staged gates pass, the documented Linux/version matrix is tested, and the user can repeatedly perform the following:

`Alt+a s → local session → Alt+a s → Omni session → create/switch remote windows → Alt+a s → another local session`, including the Russian layout equivalent, with no manual F12 transition required for managed sessions.

Disconnecting Omni must leave local switching usable. Reconnecting must either recover the exact still-existing remote session or clearly require a fresh selection. Uninstalling must restore owned bindings without deleting local or remote work.

After this release, prioritize additions using actual use: explicit tmuxp profile launching, create/rename/terminate actions with separate semantics, pane previews, opt-in automatic reconnection, and broader platform support. None of these is a prerequisite for delivering the requested unified session workflow.
