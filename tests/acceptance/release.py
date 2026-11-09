#!/usr/bin/env python3
"""Offline bundle install/update/rollback without Python or Go in runtime PATH."""
import hashlib
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]


def main():
    archive = ROOT / 'build/releases/tmux-roam-0.1.0-linux_amd64.tar.gz'
    if not archive.exists():
        raise SystemExit('build releases first: sh scripts/release-go.sh')
    with tempfile.TemporaryDirectory(prefix='tr-install-') as temporary:
        root = Path(temporary)
        checkout = root / 'checkout'
        (checkout / 'scripts').mkdir(parents=True)
        (checkout / 'bin').mkdir()
        for file in ('scripts/install-go.sh', 'bin/tmux-roam', 'VERSION'):
            shutil.copy2(ROOT / file, checkout / file)
        runtime_bin = root / 'path'
        runtime_bin.mkdir()
        for name in ('sh', 'cat', 'dirname', 'sha256sum', 'mktemp', 'tar', 'sort', 'rm', 'gzip', 'uname'):
            path = shutil.which(name)
            assert path, name
            (runtime_bin / name).symlink_to(path)
        env = {**os.environ, 'PATH': str(runtime_bin)}
        env.pop('TMUX_ROAM_BACKEND', None)
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        def install(bundle, digest, good=True):
            result = subprocess.run([str(checkout / 'scripts/install-go.sh'), str(bundle), digest], env=env,
                                    capture_output=True, text=True, timeout=15)
            assert (result.returncode == 0) == good, (result.stdout, result.stderr)
        install(archive, checksum)
        current = checkout / '.runtime/current'
        initial = current.resolve()
        assert 'releases' in initial.parts
        # A malicious working directory/PATH cannot replace the controller.
        (root / 'tmux-roam').write_text('#!/bin/sh\nexit 99\n')
        (root / 'tmux-roam').chmod(0o700)
        result = subprocess.run([str(checkout / 'bin/tmux-roam'), '--version'], env=env, cwd=root, capture_output=True, text=True)
        assert result.returncode == 0 and result.stdout.strip() == '0.1.0', result
        assert not (runtime_bin / 'python3').exists() and not (runtime_bin / 'go').exists()
        install(archive, '0'*64, False)
        assert current.resolve() == initial
        install(root / 'unavailable.tar.gz', checksum, False)
        assert current.resolve() == initial
        # Corrupt payload with a valid transport hash still fails the manifest.
        stage = root / 'stage'
        stage.mkdir()
        subprocess.run(['tar', '-xzf', str(archive), '-C', str(stage)], check=True)
        (stage / 'agents/linux_amd64').chmod(0o700)
        with (stage / 'agents/linux_amd64').open('ab') as file:
            file.write(b'corrupt')
        (stage / 'agents/linux_amd64').chmod(0o500)
        corrupt = root / 'corrupt.tar.gz'
        def pack(path):
            subprocess.run(['tar', '-C', str(stage), '-czf', str(path), 'tmux-roam', 'agents/linux_amd64', 'agents/linux_arm64', 'manifest.json'], check=True)
            return hashlib.sha256(path.read_bytes()).hexdigest()
        install(corrupt, pack(corrupt), False)
        assert current.resolve() == initial
        truncated = root / 'truncated.tar.gz'
        truncated.write_bytes(archive.read_bytes()[:1024])
        install(truncated, hashlib.sha256(truncated.read_bytes()).hexdigest(), False)
        assert current.resolve() == initial
        subprocess.run(['tar', '-xzf', str(archive), '-C', str(stage)], check=True)
        manifest = json.loads((stage / 'manifest.json').read_text())
        for field, value in (('version', '0.0.0'), ('arch', 'mips64'), ('agent_protocol', 999)):
            changed = {**manifest, field: value}
            (stage / 'manifest.json').write_text(json.dumps(changed))
            install(corrupt, pack(corrupt), False)
            assert current.resolve() == initial, field
        # Interrupt a real installer while its controller awaits the lock.
        # Signal the fixture's process group so the bootstrap and child stop.
        install_tmp = root / 'install-tmp'
        install_tmp.mkdir()
        with (checkout / '.runtime/install.lock').open('rb') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            child = subprocess.Popen([str(checkout / 'scripts/install-go.sh'), str(archive), checksum],
                                     env={**env, 'TMPDIR': str(install_tmp)}, start_new_session=True,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while True:
                    children = Path(f'/proc/{child.pid}/task/{child.pid}/children').read_text().split()
                    try:
                        waiting = any(b'\x00release\x00install\x00' in Path(f'/proc/{pid}/cmdline').read_bytes() for pid in children)
                    except FileNotFoundError:
                        waiting = False
                    if waiting:
                        break
                    assert time.monotonic() < deadline, 'installer never reached the lock'
                    time.sleep(.01)
                os.killpg(child.pid, signal.SIGTERM)
                child.communicate(timeout=5)
                assert child.returncode != 0 and current.resolve() == initial
                assert not list(install_tmp.iterdir()), 'bootstrap staging leaked'
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.communicate(timeout=5)
        # A second verified development build provides an actual rollback target.
        subprocess.run(['tar', '-xzf', str(archive), '-C', str(stage)], check=True)
        manifest = json.loads((stage / 'manifest.json').read_text())
        manifest['build'] = 'a'*64 if manifest['build'] != 'a'*64 else 'b'*64
        (stage / 'manifest.json').write_text(json.dumps(manifest))
        update = root / 'update.tar.gz'
        install(update, pack(update))
        upgraded = current.resolve()
        assert upgraded != initial
        result = subprocess.run([str(current / 'tmux-roam'), 'release', 'rollback', str(checkout / '.runtime')],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0 and current.resolve() == initial, result.stderr
        # Concurrent repeated installs retain paired immutable artifacts.
        children = [subprocess.Popen([str(checkout / 'scripts/install-go.sh'), str(archive), checksum], env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(3)]
        for child in children:
            out, err = child.communicate(timeout=15)
            assert child.returncode == 0, (out, err)
        assert current.resolve() == initial
        print('Go releases: offline compiler/Python-free install, deterministic launcher, unavailable/truncated/corrupt/mismatched bundles, interrupted installation, paired update, exercised rollback, concurrent install')


if __name__ == '__main__':
    main()
