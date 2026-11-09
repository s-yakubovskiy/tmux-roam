#!/usr/bin/env python3
"""Inventory parity through real OpenSSH and disposable local tmux/sshd servers.

Requires openssh-server, or TMUX_ROAM_TEST_SSHD pointing to an sshd executable.
Generated keys and explicit host trust exist only inside the temporary target.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import socket
import subprocess
import tempfile
import time

from compare_inventory import GO, PYTHON, call, rows


@contextmanager
def ssh_target(isolated_home=False, python_free=False):
    sshd = os.environ.get("TMUX_ROAM_TEST_SSHD") or shutil.which("sshd")
    if not sshd and Path("/usr/sbin/sshd").exists():
        sshd = "/usr/sbin/sshd"
    if not sshd:
        raise SystemExit("real SSH acceptance requires openssh-server or TMUX_ROAM_TEST_SSHD")
    with tempfile.TemporaryDirectory(prefix="tr-ssh-") as directory:
        root = Path(directory)
        for name in ("host-key", "client-key"):
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(root / name)], check=True)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        user = pwd.getpwuid(os.getuid()).pw_name
        daemon_config = root / "sshd.conf"
        daemon_config.write_text(f"""ListenAddress 127.0.0.1
Port {port}
HostKey {root / 'host-key'}
PidFile {root / 'sshd.pid'}
AuthorizedKeysFile {root / 'client-key.pub'}
StrictModes no
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
PermitRootLogin prohibit-password
AllowUsers {user}
LogLevel ERROR
SetEnv PATH={Path(shutil.which('tmux')).resolve().parent}:/usr/bin:/bin
""")
        if isolated_home:
            for name in ("home", "state", "tmp"):
                (root / name).mkdir(mode=0o700)
            with daemon_config.open("a") as config_file:
                runtime = ""
                if python_free:
                    runtime_bin = root / "runtime-bin"
                    runtime_bin.mkdir()
                    for name in ("sh", "tar", "uname", "mktemp", "rm", "tmux", "sleep"):
                        (runtime_bin / name).symlink_to(shutil.which(name))
                    runtime = f"PATH={runtime_bin} "
                config_file.write(f'ForceCommand env {runtime}HOME={root / "home"} XDG_STATE_HOME={root / "state"} TMPDIR={root / "tmp"} /bin/sh -c "$SSH_ORIGINAL_COMMAND"\n')
        host_key = (root / "host-key.pub").read_text().split()[:2]
        (root / "known_hosts").write_text(f"[127.0.0.1]:{port} {' '.join(host_key)}\n")
        ssh_config = root / "ssh.conf"
        ssh_config.write_text(f"""Host disposable
    HostName 127.0.0.1
    Port {port}
    User {user}
    IdentityFile {root / 'client-key'}
    IdentitiesOnly yes
    UserKnownHostsFile {root / 'known_hosts'}
    GlobalKnownHostsFile /dev/null
    StrictHostKeyChecking yes
    LogLevel ERROR
""")
        with (root / "sshd.log").open("w+") as log:
            daemon = subprocess.Popen([str(Path(sshd).resolve()), "-D", "-e", "-f", str(daemon_config)],
                                      stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 5
                while True:
                    if daemon.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise AssertionError(f"disposable sshd did not start: {log.read()}")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.02)
                yield root, ssh_config
            finally:
                try:
                    for control in root.rglob("ssh-*"):
                        if not control.is_socket():
                            continue
                        subprocess.run(["ssh", "-F", str(ssh_config), "-S", str(control), "-O", "exit", "disposable"],
                                       capture_output=True, timeout=5)
                finally:
                    daemon.terminate()
                    daemon.wait(timeout=5)


def main():
    if not GO.exists():
        raise SystemExit("build Go first: sh scripts/build-go.sh")
    with ssh_target() as (root, ssh_config):
        local = root / "local.sock"
        remote = root / "remote.sock"
        named = "roam-test-" + root.name
        targets = (["-S", str(local)], ["-S", str(remote)], ["-L", named])
        tmux = str(Path(shutil.which("tmux")).resolve())
        wrapper = root / "tmux 'quoted"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(tmux) + ' "$@"\n')
        wrapper.chmod(0o700)
        try:
            for target, name in zip(targets, ("local|🐱", "remote|Юникод", "named|🐱")):
                # Explicit names disable automatic shell-driven renaming while
                # successive SSH/backend observations compare window metadata.
                subprocess.run([tmux, *target, "-f", "/dev/null", "new-session", "-d", "-s", name, "-n", "fixture"], check=True)
            config = root / "config.toml"
            config.write_text(f"""[discovery]
command_timeout_seconds = 3
[[hosts]]
id = "absolute"
ssh = "disposable"
tmux_path = {json.dumps(str(wrapper))}
socket_path = {json.dumps(str(remote))}
[[hosts]]
id = "named"
ssh = "disposable"
tmux_path = {json.dumps(str(wrapper))}
socket_name = {json.dumps(named)}
[[hosts]]
id = "disabled"
ssh = "no-such-disposable-alias"
enabled = false
""")
            env = {**os.environ, "TMUX": f"{local},0,0", "TMUX_ROAM_CONFIG": str(config),
                   "TMUX_ROAM_SSH_CONFIG": str(ssh_config), "XDG_RUNTIME_DIR": str(root / "r"),
                   "XDG_CACHE_HOME": str(root / "cache")}
            # Confirm the target is reachable before exercising either controller.
            probe = subprocess.run(["ssh", "-F", str(ssh_config), "-o", "BatchMode=yes", "disposable", "true"],
                                   capture_output=True, text=True, timeout=5)
            assert probe.returncode == 0, probe.stderr
            observations = []
            for exe in (PYTHON, GO):
                code, data = call(exe, env, "list", "--json")
                assert code == 0, data
                observations.append(rows(data))
            assert observations[0] == observations[1], observations
            assert [r["host_id"] for r in observations[0]] == ["local", "absolute", "named"]
            included = root / "included.conf"
            included.write_text(ssh_config.read_text())
            ssh_config.write_text(f'Include "{included}"\n')
            jump_config = root / "jump.conf"
            jump_config.write_text(included.read_text()+included.read_text().replace("Host disposable", "Host via-jump")+"    ProxyJump disposable\n")
            for label, ssh_path, host in (("Include", ssh_config, "disposable"), ("ProxyJump", jump_config, "via-jump")):
                fixture = root / (label+".toml")
                fixture.write_text(config.read_text().replace('ssh = "disposable"', f'ssh = "{host}"'))
                for exe in (PYTHON, GO):
                    code, data = call(exe, {**env, "TMUX_ROAM_CONFIG": str(fixture), "TMUX_ROAM_SSH_CONFIG": str(ssh_path),
                                           "XDG_RUNTIME_DIR": str(root / (label+"-runtime"))}, "list", "--json")
                    assert code == 0 and rows(data) == observations[0], (label, str(exe), code, data, observations[0])
            agent_socket = root / "agent.sock"
            agent = subprocess.Popen(["ssh-agent", "-D", "-a", str(agent_socket)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic()+3
                while not agent_socket.exists():
                    assert agent.poll() is None and time.monotonic() < deadline, "test ssh-agent did not start"
                    time.sleep(.01)
                agent_env = {**env, "SSH_AUTH_SOCK": str(agent_socket)}
                subprocess.run(["ssh-add", str(root / "client-key")], env=agent_env, capture_output=True, check=True)
                agent_config = root / "agent.conf"
                agent_config.write_text(included.read_text().replace(f"IdentityFile {root / 'client-key'}", "IdentityFile none").replace("IdentitiesOnly yes", "IdentitiesOnly no"))
                for exe in (PYTHON, GO):
                    code, data = call(exe, {**agent_env, "TMUX_ROAM_SSH_CONFIG": str(agent_config),
                                           "XDG_RUNTIME_DIR": str(root / "agent-runtime")}, "list", "--json")
                    assert code == 0 and rows(data) == observations[0], data
            finally:
                agent.terminate(); agent.wait(timeout=3)
            # A new server generation cannot reuse a stale opaque ID.
            previous = next(r for r in observations[1] if r["host_id"] == "absolute")
            subprocess.run([tmux, "-S", str(remote), "kill-server"], check=True)
            # Wait until the old server has removed its socket before restarting.
            deadline = time.monotonic() + 3
            while remote.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            subprocess.run([tmux, "-S", str(remote), "-f", "/dev/null", "new-session", "-d", "-s", previous["name"], "-n", "fixture"], check=True)
            for exe in (PYTHON, GO):
                code, data = call(exe, env, "list", "--json")
                current = next(r for r in data["sessions"] if r["host_id"] == "absolute")
                assert code == 0 and current["id"] != previous["id"]
            # Long runtime paths use ordinary SSH instead of an invalid socket.
            long_runtime = root / ("long-" + "x" * 90)
            for exe in (PYTHON, GO):
                code, data = call(exe, {**env, "XDG_RUNTIME_DIR": str(long_runtime)}, "list", "--json")
                assert code == 0 and len(data["sessions"]) == 3, data
            assert not list(long_runtime.glob("tmux-roam/ssh-*"))
            # An untrusted client key must fail even with an existing good master.
            bad_ssh = root / "bad-ssh.conf"
            bad_ssh.write_text(included.read_text().replace(str(root / "client-key"), str(root / "host-key")))
            for exe in (PYTHON, GO):
                code, data = call(exe, {**env, "TMUX_ROAM_SSH_CONFIG": str(bad_ssh),
                                       "XDG_RUNTIME_DIR": str(long_runtime)}, "list", "--json")
                assert code == 2 and set(data["errors"]) == {"absolute", "named"}, data
                assert all(r["source"] == "cached" for r in data["sessions"] if r["host_id"] != "local")
            config.write_text(config.read_text().replace(str(wrapper), str(root / "missing-tmux")))
            for exe in (PYTHON, GO):
                code, data = call(exe, env, "list", "--json")
                assert code == 2 and set(data["errors"]) == {"absolute", "named"}, data
            print("real SSH parity: aliases, Include, ProxyJump, isolated key agent, host trust, quoted tmux path, -S/-L, restart, long-path fallback, disabled host, auth failure, missing tmux")
        finally:
            for target in targets:
                subprocess.run([tmux, *target, "kill-server"], capture_output=True)


if __name__ == "__main__":
    main()
