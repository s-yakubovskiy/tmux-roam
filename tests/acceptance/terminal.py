#!/usr/bin/env python3
"""Reuse the established terminal contract with Go-installed key tables.

Python sets up disposable tmux/PTY fixtures only; every tested binding is
installed by the Go binary. No Python controller handles input.
"""
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_contracts import IsolatedTmuxContracts, integration
from compare_inventory import GO


def install(_):
    result = subprocess.run([str(GO), 'integrate', 'install'], env=os.environ,
                            capture_output=True, text=True)
    if result.returncode:
        raise AssertionError(result.stderr)


if __name__ == '__main__':
    integration.install = install
    suite = unittest.TestSuite([IsolatedTmuxContracts('test_managed_prefix_keys_reach_nested_tmux')])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
