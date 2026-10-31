import base64
import concurrent.futures
import json
import os
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
    result = ["ssh"]
    if os.environ.get("TMUX_ROAM_SSH_CONFIG"):
        result += ["-F", os.environ["TMUX_ROAM_SSH_CONFIG"]]
    return result + ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={config.connect_timeout_seconds}",
            "-o", "ConnectionAttempts=1", "-o", "RemoteCommand=none",
            "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-tt" if tty else "-T", "--", host.ssh]


def _remote_command(host, commands):
    # OpenSSH sends a command string to the remote login shell. Quote every argument.
    command = " && ".join("LC_ALL=C.UTF-8 " + shlex.join(host.tmux_args() + cmd) for cmd in commands)
    return "sh -c " + shlex.quote(command)


def capture_bounded(command, timeout, output_limit=MAX_OUTPUT, error_limit=8192):
    try:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = f"command timed out after {timeout}s"
                break
            for key, _ in selector.select(min(remaining, .2)):
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
    finally:
        selector.close()
        proc.stdout.close()
        proc.stderr.close()
    return proc.returncode, bytes(out), bytes(err), failure


def remote(host: Host, config, reconcile=False):
    commands = ["LC_ALL=C.UTF-8 " + shlex.join(host.tmux_args() + args) for args in
                (["display-message", "-p", "#{pid}" + tmux.SEP + "#{start_time}" + tmux.SEP + "#{socket_path}"],
                 ["list-sessions", "-F", tmux.FORMAT])]
    commands += ["printf '%s\\n' " + CLIENT_MARKER,
                 "LC_ALL=C.UTF-8 " + shlex.join(host.tmux_args() + ["list-clients", "-F", tmux.CLIENT_FORMAT])]
    cmd = "sh -c " + shlex.quote(" && ".join(commands))
    code, stdout, stderr, failure = capture_bounded(ssh_base(host, config) + [cmd], config.command_timeout_seconds)
    if failure:
        return [], failure
    if code:
        return [], tmux.clean(stderr.decode("utf-8", "replace").strip())[:500] or f"SSH exit {code}"
    try:
        output = stdout.decode("utf-8", "strict").splitlines()
        server = output.pop(0).split(tmux.SEP, 2)
        if len(server) != 3 or not server[0].isdigit() or not server[1].isdigit() or not server[2].startswith("/"):
            raise ValueError("invalid server identity")
        if CLIENT_MARKER not in output:
            raise ValueError("missing client records")
        marker = output.index(CLIENT_MARKER)
        session_lines, client_lines = output[:marker], output[marker + 1:]
        entries = []
        for line in session_lines:
            head = line.split(tmux.SEP, 2)
            parts = head[:2] + head[2].rsplit(tmux.SEP, 2) if len(head) == 3 else head
            if (len(parts) != 5 or not parts[0].startswith("$") or not parts[0][1:].isdigit()
                    or not all(x.isdigit() for x in (parts[1], parts[3], parts[4]))):
                raise ValueError("invalid session record")
            entries.append(dict(session_id=parts[0], created=int(parts[1]),
                                name=tmux.clean(parts[2]), windows=int(parts[3]), clients=int(parts[4])))
        clients = []
        for line in client_lines:
            parts = line.split(tmux.SEP, 3)
            if len(parts) != 4 or not parts[0].isdigit() or not parts[2].startswith("$"):
                raise ValueError("invalid client record")
            clients.append(dict(pid=int(parts[0]), tty=parts[1], session_id=parts[2]))
        records = _records(host.id, host.label, server, entries, "live")
        _reconcile(host, records, clients, reconcile)
        return records, None
    except (UnicodeError, ValueError, IndexError) as exc:
        return [], f"malformed inventory from {host.id}: {exc}"


def _reconcile(host, records, clients, update):
    from . import connection
    actual = {(c["pid"], c["tty"]): c["session_id"] for c in clients}
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
            session = actual.get((state.get("pid"), state.get("tty")))
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
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return []


def save(host, records):
    path = _cache_path(host)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        with tmp.open("w") as file:
            os.chmod(tmp, 0o600)
            json.dump(dict(schema=SCHEMA, host_id=host.id, records=[asdict(r) for r in records]), file)
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
