import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import VERSION, connection, integration, inventory, picker, tmux
from .config import ConfigError, load

BIN = str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam")


def _client(value):
    if value:
        return value
    result = tmux.run(["display-message", "-p", "#{client_name}"], check=False)
    if not result.stdout.strip():
        raise RuntimeError("no invoking tmux client; pass --client from a tmux binding")
    return result.stdout.strip()


def _open(record, config, client, token=None):
    if token and not picker.navigation_valid(client, token):
        raise RuntimeError("navigation cancelled or superseded")
    if record.host_id == "local":
        current, error = inventory.local()
        if error or record.id not in {row.id for row in current}:
            raise RuntimeError("local session ended; refresh the picker")
        if token and not picker.navigation_valid(client, token):
            raise RuntimeError("navigation cancelled or superseded")
        tmux.switch(client, record.session_id)
    else:
        host = next((h for h in config.hosts if h.id == record.host_id and h.enabled), None)
        if not host:
            raise RuntimeError(f"host {record.host_id} is not configured or is disabled")
        connection.open_remote(record, host, config, client, token)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tmux-roam")
    parser.add_argument("--version", action="version", version=VERSION)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pick")
    p.add_argument("--client")
    p = sub.add_parser("popup", help=argparse.SUPPRESS)
    p.add_argument("--client", required=True)
    p = sub.add_parser("picker-state", help=argparse.SUPPRESS)
    p.add_argument("directory")
    p.add_argument("action", choices=["filter", "hidden", "refresh"])
    p = sub.add_parser("picker-header", help=argparse.SUPPRESS)
    p.add_argument("directory")
    p = sub.add_parser("picker-select", help=argparse.SUPPRESS)
    p.add_argument("directory")
    p.add_argument("opaque_id")
    p = sub.add_parser("details", help=argparse.SUPPRESS)
    p.add_argument("directory")
    p.add_argument("opaque_id")
    sub.add_parser("help", help=argparse.SUPPRESS)
    p = sub.add_parser("actions", help=argparse.SUPPRESS)
    p.add_argument("directory")
    p.add_argument("opaque_id")
    p = sub.add_parser("list")
    p.add_argument("--json", action="store_true")
    p.add_argument("--cached", action="store_true")
    p = sub.add_parser("refresh")
    p.add_argument("host_id", nargs="?")
    p = sub.add_parser("open")
    p.add_argument("opaque_id")
    p.add_argument("--client")
    sub.add_parser("doctor")
    p = sub.add_parser("integrate", help=argparse.SUPPRESS)
    p.add_argument("action", choices=["install", "unload"])
    p = sub.add_parser("config")
    p.add_argument("action", choices=["check"])
    p = sub.add_parser("supervise", help=argparse.SUPPRESS)
    p.add_argument("token")
    p.add_argument("host_id")
    p.add_argument("opaque_id")
    args = parser.parse_args(argv)
    try:
        config = load()
        if args.command == "config":
            print(f"OK: {config.path} ({len(config.hosts)} hosts)")
        elif args.command == "list":
            rows, errors = inventory.all_records(config, refresh=not args.cached)
            if args.json:
                print(json.dumps(dict(schema_version=1, sessions=[asdict(row) for row in rows], errors=errors), ensure_ascii=False))
            else:
                for row in rows:
                    print(f"{row.host_label:16} {row.name:32} {row.windows:3}w {row.source:7} {row.id}")
                for host, error in errors.items():
                    print(f"{host}: {error}", file=sys.stderr)
            return 0 if not errors else 2
        elif args.command == "refresh":
            hosts = [h for h in config.hosts if h.enabled and (not args.host_id or h.id == args.host_id)]
            if args.host_id and not hosts:
                raise RuntimeError(f"unknown or disabled host: {args.host_id}")
            errors = {}
            for host in hosts:
                rows, error = inventory.remote(host, config, True)
                if error:
                    errors[host.id] = error
                else:
                    inventory.save(host, rows)
                    print(f"{host.id}: {len(rows)} sessions")
            for host, error in errors.items():
                print(f"{host}: {error}", file=sys.stderr)
            return 0 if not errors else 2
        elif args.command == "doctor":
            print(f"tmux-roam {VERSION}; config: {config.path}")
            bad = False
            for binary in ("tmux", "fzf", "ssh"):
                path = shutil.which(binary)
                print(f"{binary}: {path or 'MISSING'}")
                bad |= path is None
            print(f"runtime Python: {sys.executable}")
            if shutil.which("tmux"):
                print("tmux version:", tmux.run(["-V"], base=["tmux"], check=False).stdout.strip())
            if shutil.which("fzf"):
                version = subprocess.run(["fzf", "--version"], capture_output=True, text=True).stdout.strip()
                help_text = subprocess.run(["fzf", "--help"], capture_output=True, text=True).stdout
                print("fzf version:", version)
                for option in ("--id-nth", "--track", "--listen", "--read0"):
                    if option not in help_text:
                        print(f"fzf missing required option: {option}")
                        bad = True
            print(f"hosts: {', '.join(h.id for h in config.hosts if h.enabled) or '(none)'}")
            rows, error = inventory.local()
            print(f"local sessions: {len(rows)}" if not error else f"local: {error}")
            for host in config.hosts:
                if not host.enabled:
                    continue
                remote_rows, remote_error = inventory.remote(host, config)
                print(f"{host.id}: {len(remote_rows)} sessions" if not remote_error else f"{host.id}: {remote_error}")
                bad |= remote_error is not None
            return 2 if bad else 0
        elif args.command == "integrate":
            if args.action == "install":
                print(f"installed {integration.install(BIN)} chooser bindings")
            else:
                print(f"restored {integration.unload()} chooser bindings")
        elif args.command == "pick":
            client = _client(args.client)
            token = picker.begin_navigation(client)
            previous_hup = signal.getsignal(signal.SIGHUP)
            previous_term = signal.getsignal(signal.SIGTERM)
            def cancelled(signum, frame):
                raise RuntimeError("navigation cancelled")
            signal.signal(signal.SIGHUP, cancelled)
            signal.signal(signal.SIGTERM, cancelled)
            try:
                record = picker.pick(config, client)
                if record:
                    print(f"Validating {record.host_label}/{record.name}…", flush=True)
                    _open(record, config, client, token)
            finally:
                picker.end_navigation(client, token)
                signal.signal(signal.SIGHUP, previous_hup)
                signal.signal(signal.SIGTERM, previous_term)
        elif args.command == "popup":
            client = _client(args.client)
            client_rows = tmux.run(["list-clients", "-F", "#{client_name}|#{client_width}|#{client_height}|#{pane_id}"]).stdout.splitlines()
            dimensions = next((line.rsplit("|", 3) for line in client_rows if line.startswith(client + "|")), None)
            if dimensions and (int(dimensions[1]) < 45 or int(dimensions[2]) < 14):
                tmux.run(["display-message", "-c", client, "tmux-roam: terminal too small; opening native picker"])
                tmux.run(["choose-tree", "-Zs", "-t", dimensions[3]])
                return 0
            binary = BIN
            command = (f"{shlex.quote(binary)} pick --client {shlex.quote(client)}; status=$?; "
                       "if [ $status -ne 0 ]; then printf '\\nPress Enter to continue.'; read _; fi")
            popup_args = ["display-popup", "-c", client, "-E", "-w", config.width, "-h", config.height,
                          "-e", "TMUX_ROAM_CONFIG=" + str(config.path)]
            if os.environ.get("TMUX_ROAM_SSH_CONFIG"):
                popup_args += ["-e", "TMUX_ROAM_SSH_CONFIG=" + os.environ["TMUX_ROAM_SSH_CONFIG"]]
            result = subprocess.run(tmux.args() + popup_args + [command], stdin=subprocess.DEVNULL)
            if result.returncode:
                raise RuntimeError("cannot open tmux popup")
        elif args.command == "picker-state":
            picker.state_action(args.directory, args.action)
        elif args.command == "picker-header":
            print(picker.mode_header(args.directory))
        elif args.command == "picker-select":
            print(picker.select_action(args.directory, args.opaque_id))
        elif args.command == "details":
            picker.details(args.directory, args.opaque_id)
        elif args.command == "help":
            picker.help_text()
        elif args.command == "actions":
            picker.actions(args.directory, args.opaque_id, config)
        elif args.command == "open":
            client = _client(args.client)
            rows, _ = inventory.all_records(config, refresh=False)
            record = next((row for row in rows if row.id == args.opaque_id), None)
            if not record:
                raise RuntimeError("unknown session identity; refresh inventory")
            _open(record, config, client)
        elif args.command == "supervise":
            host = next((h for h in config.hosts if h.id == args.host_id), None)
            if not host:
                raise RuntimeError("configured host missing")
            rows = inventory.cached(host)
            record = next((r for r in rows if r.id == args.opaque_id), None)
            if not record:
                raise RuntimeError("cached session identity missing")
            connection.supervise(args.token, host, record, config)
        return 0
    except (ConfigError, tmux.TmuxError, RuntimeError, ValueError) as exc:
        print(f"tmux-roam: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
