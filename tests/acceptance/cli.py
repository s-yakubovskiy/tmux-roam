#!/usr/bin/env python3
"""Frozen public help and malformed-argument checks before any external work."""
import json
import os
from pathlib import Path
import subprocess
from compare_inventory import GO, ROOT


def main():
    env = {**os.environ, 'TMUX_ROAM_CONFIG': '/unavailable/config'}
    expected = json.loads((ROOT / 'tests/fixtures/python_cli_help.json').read_text())
    for name, case in expected.items():
        args = ['--version'] if name == '--version' else [name, '--help']
        result = subprocess.run([str(GO), *args], env=env, capture_output=True, text=True)
        assert result.returncode == case['exit'] and result.stdout == case['stdout'] and result.stderr == case['stderr'], (name, result)
    invalid = [[], ['popup'], ['native', '--bogus', 'x'], ['native-select', '--client', 'x'],
               ['open'], ['list', '--bogus'], ['config', 'wat'], ['config', 'sync', 'host', '--all'],
               ['config', 'check', '--source', '/tmp/x'], ['integrate', 'wat'],
               ['picker-state', '/tmp/x', 'wat'], ['details', '/tmp/x'], ['doctor', '--bogus']]
    for args in invalid:
        result = subprocess.run([str(GO), *args], env=env, capture_output=True, text=True)
        assert result.returncode == 2, (args, result)
    print(f'Go CLI: {len(expected)} frozen help/version contracts; {len(invalid)} malformed invocations rejected')


if __name__ == '__main__':
    main()
