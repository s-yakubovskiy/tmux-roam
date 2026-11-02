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


def _managed_pane(session, identity, token):
    """Identify an untouched carrier pane before replacing or removing it."""
    fields = ("session_id", "session_attached", "session_windows", "window_panes",
              "pane_id", "pane_pid", "pane_dead", "pane_dead_time", OWNER, IDENTITY, STATE)
    rows = tmux.run(["list-panes", "-s", "-t", session, "-F",
                     "|".join("#{" + key + "}" for key in fields) + "|#{pane_start_command}"], check=False).stdout.splitlines()
    if len(rows) != 1:
        return None
    parts = rows[0].split("|", len(fields))
    if len(parts) != len(fields) + 1:
        return None
    pane = dict(zip(fields, parts))
    if (pane["session_id"] != session or pane["session_windows"] != "1" or pane["window_panes"] != "1" or
            pane[OWNER] != "1" or pane[IDENTITY] != identity or pane[STATE] != token):
        return None
    try:
        args = shlex.split(parts[-1])
        if len(args) == 1:
            args = shlex.split(args[0])
        # Reconciliation may have moved this carrier to another session on
        # the same remote server since its supervisor originally started.
        if (args[-4:-1] != ["supervise", token, inventory.decode_identity(identity)[0]] or
                inventory.decode_identity(args[-1])[:3] != inventory.decode_identity(identity)[:3]):
            return None
        if pane["pane_dead"] == "0":
            process = Path("/proc", pane["pane_pid"], "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
            if process[-4:] != args[-4:] or "tmux_roam.cli" not in process:
                return None
    except (OSError, ValueError, IndexError):
        return None
    return pane


def _guarded_pane_command(pane, command):
    guard = "1"
    for key, value in pane.items():
        guard = "#{&&:" + guard + ",#{==:#{" + key + "}," + value + "}}"
    result = tmux.run(["if-shell", "-F", "-t", pane["pane_id"], guard, command,
                       "display-message -p TMUX_ROAM_PANE_CHANGED"])
    if "TMUX_ROAM_PANE_CHANGED" in result.stdout:
        raise RuntimeError("Connection pane changed; select it again")


def remove_disconnected(session, identity, *, ended=False):
    """Prune an unused failed carrier after confirmed absence or a healthy duplicate."""
    with (runtime_dir() / ("target-" + identity + ".lock")).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        token = tmux.run(["show-options", "-qv", "-t", session, STATE], check=False).stdout.strip()
        if _read_state(token).get("status") not in ("error", "disconnected"):
            return False
        # Discovery may have completed just before the healthy connection dropped.
        if not ended and not any(sid != session and ident == identity and
                   _read_state(tmux.run(["show-options", "-qv", "-t", sid, STATE], check=False).stdout.strip()).get("status") == "open"
                   for sid, ident in _owned_sessions()):
            return False
        pane = _managed_pane(session, identity, token)
        if pane is None or pane["session_attached"] != "0":
            return False
        # Removing a user-renamed entry is not automatic cleanup.
        name = tmux.run(["display-message", "-p", "-t", session, "#{session_name}"], check=False).stdout.strip()
        owned_name = tmux.run(["show-options", "-qv", "-t", session, "@tmux-roam-name"], check=False).stdout.strip()
        if name != owned_name or _read_state(token).get("status") not in ("error", "disconnected"):
            return False
        _guarded_pane_command(pane, shlex.join([
            "if-shell", "-F", "-t", pane["pane_id"], "#{==:#{session_name},#{@tmux-roam-name}}",
            "kill-session -t " + session]))
        return tmux.run(["has-session", "-t", session], check=False).returncode != 0


def create_remote(host, config, name, directory):
    """Explicitly create a detached session; ordinary attachment never creates one."""
    if not name or name != name.strip() or tmux.clean(name) != name or any(c in name for c in ".:;"):
        raise ValueError("Use a session name without leading/trailing spaces, dots, colons, semicolons or control characters")
    if not directory or tmux.clean(directory) != directory or not (directory.startswith(("/", "~/")) or directory == "~"):
        raise ValueError("Use an absolute remote directory or ~/path")
    # cd before invoking tmux avoids interpreting the directory as a tmux format.
    fields = "#{pid}|#{start_time}|#{session_id}|#{session_created}|#{socket_path}"
    command = ("set -eu; directory=" + shlex.quote(directory) + "; "
               "case \"$directory\" in '~') directory=$HOME;; '~/'*) directory=$HOME/${directory#'~/'};; esac; "
               "cd -- \"$directory\"; export LC_ALL=C.UTF-8; exec " +
               shlex.join(host.tmux_args() + ["new-session", "-d", "-P", "-F", fields, "-s", name]))
    code, stdout, stderr, failure = inventory.capture_bounded(
        inventory.ssh_base(host, config) + ["sh -c " + shlex.quote(command)], config.command_timeout_seconds)
    if failure or code:
        reason = failure or tmux.clean(stderr.decode("utf-8", "replace").strip())[:500] or f"SSH exit {code}"
        raise RuntimeError(f"{host.label}: {reason}. Refresh the picker before retrying if the connection was lost.")
    try:
        pid, started, sid, created, socket = stdout.decode("utf-8", "strict").strip().split("|", 4)
        if not pid.isdigit() or not started.isdigit() or not re.fullmatch(r"\$[0-9]+", sid) or not created.isdigit() or not socket.startswith("/"):
            raise ValueError("invalid identity")
        return inventory._records(host.id, host.label, (pid, started, socket),
                                  [dict(session_id=sid, created=int(created), name=name, windows=1, clients=0)], "live")[0]
    except (ValueError, UnicodeError):
        raise RuntimeError(f"{host.label}: creation response was incomplete; refresh the picker before retrying") from None


def delete_session(record, config):
    """Delete only the explicitly confirmed server/session incarnation."""
    host = None
    if record.host_id == "local":
        fresh, error = inventory.local()
    else:
        host = next((h for h in config.hosts if h.id == record.host_id and h.enabled), None)
        if host is None:
            raise RuntimeError("Host is disabled or no longer configured")
        fresh, error = inventory.remote(host, config)
    if error:
        raise RuntimeError(error)
    current = next((r for r in fresh if r.id == record.id), None)
    if current is None:
        raise RuntimeError("Session ended or server restarted; refresh the picker")
    pid, started = current.generation.split("/")
    # Evaluate the guard and kill in the same tmux command queue, without a shell
    # between validation and deletion. Names never select the target.
    guard = "1"
    for key, value in (("pid", pid), ("start_time", started),
                       ("session_id", current.session_id), ("session_created", str(current.created))):
        guard = "#{&&:" + guard + ",#{==:#{" + key + "}," + value + "}}"
    command = ["if-shell", "-F", "-t", current.session_id, guard,
               shlex.join(["kill-session", "-t", current.session_id]),
               "display-message -p TMUX_ROAM_STALE_SESSION"]
    if host:
        code, stdout, stderr, failure = inventory.capture_bounded(
            inventory.ssh_base(host, config) + [shlex.join(host.tmux_args() + command)],
            config.command_timeout_seconds)
        if failure or code:
            reason = failure or tmux.clean(stderr.decode("utf-8", "replace").strip())[:500] or f"SSH exit {code}"
            raise RuntimeError(reason + ". Refresh to check the outcome before retrying.")
        output = stdout.decode("utf-8", "replace")
    else:
        output = tmux.run(command).stdout
    if "TMUX_ROAM_STALE_SESSION" in output:
        raise RuntimeError("Session changed while deleting; refresh the picker")
    if host:
        inventory.save(host, [r for r in fresh if r.id != current.id])
        from . import native
        for session, identity in _owned_sessions():
            if identity == current.id:
                native.remove_inactive(session, identity, ended=True)


def open_remote(record, host, config, client, navigation_token=None):
    snapshot, error = inventory.remote_snapshot(host, config, True)
    if error:
        raise RuntimeError(f"{host.label}: {error}")
    fresh = snapshot.records
    if record.id not in {row.id for row in fresh}:
        raise RuntimeError(f"{host.label}: session ended or server restarted; refresh the picker")
    inventory.save(host, fresh)
    prefix = snapshot.prefixes[record.session_id] if host.remote_prefix == "auto" else host.remote_prefix
    if prefix not in ("M-a", "C-b", "None"):
        raise RuntimeError(f"unsupported remote prefix: {prefix}")
    lock_path = runtime_dir() / ("target-" + record.id + ".lock")
    owner = False
    local_id = None
    token = None
    while True:
        candidate = None
        with lock_path.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for sid, identity in _owned_sessions():
                if identity != record.id:
                    continue
                existing_token = tmux.run(["show-options", "-v", "-t", sid, STATE], check=False).stdout.strip()
                state = _read_state(existing_token)
                status = state.get("status")
                if status == "open" and snapshot.clients.get((state.get("pid"), state.get("tty"))) == record.session_id:
                    candidate = (sid, existing_token, "open", state)
                    break
                if status in ("preparing", "attaching") or not state:
                    candidate = (sid, existing_token, "preparing", state)
                elif candidate is None and status in ("idle", "error", "disconnected", "open"):
                    candidate = (sid, existing_token, "idle" if status == "idle" else "retry", state)
            if candidate is None or candidate[2] in ("idle", "retry"):
                owner = candidate is None
                token = uuid.uuid4().hex if owner or candidate[2] == "retry" else candidate[1]
                name = _safe_name(host.id, record.name)
                width, height = _client_size(client)
                source_root = str(Path(__file__).resolve().parent.parent)
                command = ["exec", "env", "PYTHONPATH=" + source_root,
                           "TMUX_ROAM_CONFIG=" + str(config.path),
                           "TMUX_ROAM_SSH_CONFIG=" + os.environ.get("TMUX_ROAM_SSH_CONFIG", ""),
                           sys.executable, "-P", "-m", "tmux_roam.cli",
                           "supervise", token, host.id, record.id]
                if owner:
                    result = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}", "-x", width,
                                       "-y", height, "-s", name + "-" + token[:6], shlex.join(command)])
                    local_id = result.stdout.strip()
                else:
                    local_id, previous_token, kind, state = candidate
                    managed = _managed_pane(local_id, record.id, previous_token) if kind == "retry" else None
                    pane = managed["pane_id"] if managed else state.get("pane", "")
                    if kind == "retry":
                        if managed is None:
                            raise RuntimeError("Connection pane was modified; cannot safely reconnect it")
                    else:
                        actual = tmux.run(["display-message", "-p", "-t", pane, "#{session_id}|#{pane_dead}"]).stdout.strip()
                        if not pane or actual != local_id + "|1":
                            raise RuntimeError("remote placeholder was modified; reopen or remove that local entry")
                    _write_state(token, {"status": "preparing"})
                    try:
                        if kind == "retry":
                            _guarded_pane_command(managed,
                                shlex.join(["respawn-pane", "-k", "-t", pane, shlex.join(command)]) + " ; " +
                                shlex.join(["set-option", "-t", local_id, STATE, token]))
                        else:
                            tmux.run(["respawn-pane", "-t", pane, shlex.join(command)])
                    except BaseException:
                        if kind == "idle":
                            _write_state(token, state)
                        raise
                tmux.run(["set-option", "-t", local_id, OWNER, "1"])
                tmux.run(["set-option", "-t", local_id, IDENTITY, record.id])
                tmux.run(["set-option", "-t", local_id, STATE, token])
                tmux.run(["set-option", "-t", local_id, "@tmux-roam-prefix", prefix])
                tmux.run(["set-option", "-t", local_id, "prefix", "None"])
                tmux.run(["set-option", "-t", local_id, "key-table", _routing_table(prefix)])
                # The remote tmux draws its own bar; the local carrier needs no extra row.
                tmux.run(["set-option", "-t", local_id, "status", "off"])
                break
        local_id, token, kind, state = candidate
        if kind == "preparing":
            break  # Observe the existing creation without holding its lock.
        if snapshot.clients.get((state.get("pid"), state.get("tty"))) == record.session_id:
            tmux.run(["set-option", "-t", local_id, "@tmux-roam-prefix", prefix])
            tmux.run(["set-option", "-t", local_id, "prefix", "None"])
            tmux.run(["set-option", "-t", local_id, "key-table", _routing_table(prefix)])
            tmux.run(["set-option", "-t", local_id, "status", "off"])
            if not tmux.client_exists(client) or not _navigation_current(client, navigation_token):
                raise RuntimeError("invoking client disappeared or navigation was cancelled")
            from . import native
            native.decorate(local_id, next(row for row in fresh if row.id == record.id), "connected")
            tmux.switch(client, local_id)
            return local_id
    deadline = time.monotonic() + config.command_timeout_seconds + config.connect_timeout_seconds + 3
    try:
        while time.monotonic() < deadline:
            if not _navigation_current(client, navigation_token):
                raise RuntimeError("navigation cancelled or superseded")
            state = _read_state(token)
            if state.get("status") == "open":
                confirmed, error = inventory.remote_snapshot(host, config)
                if not error and record.id not in {row.id for row in confirmed.records}:
                    raise RuntimeError(f"{host.label}: session ended or server restarted; refresh the picker")
                if not error and confirmed.clients.get((state.get("pid"), state.get("tty"))) == record.session_id:
                    if not tmux.client_exists(client) or not _navigation_current(client, navigation_token):
                        raise RuntimeError("invoking client disappeared or navigation was cancelled")
                    from . import native
                    native.decorate(local_id, next(row for row in confirmed.records if row.id == record.id), "connected")
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
        chooser = tmux.run(["show-options", "-gqv", "@tmux-roam-chooser-keys"], check=False).stdout.split() or ["s"]
        print(f"Enter: retry exact session · Alt+a {chooser[0]}: choose another session")
        try:
            input()
        except EOFError:
            return
