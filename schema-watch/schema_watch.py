#!/usr/bin/env python3
"""Schema watch: compare the stores in a new corpus key with what each artifact was built on.

A key is new when the state file has not seen it, or has seen it with a different sha256. For a
new key, every store an artifact's `paths` match is read on a temp copy (SQLite with its -wal,
-shm and -journal, Android shared_prefs XML, plists), and its table, column and preference names
are compared with the same store in the keys that module cites in `sample_data`. That union is
what the module was built on. A name that left and that also appears in the module's source is
marked "named in the module" (a source-token match, not a trace of what the code reads).

It only measures and files. It never edits code and uses no model.

Usage:
    schema_watch.py --init                     record every registered key as seen, compare nothing
    schema_watch.py [--file-issues [--dry-run]] [--core ALEAPP] [--jobs 3]
    schema_watch.py --keys k1 k2 [--core ALEAPP]  treat these keys as new (state left alone)
"""
import argparse
import ast
import concurrent.futures
import datetime
import fnmatch
import hashlib
import io
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kit_config  # noqa: E402

SIDECARS = ('-wal', '-shm', '-journal')
SQLITE_MAGIC = b'SQLite format 3\x00'
INDEX_VERSION = 1
# SQLite's own tables and Android's locale table come and go with use or framework, not with the app.
INTERNAL = re.compile(r'^(sqlite_|android_metadata$)', re.I)
# A table named with a per-install hex id (ExoPlayer: "ExoPlayerCacheIndex" + hex uid) is the same table
# on every device, so the id is replaced before comparing.
HEX_SUFFIX = re.compile(r'(?<=[A-Za-z_])[0-9a-f]{8,16}$')
CORES = ('ALEAPP', 'iLEAPP', 'RLEAPP', 'VLEAPP', 'DLEAPP')


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, check=False, **kw)


# ---------------------------------------------------------------- registries

def registries(roots_file):
    """Every samples.json the corpus roots catalog names, in a fixed order."""
    roots = json.loads(Path(roots_file).read_text(encoding='utf-8'))
    found = []
    for root in roots['roots']:
        base = Path(root['path'])
        if 'registries' in root:
            found += [base / r for r in root['registries']]
        else:
            skip = set(root.get('exclude_top_level', []))
            found += [p for p in sorted(base.glob(root.get('registry_pattern', '*/samples.json')))
                      if p.parent.name not in skip]
    return [p for p in found if p.is_file()]


def registered_keys(roots_file):
    """key -> {"registry", "file", "sha256", "public"}. A key in two registries keeps the first."""
    out = {}
    for reg in registries(roots_file):
        for key, entry in json.loads(reg.read_text(encoding='utf-8')).get('samples', {}).items():
            if key in out or not isinstance(entry, dict):
                continue
            match = entry.get('match') or {}
            rel = match.get('zip') or entry.get('extraction_directory')
            out[key] = {'registry': str(reg.resolve()), 'file': str((reg.parent / rel).resolve()) if rel else None,
                        'sha256': match.get('sha256') or '',
                        # Fail closed: only an explicit true lets a key be named in a public issue.
                        'public': entry.get('allow_public_metadata') is True}
    return out


# ---------------------------------------------------------------- modules

def core_modules(repo):
    """{module file name: {"patterns": [...], "keys": [...], "tokens": set()}} from origin/main."""
    done = run(['git', '-C', str(repo), 'archive', '--format=tar', 'origin/main', 'scripts/artifacts'])
    if done.returncode != 0:
        raise RuntimeError(f'git archive failed in {repo}: {done.stderr.decode(errors="replace")[:200]}')
    commit = run(['git', '-C', str(repo), 'rev-parse', '--short', 'origin/main']).stdout.decode().strip()
    modules = {}
    with tarfile.open(fileobj=io.BytesIO(done.stdout)) as tf:
        for member in sorted(tf.getmembers(), key=lambda m: m.name):
            if not (member.isfile() and member.name.endswith('.py')) or member.name.count('/') != 2:
                continue
            info = parse_module(tf.extractfile(member).read().decode('utf-8', errors='replace'))
            if info:
                modules[Path(member.name).name] = info
    return commit, modules


def parse_module(source):
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    block = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == '__artifacts_v2__':
            try:
                block = ast.literal_eval(node.value)
            except ValueError:
                return None
    if not isinstance(block, dict):
        return None
    patterns, keys = [], []
    for info in block.values():
        if not isinstance(info, dict):
            continue
        paths = info.get('paths') or ()
        for p in ([paths] if isinstance(paths, str) else paths):
            if isinstance(p, str) and p not in patterns:
                patterns.append(p)
        for k in (info.get('sample_data') or {}):
            if k not in keys:
                keys.append(k)
    tokens = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            tokens.update(t.lower() for t in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', node.value))
            # Whole strings too, so a preference name such as "flutter.last_location" can be found.
            if len(node.value) <= 200:
                tokens.add(node.value.strip().lower())
    return {'patterns': patterns, 'keys': keys, 'tokens': tokens}


# ---------------------------------------------------------------- matching (as the LEAPP seekers do)

def required_literal(pattern):
    """A substring every match must contain (the seekers' own prefilter), or ''."""
    if '[' in pattern or ']' in pattern:
        return ''
    return max(pattern.replace('?', '*').split('*'), key=len)


def matcher(pattern):
    """The seekers match os.path.normcase(pattern) against normcase("root/" + member name)."""
    norm = os.path.normcase(pattern)
    regex = re.compile(fnmatch.translate(norm)).match
    literal = required_literal(norm)
    return lambda name: (literal in name) and regex(name) is not None


def match_names(patterns, names):
    """{pattern: [member names]} using the seekers' rule."""
    rooted = [(n, os.path.normcase('root/' + n)) for n in names]
    out = {}
    for p in patterns:
        test = matcher(p)
        hits = [n for n, r in rooted if test(r)]
        if hits:
            out[p] = hits
    return out


def store_id(pattern, name):
    """A name for a store that is the same across extractions with different roots (and users)."""
    parts = [c for c in pattern.split('/') if c]
    while parts and set(parts[0]) <= set('*'):
        parts.pop(0)
    segs = name.strip('/').split('/')
    anchor = parts[0] if parts and not re.search(r'[*?\[]', parts[0]) else None
    if anchor and anchor in segs:
        return '/'.join(segs[segs.index(anchor):])
    return '/'.join(segs[-max(len(parts), 1):])


# ---------------------------------------------------------------- reading sources

class Source:
    """A corpus file opened for listing and for copying members out. Never written to."""

    def __init__(self, path):
        self.path = Path(path)
        self.kind = None
        if self.path.is_dir():
            self.kind = 'dir'
        elif zipfile.is_zipfile(self.path):
            self.kind = 'zip'
        else:
            try:
                if tarfile.is_tarfile(self.path):
                    self.kind = 'tar'
            except OSError:
                pass
        if self.kind is None:
            raise ValueError(f'not a folder, zip or tar: {self.path.suffix or "no extension"}')

    def names(self):
        if self.kind == 'dir':
            out = []
            for top, _dirs, files in os.walk(self.path):
                for f in files:
                    out.append(os.path.relpath(os.path.join(top, f), self.path).replace(os.sep, '/'))
            return sorted(out)
        if self.kind == 'zip':
            with zipfile.ZipFile(self.path) as zf:
                return sorted({i.filename for i in zf.infolist() if not i.is_dir()
                               and not i.filename.startswith('__MACOSX')})
        with tarfile.open(self.path, 'r:*') as tf:
            return sorted({m.name for m in tf if m.isfile()})

    def copy_out(self, wanted, dest):
        """Copy the named members into dest, keeping relative paths. Returns {name: local path}.
        For a tar with a name stored twice, the last entry wins, as tar itself extracts."""
        dest = Path(dest)
        wanted = set(wanted)
        got = {}

        def target(name):
            rel = name.lstrip('/')
            if '..' in Path(rel).parts:
                return None
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            return out

        if self.kind == 'dir':
            for name in wanted:
                out = target(name)
                if out:
                    shutil.copyfile(self.path / name, out)
                    got[name] = out
        elif self.kind == 'zip':
            with zipfile.ZipFile(self.path) as zf:
                for name in wanted:
                    out = target(name)
                    if out:
                        with zf.open(name) as src, open(out, 'wb') as dst:
                            shutil.copyfileobj(src, dst)
                        got[name] = out
        else:
            # One forward pass, so a compressed tar is decompressed once.
            with tarfile.open(self.path, 'r|*') as tf:
                for m in tf:
                    if m.isfile() and m.name in wanted:
                        out = target(m.name)
                        if out:
                            with tf.extractfile(m) as src, open(out, 'wb') as dst:
                                shutil.copyfileobj(src, dst)
                            got[m.name] = out
        return got


def read_store(local, name):
    """Names only: {"kind": "sqlite", "tables": {table: [columns]}}, {"kind": "prefs"|"plist", "names": [...]},
    {"kind": "other"} or {"kind": "unreadable", "error": "..."}."""
    try:
        with open(local, 'rb') as f:
            head = f.read(16)
    except OSError as error:
        return {'kind': 'unreadable', 'error': type(error).__name__}
    if head == SQLITE_MAGIC:
        uri = Path(local).resolve().as_uri() + '?mode=ro'
        try:
            con = sqlite3.connect(uri, uri=True)
            try:
                tables = {}
                rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                for (table,) in rows:
                    cols = con.execute('SELECT name FROM pragma_table_xinfo(?)', (table,)).fetchall()
                    tables[table] = sorted(c[0] for c in cols)
            finally:
                con.close()
            return {'kind': 'sqlite', 'tables': tables}
        except sqlite3.DatabaseError as error:
            return {'kind': 'unreadable', 'error': f'sqlite: {str(error)[:80]}'}
    lower = name.lower()
    if head.startswith(b'bplist') or lower.endswith('.plist'):
        try:
            with open(local, 'rb') as f:
                data = plistlib.load(f)
            return {'kind': 'plist', 'names': sorted(map(str, data)) if isinstance(data, dict) else []}
        except Exception as error:  # plistlib raises several unrelated types on bad input
            return {'kind': 'unreadable', 'error': f'plist: {type(error).__name__}'}
    if '/shared_prefs/' in '/' + lower and lower.endswith('.xml'):
        try:
            root = ET.parse(local).getroot()
            return {'kind': 'prefs', 'names': sorted({c.get('name') for c in root if c.get('name') is not None})}
        except ET.ParseError:
            return {'kind': 'unreadable', 'error': 'xml: parse error'}
    return {'kind': 'other'}


# ---------------------------------------------------------------- per-key index (cached)

def index_key(key, entry, patterns, cache_dir, tmp_root):
    """The names of every store the patterns match in one key, cached by the registry's sha256.
    Only patterns not yet in the cache are matched and only their new members are copied."""
    cache = Path(cache_dir) / f'{key}.json'
    data = None
    if cache.is_file():
        data = json.loads(cache.read_text(encoding='utf-8'))
        if data.get('sha256') != entry['sha256'] or data.get('version') != INDEX_VERSION:
            data = None
    if not entry.get('file'):
        raise ValueError('registry entry names no file')
    source = Source(entry['file'])
    if data is None:
        data = {'version': INDEX_VERSION, 'sha256': entry['sha256'], 'names': source.names(),
                'matches': {}, 'stores': {}}
    todo = [p for p in patterns if p not in data['matches']]
    if todo:
        found = match_names(todo, data['names'])
        for p in todo:
            data['matches'][p] = found.get(p, [])
        names = set(data['names'])
        need = sorted({n for hits in found.values() for n in hits
                       if not n.endswith(SIDECARS) and n not in data['stores']})
        if need:
            copy = set(need) | {n + s for n in need for s in SIDECARS if n + s in names}
            Path(tmp_root).mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
                local = source.copy_out(copy, tmp)
                for n in need:
                    data['stores'][n] = read_store(local[n], n) if n in local else \
                        {'kind': 'unreadable', 'error': 'not copied'}
        cache.parent.mkdir(parents=True, exist_ok=True)
        part = cache.with_suffix('.part')
        part.write_text(json.dumps(data), encoding='utf-8')
        part.replace(cache)
    return data


def module_stores(module, index):
    """{store id: store} for one module in one key's index. Several members with one id are merged."""
    out = {}
    for p in module['patterns']:
        for n in index['matches'].get(p, []):
            if n.endswith(SIDECARS):
                continue
            store = index['stores'].get(n)
            if store:
                if store['kind'] == 'sqlite':
                    store = {'kind': 'sqlite', 'tables': {HEX_SUFFIX.sub('<hex>', t): c for t, c in store['tables'].items()
                                                        if not INTERNAL.match(t)}}
                merge(out, store_id(p, n), store)
    return out


def merge(out, sid, store):
    have = out.get(sid)
    if have is None:
        out[sid] = json.loads(json.dumps(store))
        return
    if have['kind'] != store['kind']:
        if have['kind'] in ('other', 'unreadable'):
            out[sid] = json.loads(json.dumps(store))
        return
    if store['kind'] == 'sqlite':
        for t, cols in store['tables'].items():
            have['tables'][t] = sorted(set(have['tables'].get(t, [])) | set(cols))
    elif store['kind'] in ('prefs', 'plist'):
        have['names'] = sorted(set(have['names']) | set(store['names']))


# ---------------------------------------------------------------- comparison

def intersect(stores):
    """The names every one of these stores (one per cited key, same kind) has."""
    first = stores[0]
    if first['kind'] == 'sqlite':
        tables = set(first['tables']).intersection(*(s['tables'] for s in stores[1:]))
        return {'kind': 'sqlite', 'tables': {t: sorted(set(first['tables'][t]).intersection(
            *(s['tables'][t] for s in stores[1:]))) for t in tables}}
    return {'kind': first['kind'], 'names': sorted(set(first['names']).intersection(*(s['names'] for s in stores[1:])))}


def named(name, tokens):
    """True when the name, or every word of it (for a name such as "flutter.last_location"), is a
    word in the module's string constants. A source-token match, not a trace of what the code reads."""
    words = [w.lower() for w in re.findall(r'[A-Za-z_][A-Za-z0-9_]*', name)]
    return name.lower() in tokens or bool(words) and all(w in tokens for w in words)


def compare(module, new_stores, baselines):
    """Differences between a new key's stores and the module's baseline.
    baselines: {cited key: {store id: store}}. A name is "added" when no cited key's store has it
    (union), and "gone" only when every cited key that has the store has it (intersection), so a
    column one vendor adds is not reported as missing on every other vendor's key.
    Returns a dict, empty when nothing changed."""
    base, every = {}, {}
    for stores in baselines.values():
        for sid, store in stores.items():
            merge(base, sid, store)
            if store['kind'] not in ('other', 'unreadable'):
                every.setdefault(sid, []).append(store)
    tokens = module['tokens']
    out = {'stores': [], 'not_compared': [], 'no_baseline': []}
    for sid in sorted(new_stores):
        new = new_stores[sid]
        if new['kind'] in ('other', 'unreadable'):
            if new['kind'] == 'unreadable':
                out['not_compared'].append({'store': sid, 'reason': new.get('error', '')})
            continue
        old = base.get(sid)
        if old is None or old['kind'] in ('other', 'unreadable'):
            size = len(new.get('tables', new.get('names', [])))
            out['no_baseline'].append({'store': sid, 'kind': new['kind'], 'count': size})
            continue
        if old['kind'] != new['kind']:
            out['not_compared'].append({'store': sid, 'reason': f"was {old['kind']}, now {new['kind']}"})
            continue
        same = [s for s in every.get(sid, []) if s['kind'] == new['kind']]
        common = intersect(same) if same else old
        change = {'store': sid, 'kind': new['kind']}
        if new['kind'] == 'sqlite':
            o, n, c = old['tables'], new['tables'], common['tables']
            change['tables_added'] = sorted(set(n) - set(o))
            change['tables_gone'] = [{'name': t, 'named_in_module': named(t, tokens)} for t in sorted(set(c) - set(n))]
            cols_added, cols_gone = {}, {}
            for t in sorted(set(o) & set(n)):
                a = sorted(set(n[t]) - set(o[t]))
                g = sorted(set(c.get(t, [])) - set(n[t]))
                if a:
                    cols_added[t] = a
                if g:
                    cols_gone[t] = [{'name': c, 'named_in_module': named(c, tokens)} for c in g]
            change['columns_added'], change['columns_gone'] = cols_added, cols_gone
            if not any((change['tables_added'], change['tables_gone'], cols_added, cols_gone)):
                continue
        else:
            o, n, c = set(old['names']), set(new['names']), set(common['names'])
            change['names_added'] = sorted(n - o)
            change['names_gone'] = [{'name': x, 'named_in_module': named(x, tokens)} for x in sorted(c - n)]
            if not (change['names_added'] or change['names_gone']):
                continue
        out['stores'].append(change)
    return {k: v for k, v in out.items() if v}


def finding_lines(found):
    """Markdown lines for one module's differences. Names and counts only."""
    def gone(items):
        return ', '.join(f"{i['name']}{' (named in the module)' if i['named_in_module'] else ''}" for i in items)
    lines = []
    for c in found.get('stores', []):
        lines.append(f"- `{c['store']}`")
        if c['kind'] == 'sqlite':
            if c['tables_added']:
                lines.append(f"  - tables in no cited key ({len(c['tables_added'])}): {', '.join(c['tables_added'])}")
            if c['tables_gone']:
                lines.append(f"  - tables every cited key has, missing here ({len(c['tables_gone'])}): {gone(c['tables_gone'])}")
            for t, cols in c['columns_added'].items():
                lines.append(f"  - `{t}` columns in no cited key ({len(cols)}): {', '.join(cols)}")
            for t, cols in c['columns_gone'].items():
                lines.append(f"  - `{t}` columns every cited key has, missing here ({len(cols)}): {gone(cols)}")
        else:
            label = 'preference names' if c['kind'] == 'prefs' else 'plist keys'
            if c['names_added']:
                lines.append(f"  - {label} in no cited key ({len(c['names_added'])}): {', '.join(c['names_added'])}")
            if c['names_gone']:
                lines.append(f"  - {label} every cited key has, missing here ({len(c['names_gone'])}): {gone(c['names_gone'])}")
    for c in found.get('no_baseline', []):
        lines.append(f"- `{c['store']}`: in this key but in none of the cited keys ({c['kind']}, {c['count']} names)")
    for c in found.get('not_compared', []):
        lines.append(f"- `{c['store']}`: not compared ({c['reason']})")
    return lines


def store_matters(c):
    """A store difference files an issue when it has a name no cited key had, or a missing name that
    appears in the module's source. A missing name the module never mentions is a note only."""
    if c['kind'] == 'sqlite':
        gone = c['tables_gone'] + [x for cols in c['columns_gone'].values() for x in cols]
        added = c['tables_added'] or c['columns_added']
    else:
        gone, added = c['names_gone'], c['names_added']
    return bool(added) or any(x['named_in_module'] for x in gone)


def is_change(found):
    """Only these differences file an issue. A store that could not be read is reported, not filed."""
    return any(store_matters(c) for c in found.get('stores', [])) or bool(found.get('no_baseline'))


# ---------------------------------------------------------------- one core

def check_core(core, repo, keys, reg, cache_dir, tmp_root, jobs, log=print):
    """Compare each new key with every module of one core. Returns (commit, results, failed keys)."""
    commit, modules = core_modules(repo)
    patterns = sorted({p for m in modules.values() for p in m['patterns']})
    failed = {}

    def index(key):
        return index_key(key, reg[key], patterns, cache_dir, tmp_root)

    new_index = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(index, k): k for k in keys}
        for f in concurrent.futures.as_completed(futures):
            k = futures[f]
            try:
                new_index[k] = f.result()
                log(f'  {k}: indexed, {len(new_index[k]["stores"])} matched store file(s)')
            except Exception as error:  # report and leave the key unseen
                failed[k] = f'{type(error).__name__}: {str(error)[:160]}'
                log(f'  {k}: could not check ({failed[k]})')
    # Baselines only for modules that have a store in a new key.
    touched = {name: m for name, m in modules.items()
               if any(module_stores(m, idx) for idx in new_index.values())}
    cited = sorted({k for m in touched.values() for k in m['keys']})
    base_index, missing = {}, {}
    todo = [k for k in cited if k in reg]
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(index, k): k for k in todo}
        for f in concurrent.futures.as_completed(futures):
            k = futures[f]
            try:
                base_index[k] = f.result()
            except Exception as error:
                missing[k] = f'{type(error).__name__}: {str(error)[:160]}'
                log(f'  baseline {k}: could not read ({missing[k]})')
    for k in cited:
        if k not in reg:
            missing[k] = 'not in any registry'
    results = []
    for key in sorted(new_index):
        for name in sorted(touched):
            m = touched[name]
            stores = module_stores(m, new_index[key])
            if not stores:
                continue
            if key in m['keys']:
                continue  # the module was built on this key
            usable = {k: module_stores(m, base_index[k]) for k in m['keys'] if k in base_index}
            if not m['keys']:
                results.append({'key': key, 'module': name, 'status': 'no cited keys', 'found': {}})
                continue
            if not usable:
                results.append({'key': key, 'module': name, 'status': 'baseline unreadable', 'found': {},
                                'baseline_keys': m['keys']})
                continue
            found = compare(m, stores, usable)
            results.append({'key': key, 'module': name, 'status': 'changed' if is_change(found) else 'unchanged',
                            'found': found, 'baseline_keys': sorted(usable),
                            'baseline_missing': sorted(k for k in m['keys'] if k not in usable)})
    return commit, results, failed


# ---------------------------------------------------------------- report and filing

def write_report(out, runs, failed, skipped):
    lines = ['# Schema watch', '']
    for core, (commit, results) in runs.items():
        by = {}
        for r in results:
            by.setdefault(r['status'], []).append(r)
        lines += [f'## {core} at {commit}', '',
                  ', '.join(f'{len(v)} {s}' for s, v in sorted(by.items())) or 'no module had a store in the new keys', '']
        for r in by.get('changed', []):
            lines += [f"### {r['module']} on {r['key']}", '',
                      f"Baseline: {', '.join(r['baseline_keys'])}"
                      + (f" (could not read: {', '.join(r['baseline_missing'])})" if r.get('baseline_missing') else ''), '']
            lines += finding_lines(r['found']) + ['']
        for r in by.get('unchanged', []):
            extra = finding_lines(r['found'])
            if extra:
                lines += [f"### {r['module']} on {r['key']} (unchanged, notes)", ''] + extra + ['']
        for status in ('no cited keys', 'baseline unreadable'):
            if by.get(status):
                lines += [f'### {status}', ''] + [f"- {r['module']} on {r['key']}" for r in by[status]] + ['']
    if failed:
        lines += ['## Could not check', ''] + [f'- {k}: {v}' for k, v in sorted(failed.items())] + ['']
    if skipped:
        lines += ['## New keys in a registry no core maps to (skipped)', ''] + [f'- {k}' for k in sorted(skipped)] + ['']
    (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')


MARKER = '<!-- schema-watch module={module} key={key} -->'
MARKER_RE = re.compile(r'<!-- schema-watch module=(\S+) key=(\S+) -->')


def issue_body(core, commit, module, items, reg, public_only=True):
    """items: results for one module (one per new key). Baseline keys are named only when public."""
    lines = [f'Found by the schema watch against {core} main at {commit}. Names and counts only.', '']
    for r in items:
        named = [k for k in r['baseline_keys'] if not public_only or (reg.get(k) or {}).get('public')]
        hidden = len(r['baseline_keys']) - len(named)
        base = ', '.join(named) if named else ''
        if hidden:
            base = (base + ', ' if base else '') + f'{hidden} cited key(s) not named here'
        lines += [f"New key {r['key']}, compared with the stores in {base}:", '']
        lines += finding_lines(r['found']) + ['']
    lines += ['A column or table marked "named in the module" appears in the module source, so a query may now fail '
              'or return less. Added names may hold data the artifact does not report. Look at the store, then '
              'decide: update the artifact, add `sample_data` for the new key, or close this as expected.', '']
    lines += [MARKER.format(module=module, key=r['key']) for r in items]
    return '\n'.join(lines)


def covered_keys(repo, module):
    """Keys an open schema-watch issue for this module already covers, or None if the search failed."""
    found = run(['gh', 'issue', 'list', '--repo', repo, '--state', 'open', '--label', 'audit-queue',
                 '--search', f'"schema-watch module={module}" in:body', '--json', 'number,body', '--limit', '100'])
    if found.returncode != 0:
        return None
    keys = set()
    for issue in json.loads(found.stdout or b'[]'):
        for m, k in MARKER_RE.findall(issue.get('body') or ''):
            if m == module:
                keys.add(k)
    return keys


def file_issues(owner, core, commit, results, reg, private_queue, dry_run, log=print):
    """One audit-queue issue per changed module, for new keys that allow public metadata. Changes on any
    other key go to the private queue file, never to GitHub. Returns (filed, private, problems)."""
    repo = f'{owner}/{core}'
    changed = {}
    for r in results:
        if r['status'] == 'changed':
            changed.setdefault(r['module'], []).append(r)
    filed, private, problems = [], [], []
    for module in sorted(changed):
        public = [r for r in changed[module] if (reg.get(r['key']) or {}).get('public')]
        hidden = [r for r in changed[module] if r not in public]
        if public:
            have = covered_keys(repo, module)
            if have is None:
                problems.append(f'could not search issues for {module}')
                log(f'  {module}: could not search issues, not filed')
            else:
                todo = [r for r in public if r['key'] not in have]
                if not todo:
                    log(f'  {module}: open issue already covers these keys, not filed')
                else:
                    title = f"{module.removesuffix('.py')}: store schema differs in a new corpus key (schema watch)"
                    body = issue_body(core, commit, module, todo, reg)
                    if dry_run:
                        log(f'  would file in {repo}: {title}')
                    else:
                        made = run(['gh', 'issue', 'create', '--repo', repo, '--label', 'audit-queue',
                                    '--title', title, '--body-file', '-'], input=body.encode())
                        text = (made.stdout or made.stderr).decode(errors='replace').strip()
                        log(f'  {module}: {text[:200]}')
                        (filed if made.returncode == 0 else problems).append(text[:200])
        if hidden:
            entry = [f"## {datetime.date.today().isoformat()} {core} {module} (schema watch)", '',
                     issue_body(core, commit, module, hidden, reg, public_only=False), '']
            if dry_run:
                log(f'  would add to the private queue: {core} {module} on {", ".join(r["key"] for r in hidden)}')
            else:
                Path(private_queue).parent.mkdir(parents=True, exist_ok=True)
                with open(private_queue, 'a', encoding='utf-8') as f:
                    f.write('\n' + '\n'.join(entry))
                log(f'  {module}: added to the private queue ({len(hidden)} key(s))')
            private.append(module)
    return filed, private, problems


# ---------------------------------------------------------------- state

def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix('.part')
    part.write_text(json.dumps(state, indent=1, sort_keys=True), encoding='utf-8')
    part.replace(path)


def new_keys(state, reg):
    seen = (state or {}).get('seen', {})
    return sorted(k for k, e in reg.items() if seen.get(k) != e['sha256'])


def core_of(reg_entry, core_registries):
    """The core whose registry holds this key, from the config's core_registries."""
    where = reg_entry['registry']
    for core, path in core_registries.items():
        if str(kit_config.expand(path).resolve()) == where:
            return next((c for c in CORES if c.lower() == core.lower()), core)
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--init', action='store_true', help='record every registered key as seen and stop')
    ap.add_argument('--keys', nargs='*', help='treat only these keys as new; the state file is left alone')
    ap.add_argument('--core', help='only this core')
    ap.add_argument('--jobs', type=int, default=3)
    ap.add_argument('--out', help='output folder (default <work_dir>/schema-watch/runs/<stamp>)')
    ap.add_argument('--file-issues', action='store_true')
    ap.add_argument('--dry-run', action='store_true', help='print what would be filed; write no state')
    args = ap.parse_args(argv)

    try:
        config = kit_config.load()
        repos = kit_config.expand(kit_config.need(config, 'repos_dir'))
        roots = kit_config.expand(kit_config.need(config, 'corpus_roots'))
        work = kit_config.expand(kit_config.need(config, 'work_dir')) / 'schema-watch'
        core_registries = kit_config.need(config, 'core_registries')
        private_queue = kit_config.expand(config.get('private_queue') or work / 'private-queue.md')
        owners = {str(k).lower(): v for k, v in (config.get('repo_owners') or {}).items()}
        owner = kit_config.need(config, 'github_owner') if args.file_issues else None
    except kit_config.ConfigError as error:
        print(f'COULD NOT CHECK: {error}', file=sys.stderr)
        return 2
    if not roots.is_file():
        print(f'COULD NOT CHECK: {roots} is not there (corpus drive not mounted?)', file=sys.stderr)
        return 2
    reg = registered_keys(roots)
    state_file = work / 'state.json'
    state = load_state(state_file)
    if args.init:
        state = {'seen': {k: e['sha256'] for k, e in reg.items()},
                 'initialised': datetime.datetime.now().isoformat(timespec='seconds')}
        save_state(state_file, state)
        print(f'recorded {len(reg)} key(s) as seen in {state_file}; nothing compared')
        return 0
    if state is None and not args.keys:
        print(f'COULD NOT CHECK: no state at {state_file}; run with --init first so existing keys are not all new',
              file=sys.stderr)
        return 2
    if args.keys:
        unknown = [k for k in args.keys if k not in reg]
        if unknown:
            print(f'not in any registry: {", ".join(unknown)}', file=sys.stderr)
            return 2
        keys = sorted(args.keys)
    else:
        keys = new_keys(state, reg)
    by_core, skipped = {}, []
    for k in keys:
        core = core_of(reg[k], core_registries)
        if core is None:
            skipped.append(k)
        elif not args.core or core.lower() == args.core.lower():
            by_core.setdefault(core, []).append(k)
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H%M%S')
    out = Path(args.out) if args.out else work / 'runs' / stamp
    out.mkdir(parents=True, exist_ok=False)
    print(f'{len(keys)} new key(s): ' + ', '.join(f'{c} {len(v)}' for c, v in sorted(by_core.items()))
          + (f', {len(skipped)} in no core registry' if skipped else ''), flush=True)
    runs, failed, problems = {}, {}, []
    for core, ks in sorted(by_core.items()):
        repo = repos / core
        if not (repo / '.git').exists():
            failed.update({k: f'no checkout at {repo}' for k in ks})
            continue
        if run(['git', '-C', str(repo), 'fetch', '-q', 'origin']).returncode != 0:
            failed.update({k: 'git fetch failed' for k in ks})
            continue
        print(f'{core}: {len(ks)} key(s)', flush=True)
        try:
            commit, results, bad = check_core(core, repo, ks, reg, work / 'index', work / 'tmp', args.jobs)
        except Exception as error:
            failed.update({k: f'{type(error).__name__}: {str(error)[:160]}' for k in ks})
            continue
        failed.update(bad)
        runs[core] = (commit, results)
        if args.file_issues:
            who = owners.get(core.lower()) or owner
            _filed, _private, probs = file_issues(who, core, commit, results, reg, private_queue, args.dry_run)
            problems += probs
            # A module whose filing failed keeps its keys unseen, so the next run tries again.
            if probs:
                for r in results:
                    if r['status'] == 'changed' and any(r['module'] in p for p in probs):
                        failed.setdefault(r['key'], 'issue filing failed')
    write_report(out, runs, failed, skipped)
    (out / 'results.json').write_text(json.dumps({c: {'commit': cm, 'results': rs} for c, (cm, rs) in runs.items()}
                                                 | {'could_not_check': failed, 'skipped': skipped}, indent=1),
                                      encoding='utf-8')
    print(f'report: {out / "report.md"}')
    if not args.keys and not args.dry_run:
        done = [k for ks in by_core.values() for k in ks if k not in failed] + skipped
        state['seen'].update({k: reg[k]['sha256'] for k in done})
        save_state(state_file, state)
    return 2 if failed or problems else 0


if __name__ == '__main__':
    sys.exit(main())
