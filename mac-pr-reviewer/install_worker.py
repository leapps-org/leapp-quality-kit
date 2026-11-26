"""Install the worker files and one five-minute LaunchAgent per repository in mac_review_repos.

Default: print what would be written and change nothing.
--install writes the files and the LaunchAgent plists. It does not load them; run
`launchctl bootstrap gui/$(id -u) <plist>` for each one when you are ready.
"""
import argparse
import os
from pathlib import Path
import plistlib
import shutil
import sys
import tempfile

import review_boundary
import settings

FILES = ("settings.py", "review_boundary.py", "review_leapp_pr.py", "corpus_check.py",
         "mac_review_worker.py", "check_pr_test_data.py")
HERE = Path(__file__).resolve().parent


def bin_dir():
    return Path.home() / "Library/Application Support/LEAPP/PR Review Worker"


def agents_dir():
    return Path.home() / "Library/LaunchAgents"


def write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(data)
    os.replace(tmp.name, path)


def tool_paths():
    # The reviewer launches Codex by its absolute path (review_boundary.CODEX), so only gh
    # has to be on the job's PATH.
    if not review_boundary.CODEX.is_file():
        raise SystemExit(f"Missing Codex at {review_boundary.CODEX}")
    binary = shutil.which("gh")
    if not binary:
        raise SystemExit("Missing gh")
    return [str(Path(binary).parent)]


def plan(data, config_path, paths):
    """Every file the install writes, as (path, bytes). Pure: it reads only this folder."""
    files = [(bin_dir() / name, (HERE / name).read_bytes()) for name in FILES]
    files.append((bin_dir() / "kit_config.py", Path(settings.kit_config.__file__).read_bytes()))
    jobs = []
    for repo in settings.kit_config.need(data, "mac_review_repos"):
        state = settings.for_project(data, repo)["state"]
        label = "org." + repo.lower() + ".mac-review"
        log = state / "pickup.log"
        spec = {"Label": label, "RunAtLoad": True, "StartInterval": 300, "Umask": 0o077,
                "ProgramArguments": [sys.executable, "-B", "-E", "-s", str(bin_dir() / "mac_review_worker.py")],
                "WorkingDirectory": str(state), "ProcessType": "Background",
                "EnvironmentVariables": {"LEAPP_REVIEW_PROJECT": repo, "GH_PROMPT_DISABLED": "1",
                                         "LEAPP_QUALITY_CONFIG": str(config_path),
                                         "PATH": ":".join(dict.fromkeys(paths + ["/usr/bin", "/bin", "/usr/sbin"]))},
                "StandardErrorPath": str(log), "StandardOutPath": str(log)}
        files.append((agents_dir() / (label + ".plist"), plistlib.dumps(spec)))
        jobs.append((state, log))
    return files, jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="write the files (default: print only)")
    args = parser.parse_args()
    try:
        data = settings.kit_config.load()
        files, jobs = plan(data, settings.kit_config.path_of().resolve(), tool_paths())
    except settings.kit_config.ConfigError as error:
        raise SystemExit(str(error)) from None
    if not args.install:
        for path, _ in files:
            print("Would write", path)
        return
    os.umask(0o077)
    for state, log in jobs:
        state.mkdir(mode=0o700, parents=True, exist_ok=True)
        log.touch(mode=0o600, exist_ok=True)
    for path, content in files:
        write(path, content)
        print("Wrote", path)


if __name__ == "__main__":
    main()
