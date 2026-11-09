# Go migration progress

Updated 2026-11-09, branch `feature/golang`. Baseline: `5fbfc123a2e99b604aeb3e51ac3e7a81f76386c3`; plan commit: `939117c616f6092e5548f18dc8ec3ebb385b8b7f`.

The Go controller, UI, connection supervisor, config-sync helper and offline release installation are implemented. This is an **unpublished release candidate, not a completed production migration**. The development checkout defaults to Go. The installed TPM clone at `~/.tmux/plugins/tmux-roam` remains on Python and was not changed or reloaded. Its HEAD is `5484dca` with extensive local modifications; the commit alone does not describe the deployed files.

## Implementation and evidence

| Stage | Current result | Remaining gate |
| --- | --- | --- |
| 0 Baseline/harness | 34 Python baseline tests pass; all 20 command helps plus version frozen; isolated tmux, real loopback SSH and PTY harnesses; current host versions recorded. | Complete visual/manual matrix and minimum-runtime matrix. |
| 1 Foundations | Strict TOML/defaults, opaque identities, bounded process capture/cancellation, private atomic files/flock, complete CLI routing and deterministic callbacks. Go tests, vet and race detector pass. | Hosted CI has not been run; minimum kernel is unverified. |
| 2 Inventory | Local batched snapshot; remote sessions/windows/clients/prefix/server generation; compatible caches; offline fallback; per-host refresh; invocation and cross-process coalescing, cancellation and publication ordering. | Broad shared indexing remains an optimization; controlled remote-command delays are measured, while actual WAN latency is unmeasured. |
| 3 Lifecycle | Exact attach/reuse, two-client isolation, same-pane reconnect, byte relay and safe opener cleanup. Real SSH covers 30 reconnects, sustained 1.4 MiB output, split UTF-8/escapes, Ctrl-C, live resize and real Vim editing/save/exit. | Physical terminal acceptance, suspend/network recovery and production soak. |
| 4 Bindings/native | Ownership journal, reload/unload, original/later-user binding preservation, Latin/Cyrillic routing, native tree, dormant entries and remote window mirrors. Prefixes, previous session, F12, mouse, copy mode and OSC 52 pass generated terminal checks. Local tmux 3.8 minimum; 3.6a safely refuses remote opens/metadata while preserving local switching. Automatic native mirrors are capped at 100 remote windows per host, with session fallback above that limit. | Physical input/clipboard acceptance. |
| 5 Pickers | Native lower search and manager; literal search, Ctrl-o, layout/nested toggles, Unicode cell widths, icons, ignored fzf defaults, live resize and small-terminal fallback. Immediate first input and typing/movement through delayed refreshes pass; ended identities remain inert. | Opening median and hydrated-native timing gates; physical narrow/wide/Unicode visual acceptance. |
| 6 Actions | Two independent projects switch normally; guarded deletion/default Cancel, dd/вв and disconnect. Lost/empty create/delete replies report uncertainty after one mutation; refresh observes the outcome. Both-view deletion and offline refusal pass. | Physical interaction acceptance. |
| 7 Config sync | Static temporary helpers, bounded protocol, previews/locking, symlinks/modes/backups, validation/reload/rollback. Actual noexec/full staging and destination mounts, transfer interruption, timeout, lost replies, safe stale staging recovery, unsupported platforms and helper cleanup pass. Final-binary canaries pass on both real hosts at the default fixture timeout. | Physical F2 flow and minimum-platform execution. Earlier Omni slow-link failure remains recorded below. |
| 8 Performance/UX | Real PTY/per-key benchmarks, native local timings, cancellation/resource checks and fuzz checks. Supervisor RSS stays below 16 MiB. Healthy reuse uses one fresh SSH request; measured local orchestration p95 is below 50 ms. Large mirrors are deferred with a 100-window/host cap. | Review the proposed native-opening exception and final limited-build timings; physical acceptance. Earlier ambient-load variability remains recorded. |
| 9 Distribution | AMD64/ARM64 controller bundles and paired helpers, manifests/digests, immutable install, atomic activation, deterministic launcher, concurrent install, actual update/rollback. | ARM64 hardware execution, oldest-runtime tests, published release and hosted CI. |
| 10 Canary/cutover | Python→Go→Python coexistence passes, including shared journal/live carrier/same-pane reconnect. The final binary passes disposable canaries on both actual hosts with existing default-server identities preserved. | Production activation, physical checks, two-working-day soak, network/suspend recovery and eventual legacy retirement. |

## Verification commands

```sh
sh scripts/build-go.sh
sh scripts/check-go.sh
sh scripts/fuzz-go.sh
# When sshd is not on PATH:
TMUX_ROAM_TEST_SSHD=/absolute/path/to/sshd sh scripts/check-go.sh
# Optional actual noexec/ENOSPC tmpfs checks, with an existing image:
TMUX_ROAM_TEST_CONTAINER_IMAGE=existing-image sh scripts/check-go.sh
# Optional actual unsupported local-server regression:
TMUX_ROAM_TEST_OLD_TMUX=/path/to/tmux-3.6a sh scripts/check-go.sh
# Explicit isolated canaries on configured hosts:
python3 tests/acceptance/real_hosts.py devdocker
python3 tests/acceptance/real_hosts.py omni
```

`doctor` now reports tmux/fzf capabilities, binary/build and helper paths, verifies installed release manifests, and distinguishes live host failures from missing dependencies. `TMUX_ROAM_TIMING=1` reports dependency, local-inventory and per-host observation timings to stderr without pane contents. Healthy and offline cases are covered by inventory acceptance.

The full check runs `go test`, vet, race tests, builds, CLI/config/inventory parity, real SSH, lifecycle, PTY picker/terminal checks, config sync, release installation, backend coexistence and all 34 Python baseline tests. Python is development test tooling and an explicit rollback backend. New Go operations do not import or invoke it. The complete November 9 release-candidate run passed Go tests/vet/race and every acceptance check, including actual filesystem mounts, startup batching, live resize with shell characters in the query, the F2 diff-scroll/default-Cancel regression, interrupted/mismatched release installers, and all 34 baseline tests (51.223 seconds for the baseline tests). All four configuration/identity/inventory/quoting fuzz targets passed 15-second runs with two workers each.

SSH fixtures cover aliases, explicit trust, keys, isolated ssh-agent authentication, Include, ProxyJump, custom quoted tmux executables, named/absolute sockets, restart identities, long control-path fallback, disabled hosts, authentication refusal and missing tmux. Generated keys and test agents are private to disposable fixtures.

Lifecycle checks verify exact remote window selection, stale/superseded navigation, two concurrent openers, follower cancellation, cleanup of an unused cancelled attempt, and preservation of renamed, split, linked, attached or repurposed resources. Reconnects keep the original pane and leave no prior supervisors or accumulating state files. Native and picker cleanup compare the current navigation generation at the actual tmux mutation.

Connection acceptance measures progress within 21 ms and cancellation/join within 62 ms (20 ms harness polling) while real SSH validation is blocked. It exercises Escape, both Latin/Russian popup toggles, lower-search return to its tree, native remote cancellation/reuse, cancellation after supervisor creation, lease cleanup and later successful retry. A killed fzf callback cannot bypass cleanup: the existing invocation owns the operation through a private temporary Unix socket. Full connection errors remain available under Tab details.

fzf 0.74.5 combined `--track --id-nth` discards input during reload. The picker now retains ordinary tracking and captures/restores opaque identities around reloads. It observes deliberate movement only while a reload is pending; steady-state movement launches no selection callback. Selection actions wait for restoration. PTY regressions cover ten immediate Cyrillic first keys, typing and movement during forced 400 ms reloads, insertion before the selected row, ended/recreated names, and query/identity retention through live resize. Native local selection also works with a stalled or removed controller.

A repeated-use benchmark exposed cancellation between native placeholder creation and ownership publication. Placeholder setup now runs as one bounded local transaction, with a regression test that cancels immediately after creation and verifies the carrier remains owned and safely reusable. New connection carriers also publish ownership in their creation queue. Captured-command cancellation closes inherited output pipes and bounds stdin-copy cleanup even when an SSH multiplex master retains descriptors outside its child process group.

The same boundary is covered for native window mirrors: carrier-index reservation and each mirror's ownership/preview/exit metadata finish within a bounded 200 ms local transaction before honoring cancellation. Regressions cancel during reservation and immediately after mirror creation, then verify complete ownership, safe cleanup and restoration of the carrier's original index. Remote renames update untouched mirrors through a server-side guard while preserving manually renamed windows.

Config sync is exercised over real SSH with Python and Go absent from runtime PATHs. Tests cover previews, unchanged files, symlinks/modes, backups, reload, stopped servers, syntax failure, rollback, concurrent sync and edits during validation. Unknown fields, oversized requests, extra JSON and trailing garbage reject before mutation. Truncated/interrupted transfers, actual validation timeout, permission denial and lost/truncated commit replies preserve the file or report the uncertain committed outcome with a fresh-diff instruction. Unsupported OS/architecture pairs reject before mutation; repeated calls leave no fixture helper processes. The generated F2 flow also covers host/all-host previews, scrolling without moving off Cancel, stale-preview refusal and successful apply while preserving session identities, symlinks and mode. A binding-order bug that made Ctrl-d move onto Sync was caught by this check and fixed.

Optional Docker acceptance uses actual noexec staging, ENOSPC extracting the archive and ENOSPC writing the destination. Containers have no network, a read-only root, no capabilities, the current UID and private tmpfs mounts. No image is pulled or host mount changed. Existing content, mode and symlink survive; stages disappear. Stale-staging recovery removes only private marked directories older than 24 hours whose owner PID is definitively dead. Active/reused PIDs, unmarked/public directories and symlinks are retained.

Offline release acceptance removes Python and Go from runtime PATH, installs from arbitrary working directories, rejects transport/payload corruption, tests deterministic controller resolution, unavailable/truncated archives, version/architecture/protocol mismatches, interrupted installation while waiting for the lock, a second candidate, actual rollback and concurrent installations. This proves local bundle installation; it does not imply a release has been published.

## Actual runtime targets

| Target | Observed version / scope |
| --- | --- |
| Workstation | Linux AMD64, kernel 6.17.0-1033-oem, glibc 2.39; Go 1.27.2 development toolchain; local tmux 3.8; fzf 0.74.5. |
| Omni (`home.omni`) | Linux AMD64, remote `/usr/bin/tmux` 3.6. Private remote HOME/state/TMPDIR/socket: exact attach/reuse/reconnect, original pane, config sync/diff and unload passed. |
| DevDocker (`devdocker`) | Linux AMD64, remote `/usr/local/bin/tmux` 3.6a. Same isolated canary passed. |
| Linux ARM64 | Static controller/helper cross-builds only; not executed on hardware. |

Canaries compare existing default-server session identities before/after and clean up their own resources. The local production TPM clone and real remote `.tmux.conf` files remain untouched.

Local tmux **3.8+** is required. An actual 3.6a local compatibility run crashed its disposable server during repeated same-pane reconnects with control clients. The crash also reproduces with the official 3.6a source built against this workstation's libraries: `input_parse()` receives an underflowed buffer length after respawn retains stale offsets. [Upstream commit 4714f478](https://github.com/tmux/tmux/commit/4714f478fed87ae2c510ecd035aa89c5a90dedf8) resets these offsets in 3.8; inspected 3.7 source lacks the fix. This changes the supported local minimum rather than modifying tmux or the user's production server. The controller checks the running server before remote opens or native metadata mutations; local switching still works. `doctor` reports the actual server separately from the client executable. The regression uses a 3.8 client against a real 3.6a server and verifies refusal before SSH/state publication/carrier creation, unchanged pane identities, preserved client focus and working local switching. Remote tmux 3.6/3.6a remains supported. Source/build/trace provenance and the refusal result are retained in [local compatibility evidence](bench/results/2026-11-09-local-compatibility.json).

November 9 DevDocker sync completed in 0.50 seconds. Omni failed under the default transfer budget, and a separate plain OpenSSH helper upload timed out after 15 seconds; small uploads completed. Its isolated canary subsequently passed with `--command-timeout 30`, taking 32.07 seconds for sync. This sets the existing discovery/validation timeout only in the fixture and enlarges the derived transfer deadline. Production and infrastructure settings were not changed. The slow-link result is not a default-budget pass.

A repeat after startup batching passed on both hosts: Omni sync took 1.77 seconds and DevDocker 0.30 seconds. Both used the explicit 30-second fixture setting; the varying link speed does not erase the earlier default-budget failure.

The final-binary refresh is recorded in [real-host results](bench/results/2026-11-09-real-hosts.json). DevDocker passed again with the default five-second fixture setting; sync took 0.52 seconds. An intermediate combined run stopped at Omni with `Permission denied (publickey)` before creating any fixture, and a direct SSH probe also failed authentication. After the user restored access, `python3 tests/acceptance/real_hosts.py omni` passed with the same default setting; sync took 1.77 seconds. Both final-binary canaries verify exact attach/reuse/reconnect, the original pane, isolated config sync/diff, unload, fixture cleanup and preservation of existing default-server session identities. The earlier slow-link failure is retained as historical evidence; no production setting was changed by these tests.

## Performance evidence

Reproduce after building:

```sh
python3 -m pip install -r bench/requirements.txt  # UI benchmark only, in a development environment
python3 bench/startup.py
python3 bench/inventory.py
TMUX_ROAM_UI_RUNS=50 python3 bench/ui.py
TMUX_ROAM_UI_RUNS=50 TMUX_ROAM_UI_SESSIONS=500 python3 bench/ui.py
TMUX_ROAM_UI_RUNS=50 TMUX_ROAM_UI_SESSIONS=0 python3 bench/ui.py
TMUX_ROAM_UI_RUNS=50 TMUX_ROAM_UI_HYDRATE=1 \
  TMUX_ROAM_UI_SCROLLBACK=20000 python3 bench/ui.py
TMUX_ROAM_UI_RUNS=50 TMUX_ROAM_UI_SESSIONS=500 TMUX_ROAM_UI_HYDRATE=1 \
  TMUX_ROAM_UI_SCROLLBACK=20000 python3 bench/ui.py
python3 bench/remote.py
TMUX_ROAM_RESOURCE_REPORT=bench/results/2026-11-09-supervisor.json \
  python3 tests/acceptance/session_lifecycle.py
```

Build flags: `CGO_ENABLED=0 -trimpath -ldflags='-s -w'`. CLI reports exclude UI; scripted SSH reports exclude real network/remote execution time. PTY reports measure actual rendered rows/prompt/footer, individual typed keys, visible close and completed cancellation; they interleave backends and record first invocation separately. Native navigation uses Go-installed direct tmux bindings for both comparisons. Percentiles use nearest rank. Timings are polling upper bounds.

The development-only pyte/wcwidth screen parser reconstructs incremental terminal redraws. The simulated terminal answers device-attribute, color, size and cursor queries. An earlier 500 ms Escape result was a harness error: tmux 3.8 deliberately increases its key timeout while terminal replies are pending. Fixing those replies, while retaining the workstation's 10 ms `escape-time`, removed that delay. Those preliminary/traced numbers are not release evidence.

Resource report: [supervisor](bench/results/2026-11-09-supervisor.json). Across 31 samples around 30 reconnects, supervisor RSS was 8,608–9,244 KiB, PSS 5,236–5,872 KiB, private memory 1,872–2,508 KiB, and FDs remained eight. Each old supervisor exits; this does not establish a long-lived process soak or measure Go goroutine counts.

Current CLI reports: [startup/config parsing](bench/results/2026-11-09-startup.json) and [inventory](bench/results/2026-11-09-inventory.json). Thirty interleaved warm runs give Go version startup 3.22 / 4.79 ms p50/p95 and config check 3.73 / 5.57 ms. Cached 20-session inventory is 11.70 / 14.15 ms versus Python 128.27 / 155.66 ms, using one local tmux command rather than three and zero SSH calls. The 500-session/2,500-window cached case is 34.58 / 38.66 ms. These are CLI completion measurements.

The current development binary has SHA256 `bc38bc5d8d9b6dc754c74901b2434e1e8cbb502ddf5d1802cb5ca1e51d5d78f1`. After the local-server safety guard and 100-window native mirror limit, its full check passed Go tests/vet/race, every acceptance scenario (including actual unsupported-local-server refusal and filesystem mounts), and all 34 Python baseline tests (53.010 seconds). Both real-host canaries passed again at default timeouts: Omni sync 1.66 seconds, DevDocker 0.32 seconds. The preceding safety-guard-only build `70ac4dc99d9581366b987a964fb628b902519c05cad4ba30e4cc7ed34e731571` also passed the complete suite (39.197 seconds for the baseline tests) and both canaries (1.43/0.15 seconds). The earlier CLI, remote-phase and hydrated-20-session reports below use preceding binary `e606edef8b2dd57dbd55487a373f72625dc3541e88e6ad5a7ecdbc81ba1c5adc`; retain those exact build identities.

The following [normal](bench/results/2026-11-09-ui-normal.json), [cached stress](bench/results/2026-11-09-ui-stress.json), [empty remote](bench/results/2026-11-09-ui-empty-remote.json), and failed large-native setup reports were collected on the immediately preceding candidate, SHA256 `16ed1fe5e088f168afe15938706154ebd7c6f44e3c3d6aba89fb5945d0d13879`. The subsequent changes finish mirror creation/index reservation after cancellation and guard mirror renames in one server queue. Those paths are exercised by the final functional suite and the current hydrated report below. The table retains the earlier candidate's exact measurements.

Each completed UI report has two ordinary local sessions and a deliberately hung remote refresh, 50 interleaved warm trials per backend, a separate first invocation, 900 individual key samples, 100 Escape closes, 50 manager local selections and 50 native local selections. Native window mirrors are disabled in the following three cases.

| Go gate | Normal | Stress | Empty remote | Target / outcome |
| --- | --- | --- | --- | --- |
| Manager opening p50 / p95 | 67.79 / 73.91 ms | 98.74 / 104.65 ms | 59.43 / 66.78 ms | Normal misses the 60 ms median; empty passes. All pass their p95 targets; stress passes 250 ms. |
| Lower search opening p50 / p95 | 62.49 / 69.41 ms | 98.16 / 103.49 ms | 60.68 / 65.72 ms | Normal/empty median misses; p95 passes. Stress passes 250 ms. |
| First-key echo p95 | 12.28 ms | 11.97 ms | 6.60 ms | Sent after complete frame/readiness check; every key accepted. |
| Individual typing p95 | 8.64 ms | 9.05 ms | 7.10 ms | Normal ≤16 ms; stress ≤50 ms: passes. |
| Manager local selection p95 | 32.43 ms | 47.03 ms | 34.15 ms | ≤50 ms: passes. |
| Visible Escape / cancellation joined p95 | 30.69 / 50.71 ms | 30.90 / 52.33 ms | 30.76 / 51.74 ms | ≤50 / ≤250 ms: passes. |
| Native open / local selection p95 | 12.80 / 7.17 ms | 12.81 / 6.66 ms | 13.27 / 7.27 ms | ≤20 ms: passes with asynchronous metadata and mirrors disabled. |
| Server FDs | 10→10 | 10→10 | 10→10 | No scoped hooks or outstanding invocation jobs after settling. |

Input-confirmed opening includes an API readiness check and the first character's echo round trip. It is reported separately as `manager_usable` / `search_usable`: normal p50 78.87 / 71.53 ms and p95 85.97 / 78.96 ms. These measurements retain the original 60/100 ms contract. Go typing has no startup settling delay; only the legacy Python comparison retains an explicitly recorded 200 ms cursor-probe wait.

[Current hydrated normal workload](bench/results/2026-11-09-ui-normal-hydrated-scrollback.json) completed 50 trials with all 20 dormant entries, 100 actual window mirrors and 20,000 scrollback lines. Initial hydration took 7.99 seconds. Manager opening was 67.70 / 76.23 ms; search was 63.97 / 75.70 ms. The opening median gates remain failed. Native opening p50/p95 was 38.48 / 41.70 ms, also missing the 20 ms p95 gate. Native local selection p95 was 9.72 ms. Typing, local selection, visible close, joined cancellation and resource checks pass.

A [second complete hydrated run](bench/results/2026-11-09-ui-normal-hydrated-scrollback-repeat.json) uses the **same binary and unchanged benchmark**, again with 50 interleaved trials, 20 entries, 100 mirrors and 20,000 scrollback lines. Manager opening p50/p95 was 42.23 / 47.44 ms; search was 41.35 / 50.18 ms. Input-confirmed opening was 48.60 / 54.32 ms for the manager and 46.44 / 56.06 ms for search. All fuzzy/input/selection/close/cancellation gates passed; server FDs stayed 10→10. Hydration took 3.89 seconds. Native opening was 18.23 / 23.30 ms, still failing its 20 ms p95 target. Starting one-minute host load was 0.82 versus 3.87 in the earlier run. This is different ambient-load evidence, not a runtime optimization or proof that load alone explains the difference; the earlier failures remain recorded and consistent responsiveness under load is unresolved.

The [500-session hydrated-native setup](bench/results/2026-11-09-ui-hydrated-scrollback.json) **failed on the preceding candidate**: 1,435 of 2,500 mirrors were created in 600.19 seconds, with no completed UI trials. This case has not been repeated after the cancellation/rename fix; the scaling gate remains open. Setup now uses cheap session window-count polling, then verifies complete mirror metadata. An earlier repeated per-window ownership scan added about 66 ms per sample and was discarded.

The [preceding safety-guard build](bench/results/2026-11-09-ui-normal-platform-guard.json) completed the same 50-trial hydrated/scrollback workload: manager opening 41.91/47.70 ms, search 41.41/56.63 ms, input-confirmed manager 48.09/55.20 ms and search 47.63/62.67 ms (p50/p95). Typing p95 was 5.33 ms, local selection 36.39 ms, visible close 40.31 ms and joined cancellation 53.75 ms. All fuzzy/input/cleanup gates pass. Native opening remains above target at 19.24/22.31 ms, with native local selection 6.35 ms p95. Hydration took 3.96 seconds and FDs stayed 10→10. Starting one-minute host load was 0.92. The safety guard adds no foreground tmux subprocess for opening.

A 50-trial interleaved diagnostic compared the native generation counter in a global option against a hidden global environment variable, with 20 entries, 100 mirrors and 20,000 scrollback lines. Native opening p50/p95 was 14.17/17.46 ms versus 14.15/17.24 ms; the difference does not justify a runtime change. The alternative was not adopted. This native-only diagnostic does not replace the complete mixed-backend UI benchmark.

Tmux 3.8 source inspection confirms that private option writes trigger global size/redraw work. Batching subprocesses does not remove that cost. A native-only diagnostic with 20 hydrated entries/100 mirrors measured plain tmux tree opening at 32.34 ms p95; deferring background refresh still measured 40.55 ms and worsened selection. Those diagnostics support a native-rendering/metadata scaling limit and were not adopted as release timing reports or runtime changes.

Startup batches client/navigation/local inventory, overlaps bounded cache reads, skips the first worker's duplicate local snapshot, and launches the popup with an explicit shell. Native metadata comparisons, index reservation, mirror settings and guarded renames use command queues; native and manager status colours share one palette. Diagnostic wrappers located roughly 20–30 ms before fzf launch and 35–45 ms from launch to rendered frame. fzf can queue the initial load action before its start action unbinds it, so guarded restoration can run once at startup. CPU-concurrency and asynchronous-initial-load experiments did not materially improve opening; neither was applied. The targets remain unchanged.

[Remote connection report](bench/results/2026-11-09-remote.json) records 30 healthy-reuse samples and 30 new attachments per controlled 0/25/100 ms remote-command delay, plus 30 new attachments after explicitly closing the fixture's idle SSH transport. Healthy reuse makes one fresh SSH request and its local orchestration p95 is 43.90 / 38.43 / 43.46 ms, passing the 50 ms gate. Every new attachment uses four SSH requests, retaining exact preflight validation and post-attachment confirmation.

| New attachment p50 | Warm transport, 0 ms added | Warm, 25 ms added | Warm, 100 ms added | Cold transport, 0 ms added |
| --- | --- | --- | --- | --- |
| Total confirmed switch | 242.92 ms | 542.01 ms | 1457.34 ms | 480.75 ms |
| Two preflight SSH observations | 84.20 ms | 287.97 ms | 893.26 ms | 320.86 ms |
| Attachment SSH start → first handshake byte | 15.04 ms | 15.86 ms | 16.76 ms | 15.78 ms |
| Handshake byte → confirmed switch | 83.59 ms | 170.08 ms | 482.56 ms | 81.06 ms |
| Confirmation SSH (included in previous row) | 41.64 ms | 144.45 ms | 445.54 ms | 42.21 ms |

The first cold preflight includes actual SSH transport establishment; the later attachment channel shares that transport. The development-only adapter timestamps subprocess boundaries and the first attachment byte without logging terminal contents. SSH durations combine transport and remote execution. Added delay applies to each remote tmux command; actual WAN latency is unmeasured. New-connection progress is independently checked within 100 ms. Generated terminal input does not establish physical terminal acceptance.

## Parity evidence map

| IDs | Evidence |
| --- | --- |
| P01 configuration | `internal/config`, `compare_config.py` (12 public CLI cases). |
| P02 host targeting | `compare_ssh_inventory.py`, `real_hosts.py`. |
| P03–P04 public CLI/callbacks | `cli.py` (21 help/version contracts, 13 invalid invocations), picker/lifecycle/coexistence harnesses. |
| P05–P09 inventory/cache/refresh/outage | `compare_inventory.py`, real SSH parity, `internal/inventory` tests. |
| P10–P13 reuse/reconnect/navigation/cleanup | `session_lifecycle.py`, `internal/session/safety_test.go`, tmux/native stale-navigation tests. |
| P14–P17 global/layout/nested/clipboard | `terminal.py`, `pickers.py`, coexistence; physical acceptance remains pending. |
| P18–P23 native/windows/search/presentation/controls | `pickers.py`, lifecycle window check, mirror safety and Unicode cell-width tests; full visual/resize matrix pending. |
| P24–P26 create/delete/disconnect | PTY forms/deletion plus `internal/session/safety_test.go`; remaining failure matrix noted above. |
| P27–P28 shared config/activation | `config_sync.py`, `internal/configsync/transaction_test.go`, agent request-framing test. |
| P29 distribution | `release.py`, `coexist.py`; publication and production cutover pending. |

## Native release scope and timing review

The [limited build's normal repeat](bench/results/2026-11-09-ui-normal-mirror-limit.json) completed 50 trials but measured manager/search medians of 70.18/64.80 ms and native opening p95 of 42.19 ms. Typing, selection, visible close, cancellation and resource checks still pass. Python opening also roughly doubled against the earlier run. Starting one-minute load was 2.30. A subsequent read-only power probe during the next workload found AC offline, `balance_power` CPU energy preference, and CPU frequencies around 1.6–2.6 GHz. Earlier runs did not record power state, so this is a hypothesis about the variance, not a proven cause. Future UI reports now record power state at both ends. The 25 ms exception remains under review; the current repeat also misses the unchanged fuzzy median gates.


The user chose **"Defer large mirrors and review a timing exception"** after reviewing the safety-fixed build's results. Automatic native window mirroring is now limited to **100 remote windows per host**. Larger hosts retain their session entries and true remote window counts; untouched old mirrors are removed with the existing ownership guards, while modified/viewed/linked/running resources remain protected. `doctor` reports the fallback. The normal 20-entry/100-mirror workload remains supported. The former 500-entry/2,500-mirror workload is deferred from this release.

The [latest unrestricted large setup](bench/results/2026-11-09-ui-large-mirrors-platform-guard.json) timed out at 600.05 seconds with 744 mirrors, before any UI trials. [Diagnostic observations](bench/results/2026-11-09-large-mirror-diagnostic.json) retain the published-state checks and profiler limitations. This report predates the cap; it must not be described as a completed stress UI run. A second native-only format simplification diagnostic showed no useful gain (p95 34.45 versus 33.94 ms), so it was not adopted.

**Proposed exception, pending review:** allow native opening p95 up to **25 ms** for the normal reference workload (20 remote entries, 100 mirrors, 20,000 scrollback lines). The original target is 20 ms; the safety-guard build measured 22.31 ms. Native local selection retains its 20 ms target. Fuzzy opening, typing, local selection, visible close, cancellation and resource gates retain their original limits. Native stress cost is reported separately as specified in the plan. The benchmark continues to report its original 20 ms native gate so the miss stays visible. Earlier ambient-load failures remain part of the evidence.

## Remaining production gates

1. Review the proposed native-opening exception after final limited-build measurements. The preceding build measured 22.31 ms p95 against 20 ms; earlier ambient-load variability remains recorded. Large mirrors are deferred by the user, with a 100-window/host cap and session fallback; full 2,500-mirror creation is outside this release scope. Validate the final 500-session fallback workload without weakening the other input/identity/resource gates. New-attachment SSH/handshake/confirmation phases are reported; actual WAN latency is unmeasured.
2. Finish physical visual/input/clipboard and F2 sync acceptance; run hosted CI and validate ARM64 hardware and minimum platforms. Both final-binary real-host canaries now pass at the default fixture timeout. Automated resize, terminal apps and filesystem/transport config-sync failures pass.
3. The dirty TPM clone has a verified read-only rollback archive; filesystem-enforced immutability remains unproven. Keep legacy Python paths while old supervisors can still depend on them.
4. Record physical Latin/Russian keyboard, touchpad, terminal clipboard, suspend/resume and real network recovery acceptance, followed by the agreed two-working-day soak.
5. Publish verified artifacts/release notes and retire Python only after all gates pass and no running legacy supervisor needs it.

## Preserved rollback and required physical acceptance

The full dirty TPM clone (including Git metadata, ignored/untracked files, modes and symlinks) is preserved at:

`/home/yakubovskiy/.local/state/tmux-roam/migration/20261109-113329-python-rollback/`

`tpm-clone.tar.gz` SHA256: `41264b80ede23f79d23d48693ac9eaa85ec223323a8ead919ea617d3b86fdb32`. Sixty regular files were compared byte-for-byte, with modes and symlink targets checked. Archive/manifest are mode 0400 and the directory is 0500. This is a verified read-only archive, not filesystem-enforced immutability. The original TPM clone remains HEAD `5484dca` with 13 dirty/untracked entries.

Run in a real terminal for physical acceptance:

```sh
python3 tests/acceptance/real_hosts.py --interactive devdocker
python3 tests/acceptance/real_hosts.py --interactive omni
```

Both hosts are available for physical acceptance. If the Omni transfer slows again, the fixture supports `--command-timeout 30` without changing production configuration. These checks need the user's actual terminal, keyboard, touchpad and clipboard; they cannot be completed by another generated-input run.

The private canary installs Latin/Cyrillic native and manager bindings, local shells and a remote shell after its automatic checks. Check native/search/manager opening and Escape, both layout aliases through nested forms, touchpad click/double-click/drag/scroll in remote applications, clipboard in both directions, compact/wide/Unicode rendering, and window resize. Return to `Local/local` and press `Alt+a d` to exit and clean up. Asked about the Omni physical check on November 9, the user reported: "I am not sure, seems good." This is tentative positive feedback; individual scenarios and canary completion are unconfirmed, so the full physical matrix remains open. Genuine network/suspend recovery and the agreed two-working-day soak remain separate gates; automated input cannot establish them.
