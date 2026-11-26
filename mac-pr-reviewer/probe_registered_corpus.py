"""Manual probe: run one file-presence check against a repository's real registry.

Usage: python3 -B probe_registered_corpus.py <repo>. Prints only PASS or an error, never archive
names, member names or values.
"""
import re
import sys
from pathlib import PurePosixPath

import corpus_check as corpus
import settings

def main(project):
    registry = settings.for_project(settings.kit_config.load(), project)["registry"]
    # Use a synthetic diff for intake and real sources for file lookup. Keep
    # all archive names and entry names out of the exported test result.
    parser = "scripts/artifacts/test_module.py"
    for archive in corpus.archives(registry):
        try:
            with corpus.members(archive) as (items, _):
                names = (PurePosixPath(item[0]).name for item in items if item[1])
                name = next((n for n in names if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_. -]{0,100}", n)), None)
        except (OSError, ValueError):
            continue
        if not name:
            continue
        check = dict(path=parser, assertion="file_present", basename=name, table="", column="")
        packet = {"context":{"changed_paths":[parser]}, "diff":f"diff --git a/{parser} b/{parser}\n+{name}"}
        result = corpus.run_checks(registry, [check], packet)
        if not re.search(r"[1-4] true",result):
            raise RuntimeError(f"{project} private corpus probe failed")
        print(project, "PASS: observed the requested member in a registered source archive")
        return
    raise RuntimeError(f"{project} has no supported test input")

if __name__ == "__main__":
    main(sys.argv[1])
