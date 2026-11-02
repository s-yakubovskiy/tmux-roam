"""Explicit distribution of one shared ~/.tmux.conf to configured SSH hosts."""
import base64
import difflib
import json
from pathlib import Path
import shlex
import tempfile

from . import inventory, tmux


def source(path=None):
    path = Path(path).expanduser() if path else Path.home() / ".tmux.conf"
    try:
        with path.open("rb") as file:
            data = file.read(inventory.MAX_OUTPUT + 1)
        if len(data) > inventory.MAX_OUTPUT:
            raise ValueError("Config exceeds 1 MiB")
        data.decode("utf-8")
    except OSError as exc:
        raise RuntimeError(f"Cannot read {path}: {exc}") from exc
    return path, data


def hosts(config, host_id=None, all_hosts=False):
    if bool(host_id) == bool(all_hosts):
        raise ValueError("Choose a host ID or --all")
    selected = [h for h in config.hosts if h.enabled and (all_hosts or h.id == host_id)]
    if not selected:
        raise ValueError(f"Unknown or disabled host: {host_id}" if host_id else "No enabled hosts configured")
    return selected


def request(host, config, action, **fields):
    # Keep the payload off the command line; reuse bounded SSH capture and its
    # process cleanup, including when the popup is cancelled during a transfer.
    helper = Path(__file__).with_name("_sync_remote.py").read_text()
    timeout = max(10, config.command_timeout_seconds)
    payload = dict(action=action, tmux=host.tmux_args(), timeout=timeout, **fields)
    with tempfile.TemporaryFile() as stdin:
        stdin.write(json.dumps(payload).encode())
        stdin.seek(0)
        code, stdout, stderr, failure = inventory.capture_bounded(
            inventory.ssh_base(host, config) + [shlex.join(["python3", "-c", helper])],
            config.connect_timeout_seconds + timeout * 5 + 5, output_limit=2 * inventory.MAX_OUTPUT,
            input_file=stdin)
    if failure:
        raise RuntimeError(f"{failure}; run config diff to check the remote file before retrying")
    try:
        result = json.loads(stdout)
    except (ValueError, UnicodeError):
        raise RuntimeError(tmux.clean(stderr.decode("utf-8", "replace")).strip()[:500] or f"SSH exit {code}: invalid response") from None
    if code or result.get("error"):
        raise RuntimeError(result.get("error") or f"SSH exit {code}")
    return result


def compare(host, config, path, content):
    remote = request(host, config, "inspect")
    previous = base64.b64decode(remote["content"], validate=True)
    diff = "".join(difflib.unified_diff(previous.decode("utf-8").splitlines(keepends=True),
                                      content.decode("utf-8").splitlines(keepends=True),
                                      fromfile=host.id + ":" + remote["path"], tofile=str(path)))
    return remote, diff


def sync(host, config, content, **options):
    return request(host, config, "sync", content=base64.b64encode(content).decode(), **options)


def describe(result):
    if result["status"] == "unchanged":
        return "already matches"
    message = "installed and reloaded" if result["status"] == "synced" else "installed; no running server, applies at next start"
    return message + ("; backup: " + result["backup"] if result.get("backup") else "")


def execute(config, action, host_id=None, all_hosts=False, source_path=None):
    selected = hosts(config, host_id, all_hosts)
    path, content = source(source_path)
    failed, different = False, False
    for host in selected:
        print(f"{host.label}: {'comparing' if action == 'diff' else 'syncing'} {path}…", flush=True)
        try:
            if action == "diff":
                remote, diff = compare(host, config, path, content)
                changed = bool(diff) or remote["sha256"] is None
                different |= changed
                print(diff if diff else ("Remote file is missing" if changed else "Already matches"), end="\n" if not diff.endswith("\n") else "")
            else:
                print(f"{host.label}: {describe(sync(host, config, content))}")
        except (OSError, ValueError, RuntimeError) as exc:
            failed = True
            print(f"{host.label}: {exc}", flush=True)
    return 2 if failed else 1 if different else 0
