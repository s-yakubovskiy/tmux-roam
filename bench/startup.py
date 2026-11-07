#!/usr/bin/env python3
"""Interleaved warm CLI startup comparison; run after scripts/build-go.sh."""
from pathlib import Path
import statistics
import subprocess
import time

root = Path(__file__).resolve().parents[1]
commands = {"Go": [str(root / "build/tmux-roam-go"), "--version"],
            "Python": [str(root / "bin/tmux-roam"), "--version"]}
measurements = {name: [] for name in commands}
for trial in range(30):
    for name in list(commands) if trial % 2 == 0 else list(reversed(commands)):
        start = time.perf_counter_ns()
        subprocess.run(commands[name], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        measurements[name].append((time.perf_counter_ns() - start) / 1e6)
for name, values in measurements.items():
    ordered = sorted(values)
    print(f"{name}: n={len(values)} p50={statistics.median(values):.2f} ms p95={ordered[28]:.2f} ms")
