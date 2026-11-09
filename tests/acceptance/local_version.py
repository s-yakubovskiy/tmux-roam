#!/usr/bin/env python3
"""Check an actual unsupported local server without attaching to remote hosts."""
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile

from compare_inventory import GO, call
from session_lifecycle import wait_for


def main():
    old_tmux = os.environ.get("TMUX_ROAM_TEST_OLD_TMUX")
    if not old_tmux:
        raise SystemExit("Set TMUX_ROAM_TEST_OLD_TMUX to an actual tmux 3.6a or 3.7 executable")
    with tempfile.TemporaryDirectory(prefix="tr-old-local-") as directory, ExitStack() as cleanup:
        root = Path(directory)
        sock = root / "s"
        subprocess.run([old_tmux, "-S", str(sock), "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 120"], check=True)
        cleanup.callback(lambda: subprocess.run([old_tmux, "-S", str(sock), "kill-server"], capture_output=True))

        # Use the current client binary against an older running server: -V
        # alone cannot establish the server's safety.
        def tmux(*args):
            return subprocess.check_output(["tmux", "-S", str(sock), *args], text=True).strip()

        server_version = tmux("display-message", "-p", "#{version}")
        assert server_version in ("3.6", "3.6a", "3.7"), server_version
        tmux("new-session", "-d", "-s", "project", "sleep 120")
        control = subprocess.Popen(["tmux", "-S", str(sock), "-C", "attach-session", "-t", "keep"],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cleanup.callback(lambda: control.communicate(b"detach-client\n", timeout=5))
        client = wait_for(lambda: tmux("list-clients", "-F", "#{client_name}"))
        config = root / "config.toml"
        config.write_text('[[hosts]]\nid="remote"\nssh="never-contact"\nsocket_path="/tmp/remote"\n')
        shim = root / "bin"
        shim.mkdir()
        ssh_calls = root / "ssh-calls"
        (shim / "ssh").write_text("#!/bin/sh\necho call >> " + shlex.quote(str(ssh_calls)) + "\nexit 255\n")
        (shim / "ssh").chmod(0o700)
        env = {**os.environ, "TMUX": f"{sock},0,0", "TMUX_ROAM_CONFIG": str(config),
               "PATH": str(shim) + ":" + os.environ["PATH"], "XDG_RUNTIME_DIR": str(root / "r"),
               "XDG_CACHE_HOME": str(root / "cache")}

        def run(*args):
            return subprocess.run([str(GO), *args], env=env, text=True, capture_output=True, timeout=10)

        _, data = call(GO, env, "list", "--cached", "--json")
        project = next(r for r in data["sessions"] if r["name"] == "project")
        result = run("open", project["id"], "--client", client)
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert tmux("list-clients", "-F", "#{session_id}") == project["session_id"]
        before = tmux("list-panes", "-a", "-F", "#{pid}|#{start_time}|#{session_id}|#{window_id}|#{pane_id}|#{pane_pid}|#{@tmux-roam-owner}")
        remote_id = base64.urlsafe_b64encode(json.dumps(["remote", "/tmp/remote", "1/2", "$0", 3], separators=(",", ":")).encode()).decode().rstrip("=")
        for args in (("open", remote_id, "--client", client),
                     ("native-refresh", "--client", client, "--pane", project["session_id"], "--navigation", "old-server")):
            result = run(*args)
            assert result.returncode == 2 and "local tmux 3.8+" in result.stderr and server_version in result.stderr, result
            assert tmux("list-panes", "-a", "-F", "#{pid}|#{start_time}|#{session_id}|#{window_id}|#{pane_id}|#{pane_pid}|#{@tmux-roam-owner}") == before
        assert not ssh_calls.exists(), "unsafe-server refusal contacted SSH"
        assert not list((root / "r").rglob("*.json")), "unsafe-server refusal published connection state"
        result = run("doctor")
        assert result.returncode == 2 and f"tmux server version: {server_version}" in result.stdout and "local tmux 3.8+" in result.stdout, result
        assert tmux("list-clients", "-F", "#{session_id}") == project["session_id"]
        print(f"Local compatibility: client {subprocess.check_output(['tmux', '-V'], text=True).strip()}, server {server_version}; local switching passes; remote open/native metadata refuse before SSH, carrier creation or pane mutation; doctor reports the server")


if __name__ == "__main__":
    main()
