#!/usr/bin/env python3
"""Exercise Go attachment/reuse/reconnect against isolated tmux and real SSH."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import shutil
import shlex
import time

from compare_inventory import GO, call
from compare_ssh_inventory import ssh_target


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("lifecycle condition did not become true")


def main():
    with ssh_target() as (root, ssh_config), ExitStack() as cleanup:
        local, remote = root / "local.sock", root / "remote.sock"
        def tmux(socket, *args):
            return subprocess.check_output(["tmux", "-S", str(socket), *args], text=True).strip()
        for sock in (local, remote):
            tmux(sock, "-f", "/dev/null", "new-session", "-d", "-s", "start", "sleep 120")
            cleanup.callback(lambda s=sock: subprocess.run(["tmux", "-S", str(s), "kill-server"], capture_output=True))
        tmux(remote, "new-session", "-d", "-s", "remote|Юникод", "sleep 120")
        config = root / "config.toml"
        config.write_text(f'[[hosts]]\nid="remote"\nssh="disposable"\nsocket_path="{remote}"\n')
        env = {**os.environ, "TMUX": f"{local},0,0", "TMUX_ROAM_CONFIG": str(config),
               "TMUX_ROAM_SSH_CONFIG": str(ssh_config), "XDG_RUNTIME_DIR": str(root / "r"),
               "XDG_CACHE_HOME": str(root / "cache"), "TERM": "xterm-256color"}
        for key in ("XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
            tmux(local, "set-environment", "-g", key, env[key])
        controls = []
        for _ in range(2):
            control = subprocess.Popen(["tmux", "-S", str(local), "-C", "attach-session", "-t", "start"],
                                       env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            controls.append(control)
            cleanup.callback(lambda p=control: p.communicate(b"detach-client\n", timeout=5))
        def clients():
            return dict(line.split("|", 1) for line in tmux(local, "list-clients", "-F", "#{client_name}|#{session_id}").splitlines())
        wait_for(lambda: len(clients()) == 2)
        first, second = list(clients())
        origin = clients()[first]
        code, data = call(GO, env, "list", "--json")
        assert code == 0, data
        record = next(r for r in data["sessions"] if r["name"] == "remote|Юникод")
        def opened(*extra):
            result = subprocess.run([str(GO), "open", record["id"], "--client", first, *extra],
                                    env=env, text=True, capture_output=True, timeout=20)
            assert result.returncode == 0, (result.stdout, result.stderr)
            return clients()[first]
        carrier = opened()
        window = tmux(remote, "new-window", "-d", "-P", "-F", "#{window_id}", "-t", record["session_id"], "-n", "editor 👩‍💻", "sleep 120")
        tmux(local, "switch-client", "-c", first, "-t", origin)
        assert opened("--window", window) == carrier
        assert tmux(remote, "display-message", "-p", "-t", record["session_id"], "#{window_id}") == window
        assert carrier != origin and clients()[second] == origin, clients()
        pane = tmux(local, "display-message", "-p", "-t", carrier, "#{pane_id}")
        token = tmux(local, "show-options", "-qv", "-t", carrier, "@tmux-roam-state")
        state_file = root / "r/tmux-roam" / (token + ".json")
        state = json.loads(state_file.read_text())
        assert state["status"] == "open", state
        remote_client_count = tmux(remote, "list-clients", "-F", "#{client_pid}").splitlines()
        assert len(remote_client_count) == 1, remote_client_count
        tmux(local, "switch-client", "-c", first, "-t", origin)
        assert opened() == carrier
        assert tmux(remote, "list-clients", "-F", "#{client_pid}").splitlines() == remote_client_count
        # Drop just this test's SSH terminal and reconnect in its original pane.
        os.kill(state["ssh_pid"], 15)
        wait_for(lambda: json.loads(state_file.read_text())["status"] == "disconnected")
        tmux(local, "switch-client", "-c", first, "-t", origin)
        assert opened() == carrier
        assert tmux(local, "display-message", "-p", "-t", carrier, "#{pane_id}") == pane
        # A superseded navigation token must never steal either client's focus.
        tmux(local, "switch-client", "-c", first, "-t", origin)
        tmux(local, "set-option", "-g", "@tmux-roam-navigation-" + first, "newer")
        result = subprocess.run([str(GO), "open", record["id"], "--client", first, "--navigation", "older"],
                                env=env, capture_output=True, timeout=10)
        assert result.returncode == 2 and clients()[first] == clients()[second] == origin
        tmux(local, "set-option", "-gu", "@tmux-roam-navigation-" + first)
        # Slow only final attachment, leaving inventory responsive. Two openers
        # must share a carrier; cancelling a follower preserves the owner's work.
        delay = root / "delay-attach"
        wrapper = root / "tmux-wrapper"
        wrapper.write_text("#!/bin/sh\ncase \"$*\" in *attach-session*) [ ! -f " + shlex.quote(str(delay)) + " ] || sleep 0.8;; esac\nexec " + shlex.quote(shutil.which("tmux")) + ' "$@"\n')
        wrapper.chmod(0o700)
        config.write_text(config.read_text() + f'tmux_path="{wrapper}"\n')
        tmux(remote, "new-session", "-d", "-s", "parallel", "sleep 120")
        _, data = call(GO, env, "list", "--json")
        parallel = next(r for r in data["sessions"] if r["name"] == "parallel")
        delay.touch()
        children = [subprocess.Popen([str(GO), "open", parallel["id"], "--client", target], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for target in (first, second)]
        wait_for(lambda: len(list((root / "r/tmux-roam").glob("opening-*-*-*"))) >= 2)
        children[1].terminate()
        _, error = children[1].communicate(timeout=5)
        assert children[1].returncode == 2, error
        _, error = children[0].communicate(timeout=15)
        assert children[0].returncode == 0, error
        assert clients()[second] == origin
        shared = clients()[first]
        owned = tmux(local, "list-sessions", "-F", "#{session_id}|#{@tmux-roam-identity}").splitlines()
        assert len([line for line in owned if line.endswith("|" + parallel["id"])]) == 1, owned
        tmux(local, "switch-client", "-c", first, "-t", origin)
        tmux(remote, "new-session", "-d", "-s", "cancel-open", "sleep 120")
        _, data = call(GO, env, "list", "--json")
        cancelled = next(r for r in data["sessions"] if r["name"] == "cancel-open")
        child = subprocess.Popen([str(GO), "open", cancelled["id"], "--client", first], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        wait_for(lambda: any(line.endswith("|"+cancelled["id"]) for line in tmux(local, "list-sessions", "-F", "#{session_id}|#{@tmux-roam-identity}").splitlines()))
        time.sleep(0.12)
        child.terminate()
        _, error = child.communicate(timeout=5)
        assert child.returncode == 2 and clients()[first] == origin, error
        wait_for(lambda: not any(line.endswith("|"+cancelled["id"]) for line in tmux(local, "list-sessions", "-F", "#{session_id}|#{@tmux-roam-identity}").splitlines()))
        delay.unlink()
        # Reconnect repeatedly in the original pane, with no new supervisors.
        tmux(local, "switch-client", "-c", first, "-t", origin)
        supervisor_pids = set()
        resources = []
        def sample_supervisor():
            pid = tmux(local, "display-message", "-p", "-t", carrier, "#{pane_pid}")
            memory = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines()[1:])
            return {"rss_kib": int(memory["Rss"].split()[0]), "pss_kib": int(memory["Pss"].split()[0]),
                    "private_kib": sum(int(memory[key].split()[0]) for key in ("Private_Clean", "Private_Dirty")),
                    "fds": len(list(Path(f"/proc/{pid}/fd").iterdir()))}
        for _ in range(int(os.environ.get("TMUX_ROAM_RECONNECT_CYCLES", "30"))):
            resources.append(sample_supervisor())
            supervisor_pids.add(tmux(local, "display-message", "-p", "-t", carrier, "#{pane_pid}"))
            active_token = tmux(local, "show-options", "-qv", "-t", carrier, "@tmux-roam-state")
            state_file = root / "r/tmux-roam" / (active_token + ".json")
            state = json.loads(state_file.read_text())
            os.kill(state["ssh_pid"], 15)
            wait_for(lambda: json.loads(state_file.read_text())["status"] == "disconnected")
            assert opened() == carrier
            tmux(local, "switch-client", "-c", first, "-t", origin)
        assert tmux(local, "display-message", "-p", "-t", carrier, "#{pane_id}") == pane
        wait_for(lambda: all(not Path(f"/proc/{pid}").exists() for pid in supervisor_pids))
        assert len(list((root / "r/tmux-roam").glob("*.json"))) <= 4
        assert not list((root / "r/tmux-roam").glob("opening-*"))
        resources.append(sample_supervisor())
        resource_report = {"scope": "Go managed SSH supervisor; real loopback SSH; 30 same-pane reconnects",
                           "samples": len(resources), "supervisor": {key: {"min": min(row[key] for row in resources),
                           "max": max(row[key] for row in resources), "last": resources[-1][key]} for key in resources[0]}}
        assert resource_report["supervisor"]["rss_kib"]["max"] <= 16*1024, resource_report
        if report_path := os.environ.get("TMUX_ROAM_RESOURCE_REPORT"):
            Path(report_path).write_text(json.dumps(resource_report, indent=2)+"\n")
        # Recreated names cannot satisfy an earlier exact identity.
        tmux(remote, "kill-session", "-t", record["session_id"])
        tmux(remote, "new-session", "-d", "-s", record["name"], "sleep 120")
        result = subprocess.run([str(GO), "open", record["id"], "--client", first], env=env, capture_output=True, timeout=10)
        assert result.returncode == 2 and clients()[first] == origin
        print("Go lifecycle: exact SSH attach, two-client isolation, reuse, disconnect/retry in same pane, superseded navigation, stale identity, concurrent opens/follower cancellation, unused-attempt cleanup, 30 reconnect cycles")


if __name__ == "__main__":
    main()
