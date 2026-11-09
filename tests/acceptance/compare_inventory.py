#!/usr/bin/env python3
"""Compare Python and Go inventory using disposable tmux servers and an SSH shim."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
GO = ROOT / "build/tmux-roam-go"
PYTHON = ROOT / "bin/tmux-roam-python"


def call(exe, env, *args):
    result = subprocess.run([str(exe), *args], env=env, text=True, capture_output=True, timeout=15)
    if not result.stdout:
        raise AssertionError(f"{exe} {args}: {result.returncode}: {result.stderr}")
    return result.returncode, json.loads(result.stdout)


def rows(data):
    result = data["sessions"]
    for row in result:
        row.pop("observed", None)
    return result


def main():
    if not GO.exists():
        raise SystemExit("build Go first: sh scripts/build-go.sh")
    with tempfile.TemporaryDirectory(prefix="tmux-roam-compare-") as directory:
        root = Path(directory)
        local, remote = root / "local.sock", root / "remote.sock"
        for socket, name in ((local, "local|🐱"), (remote, "remote|🐱")):
            subprocess.run(["tmux", "-S", str(socket), "-f", "/dev/null", "new-session", "-d", "-s", name], check=True)
        try:
            fake_bin = root / "bin"
            fake_bin.mkdir()
            offline = root / "offline"
            ssh_calls = root / "ssh-calls"
            fake = fake_bin / "ssh"
            fake.write_text(f'''#!/bin/sh
echo call >> "{ssh_calls}"
[ -f "{offline}" ] && exit 255
while [ "$1" != "--" ]; do shift; done
shift
host="$1"
shift
[ "$host" = "down" ] && exit 255
if [ -n "$TMUX_ROAM_TEST_RESPONSE" ]; then exec cat "$TMUX_ROAM_TEST_RESPONSE"; fi
if [ "$host" = slow ]; then echo $$ > "{root / 'slow.pid'}"; exec sleep 30; fi
[ -z "$TMUX_ROAM_TEST_DELAY" ] || sleep "$TMUX_ROAM_TEST_DELAY"
exec sh -c "$1"
''')
            fake.chmod(0o755)
            config = root / "config.toml"
            config.write_text(f'[[hosts]]\nid = "remote"\nlabel = "Remote #[fg=red] #{{session_name}} <&>"\nssh = "alias"\nsocket_path = "{remote}"\n')
            common = {**os.environ, "PATH": str(fake_bin) + ":" + os.environ["PATH"],
                      "TMUX": str(local) + ",0,0", "TMUX_ROAM_CONFIG": str(config),
                      "XDG_RUNTIME_DIR": str(root / "run")}
            py_env = {**common, "XDG_CACHE_HOME": str(root / "py-cache")}
            go_env = {**common, "XDG_CACHE_HOME": str(root / "go-cache")}
            py_code, py_live = call(PYTHON, py_env, "list", "--json")
            go_code, go_live = call(GO, go_env, "list", "--json")
            assert py_code == go_code == 0
            assert rows(py_live) == rows(go_live), (py_live, go_live)
            for exe, env in ((PYTHON, py_env), (GO, go_env)):
                diagnostic = subprocess.run([str(exe), "doctor"], env=env, text=True, capture_output=True)
                assert "remote: 1 sessions" in diagnostic.stdout, (exe, diagnostic.stdout, diagnostic.stderr)
            diagnostic = subprocess.run([str(GO), "doctor"], env={**go_env, "TMUX_ROAM_TIMING": "1"}, text=True, capture_output=True)
            assert diagnostic.returncode == 0 and "tmux native fuzzy search:" in diagnostic.stdout, diagnostic
            assert all(phase in diagnostic.stderr for phase in ("timing local inventory:", "timing host remote observation including SSH:", "timing doctor total:")), diagnostic.stderr
            assert "remote|🐱" not in diagnostic.stderr, diagnostic.stderr
            calls_before_cached=len(ssh_calls.read_text().splitlines())
            py_cached = rows(call(GO, {**go_env, "XDG_CACHE_HOME": py_env["XDG_CACHE_HOME"]}, "list", "--json", "--cached")[1])
            go_cached = rows(call(PYTHON, {**py_env, "XDG_CACHE_HOME": go_env["XDG_CACHE_HOME"]}, "list", "--json", "--cached")[1])
            assert len(ssh_calls.read_text().splitlines()) == calls_before_cached, "cached list invoked SSH"
            expected_py = [dict(row, source="cached") if row["host_id"] == "remote" else row for row in rows(py_live)]
            expected_go = [dict(row, source="cached") if row["host_id"] == "remote" else row for row in rows(go_live)]
            assert py_cached == expected_py, (py_cached, expected_py)
            assert go_cached == expected_go, (go_cached, expected_go)
            config.write_text(config.read_text() + '[[hosts]]\nid = "down"\nssh = "down"\nsocket_path = "' + str(remote) + '"\n')
            for exe, env in ((PYTHON, py_env), (GO, go_env)):
                code, data = call(exe, env, "list", "--json")
                assert code == 2 and "down" in data["errors"]
                assert any(row["host_id"] == "remote" and row["source"] == "live" for row in data["sessions"])
            offline.touch()
            diagnostic = subprocess.run([str(GO), "doctor"], env=go_env, text=True, capture_output=True)
            assert diagnostic.returncode == 2 and "remote:" in diagnostic.stdout and "MISSING" not in diagnostic.stdout, diagnostic
            for exe, env in ((PYTHON, py_env), (GO, go_env)):
                code, data = call(exe, env, "list", "--json")
                assert code == 2 and "remote" in data["errors"]
                assert any(row["host_id"] == "remote" and row["source"] == "cached" for row in data["sessions"])
            (Path(go_env["XDG_CACHE_HOME"]) / "tmux-roam/remote.json").write_text("{truncated")
            _, data = call(GO, go_env, "list", "--json", "--cached")
            assert not any(row["host_id"] == "remote" for row in data["sessions"])
            offline.unlink()
            config.write_text(f'[[hosts]]\nid = "remote"\nssh = "alias"\nsocket_path = "{remote}"\n')
            writers = [subprocess.Popen([str(GO), "refresh", "remote"], env=go_env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
            for writer in writers:
                stdout, stderr = writer.communicate(timeout=10)
                assert writer.returncode == 0, (stdout, stderr)
            # Overlapping processes share one actual SSH observation. A
            # cancelled follower leaves the owner and other followers running.
            before = len(ssh_calls.read_text().splitlines())
            delayed = {**go_env, "TMUX_ROAM_TEST_DELAY": "0.4"}
            children = [subprocess.Popen([str(GO), "refresh", "remote"], env=delayed,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(6)]
            time.sleep(0.12)
            children[-1].terminate()
            for index, child in enumerate(children):
                out, error = child.communicate(timeout=5)
                assert child.returncode == (2 if index == len(children)-1 else 0), (out, error)
            assert len(ssh_calls.read_text().splitlines()) == before+1, "cross-process observation was duplicated"
            assert not list((root / "run/tmux-roam").glob("inventory-*.sock"))
            # If the observing owner disappears, a waiting process takes over.
            before = len(ssh_calls.read_text().splitlines())
            owner = subprocess.Popen([str(GO), "refresh", "remote"], env=delayed, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic()+3
            while len(ssh_calls.read_text().splitlines()) == before and time.monotonic() < deadline:
                time.sleep(0.01)
            follower = subprocess.Popen([str(GO), "refresh", "remote"], env=delayed, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            time.sleep(0.08)
            owner.terminate()
            owner.communicate(timeout=5)
            out, error = follower.communicate(timeout=5)
            assert owner.returncode == 2 and follower.returncode == 0, (out, error)
            assert len(ssh_calls.read_text().splitlines()) == before+2
            for exe in (PYTHON, GO):
                code, data = call(exe, go_env, "list", "--json", "--cached")
                assert code == 0 and len(data["sessions"]) == 2, data
            response = root / "response"
            valid = b"12|34|/tmp/remote.sock\n$0|100|name|1|0|C-b\nTMUX_ROAM_CLIENTS_V1\nTMUX_ROAM_WINDOWS_V1\n$0|@0|0|1|shell\n"
            cases = {
                "missing markers": b"12|34|/tmp/remote.sock\n$0|100|name|1|0|C-b\n",
                "invalid utf8": valid.replace(b"name", b"\xff"),
                "invalid server": valid.replace(b"12|34", b"pid|34"),
                "negative window index": valid.replace(b"@0|0|1", b"@0|-1|1"),
                "signed count": valid.replace(b"name|1|0", b"name|+1|0"),
                "negative client pid": valid.replace(b"TMUX_ROAM_CLIENTS_V1\n", b"TMUX_ROAM_CLIENTS_V1\n-1|/dev/pts/1|$0|client\n"),
                "oversized output": b"x" * ((1 << 20) + 1),
                "empty inventory": b"12|34|/tmp/remote.sock\nTMUX_ROAM_CLIENTS_V1\nTMUX_ROAM_WINDOWS_V1\n",
            }
            for name, payload in cases.items():
                response.write_bytes(payload)
                for exe, env in ((PYTHON, py_env), (GO, go_env)):
                    code, data = call(exe, {**env, "TMUX_ROAM_TEST_RESPONSE": str(response)}, "list", "--json")
                    assert code == (0 if name == "empty inventory" else 2), (name, exe, data)
                    if name == "empty inventory":
                        assert len(data["sessions"]) == 1, data
                    else:
                        assert "remote" in data["errors"], data

            # Public refresh must publish the fast host while a slow one is still
            # running, and SIGTERM must reap the slow SSH process before exit.
            config.write_text(f'''[discovery]
command_timeout_seconds = 30
concurrency = 2
[[hosts]]
id = "slow"
ssh = "slow"
[[hosts]]
id = "remote"
ssh = "alias"
socket_path = "{remote}"
''')
            process = subprocess.Popen([str(GO), "refresh"], env=go_env, text=True,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ready, _, _ = select.select([process.stdout], [], [], 3)
                assert ready, "fast host waited for slow refresh"
                assert process.stdout.readline().strip() == "remote: 1 sessions"
                deadline = time.monotonic() + 3
                while not (root / "slow.pid").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                slow_pid = int((root / "slow.pid").read_text())
                start = time.monotonic()
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=3)
                assert process.returncode == 2, process.returncode
                assert time.monotonic() - start < 0.25, "SIGTERM did not promptly join workers"
                assert not Path(f"/proc/{slow_pid}").exists(), "cancelled SSH process survived"
                assert not (Path(go_env["XDG_CACHE_HOME"]) / "tmux-roam/slow.json").exists()
            finally:
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=3)
                if (root / "slow.pid").exists():
                    try:
                        os.killpg(int((root / "slow.pid").read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            print("inventory parity: live/cache/offline, 8 response cases, 8 concurrent writers, zero cached SSH; Go refresh delivery and SIGTERM cleanup")
        finally:
            for socket in (local, remote):
                subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)


if __name__ == "__main__":
    main()
