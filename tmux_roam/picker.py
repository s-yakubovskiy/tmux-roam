import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import concurrent.futures
import uuid
import unicodedata
from dataclasses import replace
import sys
from pathlib import Path

from . import inventory, tmux
from .config import hidden
from .connection import runtime_dir


def _glyphs(text):
    cluster = ""
    for char in text:
        attached = (unicodedata.category(char) in ("Mn", "Me", "Cf") or
                    "\U0001f3fb" <= char <= "\U0001f3ff" or cluster.endswith("\u200d") or
                    len(cluster) == 1 and "\U0001f1e6" <= cluster <= "\U0001f1ff" and
                    "\U0001f1e6" <= char <= "\U0001f1ff")
        if cluster and not attached:
            yield cluster
            cluster = ""
        cluster += char
    if cluster:
        yield cluster


def _width(text):
    return sum(2 if "\u200d" in glyph or "\ufe0f" in glyph or "\U0001f1e6" <= glyph[0] <= "\U0001f1ff"
               else sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
                        for c in glyph if unicodedata.category(c) not in ("Mn", "Me", "Cf")
                        and not "\U0001f3fb" <= c <= "\U0001f3ff") for glyph in _glyphs(text))


def _fit(text, width, right=False):
    text = tmux.clean(str(text))
    if _width(text) > width:
        shortened = ""
        for glyph in _glyphs(text):
            if _width(shortened + glyph) > width - 1:
                break
            shortened += glyph
        text = shortened + "…"
    padding = " " * max(0, width - _width(text))
    return padding + text if right else text + padding


def _columns():
    available = int(os.environ.get("TMUX_ROAM_COLUMNS", shutil.get_terminal_size((96, 24)).columns)) - 6
    if available < 54:
        return 10, max(8, available - 25), 0, 11
    return 12, max(12, available - 37), 7, 12


def _table_row(host, name, windows, state):
    widths = _columns()
    return "  ".join(_fit(text, width, right=index == 2) for index, (text, width)
                     in enumerate(zip((host, name, windows, state), widths)) if width).rstrip()


def _paint(text, foreground, background=None):
    def rgb(value):
        return ";".join(str(int(value[i:i + 2], 16)) for i in (1, 3, 5))
    codes = "38;2;" + rgb(foreground)
    if background:
        codes += ";48;2;" + rgb(background)
    return "\x1b[" + codes + "m" + text + "\x1b[0m"


def popup_size(config, columns, lines, count):
    width = min(columns - 2, 96, max(42, int(columns * int(config.width.rstrip("%")) / 100)))
    height = min(lines - 2, max(12, int(lines * int(config.height.rstrip("%")) / 100)),
                 max(16, min(24, count + 12)))
    return str(width), str(height)


def _footer(prompt, navigation=True):
    if not navigation:
        return "Enter Continue    Esc Cancel"
    if prompt != "Sessions":
        return "j/k Move    / Search    Enter Select    Esc Back"
    if int(os.environ.get("TMUX_ROAM_COLUMNS", "96")) < 84:
        return "\nj/k Move   / Search   Enter Open\ndd Delete   Ctrl-n New   Esc Close"
    return "\n" + "\n".join("".join(f"{label:<20}" for label in row).rstrip() for row in (
        ("j/k Move", "/ Search", "Enter Open", "Ctrl-n New session"),
        ("dd Delete", "Tab Details", "F2 Actions", "Esc Close")))


def _fzf_env():
    env = os.environ.copy()
    for key in ("FZF_DEFAULT_OPTS", "FZF_DEFAULT_OPTS_FILE", "FZF_DEFAULT_COMMAND"):
        env.pop(key, None)
    return env


def _fzf_options(prompt, navigation=True, delete_command=None, footer=None):
    """Use fzf's input toggle and bindings for vi mode in every picker menu."""
    chooser = tmux.run(["show-options", "-gqv", "@tmux-roam-chooser-keys"], check=False).stdout.split() or ["s", "ы"]
    toggle = [key.lower() if key.startswith("F") and key[1:].isdigit() else key for key in chooser]
    toggle_keys = ",".join(toggle)
    disarm = "unbind(" + toggle_keys + ")"
    normal_prompt, search_prompt = prompt + " [vi]> ", "Search> " if prompt == "Sessions" else prompt + "> "
    footer = _footer(prompt, navigation) if footer is None else footer
    movement = {"j": "down", "о": "down", "k": "up", "л": "up",
                "g": "first", "п": "first", "G": "last", "П": "last",
                "ctrl-d": "half-page-down", "ctrl-u": "half-page-up"}
    mode_keys = ",".join([*movement, "i", "ш", "/", ".", "q", "й"])
    if delete_command:
        mode_keys += ",d,в"
    footer_command = ('if [ -n "$FZF_QUERY" ]; then printf "Filter: %s\\n" "$FZF_QUERY"; fi; printf %s ' +
                      shlex.quote(footer))
    search_footer = "Enter Select    Esc Back to navigation"
    empty = "No matching sessions\n" if prompt == "Sessions" else "No matches\n"
    new_hint = "  Ctrl-n New" if prompt == "Sessions" else ""
    result_footer = ('if [ "${FZF_MATCH_COUNT:-1}" = 0 ]; then if [ "$FZF_PROMPT" = ' +
                     shlex.quote(search_prompt) + ' ]; then printf %s ' + shlex.quote(empty + "Edit search  Esc Back" + new_hint) +
                     '; else printf %s ' + shlex.quote(empty + "/ Search  Esc Close" + new_hint) + '; fi' +
                     '; elif [ "$FZF_PROMPT" = ' + shlex.quote(search_prompt) +
                     ' ]; then printf %s ' + shlex.quote(search_footer) + '; else ' + footer_command + '; fi')
    normal = (f"hide-input+change-list-label()+change-prompt({normal_prompt})+rebind({mode_keys})+{disarm}" +
              "+transform-footer~" + result_footer + "~")
    search = (f"show-input+change-prompt({search_prompt})+unbind({mode_keys})+{disarm}" +
              "+transform-footer~" + result_footer + "~")
    close = shlex.join(tmux.args() + ["display-popup", "-C", "-c"]) + ' "$TMUX_ROAM_CLIENT"'
    bindings = [f"{key}:{action}+{disarm}" for key, action in movement.items()]
    bindings += [f"{key}:{search}" for key in ("i", "ш", "/", ".")]
    bindings += ["q:abort", "й:abort", "alt-a:rebind(" + toggle_keys + ")",
                 "alt-ф:rebind(" + toggle_keys + ")", "change:" + disarm]
    bindings += [f"{key}:execute-silent({close})+abort" for key in toggle]
    escape = ('if [ "$FZF_PROMPT" = ' + shlex.quote(search_prompt) + ' ]; then printf %s ' +
              shlex.quote(normal) + '; else printf abort; fi')
    if delete_command:
        # The unused list label marks the pending operator without changing the
        # footer. fzf's previous action changes on any intervening key.
        pending = "change-list-label(dd)"
        delete = ('if [ "$FZF_ACTION" = change-list-label ]; then printf %s ' +
                  shlex.quote("execute(" + delete_command + ")+" + normal) +
                  '; else printf %s ' + shlex.quote(pending) + '; fi')
        bindings += [key + ":transform@" + delete + "@" for key in ("d", "в")]
        escape = ('if [ "$FZF_ACTION" = change-list-label ]; then printf %s ' +
                  shlex.quote(normal) + '; else ' + escape + '; fi')
    bindings += ["esc:transform@" + escape + "@", "start:" + (disarm if navigation else f"unbind({mode_keys})+{disarm}")]
    if navigation:
        bindings += ["result:transform-footer~" + result_footer + "~"]
    return ["--reverse", "--prompt=" + (normal_prompt if navigation else search_prompt),
            "--bind=" + ",".join(bindings), "--ansi", "--highlight-line", "--padding=1,2",
            "--pointer=▶", "--gutter= ", "--info=hidden", "--no-separator", "--header-first",
            "--footer=" + footer, "--footer-border=top",
            "--color=fg:#cad3f5,bg:#24273a,fg+:#ffffff,bg+:#5b6078,hl:#f5a97f,hl+:#f5a97f,"
            "header:#939ab7,footer:#939ab7,border:#494d64,pointer:#ffffff,prompt:#cad3f5,spinner:#a6da95"
            ] + (["--no-input"] if navigation else [])


def _input(prompt):
    # A small fzf input field keeps the popup toggle available during forms too.
    result = subprocess.run(["fzf", "--disabled", "--height=8", *_fzf_options(prompt.rstrip(": "), False),
                             "--bind=enter:print-query,esc:abort"], input="", text=True,
                            capture_output=True, env=_fzf_env())
    if result.returncode:
        raise EOFError
    return result.stdout.rstrip("\n")


def _navigation_option(client):
    # A tmux option lets native bindings supersede navigation without Python.
    return "@tmux-roam-navigation-" + client


def begin_navigation(client):
    token = uuid.uuid4().hex
    tmux.run(["set-option", "-g", _navigation_option(client), token])
    return token


def navigation_valid(client, token):
    try:
        return tmux.run(["show-options", "-gqv", _navigation_option(client)]).stdout.strip() == token
    except (OSError, tmux.TmuxError):
        return False


def end_navigation(client, token):
    if navigation_valid(client, token):
        tmux.run(["set-option", "-gu", _navigation_option(client)], check=False)


def _current_target():
    current_client = os.environ.get("TMUX_ROAM_CLIENT")
    current_session = next((c["session_id"] for c in tmux.clients() if c["name"] == current_client), None) if current_client else None
    current_remote = tmux.run(["show-options", "-qv", "-t", current_session, "@tmux-roam-identity"], check=False).stdout.strip() if current_session else ""
    return current_session, current_remote


def _rows(records, errors, config, mode="All", include_hidden=False):
    seen = set()
    result = []
    current_session, current_remote = _current_target()
    host_colors = {host.id: ("#f5a97f", "#a6da95")[index % 2] for index, host in enumerate(config.hosts)}
    host_labels = {host.id: host.label for host in config.hosts}
    visible_hosts = set()
    for rec in inventory.sorted_records(records, config):
        if mode == "Local" and rec.host_id != "local" or mode == "Remote" and rec.host_id == "local":
            continue
        if rec.id in seen or rec.host_id == "local" and hidden(rec.name, config) and not include_hidden:
            continue
        seen.add(rec.id)
        visible_hosts.add(rec.host_id)
        is_current = rec.host_id == "local" and rec.session_id == current_session or rec.id == current_remote
        state = ("Offline" if rec.host_id in errors else
                 "Current" if is_current else
                 "Ended" if rec.source == "ended" else
                 "Connected" if rec.state.startswith("open here") else "Checking" if rec.source == "cached" else "Ready")
        host = "Local" if rec.host_id == "local" else rec.host_label
        host_text = _fit(" " + host, _columns()[0])
        display_name = tmux.clean(rec.name)
        first = next(_glyphs(display_name), "")
        if first and ord(first[0]) >= 0x1f000 and len(display_name) > len(first) and not display_name[len(first)].isspace():
            display_name = first + " " + display_name[len(first):]
        if is_current:
            display_name = _fit(display_name, _columns()[1] - 2).rstrip() + " *"
        label = _table_row(" " + host, display_name, rec.windows, state)
        label = _paint(host_text, "#24273a", host_colors.get(rec.host_id, "#939ab7")) + label[len(host_text):]
        result.append(rec.id + "\t" + label)
    for host, error in errors.items():
        if mode == "Local" and host != "local" or mode == "Remote" and host == "local":
            continue
        if host not in visible_hosts:
            result.append("error:" + host + "\t" + _table_row(host_labels.get(host, "Local" if host == "local" else host),
                                                              "Host unavailable (Ctrl-r retries)", "—", "Offline"))
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
    deleted = path.parent / "deleted"
    removed = set(deleted.read_text().splitlines()) if deleted.exists() else set()
    records = [r for r in records if r.id not in removed]
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
    chooser = tmux.run(["show-options", "-gqv", "@tmux-roam-chooser-keys"], check=False).stdout.split() or ["s"]
    native = tmux.run(["show-options", "-gqv", "@tmux-roam-native-keys"], check=False).stdout.split() or ["S"]
    print("j/k move · g/G first/last · Ctrl-u/d half page · / or i search · Esc back/close · q close")
    print("Enter open · Ctrl-r refresh · Ctrl-f filter · Ctrl-h hidden sessions")
    print("dd delete session (вв in Russian) · Ctrl-n new remote session · Tab details · F1 help · F2 actions")
    print(f"Alt+a {chooser[0]} toggles this popup; Alt+ф with the Russian chooser key works too.")
    print(f"Managed remote: Alt+a {chooser[0]} picker, Alt+a {native[0]} local chooser, Alt+a twice preserves the remote double-prefix action")
    print("Alt+a F12 then a key sends that command directly to the remote tmux.")
    print("F12 goes to the remote terminal in a managed connection.")
    _input("Press Enter to return to the picker")


def mode_header(root):
    mode = _mode(Path(root))
    scope = {"All": "All hosts", "Local": "This machine", "Remote": "Remote hosts"}[mode["filter"]]
    if mode["hidden"]:
        scope += "  (including hidden sessions)"
    return scope + "\n\n" + _table_row(" Host", "Session", "Windows", "Status") + "\n "


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


def new_session(root, ident, config):
    from .connection import create_remote

    root = Path(root)
    creating = False
    try:
        data = json.loads((root / "metadata.json").read_text())
        row = next((r for r in data["records"] if r["id"] == ident), None)
        preferred = row["host_id"] if row else ident.removeprefix("error:")
        hosts = sorted((h for h in config.hosts if h.enabled), key=lambda h: h.id != preferred)
        if not hosts:
            raise ValueError("No enabled remote hosts are configured")
        result = subprocess.run(["fzf", "--no-sort", "--height=10", *_fzf_options("Create on host"),
                                 "--delimiter=\t", "--with-nth=2..", "--header=Choose a host for the new session"],
                                input="\n".join(h.id + "\t" + h.label for h in hosts) + "\n",
                                text=True, capture_output=True, env=_fzf_env())
        if result.returncode:
            return
        host = next(h for h in hosts if h.id == result.stdout.split("\t", 1)[0])
        print(f"\nNew session on {host.label}")
        name = _input("Session name (empty cancels): ").strip()
        if not name:
            return
        directory = _input("Remote project directory [~]: ").strip() or "~"
        print(f"Creating {host.label}/{name}…", flush=True)
        creating = True
        record = create_remote(host, config, name, directory)
        _atomic(root / "created.json", json.dumps(record.__dict__, ensure_ascii=False).encode())
        (root / "refresh.request").touch()
    except (EOFError, KeyboardInterrupt):
        (root / "created.json").unlink(missing_ok=True)
        print("\nStopped waiting; refresh the picker to check whether the session was created." if creating else "\nCancelled.")
    except (OSError, ValueError, RuntimeError, tmux.TmuxError) as exc:
        print(f"Cannot create session: {exc}")
        try:
            _input("Press Enter to return to the picker")
        except (EOFError, KeyboardInterrupt):
            pass


def delete_session(root, ident, config):
    from .connection import delete_session as delete

    root = Path(root)
    try:
        data = json.loads((root / "metadata.json").read_text())
        row = next((r for r in data["records"] if r["id"] == ident), None)
        if not row or row["source"] == "ended":
            raise RuntimeError("Session is no longer available. Refresh the picker.")
        record = inventory.Record(**row)
        host = "Local" if record.host_id == "local" else record.host_label
        header = (f"Delete session?\n\n{host} / {record.name}\n\n"
                  f"Closes all {record.windows} windows and stops their programs.\n ")
        result = subprocess.run(["fzf", "--no-sort", *_fzf_options("Confirm deletion"),
                                 "--header=" + header], input="Cancel\nDelete session\n",
                                text=True, capture_output=True, env=_fzf_env())
        if result.returncode or result.stdout.strip() != "Delete session":
            return
        print(f"Deleting {host}/{record.name}…", flush=True)
        delete(record, config)
        with (root / "deleted").open("a") as file:
            file.write(ident + "\n")
        (root / "refresh.request").touch()
    except (OSError, ValueError, RuntimeError, tmux.TmuxError) as exc:
        print(f"Cannot delete session: {exc}")
        _input("Press Enter to return to the picker")


def sync_config(config, preferred=""):
    from . import config_sync

    hosts = sorted((h for h in config.hosts if h.enabled), key=lambda h: h.id != preferred)
    if not hosts:
        raise RuntimeError("No enabled remote hosts are configured")
    rows = [h.id + "\t" + h.label for h in hosts] + ["__all__\tAll enabled hosts"]
    result = subprocess.run(["fzf", "--no-sort", *_fzf_options("Sync to host"),
                             "--delimiter=\t", "--with-nth=2..", "--header=Choose where to sync ~/.tmux.conf"],
                            input="\n".join(rows) + "\n", text=True, capture_output=True, env=_fzf_env())
    if result.returncode:
        return
    target = result.stdout.split("\t", 1)[0]
    hosts = [h for h in hosts if target == "__all__" or h.id == target]
    path, content = config_sync.source()
    pending, previews = [], []
    for host in hosts:
        print(f"Comparing {host.label}…", flush=True)
        try:
            remote, diff = config_sync.compare(host, config, path, content)
            if diff or remote["sha256"] is None:
                pending.append((host, remote["sha256"]))
                previews.append(diff or host.label + ": remote file is missing\n")
            else:
                previews.append(host.label + ": already matches\n")
        except (OSError, ValueError, RuntimeError) as exc:
            previews.append(host.label + ": unavailable — " + str(exc) + "\n")
    if not pending:
        print("\n".join(previews))
        _input("Press Enter to return")
        return
    labels = ", ".join(h.label for h, _ in pending)
    with tempfile.NamedTemporaryFile("w", dir=runtime_dir()) as preview:
        preview.write("\n".join(previews))
        preview.flush()
        result = subprocess.run(["fzf", "--no-sort", *_fzf_options("Confirm sync", footer="Ctrl-u/d Scroll diff    j/k Choose    Enter Confirm    Esc Cancel"),
                                 "--header=Sync ~/.tmux.conf to " + labels + "? Backups are kept.",
                                 "--preview=" + shlex.join(["cat", preview.name]), "--preview-window=up:70%:wrap",
                                 "--bind=ctrl-u:preview-half-page-up,ctrl-d:preview-half-page-down"],
                                input="Cancel\nSync config\n", text=True, capture_output=True, env=_fzf_env())
    if result.returncode or result.stdout.strip() != "Sync config":
        return
    for host, expected in pending:
        print(f"Syncing {host.label}…", flush=True)
        try:
            print(config_sync.describe(config_sync.sync(host, config, content, expected=expected)))
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"{host.label}: {exc}")
    _input("Press Enter to return")


def actions(root, ident, config):
    root = Path(root)
    options = ["Open/retry", "New remote session", "Delete session", "Refresh host", "Sync tmux config", "Close owned local connection", "Diagnostics"]
    result = subprocess.run(["fzf", "--no-sort", "--height=14", *_fzf_options("Action"),
                             "--header=Session actions"],
                            input="\n".join(options) + "\n", text=True, capture_output=True, env=_fzf_env())
    if result.returncode:
        return
    choice = result.stdout.strip()
    if choice == "New remote session":
        new_session(root, ident, config)
        return
    if choice == "Delete session":
        delete_session(root, ident, config)
        return
    if choice == "Open/retry":
        if select_action(root, ident) == "accept":
            _post(root / "fzf.sock", "accept")
        return
    try:
        data = json.loads((root / "metadata.json").read_text())
        row = next((r for r in data["records"] if r["id"] == ident), None)
        host_id = row["host_id"] if row else ident.removeprefix("error:")
        if choice == "Sync tmux config":
            sync_config(config, host_id)
        elif choice == "Refresh host":
            host = next((h for h in config.hosts if h.id == host_id and h.enabled), None)
            if host:
                records, error = inventory.remote(host, config, True)
                if error:
                    print(f"{host.label}: {error}")
                    _input("Press Enter to return")
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
                if _input("Type CLOSE to confirm: ") == "CLOSE":
                    active = {c["session_id"] for c in tmux.clients()}
                    for sid, _ in owned:
                        if sid in active:
                            print("A client is using this connection. Switch it to another session first.")
                            continue
                        tmux.run(["kill-session", "-t", sid])
                        print(f"Closed local connection {sid}.")
            _input("Press Enter to return")
        elif choice == "Diagnostics":
            print("Host:", host_id)
            if row:
                details(root, ident)
            if host_id in data["errors"]:
                print("Last error:", data["errors"][host_id])
            _input("Press Enter to return")
    except (OSError, ValueError, KeyError, RuntimeError, tmux.TmuxError) as exc:
        print(f"Action failed: {exc}")
        _input("Press Enter to return")


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
    os.environ["TMUX_ROAM_COLUMNS"] = str(shutil.get_terminal_size((96, 24)).columns)
    with tempfile.TemporaryDirectory(prefix="picker-", dir=runtime_dir()) as directory:
        os.chmod(directory, 0o700)
        root = Path(directory)
        snapshot = root / "rows"
        sock = root / "fzf.sock"
        _atomic(root / "mode.json", json.dumps({"filter": "All", "hidden": False}).encode())
        records, errors = inventory.all_records(config, refresh=False)
        _write_snapshot(snapshot, records, errors, config)
        current_session, current_remote = _current_target()
        current = next((r.id for r in records if r.id == current_remote or
                        r.host_id == "local" and r.session_id == current_session), None)
        initial_ids = [line.split(b"\t", 1)[0].decode() for line in snapshot.read_bytes().split(b"\0") if line]
        initial_position = initial_ids.index(current) + 1 if current in initial_ids else 1
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
              futures = {pool.submit(inventory.remote, host, config, True, closed): host for host in config.hosts if host.enabled}
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
        env = _fzf_env()
        binary = str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam")
        base = binary + " picker-state " + str(root)
        created = "+transform(test -f " + str(root / "created.json") + " && printf accept)"
        command = ["fzf", "--read0", "--print0", "--delimiter=\t", "--with-nth=2..", "--id-nth=1",
                   "--track", "--no-multi", "--no-sort", "--no-clear", "--listen=" + str(sock),
                   "--header=" + mode_header(root),
                   *_fzf_options("Sessions", delete_command=binary + " delete-session " + str(root) + " {1}"),
                   "--preview=" + binary + " details " + str(root) + " {1}",
                   "--preview-window=down:40%" + ("" if config.details else ":hidden"),
                   "--bind=load:pos(" + str(initial_position) + ")+unbind(load),enter:transform(" + binary + " picker-select " + str(root) + " {1}),"
                   "tab:toggle-preview,ctrl-r:execute-silent(" + base + " refresh),"
                   "ctrl-f:reload-sync(" + base + " filter)+transform-header(" + binary + " picker-header " + str(root) + "),"
                   "ctrl-h:reload-sync(" + base + " hidden)+transform-header(" + binary + " picker-header " + str(root) + "),"
                   "f1:execute(" + binary + " help),"
                   "ctrl-n:execute(" + binary + " new-session " + str(root) + ")" + created + ","
                   "f2:execute(" + binary + " actions " + str(root) + " {1})" + created]
        try:
            result = subprocess.run(command, input=snapshot.read_bytes(), stdout=subprocess.PIPE, env=env)
        finally:
            closed.set()
            worker.join()
        if result.returncode in (0, 1) and (root / "created.json").exists():
            return inventory.Record(**json.loads((root / "created.json").read_text()))
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
