import os
import base64
import fcntl
import json
import pty
import re
import select
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
import uuid
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from tmux_roam import cli, connection, integration, inventory, native, picker, tmux
from tmux_roam.config import Config, ConfigError, Host, load


class LauncherContracts(unittest.TestCase):
    def test_working_directory_cannot_shadow_installed_package(self):
        binary = Path(__file__).resolve().parent.parent / "bin" / "tmux-roam"
        with tempfile.TemporaryDirectory() as directory:
            shadow = Path(directory) / "tmux_roam"
            shadow.mkdir()
            (shadow / "__init__.py").write_text('raise RuntimeError("imported working directory")\n')
            result = subprocess.run([str(binary), "--version"], cwd=directory,
                                    capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        from tmux_roam import VERSION
        self.assertEqual(result.stdout.strip(), VERSION)

    @unittest.skipUnless(shutil.which("zsh"), "zsh is not installed")
    def test_popup_wrapper_runs_under_zsh(self):
        run = subprocess.run
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "rows"
            snapshot.write_bytes(b"")
            for code in (0, 1, 2, 130):
                def shell(command, **kwargs):
                    result = run(["zsh", "-c", command[-1]], input="\n",
                                 capture_output=True, text=True)
                    self.assertEqual(result.returncode, code, result.stderr)
                    self.assertEqual("Press Enter" in result.stdout, code == 2)
                    return result
                with self.subTest(code=code), patch.object(subprocess, "run", side_effect=shell):
                    def launch():
                        return picker._popup_fzf(["sh", "-c", f"exit {code}"], snapshot,
                                                 Config(Path("/unused"), ()), "/dev/pts/test", ("80", "20"))
                    if code == 2:
                        with self.assertRaisesRegex(RuntimeError, "fzf failed"):
                            launch()
                    else:
                        self.assertEqual(launch().returncode, code)


class ConfigContracts(unittest.TestCase):
    def test_rejects_unknown_and_duplicate_host_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('version = 1\n[picker]\nunknown = true\n')
            with self.assertRaisesRegex(ConfigError, "unknown key"):
                load(path)
            path.write_text('version = 1\n[[hosts]]\nid = "same"\nssh = "one"\n'
                            '[[hosts]]\nid = "same"\nssh = "two"\n')
            with self.assertRaisesRegex(ConfigError, "duplicate id"):
                load(path)

    def test_identity_separates_host_server_and_creation(self):
        a = inventory.identity("one", "/tmp/socket", "12/100", "$1", 100)
        for different in (
            inventory.identity("two", "/tmp/socket", "12/100", "$1", 100),
            inventory.identity("one", "/tmp/socket", "13/101", "$1", 100),
            inventory.identity("one", "/tmp/socket", "12/100", "$1", 101),
        ):
            self.assertNotEqual(a, different)
        self.assertEqual(inventory.decode_identity(a), ["one", "/tmp/socket", "12/100", "$1", 100])
        with self.assertRaises(ValueError):
            inventory.decode_identity("not-an-identity")


class PickerLayoutContracts(unittest.TestCase):
    def test_terminal_cells_and_long_names_keep_columns_aligned(self):
        self.assertEqual(picker._fit("🌲pnl", 8), "🌲pnl   ")
        self.assertEqual(picker._fit("e\u0301", 3), "e\u0301  ")
        self.assertEqual(picker._fit("👩‍💻project", 5), "👩‍💻pr…")
        self.assertEqual(picker._fit("项目", 6), "项目  ")
        config = Config(Path("/unused"), (Host("omni", "Omni", "unused"),))
        records = inventory._records("omni", "Omni", ("1", "1", "/socket"),
                                     [dict(session_id="$1", created=1, name="👩‍💻" + "long-project" * 12, windows=12, clients=0)], "live")
        with patch.dict(os.environ, {"TMUX_ROAM_COLUMNS": "60", "TMUX_ROAM_CLIENT": ""}), \
             patch.object(picker, "_current_target", return_value=(None, records[0].id)):
            line = picker._rows(records, {"omni": "network unavailable"}, config)[0]
            ident, label = line.split("\t", 1)
            plain = re.sub(r"\x1b\[[0-9;]*m", "", label)
            self.assertEqual(ident, records[0].id)
            self.assertTrue(plain.startswith(" Omni/👩‍💻"))
            self.assertIn("…", plain)
            self.assertIn("… *", plain)
            self.assertTrue(plain.endswith("Offline"))
            self.assertIn("\x1b[38;2;128;135;162mOffline", label)
            self.assertLessEqual(picker._width(plain), 54)
            self.assertEqual(len(picker._rows(records, {"omni": "network unavailable"}, config)), 1)
        self.assertEqual(picker.popup_size(config, 220, 80, 5), ("96", "17"))
        width, height = map(int, picker.popup_size(config, 60, 18, 50))
        self.assertLessEqual(width, 58)
        self.assertLessEqual(height, 16)


class DiscoveryContracts(unittest.TestCase):
    def test_cancelling_refresh_stops_its_command_promptly(self):
        cancelled = threading.Event()
        timer = threading.Timer(.1, cancelled.set)
        timer.start()
        start = time.monotonic()
        try:
            _, _, _, failure = inventory.capture_bounded(
                [sys.executable, "-c", "import time; time.sleep(30)"], 5, cancel=cancelled)
        finally:
            timer.cancel()
        self.assertEqual(failure, "command cancelled")
        self.assertLess(time.monotonic() - start, 2)


class IsolatedTmuxContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tmux-roam-test-")
        self.name = "roam-test-" + uuid.uuid4().hex[:10]
        self.base = ["tmux", "-L", self.name]
        subprocess.run(self.base + ["-f", "/dev/null", "new-session", "-d", "-s", "start"], check=True)
        self.env = patch.dict(os.environ, {"TMUX": f"/tmp/tmux-{os.getuid()}/{self.name},0,0",
                                          "XDG_RUNTIME_DIR": self.directory.name,
                                          "XDG_CACHE_HOME": str(Path(self.directory.name) / "cache"),
                                          "TMUX_ROAM_CONFIG": str(Path(self.directory.name) / "config.toml")})
        Path(self.directory.name, "config.toml").write_text("version = 1\n")
        self.env.start()
        for key in ("XDG_RUNTIME_DIR", "XDG_CACHE_HOME"):
            tmux.run(["set-environment", "-g", key, os.environ[key]])

    def tearDown(self):
        self.env.stop()
        subprocess.run(self.base + ["kill-server"], capture_output=True)
        self.directory.cleanup()

    def test_inventory_handles_punctuation_and_ignores_owned_proxy(self):
        name = 'pipe|quote" space 🐱'
        subprocess.run(self.base + ["new-session", "-d", "-s", name], check=True)
        rows, error = inventory.local()
        self.assertIsNone(error)
        self.assertIn(name, [r.name for r in rows])
        proxy = next(r for r in rows if r.name == name)
        tmux.run(["set-option", "-t", proxy.session_id, "@tmux-roam-owner", "1"])
        rows, _ = inventory.local()
        self.assertNotIn(name, [r.name for r in rows])

    def test_explicit_remote_creation_uses_directory_and_preserves_existing_sessions(self):
        host = Host("remote", "Remote", "unused", socket_name=self.name)
        config = load()
        project = Path(self.directory.name) / "quote' space; $(touch SHOULD_NOT_EXIST) #{session_name}"
        project.mkdir()
        name = "project 'quoted'|🐱 $(true)"
        # Exercise the actual quoted remote shell and tmux commands without SSH.
        with patch.object(inventory, "ssh_base", return_value=["sh", "-c"]):
            record = connection.create_remote(host, config, name, str(project))
            current = tmux.run(["display-message", "-p", "-t", record.session_id,
                                "#{session_name}\n#{pane_current_path}"]).stdout.splitlines()
            self.assertEqual(current, [name, str(project)])
            rows, error = inventory.remote(host, config)
            self.assertIsNone(error)
            self.assertIn(record.id, {r.id for r in rows})
            before = tmux.sessions()
            with self.assertRaisesRegex(RuntimeError, "duplicate session"):
                connection.create_remote(host, config, name, str(project))
            with self.assertRaises(RuntimeError):
                connection.create_remote(host, config, "missing-directory", str(project / "missing"))
            self.assertEqual(tmux.sessions(), before)
            home = connection.create_remote(host, config, "home-project", "~")
            self.assertEqual(tmux.run(["display-message", "-p", "-t", home.session_id,
                                       "#{pane_current_path}"]).stdout.strip(), str(Path.home()))
            self.assertFalse((project / "SHOULD_NOT_EXIST").exists())
            for bad_name in ("", "project:one", "project.one", ";", "bad\nname"):
                with self.assertRaises(ValueError):
                    connection.create_remote(host, config, bad_name, "~")
            with self.assertRaises(ValueError):
                connection.create_remote(host, config, "relative-path", "work/project")

    def test_deletion_targets_exact_identity_locally_and_remotely(self):
        host = Host("remote", "Remote", "unused", socket_name=self.name)
        config = Config(Path(self.directory.name) / "config.toml", (host,))
        with patch.object(inventory, "ssh_base", return_value=["sh", "-c"]):
            for remote in (False, True):
                with self.subTest(remote=remote):
                    name = "delete 'quoted'|🐱 $(true)"
                    tmux.run(["new-session", "-d", "-s", name])
                    rows, error = inventory.remote(host, config) if remote else inventory.local()
                    self.assertIsNone(error)
                    record = next(r for r in rows if r.name == name)
                    # A stale discovery response must still fail the server-side guard.
                    for fields in ({"generation": "1/1"}, {"created": record.created + 1}):
                        stale = replace(record, **fields)
                        with patch.object(inventory, "remote" if remote else "local", return_value=([stale], None)):
                            with self.assertRaisesRegex(RuntimeError, "changed while deleting"):
                                connection.delete_session(stale, config)
                        self.assertIn(record.session_id, {s["session_id"] for s in tmux.sessions()})
                    if remote:
                        with patch.object(inventory, "remote", return_value=([], "offline")):
                            with self.assertRaisesRegex(RuntimeError, "offline"):
                                connection.delete_session(record, config)
                    connection.delete_session(record, config)
                    self.assertNotIn(record.session_id, {s["session_id"] for s in tmux.sessions()})
                    tmux.run(["new-session", "-d", "-s", name])
                    with self.assertRaisesRegex(RuntimeError, "ended or server restarted"):
                        connection.delete_session(record, config)
                    self.assertIn(name, {s["name"] for s in tmux.sessions()})
                    tmux.run(["kill-session", "-t", "=" + name])
            self.assertEqual([s["name"] for s in tmux.sessions()], ["start"])

    @unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
    def test_fzf_vi_navigation_and_search_preserve_literal_input(self):
        root = Path(self.directory.name)
        rows, selected, sock = root / "rows", root / "selected", root / "fzf.sock"
        rows.write_text("alpha\nbeta\njk-project\n")
        tmux.run(["set-option", "-g", "@tmux-roam-chooser-keys", "b и"])
        deleted = root / "deleted"
        delete_command = "printf '%s\\n' {1} >> " + shlex.quote(str(deleted))
        command = ["fzf", "--no-sort", "--listen=" + str(sock), *picker._fzf_options("Test", delete_command=delete_command),
                   "--bind=b:show-input+change-query(TOGGLED)"]  # Observe the toggle without a real popup client.
        pid, fd = pty.fork()
        if pid == 0:
            os.dup2(os.open(rows, os.O_RDONLY), 0)
            os.dup2(os.open(selected, os.O_WRONLY | os.O_CREAT, 0o600), 1)
            os.execvpe("fzf", command, {**picker._fzf_env(), "TERM": "xterm-256color"})
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        def state():
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(.5)
                client.connect(str(sock))
                client.sendall(b"GET / HTTP/1.0\r\n\r\n")
                response = bytearray()
                while block := client.recv(65536):
                    response.extend(block)
            return json.loads(response.split(b"\r\n\r\n", 1)[1])
        def wait_for(predicate):
            end = time.monotonic() + 3
            while time.monotonic() < end:
                if select.select([fd], [], [], .02)[0]:
                    output = os.read(fd, 65536)
                    if b"\x1b[6n" in output:
                        os.write(fd, b"\x1b[1;1R")
                try:
                    if predicate(state()):
                        return
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                time.sleep(.02)
            self.fail("fzf did not reach the expected vi/search state: " + repr(state()))
        try:
            wait_for(lambda s: s["current"]["text"] == "alpha")
            for key, text in ((b"j", "beta"), (b"k", "alpha"), ("о".encode(), "beta"),
                              ("л".encode(), "alpha"), (b"G", "jk-project"), (b"g", "alpha")):
                os.write(fd, key)
                wait_for(lambda s: s["current"]["text"] == text and s["query"] == "")
            for keys in (b"dd", "вв".encode()):
                deleted.unlink(missing_ok=True)
                os.write(fd, keys)
                wait_for(lambda s: deleted.exists() and deleted.read_text() == "alpha\n")
            deleted.unlink()
            # An intervening navigation key or even an ignored character cancels d.
            for keys in (b"djd", b"dxd"):
                os.write(fd, keys)
                wait_for(lambda s: s["current"]["text"] == "beta")
                time.sleep(.1)
                self.assertFalse(deleted.exists())
                os.write(fd, b"\x1b")  # Cancel pending d without closing the picker.
                time.sleep(.15)
            os.write(fd, b"g")
            wait_for(lambda s: s["current"]["text"] == "alpha")
            os.write(fd, b"/")
            os.write(fd, "ddвв".encode())
            wait_for(lambda s: s["query"] == "ddвв")
            self.assertFalse(deleted.exists())
            os.write(fd, b"\x7f\x7f\x7f\x7f")
            wait_for(lambda s: s["query"] == "")
            os.write(fd, b"jkb")
            wait_for(lambda s: s["query"] == "jkb")  # b is text until the prefix arms the toggle.
            os.write(fd, b"\x7f\x1b")
            time.sleep(.15)  # A standalone Escape must not become Alt+g.
            os.write(fd, b"g")
            wait_for(lambda s: s["query"] == "jk" and s["current"]["text"] == "jk-project")
            os.write(fd, "ш".encode())
            os.write(fd, b"-project")
            wait_for(lambda s: s["query"] == "jk-project")
            os.write(fd, b"\x1b")
            time.sleep(.15)
            picker._post(sock, "show-input+clear-query+hide-input")
            wait_for(lambda s: s["current"]["text"] == "alpha" and s["query"] == "")
            os.write(fd, b"\x1ba")
            time.sleep(.1)
            rows.write_text("beta\nalpha\njk-project\n")
            picker._post(sock, "reload-sync(cat " + shlex.quote(str(rows)) + ")")
            wait_for(lambda s: s["current"]["text"] == "beta")
            os.write(fd, b"b")
            wait_for(lambda s: s["query"] == "TOGGLED")  # A background focus change must not disarm it.
            picker._post(sock, "show-input+change-query(jk-project)+hide-input")
            wait_for(lambda s: s["current"]["text"] == "jk-project")
            os.write(fd, b"G\r")
            end = time.monotonic() + 3
            while time.monotonic() < end and not selected.read_text():
                if select.select([fd], [], [], .02)[0]:
                    try:
                        os.read(fd, 65536)
                    except OSError:
                        break
                time.sleep(.02)
            self.assertEqual(selected.read_text(), "jk-project\n")
        finally:
            os.close(fd)
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)

    @unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
    def test_popup_toggle_closes_without_launcher_error(self):
        binary = str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam")
        tmux.run(["set-option", "-g", "@tmux-roam-chooser-keys", "b и"])
        env = {**os.environ, "TERM": "xterm-256color"}
        env.pop("TMUX", None)
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("tmux", self.base + ["attach-session", "-t", "start"], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
        output = bytearray()
        stop = threading.Event()
        def drain():
            while not stop.is_set():
                try:
                    if select.select([fd], [], [], .05)[0]:
                        data = os.read(fd, 65536)
                        if not data:
                            return
                        output.extend(data)
                        if b"\x1b[6n" in data:
                            os.write(fd, b"\x1b[1;1R")
                except OSError:
                    return
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        def wait_for(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(.03)
            self.fail("popup did not reach the expected state")
        def query():
            sock = next(connection.runtime_dir().glob("picker-*/fzf.sock"))
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(.5)
                client.connect(str(sock))
                client.sendall(b"GET / HTTP/1.0\r\n\r\n")
                response = bytearray()
                while block := client.recv(65536):
                    response.extend(block)
            return json.loads(response.split(b"\r\n\r\n", 1)[1])["query"]
        launchers = []
        try:
            wait_for(lambda: bool(tmux.clients()))
            client = tmux.clients()[0]["name"]
            original_session = tmux.clients()[0]["session_id"]
            for keys, nested in (("\x1bab", ""), ("\x1bфи", "actions"), ("\x1bab", "delete"), ("\x1bфи", "delete"),
                                 ("\x1b", "escape-empty"), ("\x1b", "escape-filtered"), ("\x1b", "escape-no-match")):
                with self.subTest(keys=keys, nested=nested):
                    output.clear()
                    launcher = subprocess.Popen([binary, "popup", "--client", client],
                                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, text=True)
                    launchers.append(launcher)
                    wait_for(lambda: b"Ctrl-o Navigate" in output)
                    time.sleep(.2)
                    if not nested:
                        os.write(fd, "ddввjkол".encode())
                        wait_for(lambda: query() == "ddввjkол")
                        self.assertEqual({s["name"] for s in tmux.sessions()}, {"start"})
                        os.write(fd, b"\x7f" * len("ddввjkол"))
                        wait_for(lambda: query() == "")
                    elif nested == "actions":
                        os.write(fd, b"\x1bOQ")
                        wait_for(lambda: b"Open/retry" in output)
                        time.sleep(.2)
                    elif nested == "delete":
                        os.write(fd, b"\x0f")  # Ctrl-o enters vi navigation.
                        wait_for(lambda: b"dd Delete" in output)
                        time.sleep(.2)
                        os.write(fd, b"dd")
                        wait_for(lambda: b"Delete session?" in output)
                        time.sleep(.2)
                    elif nested in ("escape-filtered", "escape-no-match"):
                        text = "sta" if nested == "escape-filtered" else "no-such-session"
                        os.write(fd, text.encode())
                        wait_for(lambda: query() == text)
                        if nested == "escape-no-match":
                            wait_for(lambda: b"No matching sessions" in output and b"Esc Close" in output)
                    os.write(fd, keys[:-1].encode())
                    time.sleep(.15)
                    os.write(fd, keys[-1].encode())
                    stdout, stderr = launcher.communicate(timeout=5)
                    self.assertEqual(launcher.returncode, 0, stderr)
                    self.assertEqual(stdout + stderr, "")
                    self.assertEqual(tmux.clients()[0]["session_id"], original_session)
            tmux.run(["new-session", "-d", "-s", "delete-me"])
            output.clear()
            launcher = subprocess.Popen([binary, "popup", "--client", client],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
            launchers.append(launcher)
            wait_for(lambda: b"Ctrl-o Navigate" in output)
            os.write(fd, b"\x0f")
            wait_for(lambda: b"dd Delete" in output)
            time.sleep(.2)
            os.write(fd, b"gdd")
            wait_for(lambda: b"Delete session?" in output and b"Local / delete-me" in output)
            time.sleep(.2)
            output.clear()
            os.write(fd, b"\r")  # Cancel is selected by default.
            wait_for(lambda: b"dd Delete" in output)
            self.assertIn("delete-me", {s["name"] for s in tmux.sessions()})
            time.sleep(.2)
            output.clear()
            os.write(fd, b"dd")
            wait_for(lambda: b"Delete session?" in output)
            time.sleep(.2)
            os.write(fd, b"j\r")
            wait_for(lambda: {s["name"] for s in tmux.sessions()} == {"start"})
            snapshots = list(connection.runtime_dir().glob("picker-*/rows"))
            self.assertEqual(len(snapshots), 1)
            wait_for(lambda: b"delete-me" not in snapshots[0].read_bytes())
            wait_for(lambda: b"dd Delete" in output)
            time.sleep(.2)
            os.write(fd, b"\x1ba")
            time.sleep(.15)
            os.write(fd, b"b")
            stdout, stderr = launcher.communicate(timeout=5)
            self.assertEqual((launcher.returncode, stdout, stderr), (0, "", ""))
            failed = subprocess.run([binary, "popup", "--client", "/dev/pts/nonexistent"],
                                    capture_output=True, text=True, timeout=5)
            self.assertEqual(failed.returncode, 2)
            self.assertIn("cannot open tmux popup", failed.stderr)
        finally:
            subprocess.run(self.base + ["kill-server"], capture_output=True)
            for launcher in launchers:
                if launcher.poll() is None:
                    launcher.communicate(timeout=5)
            stop.set()
            reader.join(timeout=1)
            os.close(fd)
            os.waitpid(pid, 0)

    def test_install_unload_preserves_user_binding_and_later_edit(self):
        tmux.run(["bind-key", "-T", "prefix", "s", "choose-tree", "-Zs"])
        original = integration._binding("prefix", "s")
        last_session = integration._binding("prefix", "L")
        tmux.run(["bind-key", "-T", "prefix", "Д", "display-message", "custom-session"])
        integration.install(str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam"))
        self.assertIn("run-shell", integration._binding("prefix", "s"))
        self.assertIn("switch-client -l", integration._binding("prefix", "Д"))
        integration.unload()
        self.assertEqual(original, integration._binding("prefix", "s"))
        self.assertEqual(last_session, integration._binding("prefix", "L"))
        self.assertIn("custom-session", integration._binding("prefix", "Д"))
        integration.install(str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam"))
        tmux.run(["bind-key", "-T", "prefix", "s", "display-message", "custom"])
        integration.unload()
        self.assertIn("custom", integration._binding("prefix", "s"))

    def test_spare_binding_can_be_promoted_without_leaking_key(self):
        binary = str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam")
        tmux.run(["unbind-key", "-q", "-T", "prefix", "F9"])
        tmux.run(["set-option", "-g", "@tmux-roam-chooser-keys", "F9"])
        tmux.run(["set-option", "-g", "@tmux-roam-native-keys", "s ы S Ы"])
        integration.install(binary)
        self.assertIn("run-shell", integration._binding("prefix", "F9"))
        for table in ("prefix", "roam-prefix-ma", "roam-prefix-cb", "roam-prefix-none"):
            self.assertIn("run-shell", integration._binding(table, "F9"))
            for key in ("s", "ы"):
                self.assertIn("choose-tree -Zs", integration._binding(table, key))
        tmux.run(["set-option", "-g", "@tmux-roam-chooser-keys", "s ы"])
        tmux.run(["set-option", "-g", "@tmux-roam-native-keys", "S Ы"])
        integration.install(binary)
        for table in ("prefix", "roam-prefix-ma", "roam-prefix-cb", "roam-prefix-none"):
            self.assertEqual("", integration._binding(table, "F9"))
            self.assertIn("run-shell", integration._binding(table, "s"))
        integration.unload()

    def test_new_picker_supersedes_older_navigation(self):
        first = picker.begin_navigation("/dev/pts/test")
        second = picker.begin_navigation("/dev/pts/test")
        self.assertFalse(picker.navigation_valid("/dev/pts/test", first))
        self.assertTrue(picker.navigation_valid("/dev/pts/test", second))
        picker.end_navigation("/dev/pts/test", first)
        self.assertTrue(picker.navigation_valid("/dev/pts/test", second))
        picker.end_navigation("/dev/pts/test", second)
        self.assertFalse(picker.navigation_valid("/dev/pts/test", second))

    def test_reuse_uses_one_snapshot_and_rejects_changed_identity(self):
        host = Host("remote", "Remote", "example")
        config = Config(Path(self.directory.name) / "config.toml", (host,))
        record = inventory._records("remote", "Remote", ("12", "100", "/tmp/remote"),
                                    [dict(session_id="$1", created=101, name="pipe|name", windows=2, clients=1)], "live")[0]
        # Older failed entries must not hide a working connection later in the list.
        failed, failed_token = native._placeholder(record)
        connection._write_state(failed_token, dict(status="disconnected", error="SSH exited (255)"))
        local = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}", "-s", "proxy"]).stdout.strip()
        for option, value in ((connection.OWNER, "1"), (connection.IDENTITY, record.id), (connection.STATE, "test-token")):
            tmux.run(["set-option", "-t", local, option, value])
        connection._write_state("test-token", dict(status="open", pid=42, tty="/dev/pts/99"))
        response = ("12|100|/tmp/remote\n$1|101|pipe|name|2|1|M-a\n" +
                    inventory.CLIENT_MARKER + "\n42|/dev/pts/99|$1|client\n" + inventory.WINDOW_MARKER +
                    "\n$1|@1|0|1|shell\n$1|@2|1|0|editor\n").encode()
        with patch.object(inventory, "capture_bounded", return_value=(0, response, b"", None)) as capture, \
             patch.object(tmux, "client_exists", return_value=True), patch.object(tmux, "switch") as switch:
            self.assertEqual(connection.open_remote(record, host, config, "test-client"), local)
            capture.assert_called_once()
            switch.assert_called_once_with("test-client", local)
            self.assertEqual(tmux.run(["show-options", "-v", "-t", local, "@tmux-roam-prefix"]).stdout.strip(), "M-a")
        for changed in (response.replace(b"12|100|", b"13|200|"),
                        response.replace(b"$1|101|", b"$2|102|")):
            with self.subTest(response=changed), \
                 patch.object(inventory, "capture_bounded", return_value=(0, changed, b"", None)), \
                 patch.object(tmux, "switch") as switch:
                with self.assertRaisesRegex(RuntimeError, "session ended or server restarted"):
                    connection.open_remote(record, host, config, "test-client")
                switch.assert_not_called()
        with patch.object(inventory, "capture_bounded", return_value=(None, b"", b"", "command timed out")), \
             patch.object(tmux, "switch") as switch:
            with self.assertRaisesRegex(RuntimeError, "command timed out"):
                connection.open_remote(record, host, config, "test-client")
            switch.assert_not_called()

    def _wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.02)
        self.fail("managed connection did not reach the expected state")

    def _failed_connection(self):
        root = Path(self.directory.name)
        config_path = root / "config.toml"
        config_path.write_text('version=1\n[[hosts]]\nid="remote"\nlabel="Remote"\nssh="example"\n')
        config = load(config_path)
        host = config.hosts[0]
        record = inventory._records(host.id, host.label, ("12", "100", "/tmp/remote"),
                                    [dict(session_id="$1", created=101, name="project", windows=1, clients=0)], "live")[0]
        inventory.save(host, [record])
        # Run the actual supervisor without making any network connections.
        binary = root / "bin"
        binary.mkdir()
        ssh = binary / "ssh"
        ssh.write_text("#!/bin/sh\nexit 255\n")
        ssh.chmod(0o700)
        test_path = str(binary) + os.pathsep + os.environ["PATH"]
        environment = patch.dict(os.environ, {"PATH": test_path})
        environment.start()
        self.addCleanup(environment.stop)
        tmux.run(["set-option", "-g", "default-shell", "/bin/sh"])
        tmux.run(["set-environment", "-g", "PATH", test_path])
        local, token = native._placeholder(record)
        pane = connection._read_state(token)["pane"]
        source = str(Path(__file__).resolve().parent.parent)
        command = shlex.join(["exec", "env", "PYTHONPATH=" + source, "TMUX_ROAM_CONFIG=" + str(config_path),
                              sys.executable, "-P", "-m", "tmux_roam.cli", "supervise", token, host.id, record.id])
        tmux.run(["respawn-pane", "-k", "-t", pane, command])
        self._wait_for(lambda: connection._read_state(token).get("status") == "error")
        native.decorate(local, record, "disconnected")
        self.assertIsNotNone(connection._managed_pane(local, record.id, token))
        return config, host, record, local, token, pane, command

    def test_repeated_failed_reconnect_and_cancel_reuse_the_original_pane(self):
        config, host, record, local, token, pane, _ = self._failed_connection()
        snapshot = inventory.RemoteSnapshot([record], {}, {record.session_id: "M-a"})
        original_sessions = connection._owned_sessions()
        with patch.object(inventory, "remote_snapshot", return_value=(snapshot, None)), \
             patch.object(connection, "_client_size", return_value=("100", "30")), \
             patch.object(tmux, "switch") as switch:
            for status in ("disconnected", "error", "open"):
                with self.subTest(status=status):
                    connection._write_state(token, dict(status=status, pid=42, tty="/dev/pts/missing"))
                    with self.assertRaisesRegex(RuntimeError, "SSH exit 255"):
                        connection.open_remote(record, host, config, "test-client")
                    new_token = native._option(local, connection.STATE)
                    self.assertNotEqual(token, new_token)
                    # A late write from the previous supervisor cannot poison its replacement.
                    connection._write_state(token, dict(status="open", pid=999, tty="/dev/pts/old"))
                    token = new_token
                    self.assertEqual(connection._owned_sessions(), original_sessions)
                    self.assertEqual(connection._managed_pane(local, record.id, token)["pane_id"], pane)
            with patch.object(connection, "_navigation_current", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "navigation cancelled"):
                    connection.open_remote(record, host, config, "test-client", "cancelled")
            token = native._option(local, connection.STATE)
            self._wait_for(lambda: connection._read_state(token).get("status") == "error")
            self.assertEqual(connection._owned_sessions(), original_sessions)
            switch.assert_not_called()

    def test_ended_cleanup_removes_waiting_supervisor_only_after_confirmed_absence(self):
        config, host, record, local, token, pane, _ = self._failed_connection()
        original = connection._owned_sessions()
        self.assertEqual(tmux.run(["display-message", "-p", "-t", pane, "#{pane_dead}"]).stdout.strip(), "0")
        native.sync_host(host, [], error="network unavailable")
        self.assertEqual(connection._owned_sessions(), original)
        native.sync_host(host, [], fresh=False)
        self.assertEqual(connection._owned_sessions(), original)
        native.sync_host(host, [record], fresh=True)
        self.assertEqual(connection._owned_sessions(), original)
        name = native._option(local, native.NAME)
        tmux.run(["rename-session", "-t", local, "keep-my-entry"])
        native.sync_host(host, [], fresh=True)
        self.assertEqual(connection._owned_sessions(), original)
        tmux.run(["rename-session", "-t", local, name])
        env = dict(os.environ)
        env.pop("TMUX", None)
        client = subprocess.Popen(self.base + ["-C", "attach-session", "-f", "ignore-size,no-output", "-t", local],
                                  env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self._wait_for(lambda: any(c["session_id"] == local for c in tmux.clients()))
            native.sync_host(host, [], fresh=True)
            self.assertEqual(connection._owned_sessions(), original)
        finally:
            client.communicate(b"detach-client\n", timeout=3)
        native.sync_host(host, [], fresh=True)
        self.assertEqual(connection._owned_sessions(), [])
        self.assertEqual([s["name"] for s in tmux.sessions()], ["start"])

    def test_confirmed_deletion_also_cleans_its_failed_local_carrier(self):
        config, host, record, local, token, pane, _ = self._failed_connection()
        original = connection._owned_sessions()
        with patch.object(inventory, "remote", return_value=([record], None)):
            for outcome in ((None, b"", b"", "command timed out"),
                            (0, b"TMUX_ROAM_STALE_SESSION", b"", None)):
                with self.subTest(outcome=outcome), patch.object(inventory, "capture_bounded", return_value=outcome):
                    with self.assertRaises(RuntimeError):
                        connection.delete_session(record, config)
                    self.assertEqual(connection._owned_sessions(), original)
            with patch.object(inventory, "capture_bounded", return_value=(0, b"", b"", None)):
                connection.delete_session(record, config)
        self.assertEqual(connection._owned_sessions(), [])
        self.assertEqual(inventory.cached(host), [])
        self.assertEqual([s["name"] for s in tmux.sessions()], ["start"])

    def test_reconnect_guard_preserves_concurrent_ownership_change(self):
        config, host, record, local, token, pane, _ = self._failed_connection()
        snapshot = inventory.RemoteSnapshot([record], {}, {record.session_id: "M-a"})
        guarded = connection._guarded_pane_command
        def changed(pane_info, command):
            tmux.run(["set-option", "-t", local, connection.STATE, "user-replacement"])
            return guarded(pane_info, command)
        with patch.object(inventory, "remote_snapshot", return_value=(snapshot, None)), \
             patch.object(connection, "_client_size", return_value=("100", "30")), \
             patch.object(connection, "_guarded_pane_command", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "Connection pane changed"):
                connection.open_remote(record, host, config, "test-client")
        self.assertEqual(native._option(local, connection.STATE), "user-replacement")
        self.assertEqual(tmux.run(["display-message", "-p", "-t", pane, "#{pane_dead}"]).stdout.strip(), "0")

    def test_duplicate_cleanup_preserves_outages_and_user_changes(self):
        config, host, record, local, token, pane, command = self._failed_connection()
        healthy, healthy_token = native._placeholder(record)
        connection._write_state(healthy_token, dict(status="open", pid=42, tty="/dev/pts/99"))
        inventory._reconcile(host, [record], {(42, "/dev/pts/99"): record.session_id}, True)
        native.decorate(healthy, record, "connected")
        original = connection._owned_sessions()
        native.sync_host(host, [record], error="network unavailable")
        self.assertCountEqual(connection._owned_sessions(), original)
        native.sync_host(host, [record])  # Cached "open here" is not cleanup evidence.
        self.assertCountEqual(connection._owned_sessions(), original)
        env = dict(os.environ)
        env.pop("TMUX", None)
        client = subprocess.Popen(self.base + ["-C", "attach-session", "-f", "ignore-size,no-output", "-t", local],
                                  env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self._wait_for(lambda: any(c["session_id"] == local for c in tmux.clients()))
            native.sync_host(host, [record], fresh=True)
            self.assertCountEqual(connection._owned_sessions(), original)
        finally:
            client.communicate(b"detach-client\n", timeout=3)
        name = native._option(local, native.NAME)
        tmux.run(["rename-session", "-t", local, "my-custom-name"])
        native.sync_host(host, [record], fresh=True)
        self.assertCountEqual(connection._owned_sessions(), original)
        tmux.run(["rename-session", "-t", local, name + " (old)"])
        tmux.run(["set-option", "-t", local, native.NAME, name + " (old)"])
        for creation, removal, id_format in (("new-window", "kill-window", "#{window_id}"),
                                              ("split-window", "kill-pane", "#{pane_id}")):
            extra = tmux.run([creation, "-d", "-P", "-F", id_format, "-t",
                              local if creation == "new-window" else pane, "sleep 60"]).stdout.strip()
            native.sync_host(host, [record], fresh=True)
            self.assertCountEqual(connection._owned_sessions(), original)
            tmux.run([removal, "-t", extra])
        tmux.run(["respawn-pane", "-k", "-t", pane, "sleep 60"])
        native.sync_host(host, [record], fresh=True)
        self.assertCountEqual(connection._owned_sessions(), original)
        tmux.run(["respawn-pane", "-k", "-t", pane, command])
        self._wait_for(lambda: connection._managed_pane(local, record.id, token) is not None)
        self._wait_for(lambda: connection._read_state(token).get("status") == "error")
        native.sync_host(host, [record], fresh=True)
        self.assertEqual(connection._owned_sessions(), [(healthy, record.id)])
        self.assertEqual(native._option(healthy, native.NAME), name)
        self.assertEqual(len(tmux.sessions()), 2)  # Healthy carrier plus ordinary local work.

    def test_managed_prefix_keys_reach_nested_tmux(self):
        remote = ["tmux", "-S", str(Path(self.directory.name) / "remote.sock")]
        env = {**os.environ, "TERM": "xterm-256color"}
        env.pop("TMUX", None)
        scrollback = "printf 'ROAM_COPY_TEST\\n'; seq 1 200; sleep 60"
        subprocess.run(remote + ["-f", "/dev/null", "new-session", "-d", "-s", "remote", scrollback], env=env, check=True)
        pid = fd = None
        output = bytearray()
        def remote_run(*args):
            return subprocess.run(remote + list(args), capture_output=True, text=True, check=True).stdout.strip()
        def wait_for(predicate):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if fd is not None and select.select([fd], [], [], .02)[0]:
                    output.extend(os.read(fd, 65536))
                if predicate():
                    return
                time.sleep(.02)
            self.fail("nested tmux did not receive the expected keys")
        try:
            remote_run("new-window", "-t", "remote", scrollback)
            remote_run("set-option", "-g", "mouse", "on")
            remote_run("set-option", "-gw", "mode-keys", "vi")
            remote_run("set-option", "-s", "set-clipboard", "on")
            remote_run("bind-key", "-T", "copy-mode-vi", "y", "send-keys", "-X", "copy-selection-and-cancel")
            remote_run("bind-key", "s", "set-option", "-g", "@escape-received", "yes")
            integration.install(str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam"))
            tmux.run(["set-option", "-t", "start", "prefix", "None"])
            tmux.run(["set-option", "-t", "start", "key-table", "roam-root-ma"])
            tmux.run(["set-option", "-t", "start", "mouse", "on"])
            tmux.run(["set-option", "-s", "set-clipboard", "on"])
            tmux.run(["set-option", "-as", "terminal-features", ",xterm-256color:clipboard"])
            tmux.run(["new-window", "-t", "start", "env", "-u", "TMUX", *remote, "attach-session", "-t", "remote"])
            pid, fd = pty.fork()
            if pid == 0:
                os.execvpe("tmux", self.base + ["attach-session", "-t", "start"], env)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            wait_for(lambda: bool(tmux.clients()) and bool(remote_run("list-clients")))
            for suffix, prefix in (("ma", "M-a"), ("cb", "C-b")):
                with self.subTest(remote_prefix=prefix):
                    remote_run("set-option", "-g", "prefix", prefix)
                    remote_run("bind-key", prefix, "last-window")
                    tmux.run(["set-option", "-t", "start", "key-table", "roam-root-" + suffix])
                    remote_run("select-window", "-t", "remote:0")
                    remote_run("select-window", "-t", "remote:1")
                    os.write(fd, b"\x1ba\x1ba")
                    wait_for(lambda: remote_run("display-message", "-p", "#{window_index}") == "0")
                    os.write(fd, b"\x1ba" + "n".encode())
                    wait_for(lambda: remote_run("display-message", "-p", "#{window_index}") == "1")
                    os.write(fd, ("\x1bф" * 2).encode())
                    wait_for(lambda: remote_run("display-message", "-p", "#{window_index}") == "0")
                    remote_run("set-option", "-gu", "@escape-received")
                    os.write(fd, b"\x1ba\x1b[24~s")
                    wait_for(lambda: remote_run("show-options", "-gqv", "@escape-received") == "yes")
                    output.clear()
                    os.write(fd, b"\x1ba[")
                    wait_for(lambda: remote_run("display-message", "-p", "#{pane_mode}") == "copy-mode")
                    os.write(fd, b"g0")
                    wait_for(lambda: remote_run("display-message", "-p", "#{copy_cursor_y}") == "0")
                    os.write(fd, b"j")
                    wait_for(lambda: remote_run("display-message", "-p", "#{copy_cursor_y}") == "1")
                    os.write(fd, b"k")
                    wait_for(lambda: remote_run("display-message", "-p", "#{copy_cursor_y}") == "0")
                    os.write(fd, b" $")
                    wait_for(lambda: remote_run("display-message", "-p", "#{selection_present}") == "1")
                    os.write(fd, b"y")
                    wait_for(lambda: remote_run("display-message", "-p", "#{pane_mode}") == "")
                    self.assertEqual(remote_run("show-buffer"), "ROAM_COPY_TEST")
                    wait_for(lambda: tmux.run(["show-buffer"], check=False).stdout.strip() == "ROAM_COPY_TEST")
                    wait_for(lambda: any(base64.b64decode(payload).rstrip(b"\n") == b"ROAM_COPY_TEST"
                                         for payload in re.findall(rb"\x1b\]52;[^;]*;([A-Za-z0-9+/=]*)(?:\x07|\x1b\\)", output)))
            # The same physical shortcut switches the outer client in every table.
            local = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}", "-s", "local"]).stdout.strip()
            other = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}", "-s", "other-roaming"]).stdout.strip()
            start = tmux.run(["display-message", "-p", "-t", "start", "#{session_id}"]).stdout.strip()
            tmux.run(["set-option", "-t", local, "prefix", "M-a"])
            tmux.run(["bind-key", "-n", "M-ф", "switch-client", "-T", "prefix"])
            tmux.run(["bind-key", "-T", "off", "M-a", "switch-client", "-T", "off-prefix"])
            tmux.run(["bind-key", "-T", "off", "M-ф", "switch-client", "-T", "off-prefix"])
            tmux.run(["bind-key", "-T", "off-prefix", "Any", "send-keys"])
            integration.install(str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam"))
            client = tmux.clients()[0]["name"]
            remote_run("bind-key", "L", "set-option", "-g", "@remote-last-session", "yes")
            for table in ("root", "off", "roam-root-ma", "roam-root-cb", "roam-root-none"):
                with self.subTest(last_session_table=table):
                    tmux.run(["set-option", "-t", "start", "key-table", table])
                    tmux.run(["set-option", "-t", "start", "prefix", "M-a" if table == "root" else "None"])
                    tmux.run(["set-option", "-t", other, "key-table", table])
                    tmux.run(["set-option", "-t", other, "prefix", "M-a" if table == "root" else "None"])
                    for previous in (local, other):
                        tmux.switch(client, previous)
                        tmux.switch(client, start)
                        for keys, target in ((b"\x1baL", previous), ("\x1bфД".encode(), start)):
                            os.write(fd, keys)
                            wait_for(lambda: tmux.clients()[0]["session_id"] == target)
                    self.assertEqual(remote_run("show-options", "-gqv", "@remote-last-session"), "")
            tmux.run(["set-option", "-t", "start", "key-table", "roam-root-cb"])
            os.write(fd, b"\x1ba\x1b[24~L")  # F12 still reaches the remote command explicitly.
            wait_for(lambda: remote_run("show-options", "-gqv", "@remote-last-session") == "yes")
            for suffix in ("ma", "cb", "none"):
                with self.subTest(mouse_routing=suffix):
                    tmux.run(["set-option", "-t", "start", "key-table", "roam-root-" + suffix])
                    os.write(fd, b"\x1b[<64;10;5M" * 3)  # SGR wheel reports from a touchpad.
                    wait_for(lambda: remote_run("display-message", "-p", "#{pane_mode}") == "copy-mode")
                    position = int(remote_run("display-message", "-p", "#{scroll_position}"))
                    self.assertGreater(position, 0)
                    self.assertEqual(tmux.run(["display-message", "-p", "-t", "start", "#{pane_mode}"]).stdout.strip(), "")
                    os.write(fd, b"\x1b[<65;10;5M")
                    wait_for(lambda: int(remote_run("display-message", "-p", "#{scroll_position}") or "0") < position)
                    remote_run("send-keys", "-X", "cancel")
            remote_run("new-window", "-t", "remote", "printf 'ROAM_MOUSE_WORD tail\\nsecond row\\n'; sleep 60")
            wait_for(lambda: "ROAM_MOUSE_WORD" in remote_run("capture-pane", "-p"))
            for suffix in ("ma", "cb", "none"):
                with self.subTest(click_and_drag_routing=suffix):
                    tmux.run(["set-option", "-t", "start", "key-table", "roam-root-" + suffix])
                    time.sleep(.4)  # Start a fresh click sequence after the previous drag.
                    remote_run("set-buffer", "pending")
                    click = b"\x1b[<0;6;1M\x1b[<0;6;1m"
                    os.write(fd, click)
                    time.sleep(.05)
                    os.write(fd, click)
                    wait_for(lambda: remote_run("show-buffer") == "ROAM_MOUSE_WORD")
                    time.sleep(.4)  # The following drag must not become a third click.
                    remote_run("set-buffer", "pending")
                    os.write(fd, b"\x1b[<0;1;1M\x1b[<32;5;1M\x1b[<32;16;1M")
                    wait_for(lambda: remote_run("display-message", "-p", "#{pane_mode}|#{selection_present}") == "copy-mode|1")
                    os.write(fd, b"\x1b[<0;16;1m")
                    wait_for(lambda: remote_run("show-buffer") == "ROAM_MOUSE_WORD")
                    self.assertEqual(tmux.run(["display-message", "-p", "-t", "start", "#{pane_mode}"]).stdout.strip(), "")
            mouse_log = Path(self.directory.name) / "mouse-events"
            program = ("import os, tty\ntty.setraw(0)\n"
                       "os.write(1, b'\\x1b[?1049h\\x1b[?1002h\\x1b[?1006h')\n"
                       "while True:\n"
                       " data = os.read(0, 1024)\n"
                       f" with open({str(mouse_log)!r}, 'ab') as out: out.write(data)\n")
            remote_run("new-window", "-t", "remote", sys.executable, "-c", program)
            wait_for(lambda: remote_run("display-message", "-p", "#{alternate_on}|#{mouse_any_flag}") == "1|1")
            events = b"\x1b[<64;10;5M\x1b[<65;10;5M\x1b[<0;10;5M\x1b[<32;12;5M\x1b[<0;12;5m"
            os.write(fd, events)
            wait_for(lambda: mouse_log.exists() and b"[<64;" in mouse_log.read_bytes() and b"[<65;" in mouse_log.read_bytes())
            wait_for(lambda: all(event in mouse_log.read_bytes() for event in (b"[<0;10;5M", b"[<32;12;5M", b"[<0;12;5m")))
            os.write(fd, b"ROAM_KEYS_AFTER_CLICK")
            wait_for(lambda: b"ROAM_KEYS_AFTER_CLICK" in mouse_log.read_bytes())
            received = mouse_log.read_bytes()
            tmux.run(["choose-tree", "-Zs", "-t", "start"])
            os.write(fd, events + b"\x1b")
            wait_for(lambda: tmux.run(["display-message", "-p", "-t", "start", "#{pane_mode}"]).stdout.strip() == "")
            self.assertEqual(mouse_log.read_bytes(), received)  # Local picker owns its mouse events.
        finally:
            subprocess.run(self.base + ["kill-server"], capture_output=True)
            subprocess.run(remote + ["kill-server"], capture_output=True)
            if fd is not None:
                os.close(fd)
            if pid:
                os.waitpid(pid, 0)

    def test_native_entries_are_dormant_unique_and_pruned_safely(self):
        host = Host("remote", "Remote", "example")
        records = inventory._records(host.id, host.label, ("12", "100", "/tmp/remote"),
                                     [dict(session_id="$1", created=101, name="quote'|🐱", windows=5, clients=0)], "live")
        with patch.object(inventory, "ssh_base", side_effect=AssertionError("dormant entry opened SSH")):
            self.assertTrue(native.sync_host(host, records, fresh=True))
            with patch.object(tmux, "run", wraps=tmux.run) as commands:
                records[0].observed += 15
                self.assertFalse(native.sync_host(host, records, fresh=True))
                self.assertFalse(any(call.args[0][0] in ("set-option", "rename-session", "refresh-client")
                                     for call in commands.call_args_list))
        owned = connection._owned_sessions()
        self.assertEqual(len(owned), 1)
        sid, ident = owned[0]
        state = connection._read_state(native._option(sid, connection.STATE))
        self.assertEqual(state["status"], "idle")
        self.assertEqual(tmux.run(["display-message", "-p", "-t", state["pane"], "#{pane_dead}"]).stdout.strip(), "1")
        self.assertEqual(native._option(sid, native.LABEL), "Remote / quote'|🐱")
        self.assertEqual(native._option(sid, native.WINDOWS), "5")
        native.sync_host(host, records, error="offline")
        self.assertEqual(connection._owned_sessions(), owned)
        self.assertIn("unavailable", native._option(sid, native.STATUS))
        native.sync_host(host, [], fresh=True)
        self.assertEqual(connection._owned_sessions(), [])
        self.assertEqual(len(tmux.sessions()), 1)  # The ordinary local session remains.
        native.sync_host(host, records, fresh=True)
        sid, ident = connection._owned_sessions()[0]
        token = native._option(sid, connection.STATE)
        connection._write_state(token, dict(status="open", pid=42, tty="/dev/pts/99"))
        native.sync_host(host, [], fresh=True)
        self.assertEqual(connection._owned_sessions(), [(sid, ident)])
        self.assertEqual(native._option(sid, native.STATUS), "ended")

    def test_native_binding_is_opt_in_and_respects_user_key_configuration(self):
        binary = str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam")
        tmux.run(["set-option", "-g", "@tmux-roam-chooser-keys", "F9"])
        tmux.run(["set-option", "-g", "@tmux-roam-native-keys", "s ы"])
        tmux.run(["set-option", "-g", "@tmux-roam-native", "on"])
        integration.install(binary)
        for table in ("prefix", "roam-prefix-ma", "roam-prefix-cb"):
            for key in ("s", "ы"):
                binding = integration._binding(table, key)
                self.assertIn("choose-tree -Zs", binding)
                self.assertIn("native-refresh", binding)
                self.assertIn("switch-client -c", binding)
                self.assertNotIn("native --client", binding)
            self.assertIn("popup --client", integration._binding(table, "F9"))
        tmux.run(["set-option", "-g", "@tmux-roam-native", "off"])
        integration.install(binary)
        self.assertIn("choose-tree -Zs", integration._binding("prefix", "s"))

    def test_remote_window_discovery_selection_cache_and_mirror_cleanup(self):
        remote = ["tmux", "-S", str(Path(self.directory.name) / "remote.sock")]
        host = Host("remote", "Remote", "unused", socket_path=remote[-1])
        config = Config(Path(self.directory.name) / "config.toml", (host,))
        env = dict(os.environ)
        env.pop("TMUX", None)
        subprocess.run(remote + ["-f", "/dev/null", "new-session", "-d", "-s", "project", "-n", "shell", "sleep 60"], env=env, check=True)
        subprocess.run(remote + ["new-window", "-d", "-t", "project:3", "-n", "quote'|🌲", "sleep 60"], check=True)
        tmux.run(["set-option", "-g", "@tmux-roam-native-windows", "on"])
        clients = []
        try:
            with patch.object(inventory, "ssh_base", return_value=["sh", "-c"]):
                snapshot, error = inventory.remote_snapshot(host, config)
                self.assertIsNone(error)
                record = snapshot.records[0]
                self.assertEqual([(w["index"], w["name"]) for w in record.window_details], [(0, "shell"), (3, "quote'|🌲")])
                window = record.window_details[1]["id"]
                inventory.save(host, [record])
                self.assertEqual(inventory.cached(host)[0].window_details, record.window_details)
                native.sync_host(host, [record], fresh=True)
                sid = connection._owned_sessions()[0][0]
                token = native._option(sid, connection.STATE)
                carrier_pane = connection._read_state(token)["pane"]
                def mirrors():
                    return {entry[4]: entry for entry in native._window_entries(sid) if entry[3] == record.id}
                self.assertEqual(set(mirrors()), {w["id"] for w in record.window_details})
                self.assertEqual({entry[1] for entry in mirrors().values()}, {"0", "3"})
                self.assertEqual(tmux.clients(remote), [])  # Discovery never attaches a remote client.
                before = native._window_entries(sid)
                native.sync_host(host, inventory.cached(host), error="offline")
                self.assertEqual(native._window_entries(sid), before)
                subprocess.run(remote + ["rename-window", "-t", window, "renamed|🤖"], check=True)
                snapshot, error = inventory.remote_snapshot(host, config)
                self.assertIsNone(error)
                record = snapshot.records[0]
                native.sync_host(host, [record], fresh=True)
                self.assertEqual(native._window_entries(sid), before)  # Names update in place.
                self.assertEqual(native._window_option(mirrors()[window][0], "@tmux-roam-window-name"), "renamed|🤖")
                # Control clients let us exercise exact selection without any SSH attachment.
                for base, target in ((self.base, "start"), (remote, "project")):
                    process = subprocess.Popen(base + ["-C", "attach-session", "-t", target], env=env,
                                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    clients.append(process)
                    self._wait_for(lambda: bool(tmux.clients(base)))
                client = tmux.clients()[0]["name"]
                remote_client = tmux.clients(remote)[0]
                connection._write_state(token, dict(status="open", pid=remote_client["pid"], tty=remote_client["tty"]))
                navigation = picker.begin_navigation(client)
                target = mirrors()[window][2]
                native.select(config, client, target, navigation)
                self.assertEqual(tmux.clients()[0]["session_id"], sid)
                self.assertEqual(native._client_pane(client), carrier_pane)
                self.assertEqual(len(native._window_entries(sid)), 1)
                self.assertEqual(tmux.run(["display-message", "-p", "-t", "project", "#{window_id}"], remote).stdout.strip(), window)
                self.assertEqual(len(tmux.clients(remote)), 1)  # Reused, not duplicated.
                with self.assertRaisesRegex(RuntimeError, "window ended or moved"):
                    connection.open_remote(record, host, config, client, window="@99999")
                with self.assertRaises(RuntimeError):
                    connection._select_remote_window(replace(record, generation="1/1"), host, config, record.window_details[0]["id"])
                self.assertEqual(tmux.run(["display-message", "-p", "-t", "project", "#{window_id}"], remote).stdout.strip(), window)
                tmux.switch(client, "start")
                native.sync_host(host, [record], fresh=True)
                subprocess.run(remote + ["kill-window", "-t", window], check=True)
                snapshot, error = inventory.remote_snapshot(host, config)
                self.assertIsNone(error)
                native.sync_host(host, snapshot.records, fresh=True)
                self.assertNotIn(window, mirrors())
                tmux.run(["set-option", "-g", "@tmux-roam-native-windows", "off"])
                integration.install(str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam"))
                self.assertFalse(mirrors())
                self.assertEqual(native._window_entries(sid)[0][1], "0")
        finally:
            for process in clients:
                process.communicate(b"detach-client\n", timeout=3)
            subprocess.run(remote + ["kill-server"], capture_output=True)

    def test_window_mirror_cleanup_preserves_user_changes_and_failed_carrier_reuse(self):
        config, host, record, sid, token, pane, _ = self._failed_connection()
        record.window_details = [dict(id="@1", index=0, active=True, name="shell"),
                                 dict(id="@2", index=2, active=False, name="editor")]
        tmux.run(["set-option", "-g", "@tmux-roam-native-windows", "on"])
        native.sync_host(host, [record], fresh=True)
        entries = [e for e in native._window_entries(sid) if e[3]]
        modified = entries[0]
        tmux.run(["respawn-pane", "-k", "-t", modified[2], "sleep 60"])
        native.remove_inactive(sid, record.id, ended=True)
        self.assertTrue(tmux.run(["has-session", "-t", sid], check=False).returncode == 0)
        self.assertEqual(tmux.run(["display-message", "-p", "-t", modified[2], "#{pane_dead}"]).stdout.strip(), "0")
        tmux.run(["kill-window", "-t", modified[0]])  # Explicitly remove only the test's added work.
        native.sync_host(host, [record], fresh=True)
        snapshot = inventory.RemoteSnapshot([record], {}, {record.session_id: "M-a"})
        with patch.object(inventory, "remote_snapshot", return_value=(snapshot, None)), \
             patch.object(connection, "_client_size", return_value=("100", "30")):
            with self.assertRaisesRegex(RuntimeError, "SSH exit 255"):
                connection.open_remote(record, host, config, "test-client")
        token = native._option(sid, connection.STATE)
        self.assertEqual(connection._managed_pane(sid, record.id, token)["pane_id"], pane)
        self.assertEqual(connection._owned_sessions(), [(sid, record.id)])

    def test_native_compact_rows_current_session_and_refresh_preserve_selection(self):
        tmux.run(["respawn-pane", "-k", "-t", "start", "sleep 60"])
        tmux.run(["rename-session", "-t", "start", "🐱dev"])
        tmux.run(["set-option", "-gw", "mode-keys", "vi"])
        tmux.run(["set-option", "-gw", "tree-mode-selection-style", "fg=#cad3f5,bg=#494d64,bold"])
        local = tmux.sessions()[0]["session_id"]
        other = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}", "-s", "🌲pnl", "sleep 60"]).stdout.strip()
        dd, omni = Host("dd", "DD", "unused"), Host("omni", "OMNI", "unused")
        for host, colour in ((dd, "#a6da95"), (omni, "#f5a97f")):
            tmux.run(["set-option", "-g", "@tmux-roam-native-colour-" + host.id, colour])
        records = inventory._records(dd.id, dd.label, ("12", "100", "/tmp/remote"),
                                    [dict(session_id="$1", created=101, name="gitlab", windows=3, clients=0),
                                     dict(session_id="$2", created=102, name="🌲", windows=2, clients=0)], "live")
        remote = inventory._records(omni.id, omni.label, ("15", "100", "/tmp/omni"),
                                    [dict(session_id="$1", created=101, name="🤖", windows=4, clients=0)], "live")[0]
        native.sync_host(dd, records, fresh=True)
        native.sync_host(omni, [remote], fresh=True)
        env = dict(os.environ)
        env.pop("TMUX", None)
        control = subprocess.Popen(self.base + ["-C", "attach-session", "-f", "ignore-size,no-output", "-t", other],
                                   env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("tmux", self.base + ["attach-session", "-t", local], dict(env, TERM="xterm-256color"))
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
        def capture():
            output = bytearray()
            deadline = time.monotonic() + .15
            while time.monotonic() < deadline:
                if select.select([fd], [], [], .01)[0]:
                    output.extend(os.read(fd, 65536))
            return output.decode("utf-8", "replace")
        def plain(value):
            value = re.sub(r"\x1b\[([0-9]+)C", lambda m: " " * int(m[1]), value)
            return re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[()][0-9A-Za-z])", "", value)
        try:
            self._wait_for(lambda: len(tmux.clients()) == 2)
            capture()
            client = next(c["name"] for c in tmux.clients() if c["session_id"] == local)
            pane = native._client_pane(client)
            native.open_tree(load(), client, "/bin/true")
            coloured = capture()
            frame = plain(coloured)
            self.assertIn("3w  Ready", frame)
            self.assertIn("4w  Ready", frame)
            rows = list(dict.fromkeys(re.findall(r"\([0-9]+\) \+ [^\r\n]+", frame)))
            self.assertEqual(len(rows), 5)  # Session rows start collapsed above the preview.
            self.assertNotIn("Remote session available", frame)
            self.assertNotIn("#[", frame)
            self.assertEqual([line.split(":", 1)[0] for line in rows if line.endswith("*")], ["(0) + 🐱dev"])
            widths = {picker._width(line[:re.search(r"\dw", line).start()]) for line in rows}
            self.assertEqual(len(widths), 1, rows)
            self.assertIn("38;2;147;154;183", coloured)  # Counts and Ready are neutral.
            self.assertNotIn("38;2;245;169;127", coloured)
            self.assertIn("48;2;73;77;100", coloured)
            dd_session = next(s for s, ident in connection._owned_sessions() if ident == records[0].id)
            omni_session = next(s for s, ident in connection._owned_sessions() if ident == remote.id)
            native.decorate(dd_session, records[0], "connected")
            native.decorate(omni_session, remote, "connected")
            tmux.run(["refresh-client", "-t", client])
            coloured = capture()
            connected_colours = re.findall(r"\x1b\[38;2;([0-9;]+)mConnected", coloured)
            self.assertGreaterEqual(len(connected_colours), 2)
            self.assertEqual(set(connected_colours), {"166;218;149"})
            os.write(fd, b"jj")  # Remote placeholder has an actual bottom preview.
            self.assertIn("Remote session available", plain(capture()))
            os.write(fd, b"kk")
            capture()
            os.write(fd, b"j")  # Select the other local session, independently of the current marker.
            capture()
            remote.observed = time.time() - 12 * 60
            native.sync_host(omni, [remote], error="network unavailable")
            tmux.run(["refresh-client", "-t", client])
            coloured = capture()
            self.assertIn("Unavailable · seen 12m ago", plain(coloured))
            self.assertIn("38;2;128;135;162", coloured)
            native.sync_host(omni, [remote])
            tmux.run(["refresh-client", "-t", client])
            coloured = capture()
            self.assertIn("Checking…", plain(coloured))
            self.assertIn("\x1b[38;2;138;173;244mChecking", coloured)
            native.sync_host(omni, [remote], fresh=True)
            tmux.run(["refresh-client", "-t", client])
            self.assertIn("4w  Ready", plain(capture()))
            sid = next(s for s, ident in connection._owned_sessions() if ident == records[0].id)
            connection._write_state(native._option(sid, connection.STATE), dict(status="disconnected"))
            native.sync_host(dd, records, fresh=True)
            tmux.run(["refresh-client", "-t", client])
            coloured = capture()
            self.assertIn("3w  Reconnect", plain(coloured))
            self.assertIn("\x1b[38;2;238;212;159mReconnect", coloured)
            os.write(fd, b"\r")
            self._wait_for(lambda: next(c["session_id"] for c in tmux.clients() if c["name"] == client) == other)
        finally:
            control.communicate(b"detach-client\n", timeout=3)
            subprocess.run(self.base + ["kill-server"], capture_output=True)
            os.close(fd)
            os.waitpid(pid, 0)

    @unittest.skipUnless(shutil.which("fzf"), "fzf is not installed")
    def test_native_slash_fzf_search_selection_escape_and_toggle(self):
        binary = str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam")
        root = Path(self.directory.name)
        wrapper, started, release = root / "delayed-launch", root / "started", root / "release"
        wrapper.write_text('#!/bin/sh\nif [ "$1" = popup ]; then\n' +
                           "touch " + shlex.quote(str(started)) + "\nwhile [ ! -e " + shlex.quote(str(release)) +
                           " ]; do sleep .01; done\nfi\nexec " + shlex.quote(binary) + ' "$@"\n')
        wrapper.chmod(0o700)
        tmux.run(["respawn-pane", "-k", "-t", "start", "sleep 60"])
        other = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}", "-s", "project-pine", "sleep 60"]).stdout.strip()
        for key, value in (("prefix", "M-a"), ("@tmux-roam-native", "on"),
                           ("@tmux-roam-native-keys", "s ы S Ы"), ("@tmux-roam-native-fuzzy-keys", "s ы"),
                           ("@tmux-roam-native-search", "fzf"), ("@tmux-roam-chooser-keys", "b и")):
            tmux.run(["set-option", "-g", key, value])
        tmux.run(["bind-key", "-n", "M-ф", "switch-client", "-T", "prefix"])
        tmux.run(["set-option", "-gw", "mode-keys", "vi"])
        integration.install(str(wrapper))
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("TMUX", None)
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("tmux", self.base + ["attach-session", "-t", "start"], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 110, 0, 0))
        output, stop = bytearray(), threading.Event()
        def drain():
            while not stop.is_set():
                try:
                    if select.select([fd], [], [], .05)[0]:
                        block = os.read(fd, 65536)
                        output.extend(block)
                        if b"\x1b[6n" in block:
                            os.write(fd, b"\x1b[1;1R")
                except OSError:
                    return
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        def mode():
            return tmux.run(["display-message", "-p", "-t", native._client_pane(client), "#{pane_mode}"]).stdout.strip()
        def search_state():
            for path in connection.runtime_dir().glob("picker-*/fzf.sock"):
                try:
                    with socket.socket(socket.AF_UNIX) as sock:
                        sock.settimeout(.2)
                        sock.connect(str(path))
                        sock.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
                        data = b""
                        while block := sock.recv(65536):
                            data += block
                    return json.loads(data.split(b"\r\n\r\n", 1)[1])
                except (OSError, ValueError, IndexError):
                    pass
            return {}
        try:
            self._wait_for(lambda: bool(tmux.clients()))
            client = tmux.clients()[0]["name"]
            os.write(fd, b"\x1bas")
            self._wait_for(lambda: mode() == "tree-mode")
            origin = native._client_pane(client)
            tree_token = tmux.run(["show-options", "-gqv", picker._navigation_option(client)]).stdout.strip()
            os.write(fd, b"/")
            self._wait_for(started.exists)
            self.assertEqual(mode(), "tree-mode")  # Keep the tree visible throughout startup.
            release.touch()
            self._wait_for(lambda: "query" in search_state())
            self._wait_for(lambda: b"Esc Back to tree" in output)
            self.assertEqual(mode(), "tree-mode")  # Search covers only the bottom preview.
            self.assertTrue(picker.navigation_valid(client, tree_token))
            os.write(fd, "ddввjkол".encode())
            self._wait_for(lambda: search_state().get("query") == "ddввjkол")
            os.write(fd, b"\x1b")
            self._wait_for(lambda: mode() == "tree-mode" and not search_state())
            self.assertTrue(picker.navigation_valid(client, tree_token))  # Same tree and expanded rows.
            os.write(fd, b"/")
            self._wait_for(lambda: "query" in search_state())
            os.write(fd, b"local/prpi")  # Match a host/project path while retaining exact identity.
            def selected_match():
                state = search_state()
                return (state.get("query") == "local/prpi" and state.get("matchCount") == 1 and
                        "project-pine" in (state.get("current") or {}).get("text", ""))
            self._wait_for(selected_match)
            time.sleep(.15)  # Let fzf finish its initial reload and render the match.
            os.write(fd, b"\r")
            self._wait_for(lambda: tmux.clients()[0]["session_id"] == other and not search_state())
            self.assertEqual(tmux.run(["display-message", "-p", "-t", origin, "#{pane_mode}"]).stdout.strip(), "")
            time.sleep(.15)  # The selected pane precedes the popup's final repaint.
            # Both routes and layouts close the whole search without reopening the tree.
            for managed in (False, True):
                if managed:
                    tmux.run(["set-option", "-t", other, "prefix", "None"])
                    tmux.run(["set-option", "-t", other, "key-table", "roam-root-ma"])
                for shortcut, slash in ((b"\x1bas", b"/"), ("\x1bфы".encode(), b".")):
                    os.write(fd, shortcut)
                    self._wait_for(lambda: mode() == "tree-mode")
                    os.write(fd, slash)
                    self._wait_for(lambda: "query" in search_state())
                    os.write(fd, shortcut)
                    self._wait_for(lambda: not search_state())
                    time.sleep(.15)
                    self.assertEqual(mode(), "")
                    self.assertEqual(tmux.clients()[0]["session_id"], other)
            self.assertNotIn(b"returned 2", output)
            self.assertNotIn(b"Press Enter to continue", output)
        finally:
            stop.set()
            subprocess.run(self.base + ["kill-server"], capture_output=True)
            reader.join(timeout=1)
            os.close(fd)
            os.waitpid(pid, 0)

    def test_native_fuzzy_filtering_local_remote_cancel_and_tree_fallback(self):
        root = Path(self.directory.name)
        calls, binary = root / "calls", root / "slow-roam"
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + shlex.quote(str(calls)) + '\nsleep 2\n')
        binary.chmod(0o700)
        tmux.run(["respawn-pane", "-k", "-t", "start", "sleep 60"])
        local = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}", "-s", "project-pine", "sleep 60"]).stdout.strip()
        host = Host("dd", "DD", "unused")
        record = inventory._records(host.id, host.label, ("12", "100", "/tmp/remote"),
                                    [dict(session_id="$1", created=101, name="gitlab", windows=3, clients=0)], "live")[0]
        native.sync_host(host, [record], fresh=True)
        remote = connection._owned_sessions()[0][0]
        remote_pane = connection._read_state(native._option(remote, connection.STATE))["pane"]
        for key, value in (("prefix", "M-a"), ("@tmux-roam-native", "on"),
                           ("@tmux-roam-native-keys", "s ы S Ы"), ("@tmux-roam-native-fuzzy-keys", "s ы"),
                           ("@tmux-roam-chooser-keys", "b и")):
            tmux.run(["set-option", "-g", key, value])
        tmux.run(["set-option", "-gw", "mode-keys", "vi"])
        tmux.run(["bind-key", "-n", "M-ф", "switch-client", "-T", "prefix"])
        tmux.run(["set-option", "-g", "mouse", "on"])
        integration.install(str(binary))
        for table in ("prefix", "roam-prefix-ma", "roam-prefix-cb"):
            for key in ("s", "ы"):
                self.assertIn("choose-tree -Zs", integration._binding(table, key))
                self.assertIn(native.BROWSE_TABLE, integration._binding(table, key))
            for key in ("S", "Ы"):
                self.assertIn("choose-tree -Zs", integration._binding(table, key))
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("TMUX", None)
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("tmux", self.base + ["attach-session", "-t", "start"], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
        worker = None
        def mode():
            return tmux.run(["display-message", "-p", "-t", native._client_pane(client), "#{pane_mode}"]).stdout.strip()
        try:
            self._wait_for(lambda: bool(tmux.clients()))
            client = tmux.clients()[0]["name"]
            start = tmux.clients()[0]["session_id"]
            # Russian physical j/k move tree rows and may be mixed with Latin.
            for keys, target in (("о\r", local), ("л\r", start), ("jло\r", local), ("лjл\r", start)):
                os.write(fd, b"\x1bas")
                self._wait_for(lambda: mode() == "tree-mode")
                os.write(fd, keys.encode())
                self._wait_for(lambda: mode() == "" and tmux.clients()[0]["session_id"] == target)
            os.write(fd, b"\x1bas")
            self._wait_for(lambda: mode() == "tree-mode")
            os.write(fd, b"jk/")
            self._wait_for(lambda: mode() == "switch-mode")
            os.write(fd, b"prpi\r")  # Fuzzy subsequence, not a substring.
            self._wait_for(lambda: tmux.clients()[0]["session_id"] == local)
            self.assertNotIn("native-select", calls.read_text())
            os.write(fd, b"\x1baS")
            self._wait_for(lambda: mode() == "tree-mode")
            os.write(fd, b"\x1b")
            self._wait_for(lambda: mode() == "")
            # A native mouse/command exit may leave a one-shot client table.
            # Its next key must be replayed through the usual root bindings.
            for key in ("/", ".", "x", "о", "л"):
                tmux.run(["bind-key", "-n", key, "set-option", "-g", "@replayed", key])
                os.write(fd, b"\x1bas")
                self._wait_for(lambda: mode() == "tree-mode")
                tmux.run(["send-keys", "-t", native._client_pane(client), "Escape"])
                self._wait_for(lambda: mode() == "")
                os.write(fd, key.encode())
                self._wait_for(lambda: tmux.run(["show-options", "-gqv", "@replayed"]).stdout.strip() == key)
            # Repeating the shortcut closes the picker without starting another refresh.
            os.write(fd, b"\x1bas")
            self._wait_for(lambda: mode() == "tree-mode")
            option = picker._navigation_option(client)
            token = tmux.run(["show-options", "-gqv", option]).stdout.strip()
            os.write(fd, b"\x1bas")
            self._wait_for(lambda: mode() == "")
            self.assertEqual(tmux.run(["show-options", "-gqv", option]).stdout.strip(), token)
            os.write(fd, b"\x1bas")
            self._wait_for(lambda: mode() == "tree-mode")
            # A click chooses the first row; slash must still work afterward.
            os.write(fd, b"\x1b[<0;12;1M\x1b[<0;12;1m")
            time.sleep(.05)
            os.write(fd, b"/")
            self._wait_for(lambda: mode() == "switch-mode")
            os.write(fd, b"\x1b")
            self._wait_for(lambda: mode() == "tree-mode")
            # Double click opens the first row, retaining native mouse behavior.
            os.write(fd, b"\x1b[<0;12;1M\x1b[<0;12;1m\x1b[<0;12;1M\x1b[<0;12;1m")
            self._wait_for(lambda: mode() == "")
            self.assertEqual(tmux.clients()[0]["session_id"],
                             next(s["session_id"] for s in tmux.sessions() if s["name"] == "start"))
            tmux.switch(client, local)
            tmux.run(["set-option", "-t", local, "prefix", "None"])
            tmux.run(["set-option", "-t", local, "key-table", "roam-root-ma"])
            # Both keyboard layouts toggle from browse and search on the remote routing table.
            for shortcut in (b"\x1bas", "\x1bфы".encode()):
                for search in (False, True):
                    os.write(fd, shortcut)
                    self._wait_for(lambda: mode() == "tree-mode")
                    if search:
                        os.write(fd, b"/git")
                        self._wait_for(lambda: mode() == "switch-mode")
                    os.write(fd, shortcut)
                    self._wait_for(lambda: mode() == "")
                    self.assertEqual(tmux.clients()[0]["session_id"], local)
            os.write(fd, "\x1bфы".encode())
            self._wait_for(lambda: mode() == "tree-mode")
            os.write(fd, b".")
            self._wait_for(lambda: mode() == "switch-mode")
            os.write(fd, b"no-such-session\r")
            time.sleep(.05)
            self.assertEqual(mode(), "switch-mode")
            os.write(fd, b"\x1b")
            self._wait_for(lambda: mode() == "tree-mode")
            os.write(fd, b"\x1b")
            self._wait_for(lambda: mode() == "")
            self.assertEqual(tmux.clients()[0]["session_id"], local)
            os.write(fd, b"\x1bas")
            self._wait_for(lambda: mode() == "tree-mode")
            os.write(fd, b"/")
            self._wait_for(lambda: mode() == "switch-mode")
            os.write(fd, b"ddgt")
            # The real refresh worker must run in fuzzy mode without losing the query.
            pane = native._client_pane(client)
            token = tmux.run(["show-options", "-gqv", picker._navigation_option(client)]).stdout.strip()
            record.windows = 7
            with patch.object(inventory, "remote", return_value=([record], None)) as discovery:
                worker = threading.Thread(target=native.refresh, args=(Config(root / "config.toml", (host,)), client, pane, token))
                worker.start()
                self._wait_for(lambda: native._option(remote, native.WINDOWS) == "7")
                discovery.assert_called()
                os.write(fd, b"\r")
                self._wait_for(lambda: "native-select" in calls.read_text())
                worker.join(timeout=3)
                self.assertFalse(worker.is_alive())
            selected = next(line for line in calls.read_text().splitlines() if line.startswith("native-select"))
            self.assertIn("--client " + client, selected)
            self.assertIn("--target " + remote_pane, selected)
            self.assertEqual(tmux.clients()[0]["session_id"], local)  # Only the stub selection helper ran.
            tmux.run(["set-option", "-g", "@tmux-roam-native-fuzzy-keys", ""])
            integration.install(str(binary))
            for table in (native.BROWSE_TABLE, native.SEARCH_TABLE):
                self.assertEqual(tmux.run(["list-keys", "-T", table], check=False).stdout, "")
        finally:
            subprocess.run(self.base + ["kill-server"], capture_output=True)
            if worker:
                worker.join(timeout=3)
            os.close(fd)
            os.waitpid(pid, 0)

    def test_ended_cleanup_preserves_outages_and_active_or_modified_panes(self):
        host = Host("remote", "Remote", "example")
        records = inventory._records(host.id, host.label, ("12", "100", "/tmp/remote"),
                                     [dict(session_id="$1", created=101, name="ended", windows=1, clients=0)], "live")
        native.sync_host(host, records, fresh=True)
        sid, ident = connection._owned_sessions()[0]
        token = native._option(sid, connection.STATE)
        pane = connection._read_state(token)["pane"]
        connection._write_state(token, dict(status="disconnected", error="SSH exited"))
        def wait_for(predicate):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(.02)
            self.fail("test pane or client did not reach the expected state")
        def respawn(code, *, managed=True):
            command = [sys.executable, "-c", code]
            if managed:
                command += ["supervise", token, host.id, ident]
            tmux.run(["respawn-pane", "-k", "-t", pane, shlex.join(command)])
            if code == "pass":
                wait_for(lambda: tmux.run(["display-message", "-p", "-t", pane, "#{pane_dead}"]).stdout.strip() == "1")
        def retained():
            self.assertEqual(connection._owned_sessions(), [(sid, ident)])
        respawn("pass")
        native.sync_host(host, [], error="network unavailable")
        retained()
        native.sync_host(host, records, fresh=True)  # Host recovers; remote session still exists.
        retained()
        env = dict(os.environ)
        env.pop("TMUX", None)
        client = subprocess.Popen(self.base + ["-C", "attach-session", "-f", "ignore-size,no-output", "-t", sid],
                                  env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            wait_for(lambda: any(c["session_id"] == sid for c in tmux.clients()))
            native.sync_host(host, [], fresh=True)
            retained()
        finally:
            client.stdin.write(b"detach-client\n")
            client.stdin.flush()
            client.communicate(timeout=3)
        respawn("import time; time.sleep(60)")
        native.sync_host(host, [], fresh=True)
        retained()
        extra = tmux.run(["new-window", "-d", "-P", "-F", "#{window_id}", "-t", sid, "sleep 60"]).stdout.strip()
        respawn("pass")
        native.sync_host(host, [], fresh=True)
        retained()
        tmux.run(["kill-window", "-t", extra])
        respawn("pass", managed=False)
        native.sync_host(host, [], fresh=True)
        retained()
        respawn("pass")
        native.sync_host(host, [], fresh=True)
        self.assertEqual(connection._owned_sessions(), [])
        self.assertEqual(len(tmux.sessions()), 1)

    def test_native_local_selection_does_not_wait_for_python(self):
        tmux.run(["respawn-pane", "-k", "-t", "start", "sleep", "60"])
        binary = Path(self.directory.name) / "slow-roam"
        calls = Path(self.directory.name) / "calls"
        binary.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "' + str(calls) + '"\nsleep 2\n')
        binary.chmod(0o700)
        local = tmux.run(["new-session", "-d", "-P", "-F", "#{session_id}",
                          "-s", "local'|🐱", "sleep", "60"]).stdout.strip()
        host = Host("remote", "Remote", "example")
        record = inventory._records(host.id, host.label, ("12", "100", "/tmp/remote"),
                                    [dict(session_id="$1", created=101, name="remote'|🐱", windows=3, clients=0)], "live")[0]
        native.sync_host(host, [record], fresh=True)
        proxy = connection._owned_sessions()[0][0]
        remote_pane = connection._read_state(native._option(proxy, connection.STATE))["pane"]
        for key, value in (("prefix", "M-a"), ("@tmux-roam-chooser-keys", "F9"),
                           ("@tmux-roam-native-keys", "s ы"), ("@tmux-roam-native", "on")):
            tmux.run(["set-option", "-g", key, value])
        integration.install(str(binary))
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("TMUX", None)
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("tmux", self.base + ["attach-session", "-t", "start"], env)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 28, 110, 0, 0))
        output = bytearray()
        def wait_for(predicate):
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if select.select([fd], [], [], .01)[0]:
                    output.extend(os.read(fd, 65536))
                if predicate():
                    return
            import re
            screen = re.sub(rb'\x1b(?:\[[0-?]*[ -/]*[@-~]|[()][0-9A-Za-z])', b'', bytes(output)).decode("utf8", "replace")
            self.fail("native navigation waited for the slow helper: " + screen[:1000] +
                      " clients=" + str(tmux.clients()) + " calls=" + (calls.read_text() if calls.exists() else ""))
        try:
            wait_for(lambda: bool(tmux.clients()))
            client = tmux.clients()[0]["name"]
            origin = native._client_pane(client)
            os.write(fd, b"\x1bas")
            wait_for(lambda: tmux.run(["display-message", "-p", "-t", origin, "#{pane_mode}"]).stdout.strip() == "tree-mode")
            old = tmux.run(["show-options", "-gqv", picker._navigation_option(client)]).stdout.strip()
            os.write(fd, b"1")
            wait_for(lambda: tmux.clients()[0]["session_id"] == local)
            self.assertNotIn("native-select", calls.read_text() if calls.exists() else "")
            origin = native._client_pane(client)
            os.write(fd, b"\x1bas")
            wait_for(lambda: tmux.run(["display-message", "-p", "-t", origin, "#{pane_mode}"]).stdout.strip() == "tree-mode")
            self.assertFalse(picker.navigation_valid(client, old))
            os.write(fd, b"2")
            wait_for(lambda: calls.exists() and "native-select" in calls.read_text())
            selected = next(line for line in calls.read_text().splitlines() if line.startswith("native-select"))
            self.assertIn("--client " + client, selected)
            self.assertIn("--target " + remote_pane, selected)
            with patch.object(connection, "open_remote") as connect:
                native.select(load(), client, remote_pane, old)
                connect.assert_not_called()
        finally:
            subprocess.run(self.base + ["kill-server"], capture_output=True)
            os.close(fd)
            os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
