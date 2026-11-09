#!/usr/bin/env python3
"""Use disposable Docker tmpfs mounts for actual ENOSPC/noexec acceptance.

Set TMUX_ROAM_TEST_CONTAINER_IMAGE to an existing Linux AMD64 image with busybox
or standard sh/tar/mktemp/rm tools. No image is pulled or host mount changed.
"""
import io
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile


def main():
    image = os.environ["TMUX_ROAM_TEST_CONTAINER_IMAGE"]
    repo = Path(__file__).resolve().parents[2]
    bootstrap = re.search(r"bootstrap := `([^`]+)`", (repo / "internal/configsync/client.go").read_text())[1]
    for failure in ("noexec", "full-staging", "full-destination"):
        stage_options = "rw,size=64k,exec" if failure == "full-staging" else "rw,size=16m,"+("noexec" if failure == "noexec" else "exec")
        command = ["docker", "run", "-d", "--rm", "--pull=never", "--network=none", "--read-only", "--cap-drop=ALL",
                   "--user", f"{os.getuid()}:{os.getgid()}", "--mount", f"type=bind,src={repo / 'build'},dst=/bundle,readonly",
                   "-e", "HOME=/home", "-e", "TMPDIR=/stage", "-e", "XDG_STATE_HOME=/state", "--entrypoint", "sh"]
        for destination, options in (("/home", "rw,size=64k"), ("/state", "rw,size=1m"), ("/stage", stage_options)):
            command += ["--tmpfs", f"{destination}:{options},mode=0700,uid={os.getuid()},gid={os.getgid()}"]
        container = subprocess.check_output([*command, image, "-c", "sleep 120"], text=True).strip()
        def execute(args, data=None):
            return subprocess.run(["docker", "exec", "-i", container, *args], input=data, capture_output=True, timeout=20)
        try:
            result = execute(["sh", "-c", "printf 'before\\n' > /home/target; chmod 640 /home/target; ln -s target /home/.tmux.conf"])
            assert result.returncode == 0, result.stderr
            request = {"protocol": 1, "action": "sync", "tmux": ["/bin/true"], "timeout": 1,
                       "content": base64.b64encode(b"#"*100000+b"\n").decode()}
            archive = io.BytesIO()
            with tarfile.open(fileobj=archive, mode="w") as bundle:
                for name, content, mode in (("helper", (repo / "build/agents/linux_amd64").read_bytes(), 0o700),
                                            ("request.json", json.dumps(request).encode(), 0o600)):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode = len(content), mode
                    bundle.addfile(info, io.BytesIO(content))
            result = execute(["sh", "-c", bootstrap], archive.getvalue())
            assert result.returncode != 0, (failure, result.stdout, result.stderr)
            if failure == "full-destination":
                response = json.loads(result.stdout)
                assert "no space left" in response["error"].lower(), response
            else:
                expected = b"Permission denied" if failure == "noexec" else b"No space left"
                assert expected.lower() in result.stderr.lower(), (failure, result.stderr)
            after = execute(["sh", "-c", "cat /home/target; readlink /home/.tmux.conf; stat -c %a /home/target; find /stage /home -name '*tmux-roam-sync*'"])
            assert after.returncode == 0 and after.stdout == b"before\ntarget\n640\n", (failure, after.stdout, after.stderr)
        finally:
            subprocess.run(["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL)
    print("Go config sync: actual noexec staging, ENOSPC during archive extraction and destination write; original content/symlink/mode preserved; temporary stages removed")


if __name__ == "__main__":
    main()
