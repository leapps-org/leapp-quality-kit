"""Local report and contributor feedback for one configured LEAPP repository."""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import check_pr_test_data as fixtures
import review_boundary
import settings

# Filled by configure(). The worker's LaunchAgent sets LEAPP_REVIEW_PROJECT.
PROJECT = ""
OWNER_LOGIN = ""
OWNER = None  # the owner's immutable GitHub account ID
FULL_NAME = ""
REPO = ""
OWNER_REVIEWED = False
STATE = None
STORE = None
SETTINGS = {}


def configure(data, project):
    """Set this module's settings for one repository from a config dict."""
    global PROJECT, OWNER_LOGIN, OWNER, FULL_NAME, REPO, OWNER_REVIEWED, STATE, STORE, SETTINGS
    SETTINGS = settings.for_project(data, project)
    PROJECT = project
    OWNER_LOGIN = SETTINGS["owner"]
    OWNER = SETTINGS["owner_id"]
    FULL_NAME = SETTINGS["full_name"]
    REPO = f"repos/{FULL_NAME}"
    OWNER_REVIEWED = SETTINGS["owner_reviewed"]
    STATE = SETTINGS["state"]
    STORE = SETTINGS["store"]


if os.environ.get("LEAPP_REVIEW_PROJECT"):
    configure(settings.kit_config.load(), os.environ["LEAPP_REVIEW_PROJECT"])


def api(path, *, pages=False, body=None, raw=False):
    args = ["gh", "api", path]
    if pages:
        args.extend(["--paginate", "--slurp"])
    if raw:
        args.extend(["-H", "Accept: application/vnd.github.raw+json"])
    if body is not None:
        args.extend(["--method", "POST", "--input", "-"])
    response = subprocess.run(args, input=json.dumps(body) if body is not None else None,
                              capture_output=True, text=True, check=True, timeout=120).stdout
    if raw:
        return response
    data = json.loads(response)
    return [item for page in data for item in page] if pages else data


def revision(pr):
    return pr["head"]["sha"]


def route(pr):
    author = pr["user"]
    if not isinstance(author.get("id"), int) or not author.get("login"):
        raise ValueError("PR author could not be verified")
    if OWNER_REVIEWED and author["id"] == OWNER:
        return "private"
    if author["id"] == OWNER or author["login"].casefold() == OWNER_LOGIN.casefold():
        return "skip"
    return "contributor"


def get_pr(number):
    pr = api(f"{REPO}/pulls/{number}")
    if pr["base"]["repo"]["full_name"].casefold() != FULL_NAME.casefold():
        raise ValueError("Unexpected PR base")
    return pr


def context(number):
    pr = get_pr(number)
    info = {"number": number, "head": revision(pr), "delivery": route(pr)}
    if info["delivery"] == "skip":
        return info
    files = api(f"{REPO}/pulls/{number}/files?per_page=100", pages=True)
    if len(files) != pr["changed_files"]:
        raise ValueError("Incomplete changed-file list")
    modules = fixtures.artifact_modules(files)
    touched = fixtures.covered_modules(files, modules)
    refs = {}
    manifest = set()
    if modules.keys() - touched and "image_manifest.json" in {
            f["name"] for f in api(f"{REPO}/contents/admin?ref={pr['base']['sha']}")}:
        data = api(f"{REPO}/contents/admin/image_manifest.json?ref={pr['base']['sha']}", raw=True)
        for entry in json.loads(data)["images"]:
            manifest.update((entry.get("image_name"), entry.get("sample_data_key")))
    for module in modules.keys() - touched:
        path = quote(f"scripts/artifacts/{module}.py")
        data = api(f"{REPO}/contents/{path}?ref={revision(pr)}", raw=True)
        refs[module] = fixtures.sample_data_keys_from_source(data)
    maintainer, contributor = fixtures.classify(modules, touched, refs, manifest)
    if revision(get_pr(number)) != revision(pr):
        raise ValueError("PR changed while reading fixture context")
    return {**info,
            "test_files_touched": sorted(touched), "manifest_matches": maintainer,
            "fixture_bot_would_ask": contributor,
            "changed_paths": [f["filename"] for f in files]}


def database():
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    # STORE is STATE / its file name; joining here keeps a patched STATE self-contained.
    db = sqlite3.connect(STATE / STORE.name, timeout=60)
    db.execute("CREATE TABLE IF NOT EXISTS reviews "
               "(pr INTEGER, head TEXT, result TEXT, PRIMARY KEY (pr, head))")
    return db


def queue(db):
    prs = api(f"{REPO}/pulls?state=open&per_page=100", pages=True)
    return [{"number": p["number"], "head": revision(p), "delivery": audience}
            for p in prs if not p["draft"] and (audience := route(p)) != "skip" and not db.execute(
                "SELECT 1 FROM reviews WHERE pr=? AND head=?", (p["number"], revision(p))).fetchone()]


def checked_pr(number, head):
    pr = get_pr(number)
    if pr["state"] != "open" or pr["draft"] or revision(pr) != head:
        raise ValueError("PR closed, in draft, or superseded")
    return pr


def private_report(summary, comments):
    """Include the actual inline findings even if the model summary covers only CI."""
    if not comments:
        return summary
    lines = [summary.rstrip(), "", "## Findings posted on this revision", ""]
    for index, comment in enumerate(comments, 1):
        lines.append(f"{index}. `{comment['path']}:{comment['line']}`: {comment['body']}")
    return "\n".join(lines) + "\n"


def finish(db, number, head, summary, comments):
    if not summary.strip() or not isinstance(comments, list):
        raise ValueError("Expected a private summary and an inline comment list")
    with db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM reviews WHERE pr=? AND head=?", (number, head)).fetchone():
            return "Already delivered"
        audience = route(checked_pr(number, head))
        if audience == "skip":
            return "Skipped owner PR"
        if comments:
            files = api(f"{REPO}/pulls/{number}/files?per_page=100", pages=True)
            original = comments
            comments = review_boundary.public_findings(files, original)
            withheld = len(original) - len(comments)
            if withheld:
                summary += f"\n\nWithheld {withheld} inline finding(s) that could not be validated against the current diff."
        # Inline findings are public on both routes. The summary and corpus results stay local.
        if comments:
            endpoint = f"{REPO}/pulls/{number}/reviews"
            marker = f"<!-- leapp-fixture-review:{head} -->"
            reviewer = api("user")["id"]
            prior = api(endpoint + "?per_page=100", pages=True)
            delivered = any(r["user"]["id"] == reviewer and r.get("commit_id") == head
                            and marker in (r.get("body") or "") for r in prior)
            if not delivered:
                lines = []
                for c in comments:
                    if (not isinstance(c["line"], int) or c["line"] < 1 or
                            not c["path"].strip() or not c["body"].strip()):
                        raise ValueError("Expected a changed path, line and actionable finding")
                    lines.append({"path": c["path"], "line": c["line"],
                                  "side": "RIGHT", "body": review_boundary.inert_comment(c["body"])})
                if route(checked_pr(number, head)) != audience:
                    raise ValueError("Unexpected delivery route")
                api(endpoint, body={"commit_id": head, "event": "COMMENT", "comments": lines,
                                    "body": f"{marker}\nAutomated {PROJECT} review."})
        if audience == "private":
            checked_pr(number, head)
        summary = private_report(summary, comments)
        reports = STATE / "reports"
        reports.mkdir(mode=0o700, exist_ok=True)
        report = reports / f"PR-{number}-{head}.md"
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=reports,
                                         delete=False) as draft:
            try:
                draft.write(summary)
                draft.flush()
                os.replace(draft.name, report)
            finally:
                Path(draft.name).unlink(missing_ok=True)
        db.execute("INSERT INTO reviews VALUES (?, ?, ?)", (number, head, summary))
    return f"Recorded {audience} review"


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("queue")
    inspect = commands.add_parser("context")
    inspect.add_argument("number", type=int)
    record = commands.add_parser("finish")
    record.add_argument("number", type=int)
    record.add_argument("head")
    record.add_argument("summary", type=Path)
    record.add_argument("--public-comments", type=Path)
    args = parser.parse_args()
    if args.command == "context":
        print(json.dumps(context(args.number), indent=2))
        return
    db = database()
    try:
        if args.command == "queue":
            print(json.dumps(queue(db)))
        else:
            if not re.fullmatch("[0-9a-f]{40}", args.head):
                parser.error("expected a full commit SHA")
            comments = json.loads(args.public_comments.read_text()) if args.public_comments else []
            print(finish(db, args.number, args.head, args.summary.read_text(), comments))
    finally:
        db.close()


if __name__ == "__main__":
    main()
