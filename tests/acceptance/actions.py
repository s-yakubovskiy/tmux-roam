#!/usr/bin/env python3
"""Exercise action callbacks and uncertain outcomes over real disposable SSH."""
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
        fault, mutations = root / "fault", root / "mutations"
        fault.write_text("")
        mutations.write_text("")
        wrapper = root / "remote-tmux"
        wrapper.write_text(f'''#!/bin/sh
mode=$({shutil.which('cat')} {shlex.quote(str(fault))})
[ "$mode" != offline ] || exit 255
case "$3" in
  new-session|if-shell)
    printf '%s\\n' "$3" >> {shlex.quote(str(mutations))}
    case "$mode" in
      lost) {shlex.quote(shutil.which('tmux'))} "$@" > /dev/null || exit $?; exit 255 ;;
      truncated) {shlex.quote(shutil.which('tmux'))} "$@" > /dev/null; exit $? ;;
    esac ;;
esac
exec {shlex.quote(shutil.which('tmux'))} "$@"
''')
        wrapper.chmod(0o700)
        config = root / "config.toml"
        config.write_text(f'[[hosts]]\nid="remote"\nssh="disposable"\nsocket_path="{remote}"\ntmux_path="{wrapper}"\n')
        picker = root / "picker"
        picker.mkdir(mode=0o700)
        (picker / "mode.json").write_text('{"filter":"All"}')
        stub = root / "bin"
        stub.mkdir()
        fzf = stub / "fzf"
        # The form driver returns explicit test answers; all operations and
        # identity validation still run in the real controller over SSH.
        fzf.write_text('''#!/bin/sh
case "$*" in
  *'Session name'*) printf '%s\\n' "$TR_PROJECT" ;;
  *'Remote project directory'*) printf '%s\\n' "$TR_DIRECTORY" ;;
  *'Confirm deletion'*) printf '%s\\n' 'Delete session' ;;
  *) printf '%s\\n' remote ;;
esac
''')
        fzf.chmod(0o700)
        env = {**os.environ, "PATH": str(stub)+":"+os.environ["PATH"], "TMUX": f"{local},0,0",
               "TMUX_ROAM_CONFIG": str(config), "TMUX_ROAM_SSH_CONFIG": str(ssh_config),
               "XDG_RUNTIME_DIR": str(root / "r"), "XDG_CACHE_HOME": str(root / "cache"),
               "TR_DIRECTORY": str(root), "TERM": "xterm-256color"}
        for key in ("XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
            tmux(local, "set-environment", "-g", key, env[key])
        control = subprocess.Popen(["tmux", "-S", str(local), "-C", "attach-session", "-t", "keep"],
                                   env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cleanup.callback(lambda: control.communicate(b"detach-client\n", timeout=5))
        client = wait_for(lambda: tmux(local, "list-clients", "-F", "#{client_name}"))
        def action(command, *args, expected=0, **values):
            result = subprocess.run([str(GO), command, str(picker), *args], env={**env, **values},
                                    capture_output=True, text=True, timeout=20)
            assert result.returncode == expected, result.stderr
            return result
        def refresh():
            code, data = call(GO, env, "list", "--json")
            assert code == 0, data
            (picker / "metadata.json").write_text(json.dumps({"records": data["sessions"]}))
            return data["sessions"]
        projects = []
        for name in ("first-project", "second-project"):
            action("new-session", TR_PROJECT=name)
            projects.append(json.loads((picker / "created.json").read_text()))
        assert projects[0]["id"] != projects[1]["id"]
        for project in projects * 2:
            result = subprocess.run([str(GO), "open", project["id"], "--client", client], env=env,
                                    capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, result.stderr
            carrier = tmux(local, "list-clients", "-F", "#{session_id}")
            assert tmux(local, "show-options", "-qv", "-t", carrier, "@tmux-roam-identity") == project["id"]
        tmux(local, "switch-client", "-c", client, "-t", "keep")
        for failure in ("lost", "truncated"):
            (picker / "created.json").unlink(missing_ok=True)
            fault.write_text(failure)
            before = len(mutations.read_text().splitlines())
            result = action("new-session", expected=2, TR_PROJECT=failure+"-project")
            assert "refresh to check the outcome before retrying" in result.stderr, result.stderr
            assert not (picker / "created.json").exists()
            assert len(mutations.read_text().splitlines()) == before+1, "blind creation retry"
            fault.write_text("")
            rows = refresh()
            created = next(r for r in rows if r["host_id"] == "remote" and r["name"] == failure+"-project")
            fault.write_text(failure)
            before = len(mutations.read_text().splitlines())
            result = action("delete-session", created["id"], expected=2)
            assert "refresh to check the outcome before retrying" in result.stderr, result.stderr
            assert len(mutations.read_text().splitlines()) == before+1, "blind deletion retry"
            fault.write_text("")
            assert created["id"] not in {r["id"] for r in refresh()}
        rows = refresh()
        action("delete-session", projects[0]["id"])
        assert projects[0]["id"] not in {r["id"] for r in refresh()}
        assert projects[1]["id"] in {r["id"] for r in refresh()}
        before = len(mutations.read_text().splitlines())
        fault.write_text("offline")
        action("delete-session", projects[1]["id"], expected=2)
        assert len(mutations.read_text().splitlines()) == before, "offline cache authorized deletion"
        fault.write_text("")
        assert projects[1]["id"] in {r["id"] for r in refresh()}
        assert "keep" in tmux(remote, "list-sessions", "-F", "#{session_name}").splitlines()
        print("Go actions over real SSH: two independent projects and normal switching, lost/truncated create/delete replies, exactly one mutation, refresh observes outcome, unrelated sessions preserved")


if __name__ == "__main__":
    main()
