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
import time
import uuid
from dataclasses import asdict

from . import connection, inventory, picker, tmux


RECORD = "@tmux-roam-record"
LABEL = "@tmux-roam-label"
WINDOWS = "@tmux-roam-windows"
STATUS = "@tmux-roam-status"
NAME = "@tmux-roam-name"
HINT = "@tmux-roam-hint"
ACCENT = "@tmux-roam-accent"
WINDOW_ID = "@tmux-roam-window-id"
WINDOW_OWNER = "@tmux-roam-window-owner"
CARRIER_INDEX = "@tmux-roam-carrier-index"

# The tree owns the name column. Pad its details using terminal cell widths,
# including wide Unicode names; long names retain a single separating space.
ROW_PAD = "#{R: ,#{e|-|:22,#{w:session_name}}} "
CURRENT = "#{?#{==:#{session_id},@CURRENT@},  #[bold]*#[default],}"
LOCAL_FORMAT = ("#{?session_format," + ROW_PAD + "#{session_windows}w" + CURRENT + ","
                "#{?window_format,#{window_name}#{window_flags},"
                "#{pane_current_command}}}")
REMOTE_COLOUR = "#[fg=#{?#{m:Unavailable*,#{@tmux-roam-hint}},#8087a2,#{@tmux-roam-accent}}]"
STATUS_FOREGROUND = picker.STATUS_COLOURS["Ready"]
for _state in ("Connected", "Reconnect", "Checking", "Connecting", "Unavailable", "Ended"):
    STATUS_FOREGROUND = ("#{?#{m:" + _state + "*,#{@tmux-roam-hint}}," +
                         picker.STATUS_COLOURS[_state] + "," + STATUS_FOREGROUND + "}")
STATUS_COLOUR = "#[fg=" + STATUS_FOREGROUND + "]"
REMOTE_FORMAT = ("#{?session_format," + ROW_PAD +
                 "#[fg=#{?#{m:Unavailable*,#{@tmux-roam-hint}},#8087a2,#939ab7}]#{@tmux-roam-windows}w  " +
                 STATUS_COLOUR + "#{@tmux-roam-hint}#[default]" + CURRENT + "," +
                 REMOTE_COLOUR + "#{?#{@tmux-roam-window-id},#{window_name}"
                 "#{?#{@tmux-roam-window-active}, * ,  },}" + STATUS_COLOUR + "#{@tmux-roam-hint}#[default]}")
TREE_FORMAT = "#{?#{@tmux-roam-owner}," + REMOTE_FORMAT + "," + LOCAL_FORMAT + "}"
# Gutters keep the selected row away from the terminal edges. Right alignment
# lets tmux place the trailing gutter correctly after a terminal resize.
SEARCH_FORMAT = ("#[bg=#24273a]   #[default]#{?#{@tmux-roam-owner}," + REMOTE_COLOUR + ",}"
                 "#{session_name}#[default]" + TREE_FORMAT + "#[align=right,bg=#24273a]   ")
BROWSE_TABLE = "roam-native-browse"
SEARCH_TABLE = "roam-native-search"
TREE_FILTER = "#{!=:#{@tmux-roam-window-carrier},1}"


def _option(session, name):
    return tmux.run(["show-options", "-qv", "-t", session, name], check=False).stdout.strip()


def _hint(record, status):
    if "unavailable" in status:
        age = max(0, int(time.time() - record.observed))
        seen = ("just now" if age < 60 else f"{age // 60}m ago" if age < 3600 else
                f"{age // 3600}h ago" if age < 86400 else f"{age // 86400}d ago")
        return "Unavailable · seen " + seen
    return {"connected": "Connected", "connecting": "Connecting…", "disconnected": "Reconnect",
            "available": "Ready", "cached": "Checking…", "ended": "Ended"}.get(status, "Checking…")


def decorate(session, record, status):
    session_record = asdict(record)
    # Window metadata lives in the cache and window rows. Keep session metadata
    # compatible with earlier plugin versions and their selection commands.
    session_record.pop("window_details", None)
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
    accent = tmux.run(["show-options", "-gqv", "@tmux-roam-native-colour-" + record.host_id], check=False).stdout.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        accent = "#8aadf4"
    for option, value in ((RECORD, json.dumps(session_record)), (LABEL, label),
                          (WINDOWS, str(record.windows)), (STATUS, status),
                          (HINT, _hint(record, status)), (ACCENT, accent)):
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


def _window_option(target, name):
    return tmux.run(["show-options", "-wqv", "-t", target, name], check=False).stdout.strip()


def _window_entries(session):
    rows = tmux.run(["list-windows", "-t", session, "-F",
                     "#{window_id}|#{window_index}|#{pane_id}|#{@tmux-roam-window-owner}|#{@tmux-roam-window-id}"],
                    check=False).stdout.splitlines()
    return [line.split("|", 4) for line in rows if len(line.split("|", 4)) == 5]


def _remove_window(session, entry):
    """Delete only our unchanged, exited display window; never a user's work."""
    wid, _, pane, owner, remote_id = entry
    guard = "1"
    for key, value in (("session_id", session), ("window_id", wid), ("pane_id", pane),
                       ("window_panes", "1"), ("window_linked", "0"), ("pane_dead", "1"),
                       (WINDOW_OWNER, owner), (WINDOW_ID, remote_id)):
        guard = "#{&&:" + guard + ",#{==:#{" + key + "}," + value + "}}"
    for condition in ("#{==:#{window_name},#{@tmux-roam-window-name}}",
                      "#{==:#{pane_start_command},#{@tmux-roam-window-command}}",
                      "#{==:#{pane_dead_time},#{@tmux-roam-window-exit}}",
                      "#{||:#{==:#{session_attached},0},#{==:#{window_active},0}}"):
        guard = "#{&&:" + guard + "," + condition + "}"
    tmux.run(["if-shell", "-F", "-t", pane, guard, "kill-window -t " + wid], check=False)


def remove_windows(session):
    """Called under the remote identity lock before carrier reuse or cleanup."""
    for entry in _window_entries(session):
        if entry[3] and re.fullmatch(r"@[0-9]+", entry[4]):
            _remove_window(session, entry)
    entries = _window_entries(session)
    occupied = {entry[1] for entry in entries}
    for wid, index, _, _, _ in entries:
        original = _window_option(wid, CARRIER_INDEX)
        if not original.isdigit():
            continue
        tmux.run(["set-option", "-wu", "-t", wid, "@tmux-roam-window-carrier"], check=False)
        # Restore our reserved index only if it has not been changed by the user.
        reserved = _window_option(wid, "@tmux-roam-carrier-reserved")
        if index == reserved and original in occupied:
            continue  # A preserved, modified display window still occupies it.
        if index == reserved and original != index:
            tmux.run(["move-window", "-s", session + ":" + wid, "-t", session + ":" + original], check=False)
        for option in (CARRIER_INDEX, "@tmux-roam-carrier-reserved"):
            tmux.run(["set-option", "-wu", "-t", wid, option], check=False)
    previous = _option(session, "@tmux-roam-windows-renumber")
    if previous and not any(entry[3] for entry in entries):
        command = (["set-option", "-u", "-t", session, "renumber-windows"] if previous == "inherit" else
                   ["set-option", "-t", session, "renumber-windows", previous])
        tmux.run(command, check=False)
        tmux.run(["set-option", "-u", "-t", session, "@tmux-roam-windows-renumber"], check=False)


def sync_windows(session, record):
    """Mirror window metadata as dormant local rows, keeping one SSH carrier."""
    if tmux.run(["show-options", "-gqv", "@tmux-roam-native-windows"], check=False).stdout.strip() != "on":
        remove_windows(session)
        return
    if not record.window_details:
        return  # An old cache or racing snapshot cannot prove windows ended.
    entries = _window_entries(session)
    carriers = [entry for entry in entries if not entry[3]]
    if len(carriers) != 1:
        return  # User-created windows are not ours to rearrange.
    carrier, original, _, _, _ = carriers[0]
    if not _window_option(carrier, CARRIER_INDEX):
        remote_indexes = {window["index"] for window in record.window_details}
        reserved = max(999999, max(remote_indexes) + 1)
        if not _option(session, "@tmux-roam-windows-renumber"):
            tmux.run(["set-option", "-t", session, "@tmux-roam-windows-renumber",
                      _option(session, "renumber-windows") or "inherit"])
        tmux.run(["set-option", "-t", session, "renumber-windows", "off"])
        tmux.run(["set-option", "-w", "-t", carrier, CARRIER_INDEX, original])
        tmux.run(["set-option", "-w", "-t", carrier, "@tmux-roam-carrier-reserved", str(reserved)])
        if original != str(reserved):
            tmux.run(["move-window", "-s", session + ":" + carrier, "-t", session + ":" + str(reserved)])
    wanted = {window["id"]: window for window in record.window_details}
    for entry in entries:
        if entry[3] and (entry[3] != record.id or entry[4] not in wanted or
                         entry[1] != str(wanted[entry[4]]["index"])):
            _remove_window(session, entry)
    entries = _window_entries(session)
    existing = {entry[4]: entry for entry in entries if entry[3] == record.id}
    occupied = {entry[1] for entry in entries}
    for remote in record.window_details:
        name = tmux.clean(remote["name"]).replace("#", "＃")
        entry = existing.get(remote["id"])
        if entry is None:
            index = str(remote["index"])
            if index in occupied:
                continue  # Preserve modified or unrelated windows occupying this index.
            result = tmux.run(["new-window", "-d", "-P", "-F", "#{window_id}|#{pane_id}",
                               "-t", session + ":" + index, "-n", name, "/bin/sleep", "infinity"])
            wid, pane = result.stdout.strip().split("|")
            for option, value in ((WINDOW_OWNER, record.id), (WINDOW_ID, remote["id"]),
                                  ("@tmux-roam-window-name", name), ("remain-on-exit", "on"),
                                  ("remain-on-exit-format", ""), ("automatic-rename", "off")):
                tmux.run(["set-option", "-w", "-t", wid, option, value])
            preview = "Remote window\n\nSelect this window to open its terminal.\n"
            tmux.run(["respawn-pane", "-k", "-t", pane, "/usr/bin/printf", "%s", preview])
            command = tmux.run(["display-message", "-p", "-t", pane, "#{pane_start_command}"]).stdout.strip()
            tmux.run(["set-option", "-w", "-t", wid, "@tmux-roam-window-command", command])
            for _ in range(20):
                stamp = tmux.run(["display-message", "-p", "-t", pane, "#{pane_dead_time}"]).stdout.strip()
                if stamp.isdigit():
                    break
                time.sleep(.01)
            tmux.run(["set-option", "-w", "-t", wid, "@tmux-roam-window-exit", stamp])
            occupied.add(index)
        else:
            wid, _, pane, _, _ = entry
            previous = _window_option(wid, "@tmux-roam-window-name")
            current = tmux.run(["display-message", "-p", "-t", wid, "#{window_name}"]).stdout.strip()
            if current == previous and current != name:
                tmux.run(["rename-window", "-t", wid, name])
                tmux.run(["set-option", "-w", "-t", wid, "@tmux-roam-window-name", name])
        active = "1" if remote["active"] else ""
        if _window_option(wid, "@tmux-roam-window-active") != active:
            tmux.run(["set-option", "-w", "-t", wid, "@tmux-roam-window-active", active])
    if (any(entry[3] == record.id for entry in _window_entries(session)) and
            _window_option(carrier, "@tmux-roam-window-carrier") != "1"):
        tmux.run(["set-option", "-w", "-t", carrier, "@tmux-roam-window-carrier", "1"])


def remove_inactive(session, identity, *, ended=False):
    """Prune dormant entries, or unused failed connections confirmed absent remotely."""
    if ended and connection._read_state(_option(session, connection.STATE)).get("status") != "idle":
        return connection.remove_disconnected(session, identity, ended=True)
    with (connection.runtime_dir() / ("target-" + identity + ".lock")).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        remove_windows(session)
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
                        if _option(sid, HINT) != "Checking…":
                            tmux.run(["set-option", "-t", sid, HINT, "Checking…"])
                        sync_windows(sid, record)
                        continue  # Opening a cached tree must not rewrite live metadata.
                    state = connection._read_state(_option(sid, connection.STATE))
                    status = {"open": "connected", "preparing": "connecting", "attaching": "connecting",
                              "error": "disconnected", "disconnected": "disconnected"}.get(state.get("status"),
                              "available" if fresh else "cached")
                    if error:
                        status += " · host unavailable"
                    elif fresh and state.get("status") == "open" and not record.state.startswith("open here"):
                        status = "disconnected"  # No remote client confirms the cached open state.
                    decorate(sid, record, status)
                    sync_windows(sid, record)
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
                hint = "Unavailable" if error else "Ended"
                if _option(sid, HINT) != hint:
                    tmux.run(["set-option", "-t", sid, HINT, hint])
        return before != _view()


def _view():
    return tmux.run(["list-windows", "-a", "-F",
                     "#{session_id}|#{session_name}|#{@tmux-roam-windows}|#{@tmux-roam-status}|"
                     "#{@tmux-roam-hint}|#{@tmux-roam-accent}|#{window_id}|#{window_name}|"
                     "#{@tmux-roam-window-active}"]).stdout


def _client_pane(client):
    rows = tmux.run(["list-clients", "-F", "#{client_name}|#{pane_id}"]).stdout.splitlines()
    return next((row.rsplit("|", 1)[1] for row in rows if row.rsplit("|", 1)[0] == client), None)


def navigation_bindings(config, binary):
    """Client-local mode tables leave ordinary terminal input untouched."""
    desired = []
    # tmux resets a non-repeating client table before running its binding.
    # Replay through the session's default table when the mode has closed.
    fallback = "send-keys -K"
    for table, mode in ((BROWSE_TABLE, "tree-mode"), (SEARCH_TABLE, "switch-mode")):
        active = "#{==:#{pane_mode}," + mode + "}"
        forward = ("if-shell -F '#{mouse_pane}' 'send-keys -M' 'send-keys' ; if-shell -F " +
                   shlex.quote(active) + " " + shlex.quote("switch-client -T " + table))
        desired.append((table, "Any", ["if-shell", "-F", active, forward, fallback]))
        # A prefix still reaches the local/remote routing table while browsing.
        for key in ("M-a", "M-ф"):
            desired.append((table, key, ["send-keys", "-K", key]))
    for key, direction in (("о", "Down"), ("л", "Up")):
        # Translate only tree navigation; search fields keep literal text.
        command = "send-keys " + direction + " ; switch-client -T " + BROWSE_TABLE
        desired.append((BROWSE_TABLE, key, ["if-shell", "-F", "#{==:#{pane_mode},tree-mode}",
                                           command, fallback]))
    for key in ("/", "."):  # Physical slash on the Russian layout.
        if tmux.run(["show-options", "-gqv", "@tmux-roam-native-search"], check=False).stdout.strip() == "fzf":
            from . import integration
            opening = integration._popup(binary, config, "popup --search --pane '#{pane_id}'")
        else:
            opening = binding(config, binary, fuzzy=True, searchable=True)
        command = " ".join(shlex.quote(arg) if arg != "\\;" else ";" for arg in opening)
        desired.append((BROWSE_TABLE, key, ["if-shell", "-F", "#{==:#{pane_mode},tree-mode}", command,
                                           fallback]))
    command = " ".join(shlex.quote(arg) if arg != "\\;" else ";"
                       for arg in binding(config, binary, searchable=True))
    desired.append((SEARCH_TABLE, "Escape", ["if-shell", "-F", "#{==:#{pane_mode},switch-mode}", command,
                                            fallback]))
    return desired


def binding(config, binary, client=None, pane=None, *, fuzzy=False, searchable=False, toggle=False):
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
    chooser = (["switch-mode", "-Zs", "-t", "@PANE@", "-F", SEARCH_FORMAT, template] if fuzzy else
               ["choose-tree", "-Zs", "-O", "index", "-t", "@PANE@", "-f", TREE_FILTER, "-F", TREE_FORMAT, template])
    picker_active = "#{||:#{==:#{pane_mode},tree-mode},#{==:#{pane_mode},switch-mode}}"
    close_mode = ["if-shell", "-F", "-t", "@PANE@", picker_active,
                  "send-keys -t @PANE@ Escape"]
    commands = (shlex.join(close_mode) + " ; " + shlex.join(chooser) +
                (" ; " + shlex.join(["switch-client", "-c", "@CLIENT@", "-T", SEARCH_TABLE if fuzzy else BROWSE_TABLE])
                 if searchable else "") +
                " ; run-shell -b " + shlex.quote(refresh))
    # run-shell -C executes tmux commands without a shell process. Freeze the
    # invoking client and token now; defer row/pane formats until selection.
    # Style introducers survive format expansion unchanged; doubling them would
    # render literal #[...] text. Row expressions still need one deferred pass.
    commands = commands.replace("#", "##").replace("##[", "#[").replace("@CLIENT@", client or "#{client_name}")
    commands = commands.replace("@TOKEN@", "#{@tmux-roam-generation}").replace("@PANE@", pane or "#{pane_id}")
    commands = commands.replace("@CURRENT@", "#{session_id}")
    opening = ["set-option", "-gF", "@tmux-roam-generation",
               "#{e|+|:#{?@tmux-roam-generation,#{@tmux-roam-generation},0},1}", "\\;",
               "set-option", "-gF", picker._navigation_option(client or "#{client_name}"),
               "#{@tmux-roam-generation}", "\\;", "run-shell", "-C", "-t", pane or "#{pane_id}", commands]
    if toggle:
        return ["if-shell", "-F", picker_active, "send-keys Escape",
                " ".join(shlex.quote(arg) if arg != "\\;" else ";" for arg in opening)]
    return opening


def open_tree(config, client, binary, *, searchable=False):
    # The CLI joins the invoking client's command queue; key bindings use this
    # same native command sequence directly.
    pane = _client_pane(client)
    if pane:
        tmux.run([";" if arg == "\\;" else arg
                  for arg in binding(config, binary, client, pane, searchable=searchable)])


def close_tree(pane):
    tmux.run(["if-shell", "-F", "-t", pane, "#{==:#{pane_mode},tree-mode}",
              shlex.join(["send-keys", "-t", pane, "Escape"])], check=False)


def refresh(config, client, pane, token):
    closed = threading.Event()
    channel = "roam-native-" + uuid.uuid4().hex
    hook = f"pane-mode-changed[{os.getpid()}]"
    waiter = None

    def active():
        return (not closed.is_set() and picker.navigation_valid(client, token) and
                _client_pane(client) == pane and
                tmux.run(["display-message", "-p", "-t", pane, "#{pane_mode}"], check=False).stdout.strip() in ("tree-mode", "switch-mode"))

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
        remote_window = _window_option(target, WINDOW_ID)
        if remote_window and _window_option(target, WINDOW_OWNER) != record.id:
            raise RuntimeError("remote window changed; reopen the picker")
        connection.open_remote(record, host, config, client, navigation, window=remote_window or None)
    finally:
        picker.end_navigation(client, navigation)
