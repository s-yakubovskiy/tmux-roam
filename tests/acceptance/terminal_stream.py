#!/usr/bin/env python3
"""Exercise terminal applications through the Go supervisor and real SSH PTYs."""
from contextlib import ExitStack
import json
import os
import shlex
import shutil
import subprocess

from compare_inventory import GO, call
from compare_ssh_inventory import ssh_target
from session_lifecycle import wait_for


def main():
    with ssh_target() as (root, ssh_config), ExitStack() as cleanup:
        local, remote = root / "local.sock", root / "remote.sock"
        def tmux(sock, *args):
            return subprocess.check_output(["tmux", "-S", str(sock), *args], text=True).strip()
        for sock in (local, remote):
            tmux(sock, "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 120")
            cleanup.callback(lambda s=sock: subprocess.run(["tmux", "-S", str(s), "kill-server"], capture_output=True))
            tmux(sock, "set-option", "-g", "history-limit", "50000")
        start, interrupted = root / "start", root / "interrupted"
        program = root / "stream.py"
        program.write_text(f'''import os, signal, time
signal.signal(signal.SIGINT, lambda *_: open({str(interrupted)!r}, 'w').write('received'))
while not os.path.exists({str(start)!r}): time.sleep(.01)
for i in range(20000): os.write(1, ('STREAM-%05d '%i + 'x'*60 + '\\n').encode())
for block in (b'\\x1b[', b'31m', 'Юникод 👩‍💻 é'.encode()[:3], 'Юникод 👩‍💻 é'.encode()[3:], b'\\x1b[0', b'm\\nSTREAM_READY\\n'):
    os.write(1, block); time.sleep(.01)
while True: time.sleep(.1)
''')
        tmux(remote, "new-session", "-d", "-s", "stream", shlex.join([shutil.which("python3"), str(program)]))
        config = root / "config.toml"
        config.write_text(f'[[hosts]]\nid="remote"\nssh="disposable"\nsocket_path="{remote}"\n')
        env = {**os.environ, "TMUX": f"{local},0,0", "TMUX_ROAM_CONFIG": str(config),
               "TMUX_ROAM_SSH_CONFIG": str(ssh_config), "XDG_RUNTIME_DIR": str(root / "r"),
               "XDG_CACHE_HOME": str(root / "cache"), "TERM": "xterm-256color"}
        for key in ("XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
            tmux(local, "set-environment", "-g", key, env[key])
        control = subprocess.Popen(["tmux", "-S", str(local), "-C", "attach-session", "-t", "keep"],
                                   env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cleanup.callback(lambda: control.communicate(b"detach-client\n", timeout=5))
        client = wait_for(lambda: tmux(local, "list-clients", "-F", "#{client_name}"))
        _, data = call(GO, env, "list", "--json")
        record = next(r for r in data["sessions"] if r["host_id"] == "remote" and r["name"] == "stream")
        opened = subprocess.run([str(GO), "open", record["id"], "--client", client], env=env,
                                capture_output=True, text=True, timeout=20)
        assert opened.returncode == 0, opened.stderr
        carrier = tmux(local, "list-clients", "-F", "#{session_id}")
        start.touch()
        wait_for(lambda: "STREAM_READY" in tmux(local, "capture-pane", "-p", "-t", carrier), seconds=15)
        screen = tmux(local, "capture-pane", "-p", "-t", carrier)
        assert "Юникод 👩‍💻 é" in screen and "[31m" not in screen, screen
        tmux(local, "send-keys", "-t", carrier, "C-c")
        wait_for(interrupted.exists)
        control.stdin.write(b"refresh-client -C 120,40\n")
        control.stdin.flush()
        wait_for(lambda: tmux(remote, "list-clients", "-F", "#{client_width}|#{client_height}") == "120|40")
        edited = root / "edited.txt"
        editor = shutil.which("vim") or shutil.which("nvim")
        assert editor, "terminal application acceptance requires Vim or Neovim"
        window = tmux(remote, "new-window", "-P", "-F", "#{window_id}", "-t", record["session_id"],
                      shlex.join([editor, "-u", "NONE", "-n", str(edited)]))
        wait_for(lambda: tmux(remote, "display-message", "-p", "-t", window, "#{alternate_on}") == "1")
        tmux(local, "send-keys", "-t", carrier, "-l", "iEdited through Go SSH — Юникод 👩‍💻")
        tmux(local, "send-keys", "-t", carrier, "Escape")
        tmux(local, "send-keys", "-t", carrier, "-l", ":wq")
        tmux(local, "send-keys", "-t", carrier, "Enter")
        wait_for(edited.exists)
        assert edited.read_text() == "Edited through Go SSH — Юникод 👩‍💻\n", edited.read_bytes()
        wait_for(lambda: "STREAM_READY" in tmux(local, "capture-pane", "-p", "-t", carrier))
        token = tmux(local, "show-options", "-qv", "-t", carrier, "@tmux-roam-state")
        state_file = root / "r/tmux-roam" / (token+".json")
        state = json.loads(state_file.read_text())
        os.kill(state["ssh_pid"], 15)
        wait_for(lambda: json.loads(state_file.read_text())["status"] == "disconnected")
        wait_for(lambda: "Enter: retry exact session" in tmux(local, "capture-pane", "-p", "-t", carrier))
        assert tmux(remote, "has-session", "-t", record["session_id"]) == ""
        print("Go supervisor over real SSH PTYs: 1.4 MiB sustained output/20,000-line scrollback, split escapes/UTF-8, Ctrl-C, 120x40 resize, real Vim alternate screen/edit/save/exit, SSH exit and remote-session survival")


if __name__ == "__main__":
    main()
