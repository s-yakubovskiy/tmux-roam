#!/usr/bin/env python3
"""Exercise the same configuration cases through both public CLIs."""
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
CASES = {
    "missing": None,
    "valid": 'version = 1\n[picker]\nwidth = "85%"\n[discovery]\nconcurrency = 4\n[[hosts]]\nid = "dd"\nssh = "host-alias"\n',
    "unknown": 'extra = 1\n',
    "bad version": 'version = true\n',
    "boolean as integer": '[discovery]\nconcurrency = true\n',
    "integer as boolean": '[picker]\nicons = 1\n',
    "bad width": '[picker]\nwidth = "0%"\n',
    "duplicate key": '[picker]\nwidth = "85%"\nwidth = "90%"\n',
    "duplicate host": '[[hosts]]\nid = "dd"\nssh = "dd"\n[[hosts]]\nid = "dd"\nssh = "dd"\n',
    "socket conflict": '[[hosts]]\nid = "dd"\nssh = "dd"\nsocket_name = "one"\nsocket_path = "/tmp/two"\n',
    "bad host": '[[hosts]]\nid = "a/b"\nssh = "dd"\n',
    "bad prefix": '[[hosts]]\nid = "dd"\nssh = "dd"\nremote_prefix = "wrong"\n',
}


def main():
    with tempfile.TemporaryDirectory(prefix="tmux-roam-config-") as directory:
        path = Path(directory) / "config.toml"
        for name, content in CASES.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(content)
            env = {**os.environ, "TMUX_ROAM_CONFIG": str(path)}
            codes = []
            for exe in (ROOT / "bin/tmux-roam", ROOT / "build/tmux-roam-go"):
                result = subprocess.run([str(exe), "config", "check"], env=env, capture_output=True)
                codes.append(result.returncode)
            expected = 0 if name in ("missing", "valid") else 2
            assert codes == [expected, expected], (name, codes)
    print(f"config parity: {len(CASES)} CLI cases")


if __name__ == "__main__":
    main()
