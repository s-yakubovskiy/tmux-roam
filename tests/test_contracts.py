import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from tmux_roam import integration, inventory, picker, tmux
from tmux_roam.config import ConfigError, load


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


class IsolatedTmuxContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tmux-roam-test-")
        self.name = "roam-test-" + uuid.uuid4().hex[:10]
        self.base = ["tmux", "-L", self.name]
        subprocess.run(self.base + ["-f", "/dev/null", "new-session", "-d", "-s", "start"], check=True)
        self.env = patch.dict(os.environ, {"TMUX": f"/tmp/tmux-{os.getuid()}/{self.name},0,0",
                                          "TMUX_ROAM_CONFIG": str(Path(self.directory.name) / "config.toml")})
        Path(self.directory.name, "config.toml").write_text("version = 1\n")
        self.env.start()

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

    def test_install_unload_preserves_user_binding_and_later_edit(self):
        tmux.run(["bind-key", "-T", "prefix", "s", "choose-tree", "-Zs"])
        original = integration._binding("prefix", "s")
        integration.install(str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam"))
        self.assertIn("run-shell", integration._binding("prefix", "s"))
        integration.unload()
        self.assertEqual(original, integration._binding("prefix", "s"))
        integration.install(str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam"))
        tmux.run(["bind-key", "-T", "prefix", "s", "display-message", "custom"])
        integration.unload()
        self.assertIn("custom", integration._binding("prefix", "s"))

    def test_spare_binding_can_be_promoted_without_leaking_key(self):
        binary = str(Path(__file__).resolve().parent.parent / "bin" / "tmux-roam")
        tmux.run(["unbind-key", "-q", "-T", "prefix", "F9"])
        tmux.run(["set-option", "-g", "@tmux-roam-chooser-keys", "F9"])
        integration.install(binary)
        self.assertIn("run-shell", integration._binding("prefix", "F9"))
        tmux.run(["set-option", "-g", "@tmux-roam-chooser-keys", "s ы"])
        integration.install(binary)
        self.assertEqual("", integration._binding("prefix", "F9"))
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


if __name__ == "__main__":
    unittest.main()
