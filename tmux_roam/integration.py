"""Install only owned tmux bindings; preserve user bindings for safe unload."""
import fcntl
import hashlib
import json
import os
import re
import shlex
import tempfile
from pathlib import Path

from . import native as native_picker, tmux
from .config import load
from .connection import runtime_dir


def _state_file():
    generation = "|".join(tmux.server())
    digest = hashlib.sha256(generation.encode()).hexdigest()[:24]
    return runtime_dir() / ("bindings-" + digest + ".json")


def _binding(table, key):
    lines = tmux.run(["list-keys", "-T", table], check=False).stdout.splitlines()
    pattern = re.compile(r"^bind-key\s+(?:-r\s+)?-T\s+" + re.escape(table) +
                         r"\s+" + re.escape(key) + r"(?:\s|$)")
    return next((line for line in lines if pattern.match(line)), "")


def _bind(table, key, command):
    tmux.run(["bind-key", "-T", table, key] + command)


def _canonical_binding(binding):
    # list-keys realigns its columns as keys are added or removed from a table.
    return re.sub(r"^(bind-key\s+(?:-r\s+)?-T\s+\S+\s+\S+)\s+",
                  lambda match: " ".join(match[1].split()) + " ", binding)


def _popup(binary, config, command="popup"):
    env = "TMUX_ROAM_CONFIG=" + shlex.quote(str(config.path))
    if os.environ.get("TMUX_ROAM_SSH_CONFIG"):
        env += " TMUX_ROAM_SSH_CONFIG=" + shlex.quote(os.environ["TMUX_ROAM_SSH_CONFIG"])
    launch = env + " " + shlex.quote(str(binary)) + " " + command + " --client '#{client_name}'"
    return ["run-shell", "-b", launch]


def _routing_commands(popup, chooser, native, native_command):
    # A managed session uses its own default table; ordinary sessions keep root.
    desired = []
    for suffix, remote_prefix in (("cb", "C-b"), ("ma", "M-a"), ("none", None)):
        root, routed = "roam-root-" + suffix, "roam-prefix-" + suffix
        for key in ("M-a", "M-ф"):
            desired.append((root, key, ["switch-client", "-T", routed]))
            desired.append((routed, key, ["send-keys", remote_prefix, remote_prefix] if remote_prefix else ["switch-client", "-T", root]))
        if remote_prefix:
            desired.append((routed, "F12", ["send-keys", remote_prefix, "\\;", "switch-client", "-T", "roam-escape"]))
        desired.append((root, "Any", ["send-keys"]))
        for key in ("WheelUpPane", "WheelDownPane"):
            desired.append((root, key, ["send-keys", "-M"]))
        for key in ("L", "Д"):
            desired.append((routed, key, ["switch-client", "-l"]))
        for key in chooser:
            desired.append((routed, key, popup))
        for key in native:
            desired.append((routed, key, native_command[key]))
        desired.append((routed, "Any", ["send-keys", remote_prefix, "\\;", "send-keys"] if remote_prefix else ["send-keys"]))
    desired.append(("roam-escape", "Any", ["send-keys"]))
    return desired


def _option(name, default):
    value = tmux.run(["show-options", "-gqv", name], check=False).stdout.strip()
    return value or default


def _keys(name, default):
    value = _option(name, default).split()
    if not value or len(value) > 8 or any(any(ord(c) < 33 or ord(c) == 127 for c in key) for key in value):
        raise ValueError(f"invalid {name} option")
    return value


def _restore(item):
    table, key = item["table"], item["key"]
    if _canonical_binding(_binding(table, key)) != _canonical_binding(item["installed"]):
        return False
    original = item["original"]
    if original:
        with tempfile.NamedTemporaryFile("w", delete=False, dir=runtime_dir()) as file:
            file.write(original + "\n")
            name = file.name
        try:
            tmux.run(["source-file", name])
        finally:
            Path(name).unlink(missing_ok=True)
    else:
        tmux.run(["unbind-key", "-T", table, key])
    return True


def install(binary):
    config = load()
    if _option("@tmux-roam-native", "off") != "on" or _option("@tmux-roam-native-windows", "off") != "on":
        from . import connection
        for session, identity in connection._owned_sessions():
            with (runtime_dir() / ("target-" + identity + ".lock")).open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                native_picker.remove_windows(session)
    path = _state_file()
    old = json.loads(path.read_text()) if path.exists() else {"bindings": []}
    remembered = {(b["table"], b["key"]): b for b in old["bindings"]}
    popup = _popup(binary, config)
    chooser = _keys("@tmux-roam-chooser-keys", "s ы")
    native = _keys("@tmux-roam-native-keys", "S Ы")
    fuzzy = set(_option("@tmux-roam-native-fuzzy-keys", "").split())
    if (fuzzy and _option("@tmux-roam-native-search", "native") != "fzf" and
            not any(line.startswith("switch-mode ") for line in tmux.run(["list-commands"]).stdout.splitlines())):
        fuzzy = set()  # Older tmux keeps the native tree on the same keys.
    native_command = {key: native_picker.binding(config, binary, searchable=key in fuzzy, toggle=True)
                      if _option("@tmux-roam-native", "off") == "on" else ["choose-tree", "-Zs"] for key in native}
    desired = [("prefix", key, popup) for key in chooser]
    desired += [("prefix", key, native_command[key]) for key in native]
    desired += [("prefix", key, ["switch-client", "-l"]) for key in ("L", "Д")]
    for table in _keys("@tmux-roam-extra-tables", "off-prefix"):
        if tmux.run(["list-keys", "-T", table], check=False).stdout.strip():
            desired += [(table, key, popup) for key in chooser]
            desired += [(table, key, ["switch-client", "-l"]) for key in ("L", "Д")]
    desired += _routing_commands(popup, chooser, native, native_command)
    if fuzzy and _option("@tmux-roam-native", "off") == "on":
        desired += native_picker.navigation_bindings(config, binary)
    current_keys = {(table, key) for table, key, _ in desired}
    for pair, item in list(remembered.items()):
        if pair not in current_keys:
            _restore(item)
            del remembered[pair]
    installed = 0
    for table, key, command in desired:
        previous = _binding(table, key)
        if (table, key) not in remembered:
            remembered[(table, key)] = {"table": table, "key": key, "original": previous}
        elif _canonical_binding(previous) != _canonical_binding(remembered[(table, key)].get("installed", "")):
            continue  # A later user edit owns this key now.
        _bind(table, key, command)
        remembered[(table, key)]["installed"] = _binding(table, key)
        installed += 1
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"bindings": list(remembered.values())}))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return installed


def unload():
    from . import native
    from .connection import _owned_sessions

    path = _state_file()
    if not path.exists():
        return 0
    state = json.loads(path.read_text())
    restored = 0
    for item in state["bindings"]:
        restored += _restore(item)
    path.unlink()
    for session, identity in _owned_sessions():
        native.remove_inactive(session, identity)
    return restored
