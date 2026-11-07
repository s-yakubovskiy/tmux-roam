# Go migration checkpoint

Date: 2026-11-07. Baseline: `5fbfc123a2e99b604aeb3e51ac3e7a81f76386c3`; plan commit: `939117c616f6092e5548f18dc8ec3ebb385b8b7f`. The installed `bin/tmux-roam` still runs Python. The Go binary is an isolated development artifact at `build/tmux-roam-go`.

## Environment and baseline

- Workstation: Linux AMD64, Go 1.27.2, tmux 3.7c, fzf 0.74.4. The expected TPM clone at `~/.tmux/plugins/tmux-roam` is absent; the actual installation path remains unverified.
- The current config enables one host (`omni`). A read-only SSH version probe reached it but authentication was denied (exit 255); its tmux/kernel versions could not be captured. No DevDocker entry is enabled in this environment.
- Existing Python suite: `python3 -m unittest discover -s tests -v` ran 34 tests: 29 passed, 4 failed, 1 errored. The error is `tree-mode-selection-style` unsupported by tmux 3.7c. The failures are native tree/input scenarios (`test_managed_prefix_keys_reach_nested_tmux`, `test_native_fuzzy_filtering_local_remote_cancel_and_tree_fallback`, `test_native_local_selection_does_not_wait_for_python`, and `test_native_slash_fzf_search_selection_escape_and_toggle`). These are baseline failures and have not been attributed to Go.
- The current config schema is version 1 with picker, discovery, and explicit hosts. `TMUX_ROAM_CONFIG`, `TMUX_ROAM_SSH_CONFIG`, `TMUX`, and XDG paths are the relevant runtime overrides for this slice.
- Interleaved `--version` startup, 30 warm runs each on this host: Go p50 2.54 ms, p95 7.93 ms; Python p50 130.73 ms, p95 252.81 ms. This measures process startup and CLI output, not visible picker latency. Reproduce with `python3 bench/startup.py` after building.
- Interleaved `config check` with `examples/config.toml`, 30 warm runs each: Go p50 2.73 ms, p95 3.06 ms; Python p50 104.81 ms, p95 108.12 ms. This also excludes tmux/SSH/UI work.

## Stage checks

| Stage | Implemented and verified here | Remaining gate |
| --- | --- | --- |
| 0 Baseline | Baseline suite and local tool versions recorded; parity IDs mapped to existing tests and next checks; all 20 Python command help/flags and `--version` captured; disposable tmux/SSH-shim inventory comparison added; startup benchmark script added. | CLI callback invocation and JSON/exit-code snapshot beyond help, remote target versions, PTY and UI performance harness, known baseline failures resolved or waived. |
| 1 Foundations | Go module pinned to 1.27.2 and `go-toml/v2` 2.2.4; strict config/defaults; byte-compatible opaque IDs; bounded cancellable process capture; read-only CLI routing; private atomic cache writes; CI check defined. | Complete command routing, all lifecycle/state contracts, cancellation tests for every external boundary, CI run and full compatibility/security corpus. |
| 2 Inventory | Local and remote snapshots, Python-compatible cache format, bounded host concurrency, offline cached fallback, remote-client reconciliation, `list` and `refresh` CLI. Cross-backend live/cache comparison and an isolated reconciliation test pass. | Cancellation of picker-scoped work, same-host refresh coalescing, real sshd/host acceptance, stress/performance cases. |
| 3–10 | No Go implementation or activation. | Full lifecycle, bindings, pickers, actions, Python-free config sync, release, canary, rollback and manual acceptance. |

The Go CLI supports `--version`, `config check`, `doctor`, `list [--json] [--cached]`, and `refresh [host-id]`. Unsupported commands fail with exit 2. Use `sh scripts/build-go.sh` to build and `sh scripts/check-go.sh` to run the Go checks. TOML decoding uses `go-toml/v2` for strict type-preserving parsing; all process, cache, and inventory code uses the standard library.

`tests/fixtures/python_cli_help.json` records `--help` output and exit codes for all 20 Python commands and `--version`; regenerate with `python3 bench/snapshot_cli.py` only when intentionally updating the baseline contract.

## Current verification

- `go test ./...`, `go vet ./...`, and `go test -race ./...` pass.
- `tests/acceptance/compare_config.py` passes 12 valid/invalid configuration cases through both public CLIs.
- `tests/acceptance/compare_inventory.py` passes for live local/remote rows with Unicode and a pipe in names, `doctor` remote reporting, Python-to-Go and Go-to-Python caches, zero SSH calls for cached list, isolation of one failed host, offline fallback, and corrupt-cache recovery.
- A one-off local disposable `sshd` run with key authentication and an OpenSSH alias returned matching live remote rows from Go and Python. This is not yet a checked-in, repeatable real-SSH acceptance scenario.
- Static Linux AMD64 and ARM64 binaries compile. Minimum kernel/tmux support and release packaging remain unverified.
- No active user tmux sessions or real remote hosts were mutated; comparison targets were disposable tmux servers and an SSH command shim.

## Parity IDs and next evidence

These IDs follow the parity matrix in `GO_MIGRATION_PLAN.md`. Existing tests listed here exercise Python. Every Go check still requires a backend-independent acceptance run unless marked as already compared.

| ID | Area | Existing Python evidence / next Go acceptance |
| --- | --- | --- |
| P01 | Configuration | `ConfigContracts`; 12 Go/Python CLI cases pass; expand to full fixture corpus. |
| P02 | Host targeting | Socket and SSH scenarios in isolated tests; run aliases, `-L` and `-S` against real sshd. |
| P03 | Public CLI | `ConfigSyncContracts` partly; snapshot all command help, flags, JSON and exit codes. |
| P04 | Installed callbacks | `LauncherContracts` partly; test every installed callback after Go binding port. |
| P05 | Local inventory | `test_inventory_handles_punctuation_and_ignores_owned_proxy`; Go/Python isolated comparison passes. |
| P06 | Remote inventory | `test_remote_window_discovery_selection_cache_and_mirror_cleanup`; Go/Python shim comparison passes; real sshd remains. |
| P07 | Cache | Remote window test; Go/Python read/write, corrupt-cache and zero-SSH checks pass. |
| P08 | Refresh | `test_cancelling_refresh_stops_its_command_promptly`; verify per-host Go publication and cancellation. |
| P09 | Outages | Duplicate and ended cleanup tests; offline cached fallback passes, reconnect remains. |
| P10 | Connection reuse | `test_reuse_uses_one_snapshot_and_rejects_changed_identity`; port exact attachment check. |
| P11 | Reconnect | `test_repeated_failed_reconnect_and_cancel_reuse_the_original_pane`; port supervisor and PTY test. |
| P12 | Navigation ownership | `test_new_picker_supersedes_older_navigation`; add two-client cancellation test. |
| P13 | Cleanup | Ended/duplicate cleanup tests; add modified-pane and attached-client Go tests. |
| P14 | Global shortcuts | Managed prefix test; add local/remote/off-prefix keyboard run. |
| P15 | Keyboard layouts | Vi/fzf and popup tests partly; add physical Latin/Russian acceptance. |
| P16 | Nested input | Managed prefix test; add PTY mouse, copy mode and application input. |
| P17 | Clipboard | No full automated current-suite check; add OSC 52 PTY and physical terminal acceptance. |
| P18 | Native picker | Native compact/selection/cleanup tests; port with tmux 3.7c/3.8 capability cases. |
| P19 | Remote windows | Window mirror tests; port exact selection and safe removal. |
| P20 | Bottom fuzzy search | Native slash test; port Escape/selection/toggle PTY checks. |
| P21 | Manager picker | Fzf vi and popup tests; port search-first and nested-menu PTY checks. |
| P22 | Search/presentation | Layout test; compare Unicode, narrow/wide and no-match frames. |
| P23 | Picker controls | Popup and native tests partly; exercise every key and menu in PTY. |
| P24 | Create projects | Explicit creation test; port duplicate/directory/uncertain-outcome checks. |
| P25 | Delete sessions | Exact deletion tests; port confirmation and stale-identity guards. |
| P26 | Disconnect | Reconnect/cleanup tests partly; port explicit close without remote deletion. |
| P27 | Shared config | `ConfigSyncContracts`; run Go helper diff/sync against disposable SSH targets. |
| P28 | Config activation | Config sync rollback tests; port live reload, stopped server and rollback. |
| P29 | Distribution | Launcher/binding tests partly; verify build, TPM update/unload and rollback. |

This checkpoint is not a production Go cutover. Continue with the Stage 2 exit gate, then lifecycle and UI stages in order. The Python launcher remains the rollback and production path until the Stage 10 gate.
