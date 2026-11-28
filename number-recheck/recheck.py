#!/usr/bin/env python3
"""Re-check every sample_data row count a LEAPP core declares, against main.

This is a thin wrapper over the core's own admin/scripts/validate_sample_data.py --run KEY,
which parses one corpus key end to end, refuses a count from a run that did not finish or
that caught a database error, and compares each artifact's LAVA row count with its
sample_data. This script adds what that check does not do on its own:

- a clean detached worktree of origin/main, so the counts describe main and not a checkout
  someone is editing;
- every key the core cites, each resolved to the samples.json that registers it;
- several keys at once, each with its own HOME, temp folder and log;
- one report grouped by module, with every key sorted into matched, drifted or could not
  check. A key that could not be checked is never counted as a pass.

It never edits a module and, by default, never files an issue.

Usage:
    recheck.py --core ALEAPP [--keys k1 k2] [--jobs 6] [--out DIR]
"""
import argparse
import ast
import concurrent.futures
import datetime
import json
import os
import re

import site
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kit_config  # noqa: E402

# Set from the kit's config file in main(): where the core checkouts are, and the corpus roots catalog.
GITHUB = None
CORPUS_ROOTS = None
MISMATCH_RE = re.compile(r"ERROR\s+(?P<module>[^:\s]+):(?P<artifact>\S+) declares (?P<declared>\d+) "
                         r"row\(s\) for '(?P<key>[^']+)' but the run's LAVA output holds (?P<actual>\d+)")
SUMMARY_RE = re.compile(r"(\d+) error\(s\), (\d+) warning\(s\)")


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False, **kw)


def registries():
    """Every samples.json the corpus roots name, in a fixed order."""
    roots = json.loads(CORPUS_ROOTS.read_text(encoding="utf-8"))
    found = []
    for root in roots["roots"]:
        base = Path(root["path"])
        if "registries" in root:
            found += [base / r for r in root["registries"]]
        else:
            skip = set(root.get("exclude_top_level", []))
            found += [p for p in sorted(base.glob(root.get("registry_pattern", "*/samples.json")))
                      if p.parent.name not in skip]
    return [p for p in found if p.is_file()]


def target(reg, entry):
    """The file an entry points at, resolved, so two registries naming one file agree."""
    rel = (entry.get("match") or {}).get("zip")
    return (reg.parent / rel).resolve() if rel else None


def key_registry():
    """Corpus key to the registry that holds it. A key in two registries keeps the first."""
    out, entries, dupes = {}, {}, []
    for reg in registries():
        for key, entry in json.loads(reg.read_text(encoding="utf-8")).get("samples", {}).items():
            if key in out:
                # A unified registry repeats its collections' entries; only a different entry matters.
                if target(reg, entry) != target(out[key], entries[key]):
                    dupes.append((key, str(reg)))
            else:
                out[key], entries[key] = reg, entry
    return out, dupes


def cited_keys(tree):
    """Every corpus key the core's artifacts cite in sample_data, with how many artifacts cite it."""
    counts = {}
    for path in sorted((tree / "scripts" / "artifacts").glob("*.py")):
        try:
            module = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in module.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "__artifacts_v2__":
                try:
                    block = ast.literal_eval(node.value)
                except ValueError:
                    continue
                for info in block.values():
                    for key in (info.get("sample_data") or {}) if isinstance(info, dict) else ():
                        counts[key] = counts.get(key, 0) + 1
    return counts


# A count measured with an input the unattended run does not have: "with the recovery key", or an
# encrypted iTunes backup read with its password.
NEEDS_INPUT_RE = re.compile(r"\bwith (?:the|a|an) [^|]*\bkey\b|\bencrypted iTunes backup\b", re.I)


def conditional_counts(tree):
    """(module file, artifact key, corpus key) for every sample_data value declared for a run with an
    input the unattended run does not have, such as "with the recovery key"."""
    out = set()
    for path in sorted((tree / "scripts" / "artifacts").glob("*.py")):
        try:
            module = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in module.body:
            if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "__artifacts_v2__":
                try:
                    block = ast.literal_eval(node.value)
                except ValueError:
                    continue
                for art, info in block.items():
                    for key, value in ((info.get("sample_data") or {}) if isinstance(info, dict) else {}).items():
                        if NEEDS_INPUT_RE.search(str(value)):
                            out.add((path.name, art, key))
    return out


def check_key(tree, key, registry, workdir, userbase):
    """Run the core's own validator on one key. Returns a result dict."""
    keydir = workdir / "keys" / key
    keydir.mkdir(parents=True, exist_ok=True)
    (keydir / "home").mkdir(exist_ok=True)
    (keydir / "tmp").mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(keydir / "home"), PYTHONUSERBASE=userbase,
               TMPDIR=str(keydir / "tmp"), PYTHONDONTWRITEBYTECODE="1")
    started = datetime.datetime.now(datetime.timezone.utc)
    done = run([sys.executable, "admin/scripts/validate_sample_data.py", "--registry", str(registry),
                "--run", key], cwd=str(tree), env=env)
    seconds = round((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds())
    text = (done.stdout or "") + (done.stderr or "")
    (keydir / "validate.log").write_text(text, encoding="utf-8")
    conditional = conditional_counts(tree)
    found = [m.groupdict() for m in MISMATCH_RE.finditer(text)]
    needs_input = [d for d in found if (d["module"], d["artifact"], key) in conditional]
    drifts = [d for d in found if (d["module"], d["artifact"], key) not in conditional]
    run_lines = [line.strip() for line in text.splitlines()
                 if "ERROR" in line and f"--run '{key}'" in line and not MISMATCH_RE.search(line)]
    other_errors = [line.strip() for line in text.splitlines()
                    if "ERROR" in line and not MISMATCH_RE.search(line) and f"--run '{key}'" not in line]
    compared = re.search(rf"--run '{re.escape(key)}': compared (\d+) artifact", text)
    summary = SUMMARY_RE.search(text)
    if run_lines or compared is None or summary is None:
        status = "could not check"
    elif drifts:
        status = "drifted"
    else:
        status = "matched"
    return {"key": key, "registry": str(registry), "status": status, "seconds": seconds,
            "exit": done.returncode, "compared": int(compared.group(1)) if compared else None,
            "drifts": drifts, "needs_input": needs_input, "run_errors": run_lines[:25], "other_errors": other_errors[:25],
            "log": str(keydir / "validate.log")}


def write_report(out, core, commit, results, missing, dupes, structure_errors):
    by_status = {s: [r for r in results if r["status"] == s] for s in ("matched", "drifted", "could not check")}
    modules = {}
    for r in by_status["drifted"]:
        for d in r["drifts"]:
            modules.setdefault(d["module"], []).append(d)
    lines = [f"# Number re-check, {core} at {commit}", "",
             f"Keys: {len(results)} run, {len(by_status['matched'])} matched, "
             f"{len(by_status['drifted'])} drifted, {len(by_status['could not check'])} could not check, "
             f"{len(missing)} not in any registry.",
             f"Drifted rows: {sum(len(v) for v in modules.values())} in {len(modules)} module(s).", ""]
    if modules:
        lines += ["## Drift by module", ""]
        for module in sorted(modules):
            lines.append(f"### {module}")
            for d in sorted(modules[module], key=lambda x: (x["artifact"], x["key"])):
                lines.append(f"- {d['artifact']} on {d['key']}: declares {d['declared']}, main gives {d['actual']}")
            lines.append("")
    needs = [(r["key"], d) for r in results for d in r.get("needs_input", [])]
    if needs:
        lines += ["## Counts declared for an input the run did not have (not checked)", ""]
        for key, d in sorted(needs, key=lambda x: (x[1]["module"], x[1]["artifact"], x[0])):
            lines.append(f"- {d['module']} {d['artifact']} on {key}: declares {d['declared']}, run gave {d['actual']}")
        lines.append("")
    unconfirmed = [(r["key"], d) for r in by_status["could not check"] for d in r["drifts"]]
    if unconfirmed:
        lines += ["## Drift on keys whose run was not trusted (unconfirmed)", ""]
        for key, d in sorted(unconfirmed, key=lambda x: (x[1]["module"], x[1]["artifact"], x[0])):
            lines.append(f"- {d['module']} {d['artifact']} on {key}: declares {d['declared']}, main gives {d['actual']}")
        lines.append("")
    if by_status["could not check"]:
        lines += ["## Could not check", ""]
        for r in sorted(by_status["could not check"], key=lambda x: x["key"]):
            reason = (r["run_errors"] or [f"exit {r['exit']}, no comparison line"])[0]
            lines.append(f"- {r['key']}: {reason[:300]}")
        lines.append("")
    if missing:
        lines += ["## Cited but not in any registry", ""] + [f"- {k}" for k in sorted(missing)] + [""]
    if dupes:
        lines += ["## Keys in more than one registry (first one used)", ""] + \
                 [f"- {k}: also in {r}" for k, r in dupes] + [""]
    if structure_errors:
        lines += ["## Validator errors not tied to a key", ""] + [f"- {e}" for e in structure_errors] + [""]
    lines += ["## Run times", "", f"Total key-seconds: {sum(r['seconds'] for r in results)}, "
              f"longest: {max((r['seconds'] for r in results), default=0)} s", ""]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "results.json").write_text(json.dumps({"core": core, "commit": commit, "results": results,
                                                  "missing": sorted(missing), "dupes": dupes},
                                                 indent=1), encoding="utf-8")


MARKER = "<!-- number-recheck module={module} -->"


def file_issues(owner, core, commit, results, dry_run):
    """One audit-queue issue per module with trusted drift, unless an open one already carries
    its marker. Drift on untrusted keys is listed in the issue only next to trusted drift."""
    repo = f"{owner}/{core}"
    trusted, unconfirmed = {}, {}
    for r in results:
        bucket = trusted if r["status"] == "drifted" else unconfirmed if r["status"] == "could not check" else None
        if bucket is None:
            continue
        for d in r["drifts"]:
            bucket.setdefault(d["module"], []).append(d)
    filed = []
    for module in sorted(trusted):
        marker = MARKER.format(module=module)
        found = run(["gh", "issue", "list", "--repo", repo, "--state", "open", "--label", "audit-queue",
                     "--search", f'"number-recheck module={module}" in:body', "--json", "number,body"])
        if found.returncode != 0:
            print(f"  could not search issues for {module}: {found.stderr.strip()[:200]}")
            continue
        if any(marker in (i.get("body") or "") for i in json.loads(found.stdout or "[]")):
            print(f"  {module}: open issue already exists, not filed")
            continue
        lines = [f"Found by the number re-check on main at {commit}.", ""]
        for d in sorted(trusted[module], key=lambda x: (x["artifact"], x["key"])):
            lines.append(f"- {d['artifact']} on {d['key']}: declares {d['declared']}, main gives {d['actual']}")
        extra = unconfirmed.get(module, [])
        if extra:
            lines += ["", "On keys whose run was not trusted (unconfirmed):", ""]
            for d in sorted(extra, key=lambda x: (x["artifact"], x["key"])):
                lines.append(f"- {d['artifact']} on {d['key']}: declares {d['declared']}, main gives {d['actual']}")
        lines += ["", "Find the cause first: a deliberate change that left sample_data behind (update the counts "
                  "and any notes that quote them), or a regression (fix the code).", "", marker]
        title = f"{module.removesuffix('.py')}: sample_data counts differ from main (number re-check)"
        if dry_run:
            print(f"  would file: {title}")
            continue
        made = run(["gh", "issue", "create", "--repo", repo, "--label", "audit-queue", "--title", title,
                    "--body", "\n".join(lines)])
        print(f"  {module}: {made.stdout.strip() or made.stderr.strip()[:200]}")
        filed.append(made.stdout.strip())
    return filed


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--core", required=True, help="ALEAPP, iLEAPP, RLEAPP, VLEAPP or DLEAPP")
    ap.add_argument("--keys", nargs="*", help="only these keys")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--out", help="output folder (default <work_dir>/number-recheck/<core> <stamp>)")
    ap.add_argument("--file-issues", action="store_true", help="file audit-queue issues for trusted drift")
    ap.add_argument("--dry-run", action="store_true", help="with --file-issues, print what would be filed")
    args = ap.parse_args()

    global GITHUB, CORPUS_ROOTS
    try:
        config = kit_config.load()
        GITHUB = kit_config.expand(kit_config.need(config, "repos_dir"))
        CORPUS_ROOTS = kit_config.expand(kit_config.need(config, "corpus_roots"))
        work_dir = kit_config.expand(kit_config.need(config, "work_dir"))
        owner = None
        if args.file_issues:
            # The same owner lookup the review gate uses: github_owner unless repo_owners names another.
            owners = config.get("repo_owners", {})
            if not isinstance(owners, dict):
                raise kit_config.ConfigError(f'"repo_owners" must map repo names to GitHub logins in {kit_config.path_of()}')
            owner = {str(k).lower(): v for k, v in owners.items()}.get(args.core.lower()) or kit_config.need(config, "github_owner")
    except kit_config.ConfigError as error:
        print(f"COULD NOT CHECK: {error}", file=sys.stderr)
        return 2

    if not CORPUS_ROOTS.is_file():
        print(f"COULD NOT CHECK: {CORPUS_ROOTS} is not there (corpus drive not mounted?)", file=sys.stderr)
        return 2
    repo = GITHUB / args.core
    if not (repo / ".git").exists():
        print(f"no repo at {repo}", file=sys.stderr)
        return 2
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H%M")
    out = Path(args.out) if args.out else work_dir / "number-recheck" / f"{args.core} {stamp}"
    if out.exists():
        print(f"{out} exists", file=sys.stderr)
        return 2
    out.mkdir(parents=True)

    if run(["git", "-C", str(repo), "fetch", "-q", "origin"]).returncode != 0:
        print("git fetch failed", file=sys.stderr)
        return 2
    commit = run(["git", "-C", str(repo), "rev-parse", "--short", "origin/main"]).stdout.strip()
    tree = out / "tree"
    added = run(["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(tree), "origin/main"])
    if added.returncode != 0:
        print(added.stderr, file=sys.stderr)
        return 2
    try:
        userbase = site.getuserbase()
        reg_of, dupes = key_registry()
        cited = cited_keys(tree)
        keys = sorted(args.keys) if args.keys else sorted(cited)
        missing = [k for k in keys if k not in reg_of]
        runnable = [k for k in keys if k in reg_of]
        print(f"{args.core} at {commit}: {len(runnable)} key(s) to run, {len(missing)} not registered, "
              f"jobs {args.jobs}", flush=True)
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(check_key, tree, k, reg_of[k], out, userbase): k for k in runnable}
            for f in concurrent.futures.as_completed(futures):
                r = f.result()
                results.append(r)
                print(f"  {r['key']}: {r['status']} ({len(r['drifts'])} drift, {r['seconds']} s)", flush=True)
        structure = sorted({e for r in results for e in r["other_errors"]})
        write_report(out, args.core, commit, results, missing, dupes, structure)
        print(f"report: {out / 'report.md'}")
        if args.file_issues:
            file_issues(owner, args.core, commit, results, args.dry_run)
    finally:
        # The validator deletes its own output folders; what stays in each key's tmp is the
        # tool's run log, which explains a zero, so it is kept.
        run(["git", "-C", str(repo), "worktree", "remove", "--force", str(tree)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
