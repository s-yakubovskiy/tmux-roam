# tmux-roam: Go migration implementation plan

Date: 2026-11-07. Status: Go release candidate implemented; automated acceptance and performance work continue. See [GO_MIGRATION_PROGRESS.md](GO_MIGRATION_PROGRESS.md) for evidence and remaining gates. This development checkout defaults to Go; the production TPM clone remains on Python.

Baseline: commit `5fbfc123a2e99b604aeb3e51ac3e7a81f76386c3` (`integration`). The current implementation has approximately 2,900 lines of runtime Python and 1,600 lines of tests. All 34 Python baseline tests pass in the current environment. The explicit Python launcher remains available for coexistence and rollback tests.

This plan covers a complete replacement of the Python runtime. Every current capability must be preserved or replaced by a demonstrably equivalent or better interaction. Performance, clear Go code, and ease of adding features are release criteria. A faster picker with missing lifecycle, configuration-sync, or input behavior is not a completed migration.

Use [README.md](README.md), the implementation, and executable acceptance scenarios as the current behavior reference. [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) is historical: it predates session creation/deletion, the current native picker, and several deployed fixes.

## 1. Decisions and completion contract

- Build a Go controller while retaining tmux's native tree, fzf, and the user's OpenSSH executable.
- Keep the current TOML configuration, tmux options, CLI entry point, shortcuts, and ownership model compatible.
- Preserve immediate native local navigation. Go is not inserted into individual keypress or mouse-event forwarding.
- Keep discovery workers scoped to an open picker or an explicit CLI request. No always-running inventory daemon or systemd service.
- Keep one lightweight supervisor for each active managed SSH connection. A supervisor is a process inside its owned tmux pane, not a machine-wide service.
- Make the final runtime Python-free, including configuration sync. Python may remain a development/test dependency and a temporary rollback backend during migration.
- Use a small Go helper for explicit remote config sync. Transfer and run it temporarily; do not install a remote daemon or require Go on the destination.
- Keep UI improvements incremental and measurable. Preserve familiar shortcuts, minimal styling, and current cancellation behavior.
- Introduce no database, plugin/provider framework, custom SSH client, or replacement terminal renderer in this migration.

The first supported release targets Linux AMD64 and ARM64. Validate the actual minimum kernel and tmux requirements rather than assuming static binaries work on every Linux machine. The current workstation uses tmux 3.8/fzf 0.74.5; actual isolated canaries pass on Omni with remote tmux 3.6 and DevDocker with remote tmux 3.6a. AMD64 has runtime evidence; ARM64 currently has cross-build evidence only. Retain the existing tmux-native search fallback where capability detection permits it.

### Definition of done

Checked items below identify implemented or verified individual requirements. A stage is not complete until all of its exit gates pass; unchecked broader/manual criteria remain open. Evidence is mapped to executable scenarios in the progress report.

- [ ] Every feature in the parity matrix has passing acceptance evidence against Go.
- [x] Native navigation, fzf, session lifecycle, config sync, installation, update, unload, and rollback work together (isolated acceptance).
- [x] No Python or Go toolchain is required for newly launched Go runtime operations, locally or remotely (static bundles; PATH-restricted install and config-sync acceptance).
- [ ] Performance gates pass under both healthy and failing-host conditions.
- [x] Existing active connections survive backend activation and rollback (isolated Python/Go coexistence).
- [ ] Physical keyboard, touchpad, clipboard, and resize checks pass on the workstation and real remote hosts.
- [x] A new maintainer can build, test, trace a command, and add a small action using documented package boundaries (README development guide).

## 2. Feature parity matrix

Each row receives an acceptance-test ID in Stage 0. Passing Python behavior is the baseline; an intentional behavioral improvement must have its new expected result recorded explicitly.

| Area | Behavior to preserve or improve | Primary stage |
| --- | --- | --- |
| Configuration | Strict TOML parsing; defaults; unknown/duplicate/invalid-key errors; enabled hosts; explicit host order | 1 |
| Host targeting | SSH aliases, custom tmux executable, named/absolute tmux sockets, detected or overridden remote prefix | 1–3 |
| Public CLI | `--version`, `list --json [--cached]`, `refresh`, `open`, `pick`, `native`, `doctor`, `config check/diff/sync`, `integrate install/unload` | 1–9 |
| Installed callbacks | Existing popup, picker, native-select/refresh, actions, creation/deletion, and supervisor commands continue to resolve | 1–10 |
| Local inventory | Discover ordinary sessions; hide configured patterns; fold owned connections into remote records; preserve tmuxp-created sessions | 2 |
| Remote inventory | Bounded concurrent discovery of sessions, windows, clients, prefixes, server generation, and actual socket | 2 |
| Cache | Immediate cached rows; age/source distinction; corrupt-cache recovery; atomic writes; metadata only | 2 |
| Refresh | Per-host completion; periodic refresh only while open; manual refresh; cancellation; no redraw for unchanged rows | 2, 4, 5 |
| Outages | Keep cached hosts/sessions selectable; clearly report unavailable hosts; reconnect explicitly after connectivity returns | 2, 3 |
| Connection reuse | Exact remote identity validation; verified attachment reuse; identify the remote client by PID/TTY; reconcile remote session changes | 3 |
| Reconnect | Retry the same identity in the same owned local session/pane; no duplicate carriers after outages | 3 |
| Navigation ownership | Only the invoking client switches; cancelled/superseded requests never steal focus; concurrent requests have one creation owner | 3 |
| Cleanup | Remove only proven owned, safe, inactive duplicates/ended entries; preserve attached, renamed, linked, or repurposed resources | 3, 4, 6 |
| Global shortcuts | Local/native and manager pickers from remote sessions; global previous-session toggle; double-prefix previous-window behavior | 4 |
| Keyboard layouts | Latin and Russian physical-key aliases, including literal Cyrillic input during search | 4, 5 |
| Nested input | Remote prefix `M-a`, `C-b`, and `None`; F12 escape; copy mode; mouse scrolling/clicks/dragging; application input | 3, 4 |
| Clipboard | OSC 52 through SSH and both tmux layers; local clipboard configuration remains effective | 3, 4 |
| Native picker | Immediate tree; previews; compact rows; counts; current marker; toggle; stable selection and expansion during refresh | 4 |
| Remote windows | Cached child windows; exact window selection; safe dormant mirror creation/removal; no unsolicited live pane streaming | 4 |
| Bottom fuzzy search | `/`/`.` opens search in the preview area; Escape restores the same tree; picker shortcut closes both | 5 |
| Manager picker | Opens in search; Escape closes immediately; Ctrl-o enters vi navigation; toggle works inside nested menus/forms | 5 |
| Search and presentation | Host/project paths, Unicode cell alignment, current `*`, visible selection, semantic status colors, padding, small-terminal fallback | 4, 5, 8 |
| Picker controls | Details, help, diagnostics, All/Local/Remote filter, hidden sessions, refresh, and F2 actions | 5, 6 |
| Create projects | Select host, name, existing remote directory; create an independent session; open it and publish it to both pickers | 6 |
| Delete sessions | Local and remote; `dd`/`вв` in navigation; explicit confirmation defaulting to Cancel; guarded exact-identity deletion | 6 |
| Disconnect | Explicitly close an unused owned local connection without deleting the remote session | 6 |
| Shared config | Diff/sync one host or all enabled hosts; alternate source; preview; stale-preview guard; backups; symlinks; atomic replacement | 7 |
| Config activation | Validate with destination tmux; reload selected socket without restarting sessions; save when stopped; file rollback on reload failure | 7 |
| Distribution | TPM/manual loading; dependency checks; source-build path; deterministic executable resolution; upgrade/unload/rollback | 9, 10 |

### Interaction contract

These are the current workstation bindings, while generic plugin options remain configurable.

| Context / key | Required result |
| --- | --- |
| `Alt+a s` / `Alt+ф ы` | Toggle the local native tree from an ordinary or managed remote session |
| Native tree: `/` / `.` | Open the lower search panel; retain the tree above it |
| Lower search: Escape | Restore the tree preview, selection, and expanded rows |
| Lower search: native picker shortcut | Close both search and tree |
| `Alt+a b` / `Alt+ф и` | Toggle the manager, including nested action menus and project forms |
| Manager opening | Search input active immediately |
| Manager: Escape | Close immediately, whether the query is empty, matches, or has no matches |
| Manager: Ctrl-o | Enter vi navigation, preserving the query |
| Navigation: `/` or `i`; Russian `.` or `ш` | Resume text search |
| Navigation: `j/k`, `о/л`, `g/G`, `п/П` | Move/select first or last; letters stay literal in search |
| Navigation: `dd` / `вв` | Open guarded deletion confirmation; intervening input cancels the pending operator |
| Manager: Ctrl-n | New remote session, including when the current search has no matches |
| Manager: Ctrl-r / Ctrl-f / Ctrl-h / Tab / F1 / F2 | Refresh / scope / hidden sessions / details / help / actions |
| Action/host menus | Start in vi navigation; Escape leaves their search mode first, then closes the menu |
| Name/directory fields | Accept text immediately; cancellation and global popup toggle work |
| `Alt+a Shift+l` / `Alt+ф Shift+д` | Toggle the previous outer session across local and roaming entries |
| Double `Alt+a` / double `Alt+ф` in a carrier | Preserve the remote double-prefix action |
| `Alt+a F12`, then a key | Explicitly invoke the remote command, including reserved `s` and `L` |
| `Alt+a [` in a carrier | Enter remote copy mode; remote vi selection and `y` work |

Do not replace the host's theme, add a second status bar, or add persistent terminal banners. Keep the orange OMNI and green DD identities distinct from connection-state colors.

## 3. Go architecture and extension rules

The dependency direction is CLI/application orchestration → domain operations/adapters → operating-system boundaries. Rendering does not perform SSH or mutate connection state. Parsing has no side effects. Packages receive the configuration and invoking-client context explicitly.

Proposed layout; create packages when their stage needs them rather than generating empty scaffolding:

```text
cmd/tmux-roam/          # thin controller entry point
cmd/tmux-roam-agent/    # temporary remote config-sync helper
internal/app/          # command parsing, composition, operation coordination
internal/model/        # identities, records, observations, typed lifecycle states
internal/config/       # strict TOML loading and validation
internal/process/      # captured commands, deadlines, cancellation, terminal children
internal/tmux/         # argument builders, formats, snapshots, guarded operations
internal/ssh/          # existing OpenSSH invocation and multiplexing policy
internal/store/        # atomic files, schema handling, per-target/process locks
internal/inventory/    # discovery, cached views, reconciliation, refresh scheduling
internal/session/      # attach/reuse/retry/create/delete/disconnect and supervisor
internal/bindings/     # owned bindings, hooks, installation, restoration
internal/picker/       # shared view model, native tree and fzf adapters
internal/configsync/   # local orchestration and remote transaction implementation
tests/acceptance/      # executable behavior tests against a selected backend
tests/fixtures/        # wire formats, caches, Unicode, tmux snapshots, failure cases
bench/                 # startup, UI, discovery, cancellation, resource measurements
scripts/               # explicit build, release, and installation entry points
```

Engineering rules:

- Use standard-library CLI parsing, `context`, `os/exec`, `encoding/json`, `io`, and `log/slog` where sufficient. Pin the Go toolchain and dependencies.
- Use a maintained TOML decoder, Unix primitives where needed, and a Unicode segmentation/width implementation if the standard library cannot preserve current behavior. Document each dependency's purpose. Avoid custom replacements merely to claim zero dependencies.
- Prefer concrete structs. Introduce small interfaces at actual process, transport, filesystem, or clock boundaries used by tests; no interface per struct or provider framework.
- Use typed session/host/window identities and operation states. Presentation labels and colors never drive lifecycle decisions.
- One action implementation must serve CLI, fzf, and native invocation where applicable. A new F2 action adds orchestration and its descriptor, not duplicate session logic.
- Keep one policy for quoting remote shell arguments and one policy for tmux-format escaping. They are different languages and must not share an unsafe generic escape function.
- Keep expected failures typed: unavailable, authentication failure, stale identity, cancelled, conflict, invalid config, and uncertain remote outcome. Preserve the original cause for diagnostics.
- Pass context to operations; explicitly join workers. Do not mutate process-wide environment variables to hold per-client state.
- Capture the invoking tmux socket/server generation, client name/TTY and PID, and navigation token together. Revalidate them before committing a switch so a reused TTY cannot inherit an old request.
- Do not hold filesystem/creation locks across SSH waits. Document lock order and ownership handoff.
- Keep terminal bytes separate from structured responses and logs. No scanner with a line-size limit for terminal output; bounded handshake followed by byte streaming.
- Opt-in timing diagnostics go to stderr or a private bounded file. No pane contents, credentials, full environment, or shared-config payloads in logs.
- Document how to add a picker action, a record field, a key alias, and a config field, including the required compatibility checks.

## 4. Performance contract

Numbers below are proposed release targets, not accomplished results. Stage 0 establishes an interleaved Python/Go measurement procedure and records the reference machine and workload. A missed target requires profiling and a documented decision; do not relax it silently or remove correctness checks to meet it.

November 9 scope decision: the user deferred large native mirroring. Automatic mirrors now stop above 100 remote windows per host; session entries and real remote counts remain available. A 25 ms normal native-opening exception is proposed for review; the original 20 ms benchmark gate remains visible. Local selection and every other timing/safety budget retain their existing targets. See [the review and evidence](GO_MIGRATION_PROGRESS.md#native-release-scope-and-timing-review).

Recent baseline measurements on this host: Python-only startup ~22 ms median; installed `--version` ~98 ms; `picker-select` ~76 ms, each over 30 warm runs. Native `/` search became visible at ~200 ms median over six isolated trials. Active Python supervisors showed roughly 21–24 MiB RSS each. These samples are context, not controlled release benchmarks.

| Scenario | Initial target / invariant |
| --- | --- |
| Go command startup without external work | Warm p50 ≤ 5 ms, p95 ≤ 10 ms on the reference host |
| Native tree open and local selection | p95 ≤ 20 ms; no controller process or SSH request on the foreground path |
| Manager and lower `/` search, normal cached dataset | First complete usable frame p50 ≤ 60 ms, p95 ≤ 100 ms |
| Search typing and movement | p95 input-to-frame ≤ 16 ms in the normal dataset; no SSH or metadata subprocess per keystroke |
| Manager → local session | p95 ≤ 50 ms after Enter, including cleanup needed to reveal the session |
| Escape/toggle | Visible close p95 ≤ 50 ms; cancelled discovery children reaped within 250 ms |
| Existing healthy remote connection | One fresh batched remote snapshot; ≤ 50 ms added local orchestration, with network/remote time reported separately |
| New remote connection | Progress within 100 ms; connection validation remains exact and cancellable; report SSH/handshake/confirmation phases separately |
| Unreachable or hung host | Same local-open/local-switch budgets; no first-frame network dependency |
| Unchanged refresh | No visible reload and no needless tmux writes |
| Background work | No inventory polling after all pickers close; retain only active supervisors and bounded OpenSSH transport persistence |
| Go supervisor memory | Target ≤ 16 MiB steady RSS per ordinary connection; also report PSS and aggregate private memory |
| Repeated usage | No monotonic FD, child-process, goroutine, or private-memory growth across open/cancel/reconnect cycles |

Benchmark method and workload checks:

- [x] Measure actual PTY output/usable rows, not only process start or popup-border creation (screen reconstruction with pyte).
- [x] Capture p50/p95 for at least 30 warm CLI runs and 50 UI trials; record first invocation separately without calling it a cold filesystem benchmark.
- [x] Interleave backends on the same machine, recording tmux/fzf versions, terminal size, CPU/load, and build flags.
- [ ] Cover empty inventory; normal 20-session inventory; 500 sessions/2,500 cached windows; Unicode/long names; large scrollback.
- [ ] For the stress dataset, target usable cached search within 250 ms and input p95 within 50 ms; report native-tree cost separately because tmux renders it.
- [ ] Cover zero latency, controlled added latency, one slow host among fast hosts, unreachable hosts, and cancelled SSH commands.
- [x] Record process-launch and SSH-round-trip counts as well as elapsed time. Use profiles/traces to locate excess work.
- [ ] Batch tmux reads/writes and publish only changed snapshots; chunk large updates so the tmux server remains responsive.
- [ ] Report startup, local computation, external tmux commands, SSH, and fzf rendering separately. Do not attribute network improvements to Go.

## 5. Staged implementation

Stages are delivery slices, not separate rewrites. Each stage adds observable functionality and its verification. Performance and cancellation are checked throughout; Stage 8 consolidates them rather than introducing them late.

### Stage 0 — Freeze the current contract and build the comparison harness

Deliverables: immutable Python baseline, feature-to-test map, fixtures, benchmark report, and reproducible isolated test environments.

Implementation checklist:

- [x] Record the baseline commit, actual TPM installation path/version, supported local/remote versions, and current configuration schema.
- [x] Snapshot the executable behavior of all 20 CLI command names, flags, exit codes, JSON schemas, and callback invocations. Inventory tmux options and environment overrides too.
- [x] Assign IDs to the parity matrix and identify which of the existing 34 tests are unit, integration, or manual acceptance scenarios.
- [x] Extract external-command acceptance scenarios so they can run against either binary. Existing Python tests that import implementation modules are not automatically Go parity tests.
- [ ] Freeze fixtures for opaque IDs, cache records, connection states, binding ownership, window mirrors, config-sync responses, Unicode, and malformed input.
- [x] Create disposable local tmux and SSH test targets. Keep real user sessions out of automated mutation tests.
- [ ] Establish performance baselines using the method above; store commands and results in the repository.

Check before advancing:

- [x] The baseline suite passes and known failures are explicitly recorded.
- [x] Every matrix row has a planned executable or manual acceptance check; undocumented behavior is resolved from source and observed behavior.
- [x] The harness can detect wrong-client switching, wrong-session targeting, orphaned children, unwanted SSH, and modified user panes.
- [x] Existing configs run unmodified; no migration code has been activated on the workstation.

Exit gate: an executable definition of parity and a trustworthy performance comparison exist.

### Stage 1 — Establish Go foundations and compatible data contracts

Deliverables: buildable CLI, strict configuration, domain types, process/tmux adapters, and compatible storage primitives.

Implementation checklist:

- [x] Pin a stable supported Go toolchain; add module metadata, reproducible build commands, formatting, vetting, race tests, and CI.
- [x] Implement command routing and shared error/exit-code handling. Preserve JSON stdout and put diagnostics on stderr.
- [x] Implement all current configuration fields and defaults: picker width/height/details/icons/hide patterns; discovery cache/refresh/connect/command timeouts/concurrency; host id/label/ssh/enabled/tmux path/prefix/socket selectors.
- [x] Preserve strict types/ranges, missing-config behavior, unknown-key rejection, duplicate host rejection, and path resolution.
- [x] Define typed identity/observation/connection models. Separate observation freshness from connection state.
- [x] Implement bounded command capture, timeouts, cancellation, process cleanup, and separate terminal-child execution rules.
- [x] Implement atomic private state/cache files and interoperable Unix locks, using the current namespace and permissions.
- [x] Preserve relevant environment contracts: config path, SSH config, XDG paths, tmux socket, invoking client, columns, and deterministic executable selection. Treat `TMUX_ROAM_PYTHON` as a legacy-backend option only.

Check before advancing:

- [x] Current configuration fixtures succeed or fail identically, including booleans passed where integers are required and `socket_name`/`socket_path` conflicts.
- [x] Opaque IDs are byte-compatible with Python, including JSON escaping, Unicode/surrogates, URL-safe base64, and omitted padding. Go's default JSON encoding is not assumed equivalent.
- [x] Cache/state records round-trip between implementations. Old readers are tested before introducing additional fields.
- [x] Corrupt/truncated cache data is disposable; ownership information is never inferred from corrupt state.
- [x] Shell metacharacters, tmux format markers, leading dashes, spaces, pipes, and control characters cannot change the intended command or target.
- [x] Cancellation reaps descendants without killing unrelated SSH masters or another connection's supervisor.
- [x] CLI startup meets its budget; ordinary parsing creates no network connections.

Exit gate: the foundation supports real command execution and existing data without changing the host installation.

### Stage 2 — Port inventory, cache, refresh, and reconciliation

Deliverables: compatible `list`, `list --cached`, `refresh`, and inventory diagnostics.

Implementation checklist:

- [x] Port local discovery and folding of owned connection sessions.
- [ ] Build indexed snapshots for identity, carrier ownership, client, and window lookups. Avoid repeated whole-inventory scans and a separate tmux subprocess for every rendered field.
- [x] Port one bounded remote snapshot containing server/session identity, clients, prefixes, and remote windows.
- [x] Use OpenSSH for aliases, Include, ProxyJump, keys/agents, ports, and host-key policy. Retain BatchMode and the current private ControlMaster behavior.
- [x] Preserve ordinary-SSH fallback when a private control-socket path is too long.
- [x] Schedule hosts with bounded concurrency, publish completed hosts independently, and keep cached data after failures.
- [x] Preserve remote-client reconciliation when a user changes sessions inside the remote server.
- [x] Coalesce refresh requests and serialize same-host cache publication. For cross-process coordination, allow cancellation and avoid an unresponsive host becoming a global lock.
- [x] Define freshness using elapsed time carefully; tolerate wall-clock changes and retain the last successful observation age.

Check before advancing:

- [x] Python and Go produce semantically equivalent JSON for local, healthy, empty, offline, disabled, stale, restarted-server, and malformed-response scenarios.
- [x] `list --cached` makes zero SSH calls.
- [x] A failed host does not delay another host's results or erase its own cached rows.
- [x] Large/truncated output, authentication failures, missing tmux, Unicode, and hostile labels remain bounded and actionable.
- [x] Host concurrency and refresh intervals match configuration; close cancels active work and prevents later publication.
- [x] Same-host concurrent readers/writers do not lose newer observations or create partially written records.
- [x] Discovery and cache-hit timings, subprocess counts, and output limits are recorded.

Current refresh evidence: overlapping observations coalesce both within an invocation and across processes through private temporary Unix sockets. Per-host cache publication remains short and cancellable. Actual pickers refresh periodically and stop/join workers on close. Real-SSH fixtures cover Include, ProxyJump and isolated ssh-agent authentication in addition to aliases, host trust, ports, quoted executables and socket targeting. CLI inventory and reconstructed PTY first-frame measurements are reported separately in GO_MIGRATION_PROGRESS.md.

Exit gate: read-only feature parity and failure behavior pass through the CLI.

### Stage 3 — Port connection lifecycle and terminal supervision

Deliverables: exact remote open/reuse, explicit retry, safe cleanup, and a Go supervisor.

Implementation checklist:

- [x] Specify and implement transitions for dormant, preparing/attaching, open, disconnected, and error states. Ending a remote session is distinct from losing network visibility.
- [x] Preserve the identity tuple: host ID, actual remote socket, server PID/start time, session ID, and session creation time.
- [x] Reuse only an ownership-checked carrier whose remote PID/TTY confirms the selected identity; prefer a healthy match over failed duplicates.
- [x] Port per-target creation ownership, follower behavior, short locks, and cancellation/navigation generations.
- [x] Reconnect in the original safe carrier/pane. Confirm ownership again immediately before respawn or removal.
- [x] Keep SSH stdin attached to the terminal; parse only the bounded nonce prelude from stdout, then relay bytes with backpressure.
- [x] Handle resize, terminal signals, EOF, partial writes, SSH failure, and retry prompt without corrupting terminal state.
- [x] Preserve the deliberate shared-view semantics when two local clients attach to the same carrier.

Check before advancing:

- [x] Healthy reopen reuses one carrier and one remote attachment; repeated failed retries preserve the same session/pane.
- [x] Network restoration permits Enter or picker selection to retry the exact session. No unrequested automatic reconnect loop is added.
- [x] A deleted/recreated same-name session and a restarted remote server cannot be mistaken for the original identity.
- [x] Two concurrent opens create at most one owned carrier; cancelling a follower does not tear down the owner's successful connection.
- [x] Cancellation, client disappearance, or a newer navigation request prevents late focus changes.
- [x] Failure cleanup preserves attached, renamed, split, linked, or repurposed resources and all unrelated sessions.
- [x] Sustained output, split escape sequences, OSC 52, UTF-8, Ctrl-c, resize, editor alternate screen, and SSH exit are tested through a PTY.
- [x] Supervisor memory and resource counts stabilize; no terminal traffic enters diagnostic logs (31 samples across 30 reconnects, roughly 8–9 MiB RSS and eight FDs; the long-lived soak remains open).

Exit gate: lifecycle acceptance passes on disposable SSH targets, including outage/recovery loops, before connecting the Go implementation to normal work sessions.

### Stage 4 — Port binding ownership, native navigation, and remote windows

Deliverables: native picker parity and all input routing through Go-generated tmux bindings.

Implementation checklist:

- [x] Port idempotent install/unload, original binding capture, canonical comparison, later-user-edit preservation, extra tables, and namespaced hooks.
- [x] Preserve native foreground opening/local selection with direct tmux commands; start metadata work asynchronously.
- [x] Preserve all interaction-contract aliases and remote prefix variants. Global previous-session switching must work from both ordinary and managed sessions.
- [x] Keep all mouse events routed through tmux's mouse forwarding; do not route them through the Go process.
- [x] Port dormant session entries, ownership decoration, cached states, and safe ended/duplicate cleanup.
- [x] Port remote window mirrors, exact child-window targeting, reserved carrier index handling, and safe removal before attachment.
- [x] Batch repeated option reads/writes and skip identical updates; retain selection, expansion, scroll position, and current-session markers.
- [x] Keep the configured native search capability fallback and plain-tree alternative.

Check before advancing:

- [x] English/Russian pickers and previous-session toggle work from local, `off-prefix`, and all managed prefix tables.
- [x] Double-prefix behavior and F12 escape work with `M-a`, `C-b`, and `None` without changing remote configuration.
- [x] Repeated install/unload and changing configured keys restore originals without leaking obsolete bindings or overwriting later user edits.
- [x] Native local selection still works instantly when the Go executable or SSH discovery is intentionally stalled.
- [x] Remote child rows remain useful during outages; selecting a deleted/moved window fails safely.
- [x] Attached or user-modified windows/panes are never removed as metadata mirrors.
- [x] Double-click, drag, wheel, pane/app clicks, remote vi copy mode, and OSC 52 pass in isolated tests and on disposable real-host servers.
- [ ] Native timing budgets hold while large background updates are being published.

Exit gate: `Alt+a s` and global routing are fully usable with Go on an isolated server.

### Stage 5 — Port the manager and lower fuzzy-search panel

Deliverables: fzf behavior parity, shared view models, and deterministic popup lifecycle.

Implementation checklist:

- [x] Build cached rows once and launch fzf directly; avoid a second controller startup inside the popup.
- [x] Preserve opaque IDs, NUL framing, identity tracking, stable ordering, query text, and selection through refresh.
- [x] Share formatting/state mapping across native and fzf views while retaining each view's distinct interaction model.
- [x] Preserve search-first manager, Escape-to-close, Ctrl-o navigation, nested-menu behavior, and lower-search Escape-to-tree behavior.
- [x] Port filter/hidden/details/help/refresh controls and literal Latin/Cyrillic search input.
- [x] Keep full-path fuzzy matching such as `DD/git`; do not let metadata or transient status dominate project matching.
- [x] Preserve terminal-cell alignment, emoji/combining/ZWJ text, adaptive columns, side padding, current `*`, and an unambiguous selected row.
- [x] Ignore behavioral `FZF_DEFAULT_*` overrides consistently and retain too-small-terminal fallback.
- [x] Scope workers, snapshots, and Unix sockets to the invocation. Add no persistent controller just to reduce launch overhead.

Check before advancing:

- [x] PTY tests cover empty/matching/no-match searches; immediate Escape; both toggle aliases; nested menus; and form cancellation.
- [x] `/` appears in the preview area with the tree retained above it; return preserves exact selection and expansion.
- [x] Typed `j/k`, `dd`, Cyrillic equivalents, `/`, and `.` remain literal while searching.
- [x] Background updates neither erase the query nor silently select a different identity when the chosen row ends.
- [ ] Small terminals, resize while open, many rows, and Unicode screenshots remain readable and aligned.
- [ ] First usable frame, typing, local selection, Escape, and worker cleanup meet the specified budgets.

Exit gate: both picker paths pass their acceptance scenarios and can be compared visually with the Python baseline.

### Stage 6 — Complete project/session actions

Deliverables: creation, deletion, refresh-host, disconnect, and diagnostics from CLI and picker.

Implementation checklist:

- [x] Port host selection, session naming rules, remote directory validation/expansion, explicit creation, and opening the returned exact identity.
- [x] Port deletion confirmation, `dd`/`вв`, and F2 deletion for both local and remote sessions.
- [x] Perform destructive identity validation and mutation in the same remote/local tmux command queue.
- [x] Update both pickers after successful creation/deletion and safely prune associated ended local carriers.
- [x] Preserve close-local-connection as a distinct action, including protection for carriers still used by clients.
- [x] Port all F2 actions, context-sensitive preferred host, help, and diagnostics; keep one underlying operation per action.

Check before advancing:

- [x] Create two independent projects on one remote host and switch between them normally.
- [x] Existing names, missing directories, leading dashes, shell metacharacters, invalid names, and cancelled forms cannot overwrite unrelated work.
- [x] A lost creation/deletion response is reported as an uncertain outcome; refresh before retrying rather than claiming success or issuing a blind second mutation.
- [x] Cancel remains the initial destructive-confirmation choice; opening a menu or searching never creates/deletes a session.
- [x] `dd` requires two consecutive navigation-mode presses; unrelated input breaks it.
- [x] Successful deletion disappears from the native picker as well as the manager; an offline host never supplies deletion evidence.
- [x] Closing an owned local connection leaves its remote session alive under the remote server's own lifecycle policy.
- [x] The popup toggle works throughout menus, confirmations, and input fields on both layouts.

Exit gate: every current action except config sync has backend and interaction parity.

### Stage 7 — Replace remote Python configuration sync

Deliverables: Go config-sync transaction, temporary remote helper, CLI/UI preview flow, and fully Python-free sync.

Implementation checklist:

- [x] Port `config diff/sync <host>`, `--all`, `--source`, existing exit codes, enabled-host filtering, and continued processing after one host fails.
- [x] Share transaction logic between a small `tmux-roam-agent` entry point and the local coordinator; do not create a general-purpose remote agent API.
- [x] Version the helper request/response protocol and reject incompatible versions before mutation. Preserve the 1 MiB config limit; separately bound the packaged helper/archive size and JSON response rather than applying the config limit to executable bytes.
- [x] Build static helper artifacts for supported remote architectures. Ship them with the release bundle so syncing does not require the remote host to download from the internet.
- [x] Detect remote OS/architecture only for explicit sync/diff. Reject unsupported targets before changing `.tmux.conf`.
- [x] Transfer a bounded archive containing only the trusted helper and JSON request through OpenSSH. Use fixed member names, a private staging directory, a shell cleanup trap, and a JSON-only helper response. Document the added standard `sh`/`tar` bootstrap requirements.
- [x] Keep the helper/request temporary. Handle disconnects with best-effort cleanup and narrowly scoped stale-staging cleanup; no permanent agent or periodic service.
- [x] Preserve destination symlinks, file permissions, same-filesystem atomic replacement, backups, locking, and preview/content digest checks before commit.
- [x] Validate using the destination's tmux executable on an isolated server; reload only the configured live socket, or save for its next start.
- [x] Bound and clean up the temporary validation server on success, failure, timeout, and cancellation; its private socket must never be confused with the live destination socket.
- [x] Restore the previous file after reload failure and report whether its reload succeeded. Continue to explain that arbitrary effects of sourced shell commands cannot be rolled back.
- [x] Preserve F2 host/all-host selection, diff scrolling, default Cancel, and stale-preview refusal.

Check before advancing:

- [x] Diff and sync work when Python and Go are absent from the destination PATH, including the real DevDocker compatibility target.
- [x] Matching files skip writes/backups; changed files preserve mode and symlinks and create an accurate backup.
- [x] Missing config, stopped server, invalid syntax, runtime reload failure, concurrent edits, and concurrent sync attempts have explicit tested results.
- [x] Truncated transfer/response, noexec staging, insufficient disk space, permissions, timeout, and disconnect do not produce a partial installed config or false success.
- [x] Preview-to-apply changes are rejected; symlink/target changes during the transaction are checked before replacement.
- [x] Only `.tmux.conf`, its expected backup/lock state, and temporary helper files are affected. Do not distribute plugin config, scripts, or plugins implicitly.
- [x] Repeated calls leave no running helpers or accumulating staging directories; unsupported platforms fail before config mutation.
- [x] Full sync UX is tested over SSH against disposable targets with existing sessions preserved (generated F2 host/all-host previews, diff scrolling/default Cancel, stale-preview refusal and successful apply; physical acceptance remains separate).

Exit gate: no runtime feature requires Python. The legacy helper can remain only in the rollback implementation.

### Stage 8 — Consolidate UX improvements and performance hardening

Deliverables: measured interaction improvements, profiles, performance regressions in CI, and a stable visual specification.

Implement improvements that fit the established views:

- [x] Use one semantic status model/color map across views: current, connected, ready, checking, connecting, reconnect, unavailable, ended. Host accent colors never imply connection health.
- [ ] Present `Host/project` paths consistently; use metadata and observation age without crowding the project name.
- [ ] Keep cached lists visible during refresh and expose a lightweight per-host refresh indicator without flashing or repeated empty frames.
- [x] Make long validation/connection steps visibly cancellable. Retain a clear route back to the selected entry and do not move the invoking client until commit succeeds.
- [x] Preserve the user's query and selection on recoverable failures; show short actionable errors with technical details available separately.
- [x] Keep confirmations explicit about host/project and local disconnect versus remote deletion. Ensure focus and Cancel defaults are obvious.
- [x] Keep plain backgrounds, restrained text accents, compact rows, and padding around header/footer; retain the bottom preview/search design.
- [x] Ensure no-match states provide useful refresh/create actions without reinterpreting Enter as creation.

Implementation constraints:

- Optimize measured excess tmux calls, redundant encoding, refresh churn, and controller launches before adding IPC.
- If cancellable inline operations need communication with the already-running picker controller, use a private per-invocation Unix socket and a minimal typed request protocol. Add it only when the simpler command model fails the interaction/performance gate. It must end with the picker.
- Any deliberate UX change gets before/after scenarios and a documented replacement for every affected control. Do not replace fzf/native views with a new TUI framework during parity work.

Check before advancing:

- [ ] All normal and stress performance cases pass, with reports reproducible from checked-in commands.
- [ ] Healthy local navigation remains fast while every remote host is offline or hung (November 9: typing, selection, Escape and cancellation pass; normal fuzzy opening passes in the lower-load repeat but earlier cases miss, and native opening with 100 hydrated mirrors still misses the 20 ms p95 target).
- [ ] Run at least 100 open/search/cancel cycles and 30 reconnect cycles; inspect memory, FDs, goroutines, processes, state files, and tmux hooks.
- [ ] Profiles identify no unexplained blocking operation on the first-frame or typing path.
- [x] Race detector, parser/identity/quoting fuzz tests, and cancellation tests pass; success does not replace PTY/SSH acceptance.
- [ ] Compare compact and wide layouts, Unicode, disabled icons, both keyboard layouts, and physical touchpad/clipboard behavior.
- [ ] Record manual acceptance for each changed interaction. A capability-shaped setting such as `icons` must have a visible, tested effect rather than merely parse successfully.

Exit gate: parity plus the accepted UX changes meet their responsiveness and readability contracts.

### Stage 9 — Ship binaries, TPM installation, updates, and maintenance tools

Deliverables: versioned release bundles, a small stable launcher, reliable installation/update, and user/developer documentation.

Implementation checklist:

- [ ] Cross-build controller and helper artifacts with CGO disabled; inspect linkage and test on the oldest supported runtime targets.
- [ ] Publish controller bundles for local AMD64/ARM64, containing the supported remote helper artifacts and a manifest with version/build identity and checksums.
- [x] Pin expected release identity in the source checkout. The launcher never silently runs an unrelated executable found earlier in PATH.
- [x] Install immutable release directories and atomically activate a verified bundle. Controller and helper versions must stay paired.
- [x] Keep the `.tmux` TPM entry point and explicit source-build/manual-install path. End users do not need a compiler.
- [x] Keep downloads/builds off ordinary shell startup and picker paths. A first install may fetch in a bounded background task and integrate only after completion; failure preserves the existing chooser. Existing installations keep the last working bundle until an update succeeds.
- [x] Document development-checkout behavior when no published release matches it: explicit local build, not an invisible background compiler invocation.
- [x] Keep a previous verified release for rollback; protect updates with an installation lock.
- [x] Extend `doctor` with binary provenance, capabilities, configured paths, host-specific errors, and optional timing output. Distinguish offline hosts from missing local dependencies.
- [x] Document installation, TPM updates, disable/unload, rollback, supported platform versions, config-sync helper behavior, and adding a feature to the Go codebase.
- [x] Require local tmux 3.8+ before remote opens/native metadata. Actual local 3.6a reconnects crash in upstream pane-offset handling; the 3.8 fix is absent from inspected 3.7 source. Verify safe refusal with a newer client against the older server, preserve local switching, and retain remote 3.6/3.6a support. See the progress report's compatibility evidence.

Check before advancing:

- [ ] Clean-machine TPM and manual installs work without Python or Go; native/fzf dependencies are checked with actionable errors.
- [x] Install/update works from arbitrary working directories and polluted PATHs; project files cannot shadow the runtime.
- [x] Offline, interrupted, corrupt, mismatched-version, unsupported-architecture, and concurrent updates preserve the last usable installation (offline bundle/bootstrap acceptance).
- [x] Release checksums/build identity are verified before activation; the release trust/checksum source is documented.
- [x] Updating/unloading never kills a live carrier, resets another client's navigation, or overwrites later user bindings.
- [x] Repeated tmux config reloads do not download, rebuild, duplicate hooks, or accumulate processes.
- [x] A reviewer can follow one complete feature from CLI through domain operation to adapter and acceptance test.

Exit gate: a release candidate can be installed, updated, disabled, and rolled back by a normal TPM user.

### Stage 10 — Coexistence, real-host canary, cutover, and legacy retirement

Deliverables: staged activation, verified real-host behavior, rollback evidence, and eventual removal of Python from the active implementation.

Implementation checklist:

- [x] Test Python→Go→Python against compatible caches, state files, binding ownership, and existing carriers before changing the workstation default.
- [ ] Keep immutable baseline artifacts. During migration retain any Python source paths still needed by running legacy supervisors; changing a launcher does not migrate an already-running process.
- [x] Use an explicit backend selection or isolated launcher for comparison; do not let both backends refresh/mutate the same resources as independent owners.
- [x] Run an isolated workstation client and disposable Omni/DevDocker targets first, including config sync on disposable homes/sockets (final-binary repeats pass on both hosts at the default fixture timeout, with existing default-server session identities preserved).
- [ ] Activate Go on the workstation only after prior stage gates pass, with an immediate rollback command and known prior bindings/artifact.
- [ ] Observe an agreed soak period, initially two working days including a genuine network interruption/recovery and a suspend/resume cycle when available.
- [ ] Retire legacy runtime source only after compatibility tests pass and no running supervisor still depends on it. Do not force-close sessions to reach this condition.
- [ ] Publish the final feature/compatibility matrix, benchmark report, known limits, release notes, and maintenance guide.

Check before declaring migration complete:

- [ ] Current local sessions and all managed connections remain usable after activation and rollback; client/window/pane IDs and running processes are checked.
- [ ] All native and manager controls, project actions, remote windows, mouse, clipboard, terminal apps, and global shortcuts pass on the real host pair.
- [ ] Network loss retains useful entries; reconnect reuses safe carriers; confirmed ended sessions are cleaned without harming modified resources.
- [ ] Config diff/sync, symlink preservation, backups, and stopped/running destination behavior pass with the shipped helpers.
- [ ] Physical Latin/Russian keyboard and touchpad acceptance is recorded separately from generated PTY input.
- [ ] The new runtime and installation work with Python removed from the test environment; developer-only test tooling is clearly distinguished.
- [ ] No unexplained parity, performance, cancellation, or ownership failures remain. Every exception has a documented equivalent behavior and acceptance evidence.
- [x] Rollback was actually exercised, not only documented (isolated release and backend coexistence acceptance; production activation remains pending).

Exit gate: the Go release is the supported default with complete current functionality and demonstrated performance/maintenance benefits.

## 6. Test and evidence strategy

Use the smallest test that validates the behavior, then exercise the real external boundary where it matters. Avoid tests that only repeat a command-builder implementation.

| Layer | Required evidence |
| --- | --- |
| Pure Go tests | Config validation, identity encoding, parsing, lifecycle decisions, Unicode formatting, errors, cache schema |
| Fuzz tests | Bounded inventory parsing, malformed identities, Unicode/escaping, state decoding; no panics or unbounded allocation |
| Controlled process tests | Deadlines, cancellation, descendant cleanup, stdout/stderr bounds, terminal handshake/streaming |
| Black-box CLI | Same fixtures and external operations against Python and Go; intentional differences explicitly recorded |
| Isolated tmux + PTY | Real key tables, popups, fzf, Escape/toggle, mouse sequences, copy mode, OSC 52, multiple clients |
| Disposable SSH targets | Auth/ProxyJump/aliases, exact attachment, outage recovery, remote restart, create/delete, config-sync transactions |
| Performance | Actual visible-frame and action timing, external-call counts, resource stability, normal/stress/offline cases |
| Manual acceptance | Physical keyboard layouts, touchpad gestures, perceived flicker, actual terminal clipboard, readability |

Each stage records: baseline commit; Go commit/build; commands/environment; passed and failed scenario IDs; performance changes; unresolved issues; rollback status. Checklist boxes represent verified results, not just merged implementation.

The current Python tests can remain as development-only baseline tooling while external acceptance cases are extracted. New Go logic receives Go tests. Rewriting a useful external harness solely to remove Python from development is not a runtime migration requirement.

## 7. Scope boundaries and decisions that must stay explicit

- A controller rewrite does not remove SSH round trips. Never skip identity validation to manufacture a latency improvement.
- Remote creation/deletion and config sync have uncertain outcomes when a response is lost. Report uncertainty and re-observe before retrying mutations.
- The remote client attached through a shared carrier has a shared view; independent per-local-client remote windows would be a separate feature.
- A remote host/server restart may destroy its sessions. The plugin must not silently recreate them.
- Config reload rollback can restore files and attempt a reload, but cannot undo arbitrary commands already executed by tmux configuration.
- Keep interactive password authentication, continuous remote previews, automatic reconnect loops, persistent inventory services, and non-Linux ports outside this migration unless separately specified and tested.
- UX improvements may simplify an interaction, but no current feature disappears under the label of redesign.

## 8. Execution order and review checkpoints

```text
0 baseline and harness
  → 1 foundations
  → 2 inventory
  → 3 connection lifecycle
  → 4 bindings/native/windows
  → 5 fzf views
  → 6 project actions
  → 7 Python-free config sync
  → 8 UX/performance acceptance
  → 9 release/installation acceptance
  → 10 canary/cutover/retirement
```

Release scaffolding may begin after Stage 1, and remote-helper work may begin after its process/storage prerequisites exist. Neither changes the parity gates. Keep changes reviewable by capability; avoid one final giant translation commit.

The first review checkpoint is after Stages 0–2: prove fast startup, compatible configuration/data, and correct discovery before porting the more delicate lifecycle. The second is after Stage 5: a real native/fzf navigation workflow must be faster and correct. The release checkpoint is after Stages 6–9: all features, shipped artifacts, and performance gates must pass before normal-host cutover.
