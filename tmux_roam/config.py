import fnmatch
import os
import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Host:
    id: str
    label: str
    ssh: str
    enabled: bool = True
    tmux_path: str = "tmux"
    remote_prefix: str = "auto"
    socket_name: str | None = None
    socket_path: str | None = None

    def tmux_args(self):
        args = [self.tmux_path]
        if self.socket_name:
            args += ["-L", self.socket_name]
        if self.socket_path:
            args += ["-S", self.socket_path]
        return args


@dataclass(frozen=True)
class Config:
    path: Path
    hosts: tuple[Host, ...]
    width: str = "85%"
    height: str = "75%"
    details: bool = False
    icons: bool = True
    hide_local: tuple[str, ...] = ("popup-*",)
    cache_seconds: int = 30
    refresh_seconds: int = 15
    connect_timeout_seconds: int = 3
    command_timeout_seconds: int = 5
    concurrency: int = 4


def config_path():
    return Path(os.environ.get("TMUX_ROAM_CONFIG", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "tmux-roam" / "config.toml"))


def _keys(obj, allowed, where):
    extra = set(obj) - set(allowed)
    if extra:
        raise ConfigError(f"{where}: unknown key {sorted(extra)[0]}")


def _str(value, where, pattern=None):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise ConfigError(f"{where}: expected nonempty text without control characters")
    if pattern and not re.fullmatch(pattern, value):
        raise ConfigError(f"{where}: invalid value {value!r}")
    return value


def _int(value, where, lo, hi):
    if type(value) is not int or not lo <= value <= hi:
        raise ConfigError(f"{where}: expected integer from {lo} to {hi}")
    return value


def _bool(value, where):
    if type(value) is not bool:
        raise ConfigError(f"{where}: expected boolean")
    return value


def load(path=None):
    path = Path(path or config_path())
    try:
        data = tomllib.loads(path.read_text()) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected TOML table")
    _keys(data, ("version", "picker", "discovery", "hosts"), str(path))
    if data.get("version", 1) != 1 or type(data.get("version", 1)) is not int:
        raise ConfigError(f"{path}: version must be 1")
    picker = data.get("picker", {})
    discovery = data.get("discovery", {})
    if not isinstance(picker, dict) or not isinstance(discovery, dict):
        raise ConfigError(f"{path}: picker and discovery must be tables")
    _keys(picker, ("width", "height", "details", "icons", "hide_local"), f"{path} [picker]")
    _keys(discovery, ("cache_seconds", "refresh_seconds", "connect_timeout_seconds", "command_timeout_seconds", "concurrency"), f"{path} [discovery]")
    hosts = data.get("hosts", [])
    if not isinstance(hosts, list):
        raise ConfigError(f"{path}: hosts must be an array")
    parsed = []
    seen = set()
    for i, item in enumerate(hosts):
        where = f"{path} [[hosts]] #{i + 1}"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: expected table")
        _keys(item, ("id", "label", "ssh", "enabled", "tmux_path", "remote_prefix", "socket_name", "socket_path"), where)
        try:
            ident = _str(item["id"], f"{where}.id", r"[A-Za-z0-9][A-Za-z0-9_-]*")
            ssh = _str(item["ssh"], f"{where}.ssh", r"[A-Za-z0-9_.@-]+")
        except KeyError as exc:
            raise ConfigError(f"{where}: missing {exc.args[0]}") from exc
        if ident in seen:
            raise ConfigError(f"{where}: duplicate id {ident}")
        seen.add(ident)
        name = item.get("socket_name")
        sock = item.get("socket_path")
        if name and sock:
            raise ConfigError(f"{where}: socket_name and socket_path are mutually exclusive")
        if name:
            _str(name, f"{where}.socket_name", r"[A-Za-z0-9_.-]+")
        if sock and (not isinstance(sock, str) or not os.path.isabs(sock)):
            raise ConfigError(f"{where}.socket_path: expected absolute path")
        prefix = _str(item.get("remote_prefix", "auto"), f"{where}.remote_prefix")
        if prefix not in ("auto", "M-a", "C-b", "None"):
            raise ConfigError(f"{where}.remote_prefix: supported values are auto, M-a, C-b, None")
        tmux_path = _str(item.get("tmux_path", "tmux"), f"{where}.tmux_path")
        if tmux_path.startswith("-"):
            raise ConfigError(f"{where}.tmux_path: invalid executable")
        parsed.append(Host(ident, _str(item.get("label", ident), f"{where}.label"), ssh,
                           _bool(item.get("enabled", True), f"{where}.enabled"), tmux_path, prefix, name, sock))
    hidden = picker.get("hide_local", ["popup-*"])
    if not isinstance(hidden, list):
        raise ConfigError(f"{path} [picker].hide_local: expected array")
    for pattern in hidden:
        _str(pattern, f"{path} [picker].hide_local")
    dimensions = []
    for key, default in (("width", "85%"), ("height", "75%")):
        dimensions.append(_str(picker.get(key, default), f"{path} [picker].{key}", r"(?:[1-9][0-9]?%|100%)"))
    opts = {key: _int(discovery.get(key, default), f"{path} [discovery].{key}", lo, hi)
            for key, default, lo, hi in (("cache_seconds", 30, 1, 3600), ("refresh_seconds", 15, 1, 3600),
                                         ("connect_timeout_seconds", 3, 1, 60), ("command_timeout_seconds", 5, 1, 120),
                                         ("concurrency", 4, 1, 32))}
    return Config(path, tuple(parsed), *dimensions,
                  _bool(picker.get("details", False), f"{path} [picker].details"),
                  _bool(picker.get("icons", True), f"{path} [picker].icons"), tuple(hidden), **opts)


def hidden(name, config):
    return any(fnmatch.fnmatchcase(name, p) for p in config.hide_local)


def executable(name):
    return shutil.which(name)
