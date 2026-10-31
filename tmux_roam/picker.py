import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import concurrent.futures
import hashlib
import uuid
from dataclasses import replace
import sys
from pathlib import Path

from . import inventory, tmux
from .config import hidden
from .connection import runtime_dir


def _navigation_path(client):
    server = "|".join(tmux.server())
    digest = hashlib.sha256((server + "|" + client).encode()).hexdigest()[:32]
    return runtime_dir() / ("navigation-" + digest)


def begin_navigation(client):
    token = uuid.uuid4().hex
    _atomic(_navigation_path(client), token.encode())
    return token


def navigation_valid(client, token):
    try:
        return _navigation_path(client).read_text() == token
    except (OSError, tmux.TmuxError):
        return False


def end_navigation(client, token):
    path = _navigation_path(client)
    if navigation_valid(client, token):
        path.unlink(missing_ok=True)


def _rows(records, errors, config, mode="All", include_hidden=False):
    seen = set()
    result = []
    current_client = os.environ.get("TMUX_ROAM_CLIENT")
    current_session = next((c["session_id"] for c in tmux.clients() if c["name"] == current_client), None) if current_client else None
    for rec in inventory.sorted_records(records, config):
        if mode == "Local" and rec.host_id != "local" or mode == "Remote" and rec.host_id == "local":
            continue
        if rec.id in seen or rec.host_id == "local" and hidden(rec.name, config) and not include_hidden:
            continue
        seen.add(rec.id)
        age = int(time.time() - rec.observed)
        state = ("current" if rec.host_id == "local" and rec.session_id == current_session else
                 rec.state or ("cached " + str(age) + "s" if rec.source == "cached" else "live"))
        if os.environ.get("TMUX_ROAM_COMPACT") == "1":
            label = f"{rec.host_label}/{rec.name} · {state}"
        else:
            label = f"{rec.host_label:<14.14} {rec.name:<28.28} {rec.windows:>3}w {rec.clients:>2}c  {state}"
        result.append(rec.id + "\t" + tmux.clean(label))
    for host, error in errors.items():
        if mode == "Local" and host != "local" or mode == "Remote" and host == "local":
            continue
        result.append("error:" + host + "\t" + tmux.clean(f"{host:<14.14} {error[:80]}"))
    return result


def _mode(root):
    try:
        return json.loads((root / "mode.json").read_text())
    except (OSError, ValueError):
        return {"filter": "All", "hidden": False}


def _atomic(path, payload):
    temp = path.with_suffix(".tmp")
    temp.write_bytes(payload)
    os.replace(temp, path)


def _write_snapshot(path, records, errors, config):
    mode = _mode(path.parent)
    lines = _rows(records, errors, config, mode["filter"], mode["hidden"])
    payload = b"\0".join(x.encode("utf-8") for x in lines) + (b"\0" if lines else b"")
    _atomic(path, payload)
    _atomic(path.parent / "metadata.json", json.dumps({"records": [r.__dict__ for r in records], "errors": errors}, ensure_ascii=False).encode())


def state_action(root, action):
    root = Path(root)
    mode = _mode(root)
    if action == "filter":
        modes = ("All", "Local", "Remote")
        mode["filter"] = modes[(modes.index(mode["filter"]) + 1) % len(modes)]
    elif action == "hidden":
        mode["hidden"] = not mode["hidden"]
    elif action == "refresh":
        (root / "refresh.request").touch()
        return
    _atomic(root / "mode.json", json.dumps(mode).encode())
    try:
        data = json.loads((root / "metadata.json").read_text())
        config = __import__("tmux_roam.config", fromlist=["load"]).load()
        records = [inventory.Record(**row) for row in data["records"]]
        _write_snapshot(root / "rows", records, data["errors"], config)
        sys.stdout.buffer.write((root / "rows").read_bytes())
    except (OSError, ValueError, KeyError):
        pass


def details(root, ident):
    try:
        data = json.loads((Path(root) / "metadata.json").read_text())
        row = next((r for r in data["records"] if r["id"] == ident), None)
        if not row:
            print(data["errors"].get(ident.removeprefix("error:"), "Session ended"))
            return
        for label, key in (("Host", "host_label"), ("Session", "name"), ("Session ID", "session_id"),
                           ("Windows", "windows"), ("Clients", "clients"), ("Socket", "socket"),
                           ("Observed", "observed"), ("Source", "source")):
            print(f"{label}: {row[key]}")
    except (OSError, ValueError):
        print("Details unavailable")


def help_text():
    print("Enter open · Esc cancel · Ctrl-r refresh · Ctrl-f filter · Ctrl-h hidden sessions")
    print("Tab details · F1 help · F2 actions")
    print("Managed remote: Alt+a s picker, Alt+a S local chooser, Alt+a twice then a remote key")
    print("F12 goes to the remote terminal in a managed connection.")
    input("\nPress Enter to return to the picker...")


def mode_header(root):
    mode = _mode(Path(root))
    return (f"{mode['filter']} · hidden {'shown' if mode['hidden'] else 'hidden'} | "
            "Enter open · Esc cancel · Ctrl-r refresh · Ctrl-f filter · Ctrl-h hidden · Tab details · F1 help · F2 actions")


def select_action(root, ident):
    try:
        data = json.loads((Path(root) / "metadata.json").read_text())
        row = next((r for r in data["records"] if r["id"] == ident), None)
    except (OSError, ValueError):
        row = None
    if row and row["source"] != "ended":
        return "accept"
    if ident.startswith("error:"):
        return "change-header(Host unavailable; press Tab for details or choose a session)"
    return "change-header(Session ended; choose another session explicitly)"


def actions(root, ident, config):
    root = Path(root)
    options = ["Open/retry", "Refresh host", "Close owned local connection", "Diagnostics"]
    env = os.environ.copy()
    for key in ("FZF_DEFAULT_OPTS", "FZF_DEFAULT_OPTS_FILE", "FZF_DEFAULT_COMMAND"):
        env.pop(key, None)
    result = subprocess.run(["fzf", "--prompt=Action> ", "--no-sort", "--height=10", "--reverse"],
                            input="\n".join(options) + "\n", text=True, capture_output=True, env=env)
    if result.returncode:
        return
    choice = result.stdout.strip()
    if choice == "Open/retry":
        if select_action(root, ident) == "accept":
            _post(root / "fzf.sock", "accept")
        return
    try:
        data = json.loads((root / "metadata.json").read_text())
        row = next((r for r in data["records"] if r["id"] == ident), None)
        host_id = row["host_id"] if row else ident.removeprefix("error:")
        if choice == "Refresh host":
            host = next((h for h in config.hosts if h.id == host_id and h.enabled), None)
            if host:
                records, error = inventory.remote(host, config, True)
                if error:
                    print(f"{host.label}: {error}")
                    input("Press Enter to return...")
                else:
                    inventory.save(host, records)
            (root / "refresh.request").touch()
        elif choice == "Close owned local connection":
            from . import connection
            owned = [(sid, identity) for sid, identity in connection._owned_sessions() if identity == ident]
            if not owned:
                print("No owned local connection for this session.")
            else:
                print(f"Close local connection to {row['host_label']}/{row['name']}?")
                print("Remote tmux work remains under the remote server's lifecycle policy.")
                if input("Type CLOSE to confirm: ") == "CLOSE":
                    active = {c["session_id"] for c in tmux.clients()}
                    for sid, _ in owned:
                        if sid in active:
                            print("A client is using this connection. Switch it to another session first.")
                            continue
                        tmux.run(["kill-session", "-t", sid])
                        print(f"Closed local connection {sid}.")
            input("Press Enter to return...")
        elif choice == "Diagnostics":
            print("Host:", host_id)
            if row:
                details(root, ident)
            if host_id in data["errors"]:
                print("Last error:", data["errors"][host_id])
            input("Press Enter to return...")
    except (OSError, ValueError, KeyError, tmux.TmuxError) as exc:
        print(f"Action failed: {exc}")
        input("Press Enter to return...")


def _post(sock_path, action):
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(.5)
            sock.connect(str(sock_path))
            payload = action.encode()
            sock.sendall(b"POST / HTTP/1.1\r\nHost: localhost\r\nContent-Type: text/plain\r\nContent-Length: " +
                         str(len(payload)).encode() + b"\r\n\r\n" + payload)
            sock.recv(256)
    except OSError:
        pass


def pick(config, client):
    os.environ["TMUX_ROAM_CLIENT"] = client
    if shutil.get_terminal_size((80, 24)).columns < 75:
        os.environ["TMUX_ROAM_COMPACT"] = "1"
    with tempfile.TemporaryDirectory(prefix="picker-", dir=runtime_dir()) as directory:
        os.chmod(directory, 0o700)
        root = Path(directory)
        snapshot = root / "rows"
        sock = root / "fzf.sock"
        _atomic(root / "mode.json", json.dumps({"filter": "All", "hidden": False}).encode())
        records, errors = inventory.all_records(config, refresh=False)
        _write_snapshot(snapshot, records, errors, config)
        rows_by_id = {rec.id: rec for rec in records if rec.source != "ended"}
        lock = threading.Lock()
        closed = threading.Event()

        def refresh_once():
            nonlocal records, errors, rows_by_id
            # Hosts complete independently so one slow host cannot hold back updates.
            local_rows, local_error = inventory.local()
            with lock:
                old_local = [r for r in records if r.host_id == "local"]
                new_ids = {r.id for r in local_rows}
                ended = [replace(r, source="ended", state="ended") for r in old_local if r.id not in new_ids]
                records = local_rows + ended + [r for r in records if r.host_id != "local"]
                if local_error:
                    errors["local"] = local_error
                else:
                    errors.pop("local", None)
                _write_snapshot(snapshot, records, errors, config)
                rows_by_id = {rec.id: rec for rec in records if rec.source != "ended"}
            if not closed.is_set():
                _post(sock, f"reload-sync(cat {snapshot})")
            with concurrent.futures.ThreadPoolExecutor(max_workers=config.concurrency) as pool:
              futures = {pool.submit(inventory.remote, host, config, True): host for host in config.hosts if host.enabled}
              for future in concurrent.futures.as_completed(futures):
                host = futures[future]
                if closed.is_set():
                    break
                remote_rows, error = future.result()
                with lock:
                    old_host = [r for r in records if r.host_id == host.id]
                    if error:
                        replacement = inventory.cached(host)
                    else:
                        new_ids = {r.id for r in remote_rows}
                        replacement = remote_rows + [replace(r, source="ended", state="ended")
                                                     for r in old_host if r.id not in new_ids]
                    records = [r for r in records if r.host_id != host.id] + replacement
                    if error:
                        errors[host.id] = error
                    else:
                        errors.pop(host.id, None)
                        inventory.save(host, remote_rows)
                    rows_by_id = {rec.id: rec for rec in records if rec.source != "ended"}
                    _write_snapshot(snapshot, records, errors, config)
                if not closed.is_set():
                    _post(sock, f"reload-sync(cat {snapshot})")

        def refresh():
            refresh_once()
            while not closed.is_set():
                for _ in range(config.refresh_seconds * 5):
                    if closed.wait(.2):
                        return
                    request = root / "refresh.request"
                    if request.exists():
                        request.unlink(missing_ok=True)
                        break
                refresh_once()

        worker = threading.Thread(target=refresh, daemon=True)
        worker.start()
        env = os.environ.copy()
        for key in ("FZF_DEFAULT_OPTS", "FZF_DEFAULT_OPTS_FILE", "FZF_DEFAULT_COMMAND"):
            env.pop(key, None)
        binary = str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam")
        base = binary + " picker-state " + str(root)
        command = ["fzf", "--read0", "--print0", "--delimiter=\t", "--with-nth=2..", "--id-nth=1",
                   "--track", "--no-multi", "--no-sort", "--no-clear", "--listen=" + str(sock),
                   "--header=" + mode_header(root),
                   "--prompt=Sessions> ",
                   "--preview=" + binary + " details " + str(root) + " {1}",
                   "--preview-window=down:40%" + ("" if config.details else ":hidden"),
                   "--bind=enter:transform(" + binary + " picker-select " + str(root) + " {1}),"
                   "tab:toggle-preview,ctrl-r:execute-silent(" + base + " refresh),"
                   "ctrl-f:reload-sync(" + base + " filter)+transform-header(" + binary + " picker-header " + str(root) + "),"
                   "ctrl-h:reload-sync(" + base + " hidden)+transform-header(" + binary + " picker-header " + str(root) + "),"
                   "f1:execute(" + binary + " help),"
                   "f2:execute(" + binary + " actions " + str(root) + " {1})"]
        try:
            result = subprocess.run(command, input=snapshot.read_bytes(), stdout=subprocess.PIPE, env=env)
        finally:
            closed.set()
        if result.returncode != 0 or not result.stdout:
            return None
        chosen = result.stdout.split(b"\0", 1)[0].decode("utf-8", "replace").split("\t", 1)[0]
        if chosen.startswith("error:"):
            raise RuntimeError(errors.get(chosen[6:], "host unavailable"))
        # Re-discovery in open_remote guards against a stale cached record.
        with lock:
            record = rows_by_id.get(chosen)
        if record is None:
            raise RuntimeError("selected session ended; open the picker again")
        return record
