import base64
import concurrent.futures
import hashlib
import json
import os
import re
import shlex
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from . import tmux
from .config import Host


SCHEMA = 1
MAX_OUTPUT = 1024 * 1024
CLIENT_MARKER = "TMUX_ROAM_CLIENTS_V1"
WINDOW_MARKER = "TMUX_ROAM_WINDOWS_V1"
WINDOW_FORMAT = "#{session_id}|#{window_id}|#{window_index}|#{window_active}|#{window_name}"


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tmux-roam"


def identity(host, socket, generation, session_id, created):
    payload = json.dumps([host, socket, generation, session_id, created], ensure_ascii=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_identity(value):
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (not isinstance(decoded, list) or len(decoded) != 5 or
                not all(isinstance(x, str) for x in decoded[:4]) or type(decoded[4]) is not int):
            raise ValueError("bad identity")
        return decoded
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid opaque session ID") from exc


@dataclass
class Record:
    id: str
    host_id: str
    host_label: str
    socket: str
    generation: str
    session_id: str
    created: int
    name: str
    windows: int
    clients: int
    observed: float
    source: str
    state: str = ""
    window_details: list[dict] | None = None


@dataclass
class RemoteSnapshot:
    records: list[Record]
    clients: dict[tuple[int, str], str]
    prefixes: dict[str, str]


def _records(host_id, label, server, entries, source, observed=None):
    pid, started, socket = server
    generation = f"{pid}/{started}"
    stamp = observed or time.time()
    return [Record(identity(host_id, socket, generation, e["session_id"], e["created"]),
                   host_id, label, socket, generation, e["session_id"], e["created"],
                   e["name"], e["windows"], e["clients"], stamp, source) for e in entries]


def local():
    try:
        server = tmux.server()
        entries = tmux.sessions()
        ownership = tmux.run(["list-sessions", "-F", "#{session_id}|#{@tmux-roam-owner}"], check=False)
        owned = {line.split("|", 1)[0] for line in ownership.stdout.splitlines() if line.endswith("|1")}
        return _records("local", "local", server, [e for e in entries if e["session_id"] not in owned], "live"), None
    except tmux.TmuxError as exc:
        return [], str(exc)


def ssh_base(host, config, tty=False):
    from .connection import runtime_dir

    result = ["ssh"]
    ssh_config = os.environ.get("TMUX_ROAM_SSH_CONFIG", "")
    if ssh_config:
        result += ["-F", ssh_config]
    profile = hashlib.sha256((host.ssh + "\0" + ssh_config).encode()).hexdigest()[:8]
    control = str(runtime_dir() / ("ssh-" + profile + "-%C"))
    # OpenSSH appends a temporary suffix when binding the Linux Unix socket.
    # Long custom runtime paths still work, with ordinary SSH connections.
    if len(os.fsencode(control.replace("%C", "0" * 40))) <= 90:
        result += ["-o", "ControlMaster=auto", "-o", "ControlPersist=60",
                   "-o", "ControlPath=" + control]
    return result + ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={config.connect_timeout_seconds}",
            "-o", "ConnectionAttempts=1", "-o", "RemoteCommand=none",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-tt" if tty else "-T", "--", host.ssh]


def capture_bounded(command, timeout, output_limit=MAX_OUTPUT, error_limit=8192, cancel=None, input_file=None):
    if cancel is not None and cancel.is_set():
        return None, b"", b"", "command cancelled"
    try:
        proc = subprocess.Popen(command, stdin=input_file if input_file is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return None, b"", b"", str(exc)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "out")
    selector.register(proc.stderr, selectors.EVENT_READ, "err")
    out, err = bytearray(), bytearray()
    deadline = time.monotonic() + timeout
    failure = None
    try:
        while selector.get_map():
            if cancel is not None and cancel.is_set():
                failure = "command cancelled"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = f"command timed out after {timeout}s"
                break
            for key, _ in selector.select(min(remaining, .1)):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                target = out if key.data == "out" else err
                target.extend(block)
                if len(out) > output_limit or len(err) > error_limit:
                    failure = "remote response exceeded size limit"
                    break
            if failure:
                break
        if failure:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            failure = failure or "command did not exit"
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        raise
    finally:
        selector.close()
        proc.stdout.close()
        proc.stderr.close()
    return proc.returncode, bytes(out), bytes(err), failure


def remote_snapshot(host: Host, config, reconcile=False, cancel=None):
    commands = ["LC_ALL=C.UTF-8 " + shlex.join(host.tmux_args() + args) for args in
                (["display-message", "-p", "#{pid}" + tmux.SEP + "#{start_time}" + tmux.SEP + "#{socket_path}"],
                 ["list-sessions", "-F", tmux.FORMAT + "|#{prefix}"])]
    commands += ["printf '%s\\n' " + CLIENT_MARKER,
                 "LC_ALL=C.UTF-8 " + shlex.join(host.tmux_args() + ["list-clients", "-F", tmux.CLIENT_FORMAT]),
                 "printf '%s\\n' " + WINDOW_MARKER,
                 "LC_ALL=C.UTF-8 " + shlex.join(host.tmux_args() + ["list-windows", "-a", "-F", WINDOW_FORMAT])]
    cmd = "sh -c " + shlex.quote(" && ".join(commands))
    code, stdout, stderr, failure = capture_bounded(ssh_base(host, config) + [cmd], config.command_timeout_seconds,
                                                  cancel=cancel)
    if failure:
        return None, failure
    if code:
        return None, tmux.clean(stderr.decode("utf-8", "replace").strip())[:500] or f"SSH exit {code}"
    try:
        output = stdout.decode("utf-8", "strict").splitlines()
        server = output.pop(0).split(tmux.SEP, 2)
        if len(server) != 3 or not server[0].isdigit() or not server[1].isdigit() or not server[2].startswith("/"):
            raise ValueError("invalid server identity")
        if CLIENT_MARKER not in output:
            raise ValueError("missing client records")
        marker = output.index(CLIENT_MARKER)
        session_lines, client_lines = output[:marker], output[marker + 1:]
        if WINDOW_MARKER not in client_lines:
            raise ValueError("missing window records")
        marker = client_lines.index(WINDOW_MARKER)
        client_lines, window_lines = client_lines[:marker], client_lines[marker + 1:]
        entries = []
        prefixes = {}
        for line in session_lines:
            head = line.split(tmux.SEP, 2)
            parts = head[:2] + head[2].rsplit(tmux.SEP, 3) if len(head) == 3 else head
            if (len(parts) != 6 or not parts[0].startswith("$") or not parts[0][1:].isdigit()
                    or not all(x.isdigit() for x in (parts[1], parts[3], parts[4]))):
                raise ValueError("invalid session record")
            entries.append(dict(session_id=parts[0], created=int(parts[1]),
                                name=tmux.clean(parts[2]), windows=int(parts[3]), clients=int(parts[4])))
            prefixes[parts[0]] = parts[5]
        clients = {}
        for line in client_lines:
            parts = line.split(tmux.SEP, 3)
            if len(parts) != 4 or not parts[0].isdigit() or not parts[2].startswith("$"):
                raise ValueError("invalid client record")
            clients[(int(parts[0]), parts[1])] = parts[2]
        records = _records(host.id, host.label, server, entries, "live")
        by_session = {record.session_id: record for record in records}
        for record in records:
            record.window_details = []
        for line in window_lines:
            parts = line.split(tmux.SEP, 4)
            if (len(parts) != 5 or not re.fullmatch(r"\$[0-9]+", parts[0]) or
                    not re.fullmatch(r"@[0-9]+", parts[1]) or not parts[2].isdigit() or parts[3] not in ("0", "1")):
                raise ValueError("invalid window record")
            record = by_session.get(parts[0])
            if record is not None:  # A session may be created between the two listings.
                record.window_details.append(dict(id=parts[1], index=int(parts[2]), active=parts[3] == "1",
                                                  name=tmux.clean(parts[4])))
        _reconcile(host, records, clients, reconcile)
        return RemoteSnapshot(records, clients, prefixes), None
    except (UnicodeError, ValueError, IndexError) as exc:
        return None, f"malformed inventory from {host.id}: {exc}"


def remote(host: Host, config, reconcile=False, cancel=None):
    snapshot, error = remote_snapshot(host, config, reconcile, cancel)
    return (snapshot.records if snapshot else []), error


def _reconcile(host, records, clients, update):
    from . import connection
    by_session = {r.session_id: r for r in records}
    counts = {}
    try:
        owned = connection._owned_sessions()
    except tmux.TmuxError:
        return
    for local_id, old_id in owned:
        try:
            old_host, _, generation, _, _ = decode_identity(old_id)
            if old_host != host.id or generation != (records[0].generation if records else generation):
                continue
            token = tmux.run(["show-options", "-v", "-t", local_id, connection.STATE], check=False).stdout.strip()
            state = connection._read_state(token)
            session = clients.get((state.get("pid"), state.get("tty")))
            target = by_session.get(session)
            if target:
                counts[target.id] = counts.get(target.id, 0) + 1
                if update and old_id != target.id:
                    tmux.run(["set-option", "-t", local_id, connection.IDENTITY, target.id])
        except (ValueError, RuntimeError, tmux.TmuxError):
            continue
    for record in records:
        if record.id in counts:
            record.state = "open here" + (f" ({counts[record.id]})" if counts[record.id] > 1 else "")


def _cache_path(host):
    return cache_dir() / (host.id + ".json")


def cached(host):
    try:
        data = json.loads(_cache_path(host).read_text())
        if data.get("schema") != SCHEMA or data.get("host_id") != host.id:
            return []
        result = [Record(**row) for row in data["records"]]
        for row in result:
            row.source = "cached"
            row.state = ""
            row.window_details = data.get("windows", {}).get(row.id, row.window_details)
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save(host, records):
    path = _cache_path(host)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    rows, windows = [], {}
    for record in records:
        row = asdict(record)
        details = row.pop("window_details")
        rows.append(row)
        if details is not None:
            windows[record.id] = details
    try:
        with tmp.open("w") as file:
            os.chmod(tmp, 0o600)
            # Keep record dictionaries readable by pre-window plugin versions.
            json.dump(dict(schema=SCHEMA, host_id=host.id, records=rows, windows=windows), file)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def all_records(config, refresh=True):
    local_rows, local_error = local()
    hosts = [h for h in config.hosts if h.enabled]
    rows = list(local_rows)
    errors = {}
    if local_error:
        errors["local"] = local_error
    if refresh and hosts:
        with concurrent.futures.ThreadPoolExecutor(max_workers=config.concurrency) as pool:
            futures = {pool.submit(remote, host, config): host for host in hosts}
            for future in concurrent.futures.as_completed(futures):
                host = futures[future]
                remote_rows, error = future.result()
                if error:
                    errors[host.id] = error
                    rows.extend(cached(host))
                else:
                    save(host, remote_rows)
                    rows.extend(remote_rows)
    else:
        for host in hosts:
            rows.extend(cached(host))
    return sorted_records(rows, config), errors


def sorted_records(rows, config):
    order = {host.id: index + 1 for index, host in enumerate(config.hosts)}
    return sorted(rows, key=lambda r: (order.get(r.host_id, 0), r.name.casefold(), r.session_id))
