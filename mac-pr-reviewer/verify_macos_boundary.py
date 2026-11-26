"""Verify OS restrictions with harmless canaries, outside a nested sandbox."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import review_boundary as boundary


def main():
    with tempfile.TemporaryDirectory() as area:
        base = Path(area).resolve()
        scratch = base / "review"
        scratch.mkdir()
        canary = base / "outside.txt"
        canary.write_text("test")
        python = Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python"
        profile = (boundary.sandbox_profile(scratch) +
                   "\n(allow process-exec (literal " + json.dumps(str(python)) + "))" +
                   "\n(allow file-read-data (subpath " + json.dumps(sys.base_prefix) + "))")
        # Only this diagnostic profile permits Python as the initial executable.
        program = """
import os, pathlib, subprocess, sys
p = pathlib.Path(sys.argv[1])
def fork():
    pid = os.fork()
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
checks = {"read outside scratch": p.read_text, "write outside scratch": lambda: p.write_text("test"),
          "fork": fork, "spawn true": lambda: subprocess.run(["/usr/bin/true"], check=True)}
for name, check in checks.items():
    try:
        check()
    except PermissionError:
        print(name, "denied")
    else:
        raise SystemExit(name + " unexpectedly allowed")
"""
        subprocess.run(["/usr/bin/sandbox-exec", "-p", profile, str(python), "-IBS", "-c",
                        program, str(canary)], cwd=scratch, check=True)
        assert canary.read_text() == "test"

if __name__ == "__main__":
    main()
