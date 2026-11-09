#!/usr/bin/env python3
"""Backend-independent PTY checks for native tree, search, and manager controls."""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import shutil
import select
import socket
import struct
import subprocess
import termios
import threading
import time

from compare_inventory import GO, PYTHON
if os.environ.get("TMUX_ROAM_TEST_BACKEND") == "python":
    GO = PYTHON
from compare_ssh_inventory import ssh_target
from session_lifecycle import wait_for


def main():
    with ssh_target(isolated_home=True) as (root, ssh_config):
        local, remote = root / "local.sock", root / "remote.sock"
        def tmux(sock, *args):
            return subprocess.check_output(["tmux", "-S", str(sock), *args], text=True).strip()
        for sock in (local, remote):
            tmux(sock, "-f", "/dev/null", "new-session", "-d", "-s", "start", "sleep 120")
        pid, fd, reader = None, None, None
        stopped = threading.Event()
        output = bytearray()
        try:
            other = tmux(local, "new-session", "-d", "-P", "-F", "#{session_id}", "-s", "project-pine", "sleep 120")
            config = root / "config.toml"
            config.write_text(f'[[hosts]]\nid="remote"\nlabel="OMNI"\nssh="disposable"\nsocket_path="{remote}"\n')
            local_home = root / "local-home"
            local_home.mkdir()
            source = local_home / ".tmux.conf"
            source.write_text("set -g @sync_menu after\n")
            target = root / "home/dotfiles.conf"
            target.write_text("set -g @sync_menu before\n")
            target.chmod(0o640)
            remote_config = root / "home/.tmux.conf"
            remote_config.symlink_to(target)
            env = {**os.environ, "TMUX": f"{local},0,0", "TMUX_ROAM_CONFIG": str(config), "TMUX_ROAM_SSH_CONFIG": str(ssh_config),
                   "HOME": str(local_home), "XDG_RUNTIME_DIR": str(root / "r"), "XDG_CACHE_HOME": str(root / "cache"), "TERM": "xterm-256color"}
            for key in ("HOME", "XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
                tmux(local, "set-environment", "-g", key, env[key])
            slow_reload, reload_entered = root / "slow-reload", root / "reload-entered"
            shim = root / "commands"
            shim.mkdir()
            real_cat = shutil.which("cat")
            (shim / "cat").write_text(f'''#!/bin/sh
case "$1" in
  */rows) if [ -f "{slow_reload}" ]; then touch "{reload_entered}"; sleep 0.4; fi;;
esac
exec "{real_cat}" "$@"
''')
            (shim / "cat").chmod(0o700)
            tmux(local, "set-environment", "-g", "PATH", str(shim) + ":" + os.environ["PATH"])
            polluted = root / "fzf-defaults"
            polluted.write_text("--filter=must-never-run\n")
            for key, value in (("FZF_DEFAULT_OPTS", "--filter=must-never-run --bind=esc:ignore"),
                               ("FZF_DEFAULT_OPTS_FILE", str(polluted)), ("FZF_DEFAULT_COMMAND", "exit 77")):
                tmux(local, "set-environment", "-g", key, value)
            for name, value in (("prefix", "M-a"), ("@tmux-roam-chooser-keys", "b и"), ("@tmux-roam-native-keys", "s ы S Ы"),
                                ("@tmux-roam-native", "on"), ("@tmux-roam-native-fuzzy-keys", "s ы"), ("@tmux-roam-native-search", "fzf")):
                tmux(local, "set-option", "-g", name, value)
            tmux(local, "bind-key", "-n", "M-ф", "switch-client", "-T", "prefix")
            tmux(local, "set-option", "-gw", "mode-keys", "vi")
            tmux(local, "set-option", "-s", "escape-time", "10")
            subprocess.run([str(GO), "list", "--json"], env=env, capture_output=True, check=True)
            installed = subprocess.run([str(GO), "integrate", "install"], env=env, capture_output=True, text=True)
            assert installed.returncode == 0, installed.stderr
            child_env = dict(env)
            child_env.pop("TMUX")
            pid, fd = pty.fork()
            if pid == 0:
                os.execvpe("tmux", ["tmux", "-S", str(local), "attach-session", "-t", "start"], child_env)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
            def drain():
                pending = b""
                replies = {b'\x1b[c': b'\x1b[?1;2c', b'\x1b[>c': b'\x1b[>0;400;0c',
                           b'\x1b[>q': b'\x1bP>|roam-acceptance(1)\x1b\\', b'\x1b[?2026$p': b'\x1b[?2026;2$y',
                           b'\x1b[?2004$p': b'\x1b[?2004;2$y',
                           b'\x1b]10;?\x1b\\': b'\x1b]10;rgb:caca/d3d3/f5f5\x1b\\',
                           b'\x1b]11;?\x1b\\': b'\x1b]11;rgb:2424/2727/3a3a\x1b\\',
                           b'\x1b[18t': b'\x1b[8;32;110t', b'\x1b[14t': b'\x1b[4;640;880t', b'\x1b[6n': b'\x1b[1;1R'}
                while not stopped.is_set():
                    try:
                        if select.select([fd], [], [], 0.03)[0]:
                            block = os.read(fd, 65536)
                            if not block:
                                return
                            output.extend(block)
                            pending += block
                            for query, reply in replies.items():
                                while query in pending:
                                    pending = pending.replace(query, b"", 1)
                                    if query == b'\x1b[18t':
                                        rows, columns, _, _ = struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8)))
                                        reply = f'\x1b[8;{rows};{columns}t'.encode()
                                    os.write(fd, reply)
                            pending = pending[-32:]
                    except OSError:
                        return
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            wait_for(lambda: tmux(local, "list-clients", "-F", "#{client_name}"))
            client = tmux(local, "list-clients", "-F", "#{client_name}")
            pane = tmux(local, "display-message", "-p", "-t", "start", "#{pane_id}")
            def mode():
                current_pane = tmux(local, "list-clients", "-F", "#{pane_id}")
                return tmux(local, "display-message", "-p", "-t", current_pane, "#{pane_mode}")
            first_seen = {}
            def search_state():
                for path in (root / "r/tmux-roam").glob("picker-*/fzf.sock"):
                    try:
                        with socket.socket(socket.AF_UNIX) as channel:
                            channel.settimeout(0.2)
                            channel.connect(str(path))
                            channel.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                            data = b""
                            while block := channel.recv(65536):
                                data += block
                        state = json.loads(data.split(b"\r\n\r\n", 1)[1])
                        # fzf opens its API socket before finishing terminal init.
                        # Do not type into its initial cursor-position probe.
                        started = first_seen.setdefault(str(path), time.monotonic())
                        if time.monotonic() - started < 0.2:
                            return {}
                        return state
                    except (OSError, ValueError, IndexError):
                        pass
                return {}
            def search_closed():
                return not list((root / "r/tmux-roam").glob("picker-*/metadata.json"))
            os.write(fd, b"\x1bas")
            wait_for(lambda: mode() == "tree-mode")
            wait_for(lambda: "OMNI /" in tmux(local, "list-sessions", "-F", "#{session_name}"))
            token = tmux(local, "show-options", "-gqv", "@tmux-roam-navigation-" + client)
            os.write(fd, b"/")
            wait_for(lambda: "query" in search_state())
            os.write(fd, "ddввjkол".encode())
            wait_for(lambda: search_state().get("query") == "ddввjkол")
            os.write(fd, b"\x1b")
            wait_for(search_closed)
            assert mode() == "tree-mode" and tmux(local, "show-options", "-gqv", "@tmux-roam-navigation-" + client) == token
            os.write(fd, b"/")
            wait_for(lambda: "query" in search_state())
            os.write(fd, b"local/project-pine")
            wait_for(lambda: search_state().get("query") == "local/project-pine" and search_state().get("matchCount") == 1)
            time.sleep(0.15)
            os.write(fd, b"\r")
            wait_for(lambda: dict(line.split("|", 1) for line in tmux(local, "list-clients", "-F", "#{client_name}|#{session_id}").splitlines()).get(client) == other)
            wait_for(search_closed)
            # Manager opens in literal search; Ctrl-o preserves the query.
            os.write(fd, b"\x1bab")
            wait_for(lambda: "query" in search_state())
            os.write(fd, b"project")
            wait_for(lambda: search_state().get("query") == "project")
            if os.environ.get("TMUX_ROAM_TEST_BACKEND") != "python":
                identity = search_state()["current"]["text"].split("\t", 1)[0]
                picker_root = next((root / "r/tmux-roam").glob("picker-*/metadata.json")).parent
                for rows, columns in ((24, 72), (40, 150), (32, 110)):
                    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
                    wait_for(lambda: json.loads((picker_root / "mode.json").read_text()).get("columns", 0) > 0)
                    time.sleep(0.1)
                    state = search_state()
                    assert state.get("query") == "project" and state["current"]["text"].split("\t", 1)[0] == identity, state
                assert json.loads((picker_root / "mode.json").read_text())["columns"] < 100
                # Resize transforms must defer placeholder expansion: quoted
                # query text is data even when the transform returns a command.
                injected = root / "query-must-not-execute"
                literal = "project'; $(touch " + str(injected) + ") #"
                os.write(fd, b"\x7f" * len("project") + literal.encode())
                wait_for(lambda: search_state().get("query") == literal)
                old_width = json.loads((picker_root / "mode.json").read_text())["columns"]
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 26, 84, 0, 0))
                wait_for(lambda: json.loads((picker_root / "mode.json").read_text())["columns"] != old_width)
                wait_for(lambda: not (picker_root / "reload-selection").exists())
                assert search_state().get("query") == literal and not injected.exists()
                os.write(fd, b"\x7f" * len(literal) + b"project")
                wait_for(lambda: search_state().get("query") == "project")
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
            os.write(fd, b"\x0f")
            time.sleep(0.25)
            os.write(fd, b"j")
            time.sleep(0.05)
            assert search_state().get("query") == "project"
            os.write(fd, b"/")
            time.sleep(0.1)
            os.write(fd, b"-no-match")
            wait_for(lambda: search_state().get("matchCount") == 0)
            os.write(fd, b"\x1b")
            wait_for(search_closed)
            # Both layout aliases close the manager from literal search.
            for shortcut in (b"\x1bab", "\x1bфи".encode()):
                os.write(fd, shortcut)
                wait_for(lambda: "query" in search_state())
                os.write(fd, shortcut)
                wait_for(search_closed)
            if os.environ.get("TMUX_ROAM_TEST_BACKEND") != "python":
                for _ in range(10):
                    mark = len(output)
                    os.write(fd, b"\x1bab")
                    # Prior-popup repaint bytes can arrive after its state is
                    # removed. Require this invocation's socket and a frame.
                    wait_for(lambda: list((root / "r/tmux-roam").glob("picker-*/fzf.sock")))
                    wait_for(lambda: b"Search>" in output[mark:])
                    os.write(fd, "я".encode())
                    wait_for(lambda: search_state().get("query") == "я")
                    os.write(fd, b"\x1b")
                    wait_for(search_closed)
                # Force a long reload window: input must reach fzf throughout,
                # and an inserted earlier row must not change the chosen ID.
                os.write(fd, b"\x1bab")
                wait_for(lambda: "query" in search_state())
                os.write(fd, b"OMNI/")
                wait_for(lambda: search_state().get("matchCount") == 1)
                chosen = search_state()["current"]["text"].split("\t", 1)[0]
                picker_root = next((root / "r/tmux-roam").glob("picker-*/metadata.json")).parent
                slow_reload.touch()
                tmux(remote, "new-session", "-d", "-s", "aaa-refresh", "sleep 120")
                os.write(fd, b"\x12")
                wait_for(reload_entered.exists)
                os.write(fd, b"start")
                wait_for(lambda: search_state().get("query") == "OMNI/start")
                wait_for(lambda: not (picker_root / "reload-selection").exists())
                wait_for(lambda: search_state()["current"]["text"].split("\t", 1)[0] == chosen)
                reload_entered.unlink()
                os.write(fd, b"\x7f" * len("OMNI/start") + b"OMNI/")
                wait_for(lambda: search_state().get("matchCount") == 2)
                # Choose the existing project, then insert another earlier row.
                os.write(fd, b"\x1b[B")
                wait_for(lambda: search_state()["current"]["text"].split("\t", 1)[0] == chosen)
                tmux(remote, "new-session", "-d", "-s", "000-refresh", "sleep 120")
                os.write(fd, b"\x12")
                wait_for(reload_entered.exists)
                os.write(fd, b"\r")
                time.sleep(0.05)
                assert not search_closed() and other in tmux(local, "list-clients", "-F", "#{session_id}")
                wait_for(lambda: not (picker_root / "reload-selection").exists())
                wait_for(lambda: search_state().get("matchCount") == 3)
                wait_for(lambda: search_state()["current"]["text"].split("\t", 1)[0] == chosen)
                # Navigation during the old-list window is also deliberate
                # input: retain that new choice across the replacement list.
                moved = search_state()["matches"][1]["text"].split("\t", 1)[0]
                reload_entered.unlink()
                tmux(remote, "rename-session", "-t", "aaa-refresh", "zzz-refresh")
                os.write(fd, b"\x12")
                wait_for(reload_entered.exists)
                os.write(fd, b"\x1b[A")
                wait_for(lambda: search_state()["current"]["text"].split("\t", 1)[0] == moved)
                wait_for(lambda: not (picker_root / "reload-selection").exists())
                wait_for(lambda: "zzz-refresh" in search_state()["current"]["text"])
                assert search_state()["current"]["text"].split("\t", 1)[0] == moved
                slow_reload.unlink()
                os.write(fd, b"\x1b")
                wait_for(search_closed)
                for name in ("zzz-refresh", "000-refresh"):
                    tmux(remote, "kill-session", "-t", name)
            def screen_keys(keys, text):
                start = len(output)
                os.write(fd, keys)
                wait_for(lambda: text in re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", bytes(output[start:])))
                time.sleep(0.08)
            # Nested action/host menus and input fields preserve the global toggle.
            os.write(fd, b"\x1bab")
            wait_for(lambda: "query" in search_state())
            screen_keys(b"\x1bOQ", b"Session actions")
            os.write(fd, "\x1bфи".encode())
            wait_for(search_closed)
            os.write(fd, b"\x1bab")
            wait_for(lambda: "query" in search_state())
            screen_keys(b"\x0e", b"Choose a host")
            screen_keys(b"\r", b"Session name")
            os.write(fd, b"cancelled-form")
            os.write(fd, b"\x1bab")
            wait_for(search_closed)
            assert "cancelled-form" not in tmux(remote, "list-sessions", "-F", "#{session_name}")
            # Create through the real forms and open the returned exact identity.
            os.write(fd, b"\x1bab")
            wait_for(lambda: "query" in search_state())
            screen_keys(b"\x0e", b"Choose a host")
            screen_keys(b"\r", b"Session name")
            screen_keys(b"created-project\r", b"Remote project directory")
            os.write(fd, b"\r")
            wait_for(lambda: "created-project" in tmux(remote, "list-sessions", "-F", "#{session_name}"))
            wait_for(search_closed)
            # Return locally, select the created project and confirm default Cancel.
            tmux(local, "switch-client", "-c", client, "-t", other)
            os.write(fd, b"\x1bab")
            wait_for(lambda: "query" in search_state())
            os.write(fd, b"OMNI/created-project")
            wait_for(lambda: search_state().get("query") == "OMNI/created-project" and search_state().get("matchCount") == 1)
            os.write(fd, b"\x0f")
            time.sleep(0.25)
            screen_keys(b"dd", b"Delete session?")
            os.write(fd, b"\r")
            time.sleep(0.15)
            assert "created-project" in tmux(remote, "list-sessions", "-F", "#{session_name}")
            screen_keys("вв".encode(), b"Delete session?")
            os.write(fd, b"j\r")
            wait_for(lambda: "created-project" not in tmux(remote, "list-sessions", "-F", "#{session_name}"))
            wait_for(lambda: search_state().get("matchCount") == 0)
            os.write(fd, b"\x1b")
            wait_for(search_closed)
            if os.environ.get("TMUX_ROAM_TEST_BACKEND") != "python":
                # Drive the actual F2 SSH sync flow against private homes.
                # Cancel is the default; stale previews refuse replacement.
                before = tmux(remote, "list-sessions", "-F", "#{session_id}|#{session_created}")
                os.write(fd, b"\x1bab")
                wait_for(lambda: "query" in search_state())
                os.write(fd, b"OMNI/start")
                wait_for(lambda: search_state().get("matchCount") == 1)
                def sync_preview(all_hosts=False):
                    screen_keys(b"\x1bOQ", b"Session actions")
                    screen_keys(b"jjjj\r", b"Choose a host")
                    screen_keys(b"j\r" if all_hosts else b"\r", b"Cancel is selected initially")
                sync_preview()
                screen_keys(b"\x15\x04\r", b"Search>")
                assert target.read_text() == "set -g @sync_menu before\n"
                sync_preview()
                target.write_text("set -g @sync_menu external-edit\n")
                screen_keys(b"j\r", b"changed since the preview")
                assert target.read_text() == "set -g @sync_menu external-edit\n"
                sync_preview(all_hosts=True)
                screen_keys(b"j\r", b"installed and reloaded")
                wait_for(lambda: target.read_bytes() == source.read_bytes())
                assert remote_config.is_symlink() and target.stat().st_mode & 0o777 == 0o640
                assert tmux(remote, "show-options", "-gqv", "@sync_menu") == "after"
                assert tmux(remote, "list-sessions", "-F", "#{session_id}|#{session_created}") == before
                os.write(fd, b"\x1b")
                wait_for(search_closed)
                print("Go F2 SSH config sync: host/all-host preview, diff scrolling retains default Cancel, stale preview refused, symlink/mode and existing session identities preserved")
                # A fresh observation retains the selected ended identity as
                # an inert row, even if a new session reuses its display name.
                tmux(remote, "new-session", "-d", "-s", "refresh-ended", "sleep 120")
                subprocess.run([str(GO), "list", "--json"], env=env, capture_output=True, check=True)
                os.write(fd, b"\x1bab")
                wait_for(lambda: "query" in search_state())
                os.write(fd, b"OMNI/refresh-ended")
                wait_for(lambda: search_state().get("matchCount") == 1)
                old = search_state()["current"]["text"].split("\t", 1)[0]
                tmux(remote, "kill-session", "-t", "refresh-ended")
                os.write(fd, b"\x12")
                wait_for(lambda: "Ended" in search_state().get("current", {}).get("text", ""))
                os.write(fd, b"\r")
                time.sleep(0.1)
                assert not search_closed() and other in tmux(local, "list-clients", "-F", "#{session_id}")
                tmux(remote, "new-session", "-d", "-s", "refresh-ended", "sleep 120")
                os.write(fd, b"\x12")
                wait_for(lambda: search_state().get("matchCount") == 2)
                assert search_state()["current"]["text"].split("\t", 1)[0] == old
                assert search_state().get("query") == "OMNI/refresh-ended"
                os.write(fd, b"\x1b")
                wait_for(search_closed)
                os.write(fd, b"\x1bas")
                wait_for(lambda: mode() == "tree-mode")
                wait_for(lambda: "created-project" not in tmux(local, "list-sessions", "-F", "#{session_name}"))
                os.write(fd, b"\x1b")
                wait_for(lambda: mode() == "")
                # Failed validation keeps the same live picker/query/identity.
                unavailable = root / "unavailable"
                hanging = root / "hanging"
                attaching = root / "attaching"
                entered = root / "entered"
                def stop_blocked_remote():
                    try:
                        os.kill(int(entered.read_text()), 15)
                    except ProcessLookupError:
                        pass
                    entered.unlink()
                wrapper = root / "remote-tmux"
                wrapper.write_text(f'''#!/bin/sh
[ ! -f "{unavailable}" ] || exit 1
if [ -f "{hanging}" ] || {{ [ -f "{attaching}" ] && [ "$3" = attach-session ]; }}; then
    echo $$ > "{entered}"
    exec sleep 30
fi
exec tmux "$@"
''')
                wrapper.chmod(0o700)
                config.write_text(config.read_text()+f'tmux_path="{wrapper}"\n')
                os.write(fd, b"\x1bab")
                wait_for(lambda: "query" in search_state())
                os.write(fd, b"OMNI/start")
                wait_for(lambda: search_state().get("query") == "OMNI/start" and search_state().get("matchCount") == 1)
                current = search_state()["current"]["text"].split("\t", 1)[0]
                sock = next((root / "r/tmux-roam").glob("picker-*/fzf.sock"))
                inode = sock.stat().st_ino
                unavailable.touch()
                mark = len(output)
                os.write(fd, b"\r")
                wait_for(lambda: b"SSH exit 1" in output[mark:])
                assert sock.stat().st_ino == inode
                wait_for(lambda: (state := search_state()).get("query") == "OMNI/start" and (state.get("current") or {}).get("text", "").split("\t", 1)[0] == current)
                assert other in tmux(local, "list-clients", "-F", "#{session_id}")
                unavailable.unlink()
                os.write(fd, b"\x1b")
                wait_for(search_closed)
                # Both picker views remain responsive while SSH is blocked.
                # Callback termination must cancel/join the parent-owned open.
                progress_ms, cancellation_ms = [], []
                for lower, close in ((False, b"\x1b"), (False, b"\x1bab"), (False, "\x1bфи".encode()), (True, b"\x1b")):
                    if lower:
                        pane = tmux(local, "list-clients", "-F", "#{pane_id}")
                        os.write(fd, b"\x1bas")
                        wait_for(lambda: mode() == "tree-mode")
                        os.write(fd, b"/")
                    else:
                        os.write(fd, b"\x1bab")
                    wait_for(lambda: "query" in search_state())
                    os.write(fd, b"OMNI/start")
                    wait_for(lambda: search_state().get("query") == "OMNI/start" and search_state().get("matchCount") == 1)
                    before = tmux(local, "list-sessions", "-F", "#{session_id}|#{@tmux-roam-identity}")
                    hanging.touch()
                    mark, start = len(output), time.monotonic()
                    os.write(fd, b"\r")
                    wait_for(lambda: b"Esc cancels" in output[mark:])
                    progress_ms.append((time.monotonic() - start) * 1000)
                    wait_for(entered.exists)
                    start = time.monotonic()
                    os.write(fd, close)
                    wait_for(search_closed)
                    cancellation_ms.append((time.monotonic() - start) * 1000)
                    hanging.unlink()
                    stop_blocked_remote()
                    assert other in tmux(local, "list-clients", "-F", "#{session_id}")
                    after = tmux(local, "list-sessions", "-F", "#{session_id}|#{@tmux-roam-identity}")
                    if lower:
                        # The native worker may concurrently add dormant
                        # metadata or prune confirmed ended entries. Cancellation
                        # must preserve user sessions and leave no active opener.
                        assert {line for line in before.splitlines() if line.endswith("|")} <= set(after.splitlines()), (before, after)
                        for line in tmux(local, "list-sessions", "-F", "#{@tmux-roam-identity}|#{@tmux-roam-state}").splitlines():
                            identity, state_token = line.split("|", 1)
                            if identity == current:
                                assert json.loads((root / "r/tmux-roam" / (state_token + ".json")).read_text())["status"] == "idle"
                    else:
                        assert after == before, (before, after)
                    if lower:
                        assert mode() == "tree-mode"
                        os.write(fd, b"\x1b")
                        wait_for(lambda: mode() == "")
                # Cancel after a new supervisor starts: its unused carrier and
                # opener leases disappear, and a later retry can commit.
                os.write(fd, b"\x1bab")
                wait_for(lambda: "query" in search_state())
                os.write(fd, b"OMNI/start")
                wait_for(lambda: search_state().get("matchCount") == 1)
                attaching.touch()
                os.write(fd, b"\r")
                wait_for(entered.exists)
                os.write(fd, b"\x1b")
                wait_for(search_closed)
                attaching.unlink()
                stop_blocked_remote()
                assert not list((root / "r/tmux-roam").glob("opening-*"))
                assert other in tmux(local, "list-clients", "-F", "#{session_id}")
                # Native remote selections use the same controller. Cancelling
                # their progress popup preserves the invoking client's session.
                dormant = next(line.split("|", 1)[0] for line in tmux(local, "list-sessions", "-F", "#{session_id}|#{@tmux-roam-identity}").splitlines() if line.endswith("|"+current))
                remote_pane = tmux(local, "display-message", "-p", "-t", dormant, "#{pane_id}")
                tmux(local, "set-option", "-g", "@tmux-roam-navigation-"+client, "native-connection-test")
                hanging.touch()
                native = subprocess.Popen([str(GO), "native-select", "--client", client, "--navigation", "native-connection-test", "--target", remote_pane], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                wait_for(entered.exists)
                wait_for(lambda: "query" in search_state())
                os.write(fd, b"\x1b")
                out, err = native.communicate(timeout=2)
                assert native.returncode == 0, (out, err)
                wait_for(search_closed)
                hanging.unlink()
                stop_blocked_remote()
                assert other in tmux(local, "list-clients", "-F", "#{session_id}")
                os.write(fd, b"\x1bab")
                wait_for(lambda: "query" in search_state())
                os.write(fd, b"OMNI/start")
                wait_for(lambda: search_state().get("matchCount") == 1)
                os.write(fd, b"\r")
                wait_for(search_closed)
                assert other not in tmux(local, "list-clients", "-F", "#{session_id}")
                tmux(local, "switch-client", "-c", client, "-t", other)
                tmux(local, "set-option", "-g", "@tmux-roam-navigation-"+client, "native-connection-success")
                native = subprocess.Popen([str(GO), "native-select", "--client", client, "--navigation", "native-connection-success", "--target", remote_pane], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                out, err = native.communicate(timeout=8)
                assert native.returncode == 0, (out, err)
                assert other not in tmux(local, "list-clients", "-F", "#{session_id}")
                wait_for(search_closed)
                print("Connection progress ms:", [round(x, 2) for x in progress_ms], "cancel/join ms:", [round(x, 2) for x in cancellation_ms])
                assert max(progress_ms) < 100, progress_ms
                assert max(cancellation_ms) < 250, cancellation_ms
                # Installed native bindings open and select local work even
                # when their referenced controller cannot run discovery.
                tmux(local, "switch-client", "-c", client, "-t", "start")
                copied = root / "controller"
                shutil.copy2(GO, copied)
                subprocess.run([str(copied), "integrate", "install"], env=env, check=True, capture_output=True)
                replacement = root / "stalled-controller"
                stalled = root / "stalled-pid"
                replacement.write_text(f'#!/bin/sh\necho $$ > "{stalled}"\nexec sleep 120\n')
                replacement.chmod(0o700)
                replacement.replace(copied)
                for missing in (False, True):
                    if missing:
                        copied.unlink()
                    os.write(fd, b"\x1bas")
                    wait_for(lambda: mode() == "tree-mode")
                    # Session index 1 is the second local session; remote
                    # placeholders have later indices. No callback is needed.
                    os.write(fd, b"1")
                    wait_for(lambda: tmux(local, "list-clients", "-F", "#{session_id}") == other)
                    tmux(local, "switch-client", "-c", client, "-t", "start")
                if stalled.exists():
                    try:
                        os.kill(int(stalled.read_text()), 15)
                    except ProcessLookupError:
                        pass
                subprocess.run([str(GO), "integrate", "install"], env=env, check=True, capture_output=True)
                # Manager falls back to native tree in a small terminal.
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 40, 0, 0))
                wait_for(lambda: tmux(local, "list-clients", "-F", "#{client_width}|#{client_height}") == "40|12")
                os.write(fd, b"\x1bab")
                wait_for(lambda: mode() == "tree-mode")
                assert search_closed()
                os.write(fd, b"\x1b")
                wait_for(lambda: mode() == "")
                fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
            # A later user binding survives unload.
            tmux(local, "bind-key", "-T", "prefix", "b", "display-message", "user-edit")
            result = subprocess.run([str(GO), "integrate", "unload"], env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
            assert "user-edit" in tmux(local, "list-keys", "-T", "prefix", "b")
            assert b"returned 2" not in output, output[-4000:]
            print("Go PTY pickers: native, metadata, lower search/literal Cyrillic/Escape/selection, manager search/Ctrl-o/no-match/Escape, live resize, immediate first key, inherited fzf defaults ignored, ended-row identity retained, native deletion refresh, stalled/missing controller local navigation, small-terminal fallback, layout/nested toggles, form cancel, creation, dd/вв deletion/default Cancel, safe unload")
        except Exception:
            Path("/tmp/tr-picker-pty.raw").write_bytes(output)
            print("PTY tail:", bytes(output[-6000:]).decode("utf-8", "replace"))
            print("Clients:", tmux(local, "list-clients", "-F", "#{client_name}|#{session_id}"))
            print("Modes:", tmux(local, "list-panes", "-a", "-F", "#{pane_id}|#{pane_mode}"))
            print("Search:", search_state())
            for path in (root / "r/tmux-roam").glob("picker-*/selected"):
                print("Selection:", path.read_text())
                state = search_state()
                identity = (state.get("current") or {}).get("text", "").split("\t", 1)[0]
                probe = subprocess.run([str(GO), "picker-select", str(path.parent), identity], env=env, capture_output=True, text=True)
                print("Callback:", probe.returncode, probe.stdout, probe.stderr)
            raise
        finally:
            stopped.set()
            for sock in (local, remote):
                subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)
            if reader:
                reader.join(timeout=1)
            if fd is not None:
                os.close(fd)
            if pid is not None:
                os.waitpid(pid, 0)


if __name__ == "__main__":
    main()
