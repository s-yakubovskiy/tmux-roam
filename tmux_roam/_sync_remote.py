"""Stdlib-only helper sent over SSH; nothing is installed on the remote host."""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


LIMIT = 1024 * 1024


def read(path):
    if not path.exists():
        return None
    if not path.is_file():
        raise RuntimeError("Config destination is not a regular file")
    with path.open("rb") as file:
        data = file.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise RuntimeError("Config exceeds 1 MiB")
    return data


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def run(args, timeout):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[:4000] or "tmux command failed")
    return result


def handle(request):
    path = (Path.home() / ".tmux.conf").resolve()
    original = read(path)
    if request["action"] == "inspect":
        return dict(path=str(path), sha256=digest(original), content=base64.b64encode(original or b"").decode())
    content = base64.b64decode(request["content"], validate=True)
    if len(content) > LIMIT:
        raise RuntimeError("Config exceeds 1 MiB")
    content.decode("utf-8")
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "tmux-roam"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "config-sync.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = read(path)
        if "expected" in request and digest(original) != request["expected"]:
            raise RuntimeError("Remote config changed since the preview; compare it again")
        if original == content:
            return dict(status="unchanged", path=str(path))
        args, timeout = request["tmux"], request["timeout"]
        with tempfile.TemporaryDirectory(prefix=".tmux-roam-sync-", dir=path.parent) as directory:
            stage = Path(directory) / "tmux.conf"
            stage.write_bytes(content)
            stage.chmod(path.stat().st_mode & 0o777 if original is not None else 0o600)
            # Parse with the destination's tmux version without executing the
            # config or touching its live server, even when no server is running.
            validator = [args[0], "-S", str(Path(directory) / "socket"), "-f", "/dev/null"]
            try:
                run(validator + ["start-server", ";", "set-option", "-s", "exit-empty", "off"], timeout)
                run(validator + ["source-file", "-n", str(stage)], timeout)
            finally:
                subprocess.run(validator + ["kill-server"], capture_output=True, timeout=timeout)
            probe = subprocess.run(args + ["-N", "list-sessions"], capture_output=True, text=True, timeout=timeout)
            running = probe.returncode == 0
            if not running and not any(s in probe.stderr.lower() for s in (
                    "no server running", "no sessions", "no such file", "connection refused")):
                raise RuntimeError(probe.stderr.strip() or "Cannot inspect destination tmux server")
            if read(path) != original:
                raise RuntimeError("Remote config changed during validation; compare it again")
            backup = None
            if original is not None:
                backup = state / "backups" / (time.strftime("%Y%m%d-%H%M%S") + "-config-" + uuid.uuid4().hex[:8]) / "tmux.conf"
                backup.parent.mkdir(mode=0o700, parents=True)
                shutil.copy2(path, backup)
            os.replace(stage, path)
            if running:
                try:
                    run(args + ["-N", "source-file", str(path)], timeout)
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    if backup:
                        shutil.copy2(backup, stage)
                        os.replace(stage, path)
                        try:
                            run(args + ["-N", "source-file", str(path)], timeout)
                            recovery = "previous file restored and reloaded"
                        except (RuntimeError, subprocess.TimeoutExpired) as restore_error:
                            recovery = "previous file restored; its reload also failed: " + str(restore_error)
                    else:
                        path.unlink()
                        recovery = "new file removed; no previous config existed"
                    raise RuntimeError(f"Reload failed: {exc}; {recovery}. Live changes may remain. Backup: {backup}") from None
            return dict(status="synced" if running else "saved", path=str(path), backup=str(backup) if backup else None)


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(2 * LIMIT))
        print(json.dumps(handle(request)))
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(json.dumps(dict(error=str(exc))))
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
