#!/usr/bin/env python3
"""Run a LEAPP core with every SQLite read recorded by SQLite itself.

    trace_sqlite.py --trace OUT.json -- <core>.py <core arguments...>

Run it from the core checkout, the same way the core is run. Before the core is imported,
sqlite3.connect is wrapped so that every connection gets an authorizer. SQLite calls the
authorizer while it prepares each statement, once for every column the statement reads
(SQLITE_READ, with the table and column it resolved), so joins, aliases, views and helper
functions are resolved by SQLite and no SQL text is parsed here.

Each read is credited to the outermost frame on the stack that belongs to a file under
scripts/artifacts/, which is the artifact function (a helper in another artifact file that it
calls is credited to the caller). The file each connection opened comes from PRAGMA
database_list, run once right after the connect.

The authorizer always returns SQLITE_OK and catches its own errors, so it cannot change what a
statement does. Statements are grouped by SQLite's trace callback, which fires when a prepared
statement starts to run, so a statement that read every column of a table can be told apart.

Nothing from the evidence is recorded: only file paths, table and column names, and counts.
The trace is written when the core exits, also when it exits with an error.
"""
import json
import os
import runpy
import sqlite3
import sys
import threading

_REAL_CONNECT = sqlite3.connect
_LOCK = threading.Lock()
_STATE = {'connections': [], 'callback_errors': 0, 'core_exit': None}
_MARK = os.sep + 'scripts' + os.sep + 'artifacts' + os.sep
_SKIP_TABLES = {'sqlite_master', 'sqlite_schema', 'sqlite_temp_master', 'sqlite_temp_schema'}


def _artifact_frame():
    """(module file stem, function name) of the outermost frame under scripts/artifacts/, or None."""
    frame = sys._getframe(2)
    outer = None
    while frame is not None:
        name = frame.f_code.co_filename
        if _MARK in name and name.endswith('.py'):
            outer = (os.path.splitext(os.path.basename(name))[0], frame.f_code.co_name)
        frame = frame.f_back
    return outer


def _record(conn, action, arg1, arg2, dbname, source):
    """The work of the authorizer. Separate so a test can make it fail."""
    if action == sqlite3.SQLITE_READ:
        # arg2 is '' when the statement reads the rowid; the analysis maps that to the table's
        # INTEGER PRIMARY KEY column, which is the rowid under another name.
        if arg1 in _SKIP_TABLES or arg2 is None:
            return
        owner = _artifact_frame()
        key = (owner[0], owner[1]) if owner else ('', '')
        with _LOCK:
            conn['reads'].setdefault(key, set()).add((dbname or 'main', arg1, arg2))
            conn['pending'].setdefault(key, set()).add((dbname or 'main', arg1, arg2))
    elif action == sqlite3.SQLITE_ATTACH:
        with _LOCK:
            conn['attached'].append(arg1 or '')


def _statement_started(conn):
    """A prepared statement started to run: the reads since the last one belong to it."""
    with _LOCK:
        for key, reads in conn['pending'].items():
            tables = {}
            for schema, table, column in reads:
                tables.setdefault((schema, table), set()).add(column)
            for (schema, table), columns in tables.items():
                conn['statements'].append([key[0], key[1], schema, table, sorted(columns)])
        conn['pending'] = {}


def _instrument(connection):
    conn = {'files': {}, 'reads': {}, 'pending': {}, 'statements': [], 'attached': []}
    try:
        for row in connection.execute('PRAGMA database_list'):
            conn['files'][row[1]] = row[2] or ''
    except Exception:  # noqa: BLE001 - a store that refuses the pragma still gets traced
        conn['files_error'] = True

    def authorizer(action, arg1, arg2, dbname, source):
        try:
            _record(conn, action, arg1, arg2, dbname, source)
        except Exception:  # noqa: BLE001 - the run must never see a tracer fault
            with _LOCK:
                _STATE['callback_errors'] += 1
        return sqlite3.SQLITE_OK

    def started(_statement):
        try:
            _statement_started(conn)
        except Exception:  # noqa: BLE001
            with _LOCK:
                _STATE['callback_errors'] += 1

    try:
        connection.set_authorizer(authorizer)
        connection.set_trace_callback(started)
    except Exception:  # noqa: BLE001 - an exotic factory: count it, leave the connection alone
        conn['not_instrumented'] = True
    with _LOCK:
        _STATE['connections'].append(conn)


def traced_connect(*args, **kwargs):
    connection = _REAL_CONNECT(*args, **kwargs)
    try:
        _instrument(connection)
    except Exception:  # noqa: BLE001
        with _LOCK:
            _STATE['callback_errors'] += 1
    return connection


def install():
    sqlite3.connect = traced_connect
    sqlite3.dbapi2.connect = traced_connect


def snapshot():
    """The trace as plain JSON-ready data."""
    with _LOCK:
        out = []
        for conn in _STATE['connections']:
            for key, reads in conn['pending'].items():
                tables = {}
                for schema, table, column in reads:
                    tables.setdefault((schema, table), set()).add(column)
                for (schema, table), columns in tables.items():
                    conn['statements'].append([key[0], key[1], schema, table, sorted(columns)])
            conn['pending'] = {}
            reads = [[m, f, s, t, c] for (m, f), cols in conn['reads'].items() for s, t, c in sorted(cols)]
            out.append({'files': conn['files'], 'reads': reads, 'statements': conn['statements'],
                        'attached': conn['attached'], 'not_instrumented': bool(conn.get('not_instrumented')),
                        'files_error': bool(conn.get('files_error'))})
        return {'version': 1, 'connections': out, 'callback_errors': _STATE['callback_errors'],
                'core_exit': _STATE['core_exit']}


def main(argv):
    if len(argv) < 4 or argv[0] != '--trace' or argv[2] != '--':
        print('usage: trace_sqlite.py --trace OUT.json -- <core>.py <arguments...>', file=sys.stderr)
        return 2
    trace_path, script, rest = argv[1], argv[3], argv[4:]
    install()
    sys.argv = [script] + rest
    # The core's folder replaces this tool's folder, as if the core had been started directly.
    sys.path[0] = os.path.dirname(os.path.abspath(script))
    code = 0
    try:
        runpy.run_path(script, run_name='__main__')
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
    except BaseException:  # noqa: BLE001 - record the trace, then fail the way the core did
        code = 1
        raise
    finally:
        _STATE['core_exit'] = code
        with open(trace_path, 'w', encoding='utf-8') as handle:
            json.dump(snapshot(), handle)
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
