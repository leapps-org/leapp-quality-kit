"""Local evidence checks. PR text selects a fixed predicate, never SQL or a command."""
import json
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

FIELDS = ("path", "assertion", "basename", "table", "column")
ASSERTIONS = ("file_present", "table_present", "column_present", "column_has_null")
SCHEMA = {"type": "object", "additionalProperties": False, "required": list(FIELDS),
          "properties": {key: {"type": "string"} for key in FIELDS}}
SCHEMA["properties"]["assertion"]["enum"] = list(ASSERTIONS)
MAX_DB = 64 * 1024 ** 2
MAX_ARCHIVES = 30

def archives(registry):
    """Registered ZIP and TAR sources, smallest first. Relative paths resolve under the
    registry's folder, and nothing outside that folder is read."""
    if not registry:
        raise ValueError("No registry configured")
    registry = Path(registry)
    root = registry.parent.resolve(strict=True)
    if not registry.resolve(strict=True).is_relative_to(root):
        raise ValueError("Registry outside corpus root")
    files = {}
    for entry in json.loads(registry.read_text())["samples"].values():
        rel = entry.get("match", {}).get("zip")
        if not isinstance(rel, str) or Path(rel).is_absolute():
            continue
        source = (root / rel).resolve()
        if not source.is_relative_to(root) or source.suffix.lower() not in (".zip", ".tar"):
            continue
        try:
            info = source.stat()
        except OSError:
            continue
        limit = 4 * 1024**3 if source.suffix.lower() == ".zip" else 256 * 1024**2
        if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= limit:
            files[source] = info.st_size
    return sorted(files, key=lambda p: (files[p], str(p)))[:MAX_ARCHIVES]

@contextmanager
def members(source):
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            items = [(i.filename, i.file_size, i) for i in archive.infolist()
                     if not i.is_dir() and not stat.S_ISLNK(i.external_attr >> 16)]
            yield items, archive.open
    else:
        with tarfile.open(source, "r:") as archive:
            items = [(i.name, i.size, i) for i in archive if i.isfile()]
            yield items, archive.extractfile

def sqlite_predicate(dbfile, check):
    with closing(sqlite3.connect(dbfile.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        cutoff = time.monotonic() + 2
        db.set_progress_handler(lambda: time.monotonic() > cutoff, 1000)
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA query_only=ON")
        table, column = check["table"], check["column"]
        # Views and virtual tables can execute schema-defined behavior.
        row = db.execute("SELECT sql FROM sqlite_schema WHERE name=? COLLATE NOCASE AND type='table'",
                         (table,)).fetchone()
        if not row:
            return False if check["assertion"] == "table_present" else None
        if not re.match(r"^CREATE\s+TABLE\b", row[0] or "", re.I):
            return None
        if check["assertion"] == "table_present":
            return True
        fields = {r[1].casefold() for r in db.execute("SELECT * FROM pragma_table_info(?)", (table,))}
        present = column.casefold() in fields
        if check["assertion"] == "column_present":
            return present
        if not present:
            return None
        # Both identifiers passed the ASCII identifier allowlist before this function.
        return bool(db.execute(f'SELECT 1 FROM "{table}" WHERE "{column}" IS NULL LIMIT 1').fetchone())

def observe(items, opener, index, check):
    name = index[0]
    if check["assertion"] == "file_present":
        return True
    with tempfile.TemporaryDirectory() as scratch:
        database = Path(scratch) / "evidence.sqlite"
        for suffix in ("", "-wal", "-shm", "-journal"):
            matches = [item for item in items if item[0] == name + suffix]
            if not matches:
                continue
            if len(matches) != 1 or matches[0][1] > MAX_DB:
                return None
            with opener(matches[0][2]) as stream, Path(str(database) + suffix).open("xb") as output:
                shutil.copyfileobj(stream, output, 256 * 1024)
        return sqlite_predicate(database, check)

def applicable(check, packet):
    if not isinstance(check, dict) or any(not isinstance(check.get(k), str) for k in FIELDS):
        return False
    path = check["path"]
    if path not in packet["context"]["changed_paths"] or not re.fullmatch(r"scripts/artifacts/[\w.-]+\.py", path):
        return False
    if check["assertion"] not in ASSERTIONS or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_. -]{0,100}", check["basename"]):
        return False
    chunks = packet["diff"].split("diff --git ")
    patch = next((c for c in chunks if c.startswith(f"a/{path} b/{path}\n")), "")
    terms = [check["basename"]]
    if check["assertion"] != "file_present":
        terms.append(check["table"])
    if check["assertion"] in ("column_present", "column_has_null"):
        terms.append(check["column"])
    if check["assertion"] == "file_present" and check["table"]:
        return False
    if check["assertion"] in ("file_present", "table_present") and check["column"]:
        return False
    return all(term in patch for term in terms) and all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,80}", term) for term in terms[1:])

def verify(registry, checks, packet):
    if not checks:
        return ""
    messages = ["", "", "Private corpus verification", ""]
    try:
        candidates = archives(registry)
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        return "\n".join(messages + ["Corpus registry unavailable. Verification remains open."])
    deadline = time.monotonic() + 90
    for check in checks[:4]:
        if not applicable(check, packet):
            messages.append("- Requested check requires manual verification.")
            continue
        yes = no = unknown = 0
        for source in candidates:
            if time.monotonic() >= deadline or yes + no >= 4:
                break
            try:
                with members(source) as (items, opener):
                    for item in items:
                        if PurePosixPath(item[0]).name != check["basename"]:
                            continue
                        result = observe(items, opener, item, check)
                        yes += result is True
                        no += result is False
                        unknown += result is None
                        if yes + no >= 4 or time.monotonic() >= deadline:
                            break
            except (OSError, ValueError, sqlite3.Error, zipfile.BadZipFile, tarfile.TarError,
                    EOFError, RuntimeError, NotImplementedError):
                unknown += 1
        predicate = " / ".join(check[k] for k in ("assertion","basename","table","column") if check[k])
        messages.append(f"- {predicate}: {yes} true, {no} false on inspected matching files. "
                        f"{unknown} inconclusive reads." if yes + no else
                        f"- {predicate}: inconclusive in the bounded registered sample search.")
    if len(checks) > 4:
        messages.append("- Additional requested checks remain open.")
    messages.append("Only the listed source predicates were checked. Parser output and event meaning require separate verification.")
    return "\n".join(messages)


def run_checks(registry, checks, packet):
    if not checks:
        return ""
    # A slow disk, corrupt archive, or unsupported database cannot prevent delivery.
    # The child runs isolated (-I), so it gets the registry path here, not from the config.
    payload = {"registry": registry, "checks": checks, "packet": {"diff": packet["diff"],
               "context": {"changed_paths": packet["context"]["changed_paths"]}}}
    try:
        child = subprocess.run([sys.executable, "-IB", str(Path(__file__).resolve())],
                               input=json.dumps(payload), text=True, capture_output=True,
                               timeout=120, check=True, env={"PATH": "/usr/bin:/bin"})
        return child.stdout
    except (OSError, subprocess.SubprocessError):
        return "\n\nPrivate corpus verification was inconclusive. The local check did not finish."


if __name__ == "__main__":
    request = json.load(sys.stdin)
    sys.stdout.write(verify(request["registry"], request["checks"], request["packet"]))
