import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import VERSION, config_sync, connection, integration, inventory, native, picker, tmux
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
    p.add_argument("--search", action="store_true")
    p.add_argument("--pane")
    p = sub.add_parser("native", help="open the native tmux session tree with remote entries")
    p.add_argument("--client")
    p = sub.add_parser("native-select", help=argparse.SUPPRESS)
    p.add_argument("--client", required=True)
    p.add_argument("--navigation", required=True)
    p.add_argument("--target", required=True)
    p = sub.add_parser("native-refresh", help=argparse.SUPPRESS)
    p.add_argument("--client", required=True)
    p.add_argument("--navigation", required=True)
    p.add_argument("--pane", required=True)
    p = sub.add_parser("popup", help=argparse.SUPPRESS)
    p.add_argument("--client", required=True)
    p.add_argument("--search", action="store_true")
    p.add_argument("--pane")
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
    p = sub.add_parser("new-session", help=argparse.SUPPRESS)
    p.add_argument("directory")
    p.add_argument("opaque_id", nargs="?", default="")
    p = sub.add_parser("delete-session", help=argparse.SUPPRESS)
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
    p.add_argument("action", choices=["check", "diff", "sync"])
    group = p.add_mutually_exclusive_group()
    group.add_argument("host_id", nargs="?")
    group.add_argument("--all", action="store_true", dest="all_hosts")
    p.add_argument("--source", help="shared tmux config (default: ~/.tmux.conf)")
    p = sub.add_parser("supervise", help=argparse.SUPPRESS)
    p.add_argument("token")
    p.add_argument("host_id")
    p.add_argument("opaque_id")
    args = parser.parse_args(argv)
    try:
        config = load()
        if args.command == "config":
            if args.action != "check":
                return config_sync.execute(config, args.action, args.host_id, args.all_hosts, args.source)
            if args.host_id or args.all_hosts or args.source:
                raise ValueError("config check validates config.toml; use config diff or config sync for tmux.conf")
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
                for option in ("--id-nth", "--track", "--listen", "--read0", "--no-input", "--footer"):
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
        elif args.command == "native":
            args.client = _client(args.client)
            native.open_tree(config, args.client, BIN)
        elif args.command == "native-select":
            native.select(config, args.client, args.target, args.navigation)
        elif args.command == "native-refresh":
            native.refresh(config, args.client, args.pane, args.navigation)
        elif args.command in ("pick", "popup"):
            client = _client(args.client)
            origin = args.pane or native._client_pane(client)
            if args.search and native._client_pane(client) != origin:
                return 0
            popup = None
            if args.command == "popup":
                rows = tmux.run(["list-clients", "-F", "#{client_name}|#{client_width}|#{client_height}|#{pane_width}|#{pane_height}"]).stdout.splitlines()
                dimensions = next((line.rsplit("|", 4) for line in rows if line.startswith(client + "|")), None)
                if not dimensions:
                    raise RuntimeError("cannot open tmux popup: invoking client disappeared")
                popup = tuple(map(int, dimensions[3:] if args.search else dimensions[1:3]))
                if popup[0] < 45 or popup[1] < 14:
                    tmux.run(["display-message", "-c", client, "tmux-roam: terminal too small; opening native picker"])
                    tmux.run(["choose-tree", "-Zs", "-t", origin])
                    return 0
            tree_token = ""
            if args.search and tmux.run(["display-message", "-p", "-t", origin, "#{pane_mode}"]).stdout.strip() == "tree-mode":
                tree_token = tmux.run(["show-options", "-gqv", picker._navigation_option(client)]).stdout.strip()
            # Search is an overlay within this tree navigation. Reuse its token
            # so Escape can preserve the selection, expansion and refresh worker.
            token = tree_token or picker.begin_navigation(client)
            keep_tree = False
            previous_hup = signal.getsignal(signal.SIGHUP)
            previous_term = signal.getsignal(signal.SIGTERM)
            def cancelled(signum, frame):
                raise RuntimeError("navigation cancelled")
            signal.signal(signal.SIGHUP, cancelled)
            signal.signal(signal.SIGTERM, cancelled)
            try:
                record = picker.pick(config, client, search=args.search, popup=popup, pane=origin)
                if record:
                    if args.search and picker.navigation_valid(client, token):
                        native.close_tree(origin)
                    if not popup:
                        print(f"Validating {record.host_label}/{record.name}…", flush=True)
                    elif record.host_id != "local":
                        tmux.run(["display-message", "-c", client, f"Connecting to {tmux.clean(record.host_label)}/{tmux.clean(record.name)}…"])
                    _open(record, config, client, token)
                elif (args.search and picker.navigation_valid(client, token) and
                      native._client_pane(client) == origin):
                    if tree_token and tmux.run(["display-message", "-p", "-t", origin, "#{pane_mode}"]).stdout.strip() == "tree-mode":
                        tmux.run(["switch-client", "-c", client, "-T", native.BROWSE_TABLE])
                        keep_tree = True
                    else:
                        native.open_tree(config, client, BIN, searchable=True)
            finally:
                if not keep_tree:
                    if args.search and picker.navigation_valid(client, token):
                        native.close_tree(origin)
                    picker.end_navigation(client, token)
                signal.signal(signal.SIGHUP, previous_hup)
                signal.signal(signal.SIGTERM, previous_term)
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
        elif args.command == "new-session":
            picker.new_session(args.directory, args.opaque_id, config)
        elif args.command == "delete-session":
            picker.delete_session(args.directory, args.opaque_id, config)
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
    except (EOFError, KeyboardInterrupt):
        return 0
    except (ConfigError, tmux.TmuxError, RuntimeError, ValueError) as exc:
        if args.command in ("native", "native-select", "native-refresh") and args.client:
            tmux.run(["display-message", "-c", args.client, "tmux-roam: " + tmux.clean(str(exc))], check=False)
            return 0
        print(f"tmux-roam: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
