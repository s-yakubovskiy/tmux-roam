#!/usr/bin/env python3
"""Compare Python and Go inventory using disposable tmux servers and an SSH shim."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[2]
GO = ROOT / "build/tmux-roam-go"
PYTHON = ROOT / "bin/tmux-roam"


def call(exe, env, *args):
    result = subprocess.run([str(exe), *args], env=env, text=True, capture_output=True)
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
            fake.write_text(f'#!/bin/sh\necho call >> "{ssh_calls}"\n[ -f "{offline}" ] && exit 255\nwhile [ "$1" != "--" ]; do shift; done\nshift\nhost="$1"\nshift\n[ "$host" = "down" ] && exit 255\nexec sh -c "$1"\n')
            fake.chmod(0o755)
            config = root / "config.toml"
            config.write_text(f'[[hosts]]\nid = "remote"\nssh = "alias"\nsocket_path = "{remote}"\n')
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
            for exe, env in ((PYTHON, py_env), (GO, go_env)):
                code, data = call(exe, env, "list", "--json")
                assert code == 2 and "remote" in data["errors"]
                assert any(row["host_id"] == "remote" and row["source"] == "cached" for row in data["sessions"])
            (Path(go_env["XDG_CACHE_HOME"]) / "tmux-roam/remote.json").write_text("{truncated")
            _, data = call(GO, go_env, "list", "--json", "--cached")
            assert not any(row["host_id"] == "remote" for row in data["sessions"])
            print("inventory parity: live, cache round trips, isolated host failure, offline fallback, corrupt cache, zero cached SSH")
        finally:
            for socket in (local, remote):
                subprocess.run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)


if __name__ == "__main__":
    main()
