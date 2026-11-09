#!/usr/bin/env python3
"""Interleaved CLI startup/config parsing; run after scripts/build-go.sh."""
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import time

root = Path(__file__).resolve().parents[1]
backends = {"Go": root / "build/tmux-roam-go", "Python": root / "bin/tmux-roam-python"}
report = {"controller_sha256": hashlib.sha256(backends["Go"].read_bytes()).hexdigest(), "platform": platform.platform(), "load": os.getloadavg(), "runs": 30,
          "go": subprocess.check_output(["go", "version"], text=True).strip(),
          "build": "CGO_ENABLED=0 -trimpath -ldflags='-s -w'", "scope": "process completion; no tmux, SSH or UI; first invocation is not a cold filesystem benchmark", "results": {}}
for scenario, args in (("version", ["--version"]), ("config", ["config", "check"])):
    timings = {name: [] for name in backends}
    first = {}
    env = {**os.environ, "TMUX_ROAM_CONFIG": str(root / "examples/config.toml")}
    for trial in range(31):
        for name in list(backends) if trial % 2 == 0 else list(reversed(backends)):
            start = time.perf_counter_ns()
            subprocess.run([str(backends[name]), *args], env=env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elapsed = (time.perf_counter_ns() - start) / 1e6
            if trial:
                timings[name].append(elapsed)
            else:
                first[name] = round(elapsed, 2)
    report["results"][scenario] = {name: {"first_ms": first[name], "p50_ms": round(statistics.median(values), 2),
                              "p95_ms": round(sorted(values)[math.ceil(.95*len(values))-1], 2)} for name, values in timings.items()}
print(json.dumps(report, indent=2))
