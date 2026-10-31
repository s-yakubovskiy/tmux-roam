import os
import re
import subprocess


SEP = "|"
FORMAT = SEP.join(("#{session_id}", "#{session_created}", "#{session_name}",
                   "#{session_windows}", "#{session_attached}"))
CLIENT_FORMAT = SEP.join(("#{client_pid}", "#{client_tty}", "#{session_id}", "#{client_name}"))


class TmuxError(RuntimeError):
    pass


def args():
    sock = os.environ.get("TMUX", "").split(",", 1)[0]
    return ["tmux", "-S", sock] if sock else ["tmux"]


def run(cmd, base=None, timeout=5, check=True):
    try:
        result = subprocess.run((base or args()) + cmd, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TmuxError(str(exc)) from exc
    if check and result.returncode:
        raise TmuxError(result.stderr.strip() or result.stdout.strip() or "tmux failed")
    return result


def clean(value):
    return "".join(c if ord(c) >= 32 and not 127 <= ord(c) <= 159 else "?" for c in value)


def server(base=None):
    result = run(["display-message", "-p", "#{pid}" + SEP + "#{start_time}" + SEP + "#{socket_path}"], base)
    parts = result.stdout.rstrip("\n").split(SEP, 2)
    if len(parts) != 3 or not all(parts[:2]):
        raise TmuxError("malformed tmux server identity")
    return parts


def sessions(base=None):
    result = run(["list-sessions", "-F", FORMAT], base, check=False)
    if result.returncode:
        if "no server running" in result.stderr or "no sessions" in result.stderr:
            return []
        raise TmuxError(result.stderr.strip() or "cannot list sessions")
    out = []
    for line in result.stdout.splitlines():
        head = line.split(SEP, 2)
        parts = head[:2] + head[2].rsplit(SEP, 2) if len(head) == 3 else head
        if len(parts) != 5 or not re.fullmatch(r"\$[0-9]+", parts[0]) or not all(x.isdigit() for x in (parts[1], parts[3], parts[4])):
            raise TmuxError("malformed tmux session record")
        out.append(dict(session_id=parts[0], created=int(parts[1]), name=clean(parts[2]),
                        windows=int(parts[3]), clients=int(parts[4])))
    return out


def clients(base=None):
    result = run(["list-clients", "-F", CLIENT_FORMAT], base, check=False)
    if result.returncode:
        return []
    out = []
    for line in result.stdout.splitlines():
        parts = line.split(SEP, 3)
        if len(parts) == 4 and parts[0].isdigit():
            out.append(dict(pid=int(parts[0]), tty=parts[1], session_id=parts[2], name=clean(parts[3])))
    return out


def client_exists(client):
    return any(c["name"] == client for c in clients())


def switch(client, session_id):
    # -c identifies one client; a session ID is resolved by tmux, never a label.
    run(["switch-client", "-c", client, "-t", session_id])
