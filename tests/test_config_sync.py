import contextlib
import io
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
import uuid
from unittest.mock import patch

from tmux_roam import config_sync, inventory
from tmux_roam.config import Config, Host


class ConfigSyncContracts(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="roam-sync-")
        self.home = Path(self.directory.name)
        self.host = Host("remote", "Remote", "unused", socket_name="roam-sync-" + uuid.uuid4().hex[:8])
        self.config = Config(self.home / "config.toml", (self.host,))
        self.base = self.host.tmux_args()
        subprocess.run(self.base + ["-f", "/dev/null", "new-session", "-d", "-s", "keep"], check=True)
        self.path = self.home / ".tmux.conf"
        self.old = b"set -g @sync_test before\n"
        self.path.write_bytes(self.old)
        self.path.chmod(0o640)
        subprocess.run(self.base + ["source-file", str(self.path)], check=True)
        self.env = patch.dict(os.environ, HOME=str(self.home), XDG_STATE_HOME=str(self.home / "state"))
        self.env.start()
        self.ssh = patch.object(inventory, "ssh_base", return_value=["sh", "-c"])
        self.ssh.start()

    def tearDown(self):
        self.ssh.stop()
        self.env.stop()
        subprocess.run(self.base + ["kill-server"], capture_output=True)
        self.directory.cleanup()

    def test_diff_backup_atomic_install_reload_and_skip_identical(self):
        content = b"# $(touch SHOULD_NOT_EXIST)\nset -g @sync_test 'after | quoted'\n"
        remote, diff = config_sync.compare(self.host, self.config, Path("shared.conf"), content)
        self.assertIn("-set -g @sync_test before", diff)
        self.assertEqual(self.path.read_bytes(), self.old)
        result = config_sync.sync(self.host, self.config, content, expected=remote["sha256"])
        self.assertEqual(result["status"], "synced")
        self.assertEqual(Path(result["backup"]).read_bytes(), self.old)
        self.assertEqual(self.path.read_bytes(), content)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertEqual(subprocess.check_output(self.base + ["show-options", "-gqv", "@sync_test"], text=True).strip(), "after | quoted")
        self.assertEqual(subprocess.check_output(self.base + ["list-sessions", "-F", "#{session_name}"], text=True), "keep\n")
        stamp = self.path.stat().st_mtime_ns
        self.assertEqual(config_sync.sync(self.host, self.config, content)["status"], "unchanged")
        self.assertEqual(self.path.stat().st_mtime_ns, stamp)
        self.assertEqual(len(list((self.home / "state/tmux-roam/backups").glob("*/tmux.conf"))), 1)
        self.assertEqual(config_sync.compare(self.host, self.config, Path("shared.conf"), content)[1], "")
        self.assertFalse((self.home / "SHOULD_NOT_EXIST").exists())

    def test_invalid_config_never_installs_and_reload_failure_restores_file(self):
        with self.assertRaisesRegex(RuntimeError, "unknown command"):
            config_sync.sync(self.host, self.config, b"not-a-tmux-command\n")
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertFalse((self.home / "state/tmux-roam/backups").exists())
        with self.assertRaisesRegex(RuntimeError, "previous file restored and reloaded"):
            config_sync.sync(self.host, self.config, b"set -g @sync_test partial\nset -g not-a-tmux-option invalid\n")
        self.assertEqual(self.path.read_bytes(), self.old)
        self.assertEqual(subprocess.check_output(self.base + ["show-options", "-gqv", "@sync_test"], text=True).strip(), "before")
        self.assertEqual(list(self.home.glob(".tmux-roam-sync-*")), [])

    def test_preview_races_symlinks_and_stopped_server(self):
        target = self.home / "dotfiles.conf"
        self.path.rename(target)
        self.path.symlink_to(target)
        remote, _ = config_sync.compare(self.host, self.config, Path("shared.conf"), b"set -g @sync_test new\n")
        target.write_text("# user edit\n" + self.old.decode())
        with self.assertRaisesRegex(RuntimeError, "changed since the preview"):
            config_sync.sync(self.host, self.config, b"set -g @sync_test new\n", expected=remote["sha256"])
        self.assertIn("# user edit", target.read_text())
        config_sync.sync(self.host, self.config, b"set -g @sync_test new\n")
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_bytes(), b"set -g @sync_test new\n")
        subprocess.run(self.base + ["kill-server"], check=True)
        result = config_sync.sync(self.host, self.config, self.old)
        self.assertEqual(result["status"], "saved")
        self.assertNotEqual(subprocess.run(self.base + ["-N", "list-sessions"], capture_output=True).returncode, 0)

    def test_all_continues_after_offline_host_and_requires_explicit_targets(self):
        other = Host("other", "Other", "unused")
        config = Config(self.config.path, (self.host, other))
        for host_id, all_hosts in ((None, False), ("missing", False), ("remote", True)):
            with self.assertRaises(ValueError):
                config_sync.hosts(config, host_id, all_hosts)
        with patch.object(config_sync, "sync", side_effect=[RuntimeError("offline"), dict(status="unchanged")]) as sync, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = config_sync.execute(config, "sync", all_hosts=True, source_path=self.path)
        self.assertEqual(result, 2)
        self.assertEqual([call.args[0].id for call in sync.call_args_list], ["remote", "other"])
        self.assertIn("Other: already matches", output.getvalue())


if __name__ == "__main__":
    unittest.main()
