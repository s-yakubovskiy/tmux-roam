"""Remote sessions presented through tmux's own choose-tree UI."""
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import uuid
from dataclasses import asdict

from . import connection, inventory, picker, tmux


RECORD = "@tmux-roam-record"
LABEL = "@tmux-roam-label"
WINDOWS = "@tmux-roam-windows"
STATUS = "@tmux-roam-status"
NAME = "@tmux-roam-name"

LOCAL_FORMAT = ("#{?session_format,#{session_windows} windows"
                "#{?session_attached, (attached),},"
                "#{?window_format,#{window_name}#{window_flags},"
                "#{pane_current_command}}}")
REMOTE_FORMAT = ("#{?session_format,#{@tmux-roam-windows} remote windows"
                 " · #{@tmux-roam-status},remote terminal · #{@tmux-roam-status}}")
TREE_FORMAT = "#{?#{@tmux-roam-owner}," + REMOTE_FORMAT + "," + LOCAL_FORMAT + "}"


def _option(session, name):
    return tmux.run(["show-options", "-qv", "-t", session, name], check=False).stdout.strip()


def decorate(session, record, status):
    label = tmux.clean(f"{record.host_label} / {record.name}")
    # Labels are data, including when tmux later parses display styles.
    label = label.replace("#", "＃")
    current = tmux.run(["display-message", "-p", "-t", session, "#{session_name}"]).stdout.strip()
    previous = _option(session, NAME)
    if current == previous or not previous and current.startswith("roam-"):
        base = re.sub(r"[.:]", "_", label)
        names = {row["name"] for row in tmux.sessions() if row["session_id"] != session}
        name, number = base, 2
        while name in names:
            name = f"{base} ({number})"
            number += 1
        if current != name:
            tmux.run(["rename-session", "-t", session, name])
        if previous != name:
            tmux.run(["set-option", "-t", session, NAME, name])
    for option, value in ((RECORD, json.dumps(asdict(record))), (LABEL, label),
                          (WINDOWS, str(record.windows)), (STATUS, status)):
        old = _option(session, option)
        if option == RECORD and old:
            try:
                previous_record, new_record = json.loads(old), json.loads(value)
                for field in ("observed", "source", "state"):
                    previous_record.pop(field, None)
                    new_record.pop(field, None)
                if previous_record == new_record:
                    continue
            except (ValueError, AttributeError):
                pass
        if old != value:
            tmux.run(["set-option", "-t", session, option, value])


def _placeholder(record):
    token = uuid.uuid4().hex
    result = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}|#{pane_id}",
                       "-s", "roam-entry-" + token[:12], "-n", "remote", "/bin/sleep", "infinity"])
    session, pane = result.stdout.strip().split("|")
    try:
        for option, value in ((connection.OWNER, "1"), (connection.IDENTITY, record.id),
                              (connection.STATE, token)):
            tmux.run(["set-option", "-t", session, option, value])
        tmux.run(["set-option", "-w", "-t", pane, "remain-on-exit", "on"])
        tmux.run(["set-option", "-w", "-t", pane, "remain-on-exit-format", ""], check=False)
        preview = (f"{tmux.clean(record.host_label)} / {tmux.clean(record.name)}\n\n"
                   "Remote session available.\n"
                   "Select this entry in the session picker to connect.\n"
                   "The terminal preview appears after connecting.\n")
        # A one-shot printf leaves a retained pane with no running process.
        tmux.run(["respawn-pane", "-k", "-t", pane, "/usr/bin/printf", "%s", preview])
        connection._write_state(token, {"status": "idle", "pane": pane})
        return session, token
    except BaseException:
        tmux.run(["kill-session", "-t", session], check=False)
        raise


def remove_inactive(session, identity, *, ended=False):
    """Prune dormant entries, or unused failed connections confirmed absent remotely."""
    if ended and connection._read_state(_option(session, connection.STATE)).get("status") != "idle":
        return connection.remove_disconnected(session, identity, ended=True)
    with (connection.runtime_dir() / ("target-" + identity + ".lock")).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        token = _option(session, connection.STATE)
        state = connection._read_state(token)
        pane = state.get("pane", "")
        if state.get("status") != "idle":
            return False
        if not re.fullmatch(r"%[0-9]+", pane):
            return False
        details = tmux.run(["display-message", "-p", "-t", pane,
                            "#{session_id}|#{session_attached}|#{session_windows}|#{window_panes}|#{pane_dead}|#{pane_dead_time}"],
                           check=False).stdout.strip().split("|")
        if len(details) != 6 or details[:5] != [session, "0", "1", "1", "1"] or not details[5].isdigit():
            return False
        # Recheck activity, ownership and the observed exit atomically in tmux.
        guard = "1"
        for key, value in (("session_id", session), (connection.OWNER, "1"),
                           (connection.IDENTITY, identity), (connection.STATE, token),
                           ("session_attached", "0"), ("session_windows", "1"),
                           ("window_panes", "1"), ("pane_dead", "1"), ("pane_dead_time", details[5])):
            guard = "#{&&:" + guard + ",#{==:#{" + key + "}," + value + "}}"
        tmux.run(["if-shell", "-F", "-t", pane, guard, "kill-session -t " + session])
        return tmux.run(["has-session", "-t", session], check=False).returncode != 0


def sync_host(host, records, *, fresh=False, error=None):
    """Publish metadata and dormant entries; never attach or destroy remote work."""
    digest = hashlib.sha256("|".join(tmux.server()).encode()).hexdigest()[:24]
    with (connection.runtime_dir() / ("native-" + digest + ".lock")).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = _view()
        present = {record.id for record in records}
        for record in records:
            if fresh and not error and record.state.startswith("open here"):
                owned = [(sid, ident) for sid, ident in connection._owned_sessions() if ident == record.id]
                if len(owned) > 1:
                    for sid, ident in owned:
                        connection.remove_disconnected(sid, ident)
            with (connection.runtime_dir() / ("target-" + record.id + ".lock")).open("w") as target_lock:
                fcntl.flock(target_lock, fcntl.LOCK_EX)
                owned = [(sid, ident) for sid, ident in connection._owned_sessions() if ident == record.id]
                if not owned:
                    sid, _ = _placeholder(record)
                    owned = [(sid, record.id)]
                for sid, _ in owned:
                    if not fresh and not error and _option(sid, RECORD):
                        continue  # Opening a cached tree must not rewrite live metadata.
                    state = connection._read_state(_option(sid, connection.STATE))
                    status = {"open": "connected", "preparing": "connecting", "attaching": "connecting",
                              "error": "disconnected", "disconnected": "disconnected"}.get(state.get("status"),
                              "available" if fresh else "cached")
                    if error:
                        status += " · host unavailable"
                    decorate(sid, record, status)
        if fresh or error:
            for sid, identity in connection._owned_sessions():
                if inventory.decode_identity(identity)[0] != host.id or identity in present:
                    continue
                if fresh and not error and remove_inactive(sid, identity, ended=True):
                    continue
                # Keep active, attached or modified panes, and all entries on network errors.
                status = "host unavailable" if error else "ended"
                if _option(sid, STATUS) != status:
                    tmux.run(["set-option", "-t", sid, STATUS, status])
        return before != _view()


def _view():
    return tmux.run(["list-sessions", "-F",
                     "#{session_id}|#{session_name}|#{@tmux-roam-windows}|#{@tmux-roam-status}"]).stdout


def _client_pane(client):
    rows = tmux.run(["list-clients", "-F", "#{client_name}|#{pane_id}"]).stdout.splitlines()
    return next((row.rsplit("|", 1)[1] for row in rows if row.rsplit("|", 1)[0] == client), None)


def binding(config, binary, client=None, pane=None):
    """Build native commands; opening and local selection never wait for a process."""
    command = ["env", "TMUX_ROAM_CONFIG=" + str(config.path)]
    if os.environ.get("TMUX_ROAM_SSH_CONFIG"):
        command += ["TMUX_ROAM_SSH_CONFIG=" + os.environ["TMUX_ROAM_SSH_CONFIG"]]
    command += [str(binary)]
    launch = (shlex.join(command + ["native-select"]) +
              " --client '@CLIENT@' --navigation '@TOKEN@' --target '#{pane_id}'")
    # %% escapes a single-quoted target; %1% escapes repeated double-quoted
    # targets. Plain %1 would let quotes in a session name alter the command.
    remote = 'run-shell -b -t "%1%" ' + shlex.quote(launch)
    template = ("if-shell -F -t '%%' '#{@tmux-roam-owner}' { " + remote +
                " } { switch-client -c '@CLIENT@' -t \"%1%\" }")
    refresh = (shlex.join(command + ["native-refresh"]) +
               " --client '@CLIENT@' --navigation '@TOKEN@' --pane '@PANE@'")
    commands = (shlex.join(["choose-tree", "-Zs", "-t", "@PANE@", "-F", TREE_FORMAT, template]) +
                " ; run-shell -b " + shlex.quote(refresh))
    # run-shell -C executes tmux commands without a shell process. Freeze the
    # invoking client and token now; defer row/pane formats until selection.
    commands = commands.replace("#", "##").replace("@CLIENT@", client or "#{client_name}")
    commands = commands.replace("@TOKEN@", "#{@tmux-roam-generation}").replace("@PANE@", pane or "#{pane_id}")
    return ["set-option", "-gF", "@tmux-roam-generation",
            "#{e|+|:#{?@tmux-roam-generation,#{@tmux-roam-generation},0},1}", "\\;",
            "set-option", "-gF", picker._navigation_option(client or "#{client_name}"),
            "#{@tmux-roam-generation}", "\\;", "run-shell", "-C", commands]


def open_tree(config, client, binary):
    # The CLI joins the invoking client's command queue; key bindings use this
    # same native command sequence directly.
    pane = _client_pane(client)
    if pane:
        tmux.run([";" if arg == "\\;" else arg for arg in binding(config, binary, client, pane)])


def refresh(config, client, pane, token):
    closed = threading.Event()
    channel = "roam-native-" + uuid.uuid4().hex
    hook = f"pane-mode-changed[{os.getpid()}]"
    waiter = None

    def active():
        return (not closed.is_set() and picker.navigation_valid(client, token) and
                _client_pane(client) == pane and
                tmux.run(["display-message", "-p", "-t", pane, "#{pane_mode}"], check=False).stdout.strip() == "tree-mode")

    def redraw(changed):
        if changed and active():
            tmux.run(["refresh-client", "-t", client], check=False)

    try:
        tmux.run(["set-hook", "-p", "-t", pane, hook, "wait-for -S " + channel])
        waiter = subprocess.Popen(tmux.args() + ["wait-for", channel], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def watch():
            waiter.wait()
            closed.set()
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        if not active():
            return
        hosts = [host for host in config.hosts if host.enabled]
        for host in hosts:
            redraw(sync_host(host, inventory.cached(host)))
        while active():
            with concurrent.futures.ThreadPoolExecutor(max_workers=config.concurrency) as pool:
                futures = {pool.submit(inventory.remote, host, config, True, closed): host for host in hosts}
                for future in concurrent.futures.as_completed(futures):
                    if closed.is_set():
                        break
                    host = futures[future]
                    records, error = future.result()
                    if not error:
                        inventory.save(host, records)
                    if active():
                        redraw(sync_host(host, inventory.cached(host) if error else records, fresh=not error, error=error))
            if closed.wait(config.refresh_seconds):
                return
    finally:
        closed.set()
        tmux.run(["set-hook", "-pu", "-t", pane, hook], check=False)
        if waiter:
            if waiter.poll() is None:
                waiter.terminate()
            waiter.wait()
        # The queued selection still uses the token. The next picker replaces it.


def select(config, client, target, navigation):
    if not re.fullmatch(r"%[0-9]+", target):
        raise RuntimeError("invalid native picker target")
    if not picker.navigation_valid(client, navigation) or not tmux.client_exists(client):
        return
    session = tmux.run(["display-message", "-p", "-t", target, "#{session_id}"]).stdout.strip()
    if _option(session, connection.OWNER) != "1":
        tmux.switch(client, target)
        return
    try:
        try:
            record = inventory.Record(**json.loads(_option(session, RECORD)))
        except (ValueError, TypeError) as exc:
            raise RuntimeError("remote metadata unavailable; reopen the picker after discovery finishes") from exc
        if record.id != _option(session, connection.IDENTITY):
            raise RuntimeError("remote session changed; reopen the picker")
        host = next((h for h in config.hosts if h.id == record.host_id and h.enabled), None)
        if not host:
            raise RuntimeError("remote host is disabled or no longer configured")
        tmux.run(["display-message", "-c", client, f"Connecting to {tmux.clean(record.host_label)}/{tmux.clean(record.name)}…"])
        connection.open_remote(record, host, config, client, navigation)
    finally:
        picker.end_navigation(client, navigation)
