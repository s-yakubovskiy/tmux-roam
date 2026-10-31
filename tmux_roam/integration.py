"""Install only owned tmux bindings; preserve user bindings for safe unload."""
import hashlib
import json
import os
import re
import shlex
import tempfile
from pathlib import Path

from . import tmux
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


def _popup(binary, config):
    env = "TMUX_ROAM_CONFIG=" + shlex.quote(str(config.path))
    if os.environ.get("TMUX_ROAM_SSH_CONFIG"):
        env += " TMUX_ROAM_SSH_CONFIG=" + shlex.quote(os.environ["TMUX_ROAM_SSH_CONFIG"])
    launch = env + " " + shlex.quote(str(binary)) + " popup --client '#{client_name}'"
    return ["run-shell", "-b", launch]


def _routing_commands(popup):
    # A managed session uses its own default table; ordinary sessions keep root.
    desired = []
    for suffix, remote_prefix in (("cb", "C-b"), ("ma", "M-a"), ("none", None)):
        root, routed = "roam-root-" + suffix, "roam-prefix-" + suffix
        for key in ("M-a", "M-ф"):
            desired.append((root, key, ["switch-client", "-T", routed]))
            desired.append((routed, key, ["send-keys", remote_prefix] if remote_prefix else ["switch-client", "-T", root]))
        desired.append((root, "Any", ["send-keys"]))
        for key in ("s", "ы"):
            desired.append((routed, key, popup))
        for key in ("S", "Ы"):
            desired.append((routed, key, ["choose-tree", "-Zs"]))
        desired.append((routed, "Any", ["send-keys", remote_prefix, "\\;", "send-keys"] if remote_prefix else ["send-keys"]))
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
    if _binding(table, key) != item["installed"]:
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
    path = _state_file()
    old = json.loads(path.read_text()) if path.exists() else {"bindings": []}
    remembered = {(b["table"], b["key"]): b for b in old["bindings"]}
    popup = _popup(binary, config)
    chooser = _keys("@tmux-roam-chooser-keys", "s ы")
    native = _keys("@tmux-roam-native-keys", "S Ы")
    desired = [("prefix", key, popup) for key in chooser]
    desired += [("prefix", key, ["choose-tree", "-Zs"]) for key in native]
    for table in _keys("@tmux-roam-extra-tables", "off-prefix"):
        if tmux.run(["list-keys", "-T", table], check=False).stdout.strip():
            desired += [(table, key, popup) for key in chooser]
    desired += _routing_commands(popup)
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
        elif previous != remembered[(table, key)].get("installed"):
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
    path = _state_file()
    if not path.exists():
        return 0
    state = json.loads(path.read_text())
    restored = 0
    for item in state["bindings"]:
        restored += _restore(item)
    path.unlink()
    return restored
