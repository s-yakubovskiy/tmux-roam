#!/usr/bin/env python3
"""Interleaved inventory CLI measurements with real local tmux and scripted SSH.

Remote transcript timing excludes network and remote tmux. stdout is JSON so a
run can be retained verbatim. This does not measure visible picker performance.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import statistics
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = {"Go": ROOT / "build/tmux-roam-go", "Python": ROOT / "bin/tmux-roam-python"}
RUNS = 30


def transcript(count):
    sessions = [f"${i}|100|project-{i:03}-Юникод|5|0|C-b" for i in range(count)]
    windows = [f"${i}|@{i * 5 + j}|{j}|{int(j == 0)}|window-{j}" for i in range(count) for j in range(5)]
    return "\n".join(["12|34|/tmp/remote.sock", *sessions, "TMUX_ROAM_CLIENTS_V1", "TMUX_ROAM_WINDOWS_V1", *windows]) + "\n"


def main():
    if not BACKENDS["Go"].exists():
        raise SystemExit("build Go first: sh scripts/build-go.sh")
    tmux = str(Path(shutil.which("tmux")).resolve())
    report = {"controller_sha256": hashlib.sha256(BACKENDS["Go"].read_bytes()).hexdigest(), "platform": platform.platform(), "cpu": platform.processor(), "load": os.getloadavg(),
              "tmux": subprocess.check_output([tmux, "-V"], text=True).strip(),
              "go": subprocess.check_output(["go", "version"], text=True).strip(),
              "build": "CGO_ENABLED=0 -trimpath -ldflags='-s -w'", "runs": RUNS,
              "scope": "CLI JSON completion; 1 local session; scripted SSH, no network/remote cost; no UI", "scenarios": {}}
    with tempfile.TemporaryDirectory(prefix="tr-bench-") as directory:
        root = Path(directory)
        sock = root / "local.sock"
        subprocess.run([tmux, "-S", str(sock), "-f", "/dev/null", "new-session", "-d", "-s", "local"], check=True)
        try:
            script_bin = root / "bin"
            script_bin.mkdir()
            counter = root / "calls"
            data_file = root / "transcript"
            scripts = {
                "tmux": f"echo tmux >> {shlex.quote(str(counter))}\nexec {shlex.quote(tmux)} \"$@\"\n",
                "ssh": f'''echo ssh >> {shlex.quote(str(counter))}
while [ "$1" != -- ]; do shift; done
shift
if [ "$1" = down ]; then sleep 0.025; exit 255; fi
exec cat {shlex.quote(str(data_file))}
''',
            }
            for name, content in scripts.items():
                path = script_bin / name
                path.write_text("#!/bin/sh\n" + content)
                path.chmod(0o700)
            config = root / "config.toml"
            for count, cached, failed in ((0, False, False), (0, True, False), (20, False, False), (20, True, False), (500, False, False),
                                          (500, True, False), (20, False, True)):
                data_file.write_text(transcript(count))
                text = '[[hosts]]\nid = "remote"\nssh = "remote"\n'
                if failed:
                    text += '[[hosts]]\nid = "down"\nssh = "down"\n'
                config.write_text(text)
                common = {**os.environ, "PATH": str(script_bin) + ":" + os.environ["PATH"],
                          "TMUX": f"{sock},0,0", "TMUX_ROAM_CONFIG": str(config), "XDG_RUNTIME_DIR": str(root / "r")}
                envs = {name: {**common, "XDG_CACHE_HOME": str(root / (name + "-cache"))} for name in BACKENDS}
                for name, exe in BACKENDS.items():
                    subprocess.run([str(exe), "list", "--json"], env=envs[name], capture_output=True, timeout=10)
                timings = {name: [] for name in BACKENDS}
                launches = {name: [] for name in BACKENDS}
                for trial in range(RUNS):
                    for name in list(BACKENDS) if trial % 2 == 0 else list(reversed(BACKENDS)):
                        counter.write_text("")
                        args = [str(BACKENDS[name]), "list", "--json"] + (["--cached"] if cached else [])
                        start = time.perf_counter_ns()
                        result = subprocess.run(args, env=envs[name], capture_output=True, timeout=10)
                        timings[name].append((time.perf_counter_ns() - start) / 1e6)
                        data = json.loads(result.stdout)
                        assert result.returncode == (2 if failed else 0), data
                        assert len(data["sessions"]) == count + 1
                        assert sum(len(r["window_details"] or []) for r in data["sessions"]) == count * 5
                        calls = counter.read_text().splitlines()
                        launches[name].append({"tmux": calls.count("tmux"), "ssh": calls.count("ssh")})
                key = f"{count} sessions/{count * 5} windows; " + ("cached" if cached else "live") + ("; one 25ms failing host" if failed else "")
                report["scenarios"][key] = {
                    name: {"p50_ms": round(statistics.median(values), 2), "p95_ms": round(sorted(values)[28], 2),
                           "process_counts": launches[name][0]} for name, values in timings.items()}
                for counts in launches.values():
                    assert all(c == counts[0] for c in counts), counts
        finally:
            subprocess.run([tmux, "-S", str(sock), "kill-server"], capture_output=True)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
