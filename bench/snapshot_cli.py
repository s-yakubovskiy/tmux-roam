#!/usr/bin/env python3
"""Freeze Python argparse help and exit codes for all 20 command names."""
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
commands = ("pick", "native", "native-select", "native-refresh", "popup",
            "picker-state", "picker-header", "picker-select", "details", "help",
            "actions", "new-session", "delete-session", "list", "refresh",
            "open", "doctor", "integrate", "config", "supervise")
result = {}
for name in commands:
    completed = subprocess.run([str(root / "bin/tmux-roam-python"), name, "--help"],
                               capture_output=True, text=True, env={**os.environ, "TMUX_ROAM_CONFIG": "/nonexistent/tmux-roam/config.toml"})
    result[name] = {"exit": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
completed = subprocess.run([str(root / "bin/tmux-roam-python"), "--version"], capture_output=True, text=True)
result["--version"] = {"exit": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
path = root / "tests/fixtures/python_cli_help.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(f"captured {len(commands)} commands and --version to {path}")
