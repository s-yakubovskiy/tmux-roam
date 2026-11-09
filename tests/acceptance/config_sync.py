#!/usr/bin/env python3
"""Run Python-free Go config sync through real SSH into a private remote HOME."""
import os
import shutil
from pathlib import Path
import subprocess
import time

from compare_inventory import GO
from compare_ssh_inventory import ssh_target
from session_lifecycle import wait_for


def main():
    with ssh_target(isolated_home=True, python_free=True) as (root, ssh_config):
        remote = root / "remote.sock"
        tmux = ["tmux", "-S", str(remote)]
        subprocess.run([*tmux, "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 120"], check=True)
        try:
            target = root / "home/dotfiles.conf"
            target.write_text("set -g @sync_test before\n")
            target.chmod(0o640)
            path = root / "home/.tmux.conf"
            path.symlink_to(target)
            config = root / "config.toml"
            config.write_text(f'[[hosts]]\nid="remote"\nssh="disposable"\nsocket_path="{remote}"\n')
            local_bin = root / "local-bin"
            local_bin.mkdir()
            for name in ("ssh", "tmux", "diff", "sh"):
                (local_bin / name).symlink_to(shutil.which(name))
            env = {**os.environ, "PATH": str(local_bin), "TMUX_ROAM_CONFIG": str(config), "TMUX_ROAM_SSH_CONFIG": str(ssh_config),
                   "XDG_RUNTIME_DIR": str(root / "r")}
            source = root / "shared.conf"
            source.write_text("set -g @sync_test 'after | quoted'\n")
            def call(action, expected):
                result = subprocess.run([str(GO), "config", action, "remote", "--source", str(source)],
                                        env=env, text=True, capture_output=True, timeout=30)
                assert result.returncode == expected, (result.stdout, result.stderr)
                return result.stdout
            assert "-set -g @sync_test before" in call("diff", 1)
            assert target.read_text() == "set -g @sync_test before\n"
            assert "installed and reloaded" in call("sync", 0)
            assert path.is_symlink() and target.read_bytes() == source.read_bytes()
            assert target.stat().st_mode & 0o777 == 0o640
            assert subprocess.check_output([*tmux, "show-options", "-gqv", "@sync_test"], text=True).strip() == "after | quoted"
            assert subprocess.check_output([*tmux, "list-sessions", "-F", "#{session_name}"], text=True) == "keep\n"
            backups = list((root / "state/tmux-roam/backups").glob("*/tmux.conf"))
            assert len(backups) == 1 and backups[0].read_text() == "set -g @sync_test before\n"
            assert "already matches" in call("sync", 0)
            call("diff", 0)
            source.write_text("not-a-tmux-command\n")
            call("sync", 2)
            assert target.read_text() == "set -g @sync_test 'after | quoted'\n"
            source.write_text("set -g @sync_test partial\nset -g not-a-tmux-option invalid\n")
            assert "previous file restored and reloaded" in call("sync", 2)
            assert subprocess.check_output([*tmux, "show-options", "-gqv", "@sync_test"], text=True).strip() == "after | quoted"
            subprocess.run([*tmux, "kill-server"], check=True)
            source.write_text("set -g @sync_test saved\n")
            assert "no running server" in call("sync", 0)
            # Inject bootstrap failures through actual SSH, still with no
            # Python/Go in either runtime PATH. Never modify the real HOME.
            fault = root / "fault"
            fault.write_text("")
            tar = root / "runtime-bin/tar"
            tar.unlink()
            tar.write_text(f'''#!/bin/sh
mode=$({shutil.which('cat')} "{fault}")
case "$mode" in
  truncated-transfer) {shutil.which('head')} -c 1024 | {shutil.which('tar')} "$@"; exit $? ;;
  unpack-full) echo 'tar: No space left on device (injected)' >&2; exit 2 ;;
  interrupted-transfer) sleep 1 ;;
esac
{shutil.which('tar')} "$@" || exit $?
if [ "$mode" = noexec ]; then {shutil.which('chmod')} -x "$4/helper"; fi
''')
            tar.chmod(0o700)
            shell = root / "runtime-bin/sh"
            shell.unlink()
            shell.write_text(f'''#!/bin/sh
mode=$({shutil.which('cat')} "{fault}")
case "$mode" in
  truncated-response) /bin/sh "$@" | {shutil.which('head')} -c 12 ;;
  lost-response) /bin/sh "$@" > /dev/null; exit 255 ;;
  *) exec /bin/sh "$@" ;;
esac
''')
            shell.chmod(0o700)
            for failure in ("truncated-transfer", "unpack-full", "noexec", "permissions"):
                before = target.read_bytes()
                source.write_text(f"set -g @sync_test {failure}\n")
                fault.write_text(failure)
                if failure == "permissions":
                    target.parent.chmod(0o500)
                try:
                    call("sync", 2)
                finally:
                    target.parent.chmod(0o700)
                assert target.read_bytes() == before, failure
                assert path.is_symlink() and target.stat().st_mode & 0o777 == 0o640
                assert not list((root / "tmp").glob("tmux-roam-sync.*")), failure
                assert not list((root / "home").glob(".tmux-roam-sync-*")), failure
            for failure in ("truncated-response", "lost-response"):
                source.write_text(f"set -g @sync_test {failure}\n")
                fault.write_text(failure)
                assert "run config diff" in call("sync", 2)
                # The helper committed before its reply was lost. A new diff
                # observes that fact; the coordinator never reports success.
                assert target.read_bytes() == source.read_bytes()
                fault.write_text("")
                call("diff", 0)
            uname = root / "runtime-bin/uname"
            uname.unlink()
            for platform in ("Darwin arm64", "Linux mips"):
                uname.write_text(f"#!/bin/sh\nprintf '%s\\n' '{platform}'\n")
                uname.chmod(0o700)
                before = target.read_bytes()
                call("sync", 2)
                assert target.read_bytes() == before and not list((root / "tmp").glob("tmux-roam-sync.*"))
            uname.unlink()
            uname.symlink_to(shutil.which("uname"))
            before = target.read_bytes()
            source.write_text("set -g @sync_test interrupted\n")
            fault.write_text("interrupted-transfer")
            child = subprocess.Popen([str(GO), "config", "sync", "remote", "--source", str(source)],
                                     env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            wait_for(lambda: list((root / "tmp").glob("tmux-roam-sync.*/owner")))
            child.terminate()
            output, error = child.communicate(timeout=5)
            assert child.returncode == 2 and "run config diff" in output+error, (output, error)
            wait_for(lambda: not list((root / "tmp").glob("tmux-roam-sync.*")))
            assert target.read_bytes() == before
            fault.write_text("")
            remote_tmux = root / "runtime-bin/tmux"
            remote_tmux.unlink()
            remote_tmux.write_text(f'''#!/bin/sh
case "$*" in
  *'source-file -n'*) sleep 30 ;;
esac
exec {shutil.which('tmux')} "$@"
''')
            remote_tmux.chmod(0o700)
            started = time.monotonic()
            call("sync", 2)
            assert time.monotonic()-started < 20, "validation timeout was unbounded"
            assert target.read_bytes() == before
            assert not list((root / "tmp").glob("tr-validate-*")), "validation server directory leaked"
            remote_tmux.unlink()
            remote_tmux.symlink_to(shutil.which("tmux"))
            # Only old, explicitly marked, private directories with a dead
            # owner may be recovered. Active and unrelated paths survive.
            stale = root / "tmp/tmux-roam-sync.stale"
            active = root / "tmp/tmux-roam-sync.active"
            unrelated = root / "tmp/tmux-roam-sync.unmarked"
            outside = root / "outside"
            outside.mkdir()
            link = root / "tmp/tmux-roam-sync.link"
            link.symlink_to(outside, target_is_directory=True)
            for directory, owner in ((stale, 2147483647), (active, os.getpid()), (unrelated, None)):
                directory.mkdir(mode=0o700)
                if owner:
                    marker = directory / "owner"
                    marker.write_text(f"tmux-roam-sync-v1\n{owner}\n")
                    os.utime(marker, (time.time()-48*3600,)*2)
            call("diff", 1)
            assert not stale.exists()
            assert active.exists() and unrelated.exists() and link.is_symlink() and outside.exists()
            shutil.rmtree(active)
            shutil.rmtree(unrelated)
            link.unlink()
            assert not list((root / "tmp").glob("tmux-roam-sync.*")), "temporary remote helper leaked"
            assert not list((root / "home").glob(".tmux-roam-sync-*")), "transaction staging leaked"
            for process in Path("/proc").iterdir():
                if process.name.isdecimal():
                    try:
                        executable = (process / "exe").readlink()
                    except (OSError, PermissionError):
                        continue
                    assert not str(executable).startswith(str(root / "tmp/tmux-roam-sync.")), "remote helper still running"
            print("Go config sync over real SSH: diff, backup, symlink/perms, reload, unchanged, syntax rejection, rollback, stopped server, truncated/interrupted transfer, validation timeout, injected unpack ENOSPC, non-executable helper, permission denial, lost/truncated commit replies, safe stale staging recovery, helper cleanup")
        finally:
            subprocess.run([*tmux, "kill-server"], capture_output=True)


if __name__ == "__main__":
    main()
