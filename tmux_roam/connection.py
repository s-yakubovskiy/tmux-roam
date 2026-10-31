"""Managed SSH attachment. The remote tmux client remains on its original host."""
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import inventory, tmux


OWNER = "@tmux-roam-owner"
IDENTITY = "@tmux-roam-identity"
STATE = "@tmux-roam-state"


def runtime_dir():
    root = Path(os.environ.get("XDG_RUNTIME_DIR", f"/tmp/tmux-roam-{os.getuid()}")) / "tmux-roam"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise RuntimeError(f"unsafe runtime directory: {root}")
    return root


def _owned_sessions():
    result = tmux.run(["list-sessions", "-F", "#{session_id}" + tmux.SEP + "#{" + OWNER + "}" + tmux.SEP + "#{" + IDENTITY + "}"], check=False)
    found = []
    for line in result.stdout.splitlines():
        parts = line.split(tmux.SEP)
        if len(parts) == 3 and parts[1] == "1":
            found.append((parts[0], parts[2]))
    return found


def _remote_client(host, config, pid, tty):
    command = inventory._remote_command(host, [["list-clients", "-F", tmux.CLIENT_FORMAT]])
    try:
        result = subprocess.run(inventory.ssh_base(host, config) + [command],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=config.command_timeout_seconds)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(tmux.SEP)
        if len(parts) == 4 and parts[0] == str(pid) and parts[1] == tty:
            return parts[2]
    return None


def _prefix(host, config):
    if host.remote_prefix != "auto":
        return host.remote_prefix
    command = inventory._remote_command(host, [["show-options", "-gv", "prefix"]])
    try:
        result = subprocess.run(inventory.ssh_base(host, config) + [command], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=config.command_timeout_seconds)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot read remote prefix: {exc}") from exc
    if result.returncode:
        raise RuntimeError("cannot read remote prefix: " + result.stderr.decode("utf-8", "replace")[:300])
    prefix = result.stdout.decode("ascii", "replace").strip()
    if prefix not in ("M-a", "C-b", "None"):
        raise RuntimeError(f"unsupported remote prefix: {prefix}")
    return prefix


def _state_path(token):
    return runtime_dir() / (token + ".json")


def _write_state(token, state):
    target = _state_path(token)
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(state))
    os.chmod(temp, 0o600)
    os.replace(temp, target)


def _read_state(token):
    try:
        return json.loads(_state_path(token).read_text())
    except (OSError, ValueError):
        return {}


def _safe_name(host, name):
    # tmux session names may not contain periods or colons on some versions.
    clean = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_")[:32] or "session"
    return f"roam-{host}-{clean}"


def _routing_table(prefix):
    return "roam-root-" + {"M-a": "ma", "C-b": "cb", "None": "none"}[prefix]


def _client_size(client):
    result = tmux.run(["list-clients", "-F", "#{client_name}|#{client_width}|#{client_height}"])
    for line in result.stdout.splitlines():
        parts = line.rsplit("|", 2)
        if len(parts) == 3 and parts[0] == client and parts[1].isdigit() and parts[2].isdigit():
            return parts[1], parts[2]
    raise RuntimeError("invoking tmux client disappeared")


def _navigation_current(client, token):
    if token is None:
        return True
    from . import picker
    return picker.navigation_valid(client, token)


def open_remote(record, host, config, client, navigation_token=None):
    fresh, error = inventory.remote(host, config, True)
    if error:
        raise RuntimeError(f"{host.label}: {error}")
    if record.id not in {row.id for row in fresh}:
        raise RuntimeError(f"{host.label}: session ended or server restarted; refresh the picker")
    inventory.save(host, fresh)
    prefix = _prefix(host, config)
    lock_path = runtime_dir() / ("target-" + record.id + ".lock")
    owner = False
    local_id = None
    token = None
    stale = set()
    while True:
        candidate = None
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for sid, identity in _owned_sessions():
                if identity != record.id or sid in stale:
                    continue
                existing_token = tmux.run(["show-options", "-v", "-t", sid, STATE], check=False).stdout.strip()
                state = _read_state(existing_token)
                if state.get("status") == "open":
                    candidate = (sid, existing_token, "open", state)
                    break
                if state.get("status") in ("preparing", "attaching") or not state:
                    candidate = (sid, existing_token, "preparing", state)
                    break
            if candidate is None:
                owner = True
                token = uuid.uuid4().hex
                name = _safe_name(host.id, record.name)
                width, height = _client_size(client)
                source_root = str(Path(__file__).resolve().parent.parent)
                command = ["env", "PYTHONPATH=" + source_root,
                           "TMUX_ROAM_CONFIG=" + str(config.path),
                           "TMUX_ROAM_SSH_CONFIG=" + os.environ.get("TMUX_ROAM_SSH_CONFIG", ""),
                           sys.executable, "-m", "tmux_roam.cli",
                           "supervise", token, host.id, record.id]
                result = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}", "-x", width,
                                   "-y", height, "-s", name + "-" + token[:6], shlex.join(command)])
                local_id = result.stdout.strip()
                tmux.run(["set-option", "-t", local_id, OWNER, "1"])
                tmux.run(["set-option", "-t", local_id, IDENTITY, record.id])
                tmux.run(["set-option", "-t", local_id, STATE, token])
                tmux.run(["set-option", "-t", local_id, "@tmux-roam-prefix", prefix])
                tmux.run(["set-option", "-t", local_id, "prefix", "None"])
                tmux.run(["set-option", "-t", local_id, "key-table", _routing_table(prefix)])
                tmux.run(["set-option", "-t", local_id, "status-left", "[roam #{session_name}] "])
                break
        local_id, token, kind, state = candidate
        if kind == "preparing":
            break  # Observe the existing creation without holding its lock.
        if _remote_client(host, config, state.get("pid"), state.get("tty")) == record.session_id:
            tmux.run(["set-option", "-t", local_id, "@tmux-roam-prefix", prefix])
            tmux.run(["set-option", "-t", local_id, "prefix", "None"])
            tmux.run(["set-option", "-t", local_id, "key-table", _routing_table(prefix)])
            if not tmux.client_exists(client) or not _navigation_current(client, navigation_token):
                raise RuntimeError("invoking client disappeared or navigation was cancelled")
            tmux.switch(client, local_id)
            return local_id
        stale.add(local_id)
    deadline = time.monotonic() + config.command_timeout_seconds + config.connect_timeout_seconds + 3
    try:
        while time.monotonic() < deadline:
            if not _navigation_current(client, navigation_token):
                raise RuntimeError("navigation cancelled or superseded")
            state = _read_state(token)
            if state.get("status") == "open":
                if _remote_client(host, config, state.get("pid"), state.get("tty")) == record.session_id:
                    if not tmux.client_exists(client) or not _navigation_current(client, navigation_token):
                        raise RuntimeError("invoking client disappeared or navigation was cancelled")
                    tmux.switch(client, local_id)
                    return local_id
            if state.get("status") in ("error", "disconnected"):
                raise RuntimeError(state.get("error", "attachment failed"))
            time.sleep(.1)
        raise RuntimeError(f"{host.label}: connection did not confirm remote attachment")
    except BaseException:
        if owner and local_id:
            attached = any(c["session_id"] == local_id for c in tmux.clients())
            if not attached:
                tmux.run(["kill-session", "-t", local_id], check=False)
        raise


def supervise(token, host, record, config):
    """Run in the managed tmux pane; relay SSH stdout after a precise prelude."""
    while True:
        nonce = uuid.uuid4().hex
        remote = ("printf '%s:%s:%s\\n' " + shlex.quote("TMUX_ROAM_" + nonce) +
                  " \"$$\" \"$(tty)\"; export LC_ALL=C.UTF-8; exec " +
                  shlex.join(host.tmux_args() + ["attach-session", "-t", record.session_id]))
        command = "sh -c " + shlex.quote(remote)
        _write_state(token, {"status": "preparing"})
        proc = None
        try:
            fresh, error = inventory.remote(host, config)
            if error or record.id not in {r.id for r in fresh}:
                raise RuntimeError(error or "session ended or server restarted; choose a fresh session")
            proc = subprocess.Popen(inventory.ssh_base(host, config, tty=True) + [command],
                                    stdin=None, stdout=subprocess.PIPE, stderr=None, bufsize=0)
            first = bytearray()
            while len(first) < 512 and b"\n" not in first:
                block = os.read(proc.stdout.fileno(), 1)
                if not block:
                    raise RuntimeError(f"SSH ended before tmux attachment (exit {proc.poll()})")
                first.extend(block)
            line, sep, remainder = bytes(first).partition(b"\n")
            parts = line.decode("utf-8", "replace").rstrip("\r").split(":")
            if not sep or len(parts) != 3 or parts[0] != "TMUX_ROAM_" + nonce or not parts[1].isdigit() or not parts[2].startswith("/dev/"):
                raise RuntimeError("remote attachment handshake failed")
            _write_state(token, {"status": "open", "pid": int(parts[1]), "tty": parts[2], "ssh_pid": proc.pid})
            if remainder:
                os.write(sys.stdout.fileno(), remainder)
            while True:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    view = view[os.write(sys.stdout.fileno(), view):]
            code = proc.wait()
            _write_state(token, {"status": "disconnected", "error": f"SSH exited ({code})"})
            print(f"\r\nDisconnected from {host.label}/{record.name} (SSH exit {code}).")
        except Exception as exc:
            if proc and proc.poll() is None:
                proc.terminate()
            _write_state(token, {"status": "error", "error": str(exc)})
            print(f"\r\nConnection error: {exc}")
        print("Enter: retry exact session · Alt+a s: choose another session")
        try:
            input()
        except EOFError:
            return
