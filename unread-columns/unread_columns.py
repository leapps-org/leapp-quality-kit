#!/usr/bin/env python3
"""List, for each SQLite table an artifact reads, the columns its SQL never reads.

    unread_columns.py --core ALEAPP --modules cmhTags kwai --keys anne_a15 [more keys]
                      [--tree PATH] [--registry PATH] [--out DIR]

For each corpus key it runs the core once, with a profile holding every artifact of the named
modules, under trace_sqlite.py. SQLite reports every column each statement reads while it
prepares the statement. After the run, each store the artifacts read is copied with its -wal,
-shm and -journal into a temporary folder and opened read-only, and its table_xinfo gives the
table's full column list. The difference is the unread list.

For every unread column the report gives counts only: rows in the table, rows where the column
is not NULL, distinct values, and any read column of the same table it equals on every row. No
value from the evidence is written anywhere.

It is a warning list for the session working the artifact. It decides nothing, never edits a
module, and has no part in the review gate. Exit 0 when the report was written (whatever it
lists), 2 when it could not run.

    unread_columns.py --analyze TRACE.json --run-dir DIR --tree PATH --modules m1 [m2]

analyses a trace from an earlier run without running the core again.
"""
import argparse
import ast
import datetime
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kit_config  # noqa: E402

HERE = Path(__file__).resolve().parent
TRACER = HERE / 'trace_sqlite.py'
PROFILE_EXT = {'aleapp': 'alprofile', 'ileapp': 'ilprofile', 'rleapp': 'rlprofile', 'dleapp': 'dlprofile',
               'vleapp': 'vlprofile'}
TYPES = (('.tar.gz', 'gz'), ('.tgz', 'gz'), ('.zip', 'zip'), ('.tar', 'tar'))
SIDECARS = ('-wal', '-shm', '-journal')
# How many rows a table may hold before the per-column equality check is skipped. Counts of
# non-NULL and distinct values are always taken.
EQUALITY_ROW_LIMIT = 2_000_000


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False, **kw)


def module_artifacts(tree, module):
    """{artifact key: paths tuple} from a module's __artifacts_v2__, read with ast, never imported."""
    source = tree / 'scripts' / 'artifacts' / f'{module}.py'
    tree_ast = ast.parse(source.read_text(encoding='utf-8'))
    for node in tree_ast.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == '__artifacts_v2__':
            if not isinstance(node.value, ast.Dict):
                break
            # Only the keys and each paths value are read, so a notes text built with an f-string
            # does not stop the module from being profiled.
            out = {}
            for key_node, info in zip(node.value.keys, node.value.values):
                key = ast.literal_eval(key_node)
                paths = ()
                if isinstance(info, ast.Dict):
                    for name, value in zip(info.keys, info.values):
                        if isinstance(name, ast.Constant) and name.value == 'paths':
                            try:
                                paths = ast.literal_eval(value)
                            except ValueError:
                                paths = ()
                out[key] = (paths,) if isinstance(paths, str) else tuple(paths)
            return out
    raise ValueError(f'{source} has no __artifacts_v2__ block')


def quote(name):
    return '"' + str(name).replace('"', '""') + '"'


def copy_store(path, into):
    """Copy a staged store and its sidecars into a fresh folder. Returns the copy's path."""
    into.mkdir(parents=True, exist_ok=True)
    target = into / path.name
    shutil.copy2(path, target)
    for suffix in SIDECARS:
        side = Path(str(path) + suffix)
        if side.is_file():
            shutil.copy2(side, Path(str(target) + suffix))
    return target


def open_ro(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def table_columns(db, table):
    """Every column of a table, hidden and generated ones too, in declared order."""
    rows = db.execute(f'PRAGMA table_xinfo({quote(table)})').fetchall()
    return [row[1] for row in rows]


def column_counts(db, table, unread, read):
    """Counts for each unread column. Never returns a value from the table.

    Values are compared byte for byte (COLLATE BINARY), so a column declared with an app's own
    collation, such as Android contacts' PHONEBOOK, is still counted."""
    total = db.execute(f'SELECT count(*) FROM {quote(table)}').fetchone()[0]
    out = {}
    for column in unread:
        non_null, distinct = db.execute(
            f'SELECT count({quote(column)}), count(DISTINCT {quote(column)} COLLATE BINARY) FROM {quote(table)}'
        ).fetchone()
        same_as = []
        if 0 < total <= EQUALITY_ROW_LIMIT:
            for other in read:
                equal = db.execute(f'SELECT count(*) FROM {quote(table)} WHERE {quote(column)} COLLATE BINARY IS {quote(other)}'
                                   ).fetchone()[0]
                if equal == total:
                    same_as.append(other)
        out[column] = {'rows': total, 'not_null': non_null, 'distinct': distinct, 'same_as': same_as}
    return out


def kind(entry):
    """A short sorting hint from the counts alone. The session makes the decision."""
    if entry['rows'] == 0:
        return 'table empty on this key'
    if entry['not_null'] == 0:
        return 'NULL on every row'
    # A column with one value equals every other constant column by chance, so it is called
    # constant and equality is named only for a column that varies.
    if entry['distinct'] == 1 and entry['not_null'] == entry['rows']:
        return 'one value on every row'
    if entry['distinct'] == 1:
        return 'one value where set'
    if entry['same_as']:
        return 'equals ' + ', '.join(entry['same_as']) + ' on every row'
    return 'varies'


def store_label(path, patterns, labels):
    """The store's file name when a module path pattern names it literally, else store N.

    A file name that only a wildcard matched can carry an account or contact identifier, so it
    is not written."""
    name = path.name
    for pattern in patterns:
        tail = pattern.replace('\\', '/').rsplit('/', 1)[-1].rstrip('*')
        if tail and not any(ch in tail for ch in '*?[') and name == tail:
            return name
    labels.setdefault(str(path), f'store {len(labels) + 1}')
    return labels[str(path)]


def lava_artifacts(run_dir):
    """{artifact key: (record count, column count)} from the run's _lava_data.lava, if there is one."""
    found = sorted(Path(run_dir).rglob('_lava_data.lava'))
    if not found:
        return None
    data = json.loads(found[0].read_text(encoding='utf-8'))
    out = {}
    for items in (data.get('artifacts') or {}).values():
        for item in items:
            out[item.get('artifact_key')] = (item.get('record_count'), len(item.get('column_map') or {}))
    return out


def placement(path, run_dir, inputs):
    """'staged' for a store the run staged or read in place, 'output' for the core's own output
    (the LAVA database, the timeline), 'elsewhere' for anything else, such as a temporary copy a
    module made before opening it."""
    resolved = Path(path).resolve()
    try:
        rel = resolved.relative_to(Path(run_dir).resolve())
    except ValueError:
        inside = any(resolved == Path(i).resolve() or resolved.is_relative_to(Path(i).resolve()) for i in inputs)
        return 'staged' if inside else 'elsewhere'
    return 'staged' if 'data' in rel.parts[:-1] else 'output'


def analyze(trace, run_dir, artifacts, inputs=(), scratch=None):
    """The unread list for one run. artifacts is {key: paths} for the profiled artifacts."""
    wanted = set(artifacts)
    per = {key: {'stores': {}, 'connections': 0} for key in wanted}
    skipped, errors, labels = 0, [], {}
    lava = lava_artifacts(run_dir)
    # First every read, unioned per (artifact, file, table) across all its connections, so a store
    # opened twice by one artifact is measured once.
    touched, whole = {}, {}
    for conn in trace.get('connections', []):
        files = conn.get('files') or {}
        for module, func, schema, table, column in conn.get('reads', []):
            if func not in wanted:
                continue
            target = files.get(schema)
            if target is None and schema != 'main' and len(conn.get('attached') or []) == 1:
                target = conn['attached'][0]
            if not target:
                errors.append(f'{func}: a read of table {table} in schema {schema} has no file '
                              '(in memory or an attach the trace could not resolve)')
                continue
            touched.setdefault((func, target, table), set()).add(column)
        for module, func, schema, table, columns in conn.get('statements', []):
            if func in wanted:
                whole.setdefault((func, files.get(schema) or '', table), []).append(set(columns))
        for func in {r[1] for r in conn.get('reads', []) if r[1] in wanted}:
            per[func]['connections'] += 1
    errors = sorted(set(errors))
    with tempfile.TemporaryDirectory(dir=scratch) as tmp:
        copies = {}
        for (func, target, table), read in sorted(touched.items()):
            source = Path(target)
            where = placement(source, run_dir, inputs)
            if where == 'output':
                skipped += 1
                continue
            if where == 'elsewhere':
                errors.append(f'{func}: table {table} was read from a file outside the staged data '
                              '(a temporary copy the module made), so it was not measured')
                continue
            if not source.is_file():
                errors.append(f'{func}: a store it read is gone after the run')
                continue
            if str(source) not in copies:
                copies[str(source)] = copy_store(source, Path(tmp) / str(len(copies)))
            label = store_label(source, artifacts[func], labels)
            try:
                with closing(open_ro(copies[str(source)])) as db:
                    columns = table_columns(db, table)
                    # SQLite reports a touch of the table with no named column (count(*), SELECT 1)
                    # as an empty name. The table counts as read; no column does.
                    # A rowid read on a table with no INTEGER PRIMARY KEY is reported as ROWID (or
                    # _rowid_, oid), which is not a column, so only real columns count.
                    read = read & set(columns)
                    if not columns:
                        errors.append(f'{func}: {label} table {table}: no columns in the copy')
                        continue
                    unread = [c for c in columns if c not in read]
                    counts = column_counts(db, table, unread, [c for c in columns if c in read])
            except sqlite3.Error as error:
                errors.append(f'{func}: {label} table {table}: could not read the copy ({type(error).__name__})')
                continue
            entry = per[func]['stores'].setdefault(label, {}).setdefault(
                table, {'all': set(), 'read': set(), 'unread': {}, 'whole_row_read': False, 'copies': 0})
            entry['copies'] += 1
            entry['all'] |= set(columns)
            entry['read'] |= read
            entry['whole_row_read'] = entry['whole_row_read'] or any(
                cols >= set(columns) for cols in whole.get((func, target, table), []))
            for column, value in counts.items():
                seen = entry['unread'].get(column)
                if seen is None:
                    entry['unread'][column] = dict(value, kinds=[kind(value)])
                else:
                    # Several files with one name (two Android users, a second app instance):
                    # rows add up, distinct is the largest of any one file.
                    for name in ('rows', 'not_null'):
                        seen[name] += value[name]
                    seen['distinct'] = max(seen['distinct'], value['distinct'])
                    seen['same_as'] = sorted(set(seen['same_as']) & set(value['same_as']))
                    seen['kinds'] = sorted(set(seen['kinds']) | {kind(value)})
    for info in per.values():
        for tables in info['stores'].values():
            for t in tables.values():
                # A column one file reads and another file lacks or leaves unread counts as read.
                t['unread'] = {c: v for c, v in t['unread'].items() if c not in t['read']}
                t['columns'] = len(t.pop('all'))
    result = {'artifacts': {}, 'skipped_connections': skipped, 'errors': sorted(set(errors)),
              'callback_errors': trace.get('callback_errors', 0), 'core_exit': trace.get('core_exit'),
              'lava_found': lava is not None}
    # Which profiled artifact reads each column, so a column one artifact leaves out and a sibling
    # reports is marked as such.
    readers = {}
    for key in wanted:
        for label, tables in per[key]['stores'].items():
            for table, t in tables.items():
                for column in t['read']:
                    readers.setdefault((label, table, column), set()).add(key)
    for key in wanted:
        for label, tables in per[key]['stores'].items():
            for table, t in tables.items():
                for column, c in t['unread'].items():
                    c['read_by'] = sorted(readers.get((label, table, column), set()) - {key})
    for key in sorted(wanted):
        rows, headers = (lava or {}).get(key, (None, None))
        stores = {label: {table: {'columns': t['columns'], 'read': len(t['read']), 'copies': t['copies'],
                                  'whole_row_read': t['whole_row_read'], 'unread': t['unread']}
                          for table, t in sorted(tables.items())}
                  for label, tables in sorted(per[key]['stores'].items())}
        result['artifacts'][key] = {'lava_rows': rows, 'lava_columns': headers,
                                    'traced_connections': per[key]['connections'], 'stores': stores}
    return result


def status_line(info):
    if info['lava_rows'] is None and not info['stores']:
        return 'not in the LAVA output and no SQLite read traced (it did not run on this key, or found nothing)'
    if not info['stores']:
        return ('no SQLite read traced. A store read another way (sqlcipher3, apsw, plist, protobuf, '
                'JSON, XML) is not covered, so this is not a finding')
    if not info['lava_rows']:
        return 'reads SQLite but wrote no rows on this key; the lists describe the store, not the output'
    return f"{info['lava_rows']} row(s) in LAVA"


GROUPED = ('NULL on every row', 'table empty on this key', 'one value on every row')


def column_lines(unread, indent='  '):
    """One line per column that needs a look, one line per group of NULL, empty or constant columns."""
    lines, groups = [], {}
    for column, c in sorted(unread.items(), key=lambda item: item[0].lower()):
        if len(c['kinds']) == 1 and c['kinds'][0] in GROUPED:
            groups.setdefault(c['kinds'][0], []).append(column)
            continue
        lines.append(f"{indent}- {column}: {c['not_null']} of {c['rows']} rows set, "
                     f"{c['distinct']} distinct; {'; '.join(c['kinds'])}")
    for name in GROUPED:
        if name in groups:
            lines.append(f"{indent}- {name} ({len(groups[name])}): {', '.join(groups[name])}")
    return lines


def by_no_artifact(info_by_artifact):
    """{(store, table): (column count, copies, {column: counts})} for columns no profiled artifact reads."""
    tables = {}
    for info in info_by_artifact.values():
        for label, store in info['stores'].items():
            for table, t in store.items():
                seen = tables.setdefault((label, table), {'columns': 0, 'copies': 0, 'readers': 0, 'unread': {}})
                seen['readers'] += 1
                seen['columns'] = max(seen['columns'], t['columns'])
                seen['copies'] = max(seen['copies'], t['copies'])
                # Every artifact's list is merged: two artifacts can have read different files
                # under one store name, and a column only one of those files has must stay.
                for column, value in t['unread'].items():
                    if not value.get('read_by'):
                        seen['unread'].setdefault(column, value)
    return dict(sorted(tables.items()))


def report_lines(core, commit, results):
    lines = [f'# Unread columns, {core} at {commit}', '',
             'Columns of each SQLite table an artifact reads that no statement of that artifact read, '
             'measured by SQLite during the run. Counts only. A column read only in a JOIN or WHERE '
             'counts as read and is not listed. SQLite stores only; other formats are not covered.', '']
    for key, res in results.items():
        lines.append(f'## {key}')
        if res.get('status') != 'ran':
            lines += [f"Could not check: {res.get('reason')}", '']
            continue
        if res['callback_errors']:
            lines.append(f"Tracer errors: {res['callback_errors']} (the trace may be incomplete)")
        if res['core_exit'] not in (0, None):
            lines.append(f"The core exited {res['core_exit']}")
        tables = by_no_artifact(res['artifacts'])
        lines += ['', '### Read by no profiled artifact']
        if not tables:
            lines.append('No SQLite table was read.')
        for (label, table), t in tables.items():
            head = f"- {label}, table {table}: {t['columns'] - len(t['unread'])} of {t['columns']} columns read"
            if t['copies'] > 1:
                head += f" (over {t['copies']} copies of the store; distinct is the largest in one copy)"
            lines.append(head)
            lines += column_lines(t['unread'])
        lines += ['', '### Per artifact']
        for artifact, info in res['artifacts'].items():
            lines.append(f'- {artifact}: {status_line(info)}')
            for label, store in info['stores'].items():
                for table, t in store.items():
                    sibling = {c: v['read_by'] for c, v in t['unread'].items() if v.get('read_by')}
                    text = f"  - {label}, table {table}: {t['read']} of {t['columns']} columns read"
                    if sibling:
                        text += '; read by another artifact but not this one: ' + ', '.join(
                            f"{c} ({', '.join(r)})" for c, r in sorted(sibling.items()))
                    lines.append(text)
                    if t['whole_row_read'] and not t['unread']:
                        lines.append('    - one statement read every column (SELECT * or a full list). '
                                     'Whether each is reported cannot be told from the SQL.')
        if res['errors']:
            lines += ['', 'Could not measure:'] + [f'- {e}' for e in res['errors']]
        if res['skipped_connections']:
            lines.append(f"\n{res['skipped_connections']} read(s) of the core's own output files were left out.")
        lines.append('')
    return lines


def image_type(image):
    lower = image.name.lower()
    for suffix, name in TYPES:
        if lower.endswith(suffix):
            return name
    return 'fs' if image.is_dir() else None


def run_key(tree, core, key, registry, profile, workdir, python, userbase):
    keydir = workdir / key
    entry = json.loads(registry.read_text(encoding='utf-8')).get('samples', {}).get(key)
    if not entry:
        return {'status': 'could not check', 'reason': 'key not in the registry'}
    image = registry.parent / entry['match']['zip']
    kind_ = image_type(image)
    if not image.exists() or kind_ is None:
        return {'status': 'could not check', 'reason': 'image missing or of a type this tool does not run'}
    for name in ('home', 'tmp', 'run'):
        (keydir / name).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, HOME=str(keydir / 'home'), TMPDIR=str(keydir / 'tmp'), PYTHONUSERBASE=userbase,
               PYTHONDONTWRITEBYTECODE='1')
    trace = keydir / 'trace.json'
    done = run([python, str(TRACER), '--trace', str(trace), '--', f'{core.lower()}.py', '-t', kind_,
                '-i', str(image), '-o', str(keydir / 'run'), '-m', str(profile)], cwd=str(tree), env=env)
    (keydir / 'run.log').write_text((done.stdout or '') + (done.stderr or ''), encoding='utf-8')
    if not trace.is_file():
        return {'status': 'could not check', 'reason': f'no trace written (exit {done.returncode})'}
    if done.returncode != 0 or 'Report generation Completed' not in (done.stdout or '') + (done.stderr or ''):
        return {'status': 'could not check', 'reason': f'the run did not complete (exit {done.returncode})'}
    return {'status': 'ran', 'trace': trace, 'run_dir': keydir / 'run', 'input': image}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--core', default='ALEAPP')
    parser.add_argument('--modules', nargs='+', required=True, help='module file names without .py')
    parser.add_argument('--keys', nargs='+', help='corpus keys to run')
    parser.add_argument('--tree', help='core checkout to run (default <repos_dir>/<core>)')
    parser.add_argument('--registry', help='samples.json (default core_registries[core] from the config)')
    parser.add_argument('--out', help='output folder (default unread_columns.out_dir from the config)')
    parser.add_argument('--analyze', help='analyse this trace.json instead of running the core')
    parser.add_argument('--run-dir', help='with --analyze: the core output folder of that run')
    args = parser.parse_args(argv)
    try:
        data = kit_config.load()
        settings = kit_config.unread_columns(data)
    except kit_config.ConfigError as error:
        if not (args.analyze and args.tree):
            print(f'could not run: {error}', file=sys.stderr)
            return 2
        data, settings = {}, {}
    tree = (Path(args.tree).expanduser() if args.tree else
            kit_config.expand(kit_config.need(data, 'repos_dir')) / args.core).resolve()
    try:
        artifacts = {}
        for module in args.modules:
            artifacts.update(module_artifacts(tree, module))
    except (OSError, ValueError, SyntaxError) as error:
        print(f'could not run: {error}', file=sys.stderr)
        return 2

    if args.analyze:
        if not args.run_dir:
            print('could not run: --analyze needs --run-dir', file=sys.stderr)
            return 2
        result = analyze(json.loads(Path(args.analyze).read_text(encoding='utf-8')), args.run_dir, artifacts)
        result['status'] = 'ran'
        print('\n'.join(report_lines(args.core, 'given trace', {Path(args.analyze).parent.name: result})))
        return 0

    if not args.keys:
        print('could not run: give --keys', file=sys.stderr)
        return 2
    registry = Path(args.registry).expanduser() if args.registry else kit_config.expand(
        (data.get('core_registries') or {}).get(args.core.lower(), ''))
    if not registry.is_file():
        print(f'could not run: no registry for {args.core}', file=sys.stderr)
        return 2
    # The core runs from its own checkout, so every path handed to it must be absolute.
    registry = registry.resolve()
    python = str(kit_config.expand(data.get('python') or sys.executable))
    userbase = run([python, '-m', 'site', '--user-base']).stdout.strip()
    commit = run(['git', '-C', str(tree), 'rev-parse', '--short', 'HEAD']).stdout.strip() or 'unknown'
    status = run(['git', '-C', str(tree), 'status', '--porcelain'])
    if status.returncode != 0 or status.stdout.strip():
        # The files on disk are what runs, so a report must not name only the commit.
        commit += ' with local changes' if status.returncode == 0 else ' (git status failed)'
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H%M%S')
    workdir = ((Path(args.out).expanduser() if args.out else settings['out_dir']) / f'{args.core} {stamp}').resolve()
    workdir.parent.mkdir(parents=True, exist_ok=True)
    # The run folder holds the trace, the core's log and its staged copies of the evidence.
    workdir.mkdir(mode=0o700, exist_ok=False)
    os.chmod(workdir, 0o700)
    ext = PROFILE_EXT[args.core.lower()]
    profile = workdir / f'profile.{ext}'
    profile.write_text(json.dumps({'leapp': args.core.lower(), 'format_version': 1, 'plugins': sorted(artifacts)}))
    results = {}
    for key in args.keys:
        res = run_key(tree, args.core, key, registry, profile, workdir, python, userbase)
        if res['status'] == 'ran':
            trace = json.loads(res['trace'].read_text(encoding='utf-8'))
            res = dict(analyze(trace, res['run_dir'], artifacts, inputs=[res['input']], scratch=workdir), status='ran')
        results[key] = res
    (workdir / 'results.json').write_text(json.dumps(results, indent=1, default=sorted), encoding='utf-8')
    (workdir / 'report.md').write_text('\n'.join(report_lines(args.core, commit, results)), encoding='utf-8')
    print(workdir / 'report.md')
    return 0


if __name__ == '__main__':
    sys.exit(main())
