#!/usr/bin/env python3
"""Measure healthy reuse and new-connection SSH/handshake/confirmation phases.

The tiny development-only C adapter clocks the actual OpenSSH child; no tracing
or controller instrumentation is enabled. Added delay runs on the remote side
of the disposable SSH target. This is an orchestration benchmark, not WAN data.
"""
from contextlib import ExitStack
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shlex
import shutil
import statistics
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/acceptance"))
from compare_inventory import GO, call
from compare_ssh_inventory import ssh_target
from session_lifecycle import wait_for


def summary(values):
    return {"samples": len(values), "p50_ms": round(statistics.median(values), 2),
            "p95_ms": round(sorted(values)[math.ceil(.95*len(values))-1], 2)}


def main():
    report = {"platform": platform.platform(), "load": os.getloadavg(), "tmux": subprocess.check_output(["tmux", "-V"], text=True).strip(),
              "controller_sha256": hashlib.sha256(GO.read_bytes()).hexdigest(),
              "scope": "real loopback OpenSSH and tmux; warm reuse and warm/cold transport new attachments; added delay per remote tmux command, not WAN latency; SSH durations include transport and remote execution; no terminal contents logged",
              "scenarios": {}, "new_connection": {}, "cold_transport_new_connection": {}}
    with ssh_target() as (root, ssh_config), ExitStack() as cleanup:
        local, remote = root / "local.sock", root / "remote.sock"
        def tmux(sock, *args):
            return subprocess.check_output(["tmux", "-S", str(sock), *args], text=True).strip()
        for sock in (local, remote):
            tmux(sock, "-f", "/dev/null", "new-session", "-d", "-s", "keep", "sleep 1800")
            cleanup.callback(lambda s=sock: subprocess.run(["tmux", "-S", str(s), "kill-server"], capture_output=True))
        script_bin = root / "bin"
        script_bin.mkdir()
        trace = root / "timings"
        source = root / "clock.c"
        source.write_text('''#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <sys/prctl.h>
#include <sys/wait.h>
static int terminal;
static void event(const char *phase) {
  struct timespec now; clock_gettime(CLOCK_MONOTONIC, &now);
  int out=open(getenv("TR_SSH_TIMINGS"), O_WRONLY|O_CREAT|O_APPEND, 0600);
  if(out<0) exit(2);
  dprintf(out,"%d %d %s %.9f\\n",getpid(),terminal,phase,now.tv_sec+now.tv_nsec/1e9);
  close(out);
}
int main(int argc, char **argv) {
  int channel[2], status, first=1;
  for(int i=1;i<argc;i++) if(strcmp(argv[i],"-tt")==0) terminal=1;
  if(terminal && pipe(channel)<0) return 2;
  event("start");
  pid_t pid=fork();
  if(pid<0) return 2;
  if(pid==0) {
    prctl(PR_SET_PDEATHSIG,SIGTERM);
    if(terminal) { dup2(channel[1],STDOUT_FILENO); close(channel[0]); close(channel[1]); }
    execv(getenv("TR_REAL_SSH"), argv); _exit(127);
  }
  if(terminal) {
    close(channel[1]);
    char buffer[65536]; ssize_t size;
    while((size=read(channel[0],buffer,sizeof buffer))!=0) {
      if(size<0) { if(errno==EINTR) continue; return 2; }
      if(first) { event("first"); first=0; }
      for(ssize_t sent=0;sent<size;) {
        ssize_t n=write(STDOUT_FILENO,buffer+sent,size-sent);
        if(n<0) { if(errno==EINTR) continue; return 2; }
        sent+=n;
      }
    }
    close(channel[0]);
  }
  if(waitpid(pid, &status, 0)<0) return 2;
  event("exit");
  return WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
}
''')
        subprocess.run(["cc", "-O2", "-o", str(script_bin / "ssh"), str(source)], check=True)
        delay = root / "delay"
        delay.write_text("0\n")
        wrapper = root / "remote-tmux"
        wrapper.write_text(f'#!/bin/sh\nread delay < {shlex.quote(str(delay))}\nsleep "$delay"\nexec {shlex.quote(shutil.which("tmux"))} "$@"\n')
        wrapper.chmod(0o700)
        config = root / "config.toml"
        config.write_text(f'[[hosts]]\nid="remote"\nssh="disposable"\nsocket_path="{remote}"\ntmux_path="{wrapper}"\n')
        env = {**os.environ, "PATH": str(script_bin)+":"+os.environ["PATH"], "TMUX": f"{local},0,0",
               "TMUX_ROAM_CONFIG": str(config), "TMUX_ROAM_SSH_CONFIG": str(ssh_config), "XDG_RUNTIME_DIR": str(root / "r"),
               "XDG_CACHE_HOME": str(root / "cache"), "TERM": "xterm-256color", "TR_REAL_SSH": shutil.which("ssh"), "TR_SSH_TIMINGS": str(trace)}
        for key in ("XDG_RUNTIME_DIR", "XDG_CACHE_HOME", "PATH", "TR_REAL_SSH", "TR_SSH_TIMINGS"):
            tmux(local, "set-environment", "-g", key, env[key])
        control = subprocess.Popen(["tmux", "-S", str(local), "-C", "attach-session", "-t", "keep"],
                                   env=env, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        cleanup.callback(lambda: control.communicate(b"detach-client\n", timeout=5))
        client = wait_for(lambda: tmux(local, "list-clients", "-F", "#{client_name}"))
        _, data = call(GO, env, "list", "--json")
        record = next(r for r in data["sessions"] if r["host_id"] == "remote")
        argv = [str(GO), "open", record["id"], "--client", client]
        subprocess.run(argv, env=env, check=True, capture_output=True)
        def requests(since):
            result = {}
            for line in trace.read_text().splitlines():
                pid, terminal, phase, at = line.split()
                if float(at) >= since:
                    result.setdefault(pid, {"terminal": terminal == "1"})[phase] = float(at)
            return [p for p in result.values() if "start" in p]
        for added_ms in (0, 25, 100):
            delay.write_text(f"{added_ms/1000}\n")
            total, ssh, local_time = [], [], []
            for _ in range(30):
                trace.write_text("")
                start = time.perf_counter()
                result = subprocess.run(argv, env=env, capture_output=True, timeout=10)
                elapsed = (time.perf_counter()-start)*1000
                assert result.returncode == 0, result.stderr
                phases = requests(start)
                assert len(phases) == 1 and not phases[0]["terminal"], phases
                external = (phases[0]["exit"]-phases[0]["start"])*1000
                total.append(elapsed)
                ssh.append(external)
                local_time.append(elapsed-external)
            report["scenarios"][f"{added_ms}ms added remote delay"] = {"total": summary(total), "ssh_child": summary(ssh), "local_orchestration": summary(local_time), "ssh_requests_per_reuse": 1}
        def close_carrier():
            carrier = tmux(local, "list-clients", "-F", "#{session_id}")
            tmux(local, "switch-client", "-c", client, "-t", "keep")
            tmux(local, "kill-session", "-t", carrier)
            wait_for(lambda: tmux(remote, "list-sessions", "-F", "#{session_attached}") == "0")
        close_carrier()
        for added_ms, cold in ((0, False), (25, False), (100, False), (0, True)):
            delay.write_text(f"{added_ms/1000}\n")
            metrics = {key: [] for key in ("total", "preflight_ssh", "attach_to_first_handshake_byte", "handshake_to_confirmed_switch", "confirmation_ssh", "outside_ssh")}
            counts = []
            for _ in range(30):
                if cold:
                    # End only this fixture's idle transport, before timing.
                    # The first preflight must establish a real SSH transport.
                    for path in (root / "r/tmux-roam").glob("ssh-*"):
                        if path.is_socket():
                            subprocess.run([env["TR_REAL_SSH"], "-F", str(ssh_config), "-S", str(path), "-O", "exit", "--", "disposable"], check=True, capture_output=True)
                            wait_for(lambda: not path.exists())
                trace.write_text("")
                start = time.perf_counter()
                result = subprocess.run(argv, env=env, capture_output=True, timeout=10)
                end = time.perf_counter()
                assert result.returncode == 0, result.stderr
                phases = requests(start)
                attachments = [p for p in phases if p["terminal"]]
                assert len(attachments) == 1 and "first" in attachments[0], phases
                attached = attachments[0]
                before = [p for p in phases if not p["terminal"] and p["start"] < attached["start"]]
                confirmed = [p for p in phases if not p["terminal"] and p["start"] >= attached["first"]]
                assert len(before) == 2 and confirmed and all("exit" in p for p in before+confirmed), phases
                preflight = sum(p["exit"]-p["start"] for p in before)
                confirmation = sum(p["exit"]-p["start"] for p in confirmed)
                handshake = attached["first"]-attached["start"]
                for key, seconds in {"total": end-start, "preflight_ssh": preflight,
                                     "attach_to_first_handshake_byte": handshake,
                                     "handshake_to_confirmed_switch": end-attached["first"],
                                     "confirmation_ssh": confirmation,
                                     "outside_ssh": end-start-preflight-handshake-confirmation}.items():
                    metrics[key].append(seconds*1000)
                counts.append(len(phases))
                close_carrier()
            destination = report["cold_transport_new_connection"] if cold else report["new_connection"]
            destination[f"{added_ms}ms added remote delay"] = {**{key: summary(values) for key, values in metrics.items()}, "ssh_requests_min_max": [min(counts), max(counts)]}
    print(json.dumps(report, indent=2))
    assert all(row["local_orchestration"]["p95_ms"] <= 50 for row in report["scenarios"].values()), report


if __name__ == "__main__":
    main()
