"""Tests for the unread-column list, on a small synthetic core. No config, corpus or network."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import closing
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import trace_sqlite  # noqa: E402
import unread_columns  # noqa: E402

PLANTED = 'planted-value-7f3c'

ARTIFACT = textwrap.dedent('''
    __artifacts_v2__ = {
        "demo_two": {"name": "Two", "paths": ("*/app/databases/demo.db*",)},
        "demo_star": {"name": "Star", "paths": ("*/app/databases/demo.db*",)},
        "demo_helper": {"name": "Helper", "paths": ("*/app/databases/demo.db*",)},
        "demo_wild": {"name": "Wild", "paths": ("*/app/databases/*.db",)},
        "demo_temp": {"name": "Temp", "paths": ("*/app/databases/demo.db*",)},
    }
    import sqlite3
    from pathlib import Path
    from scripts.artifacts import helper

    def _open(path):
        return sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)

    def demo_two(path):
        # Two connections to one store: its rows must be counted once.
        with _open(path) as db:
            db.execute("SELECT id FROM msgs").fetchall()
        with _open(path) as db:
            return db.execute("SELECT m.id, m.body FROM msgs m WHERE m.id > 0").fetchall()

    def demo_star(path):
        with _open(path) as db:
            return db.execute("SELECT * FROM people").fetchall()

    def demo_helper(path):
        return helper.read_people_names(path)

    def demo_temp(path):
        # A module that copies the store elsewhere before opening it, then deletes the copy.
        import os, shutil, tempfile
        folder = tempfile.mkdtemp()
        copy = os.path.join(folder, "demo.db")
        for suffix in ("", "-wal", "-shm"):
            shutil.copy2(str(path) + suffix, copy + suffix)
        with _open(copy) as db:
            rows = db.execute("SELECT id FROM people").fetchall()
        shutil.rmtree(folder)
        return rows

    def demo_wild(path):
        with _open(path) as db:
            return db.execute("SELECT id FROM msgs").fetchall()
''')

HELPER = textwrap.dedent('''
    import sqlite3
    from pathlib import Path

    def read_people_names(path):
        with sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True) as db:
            return db.execute("SELECT name FROM people").fetchall()
''')

# A stand-in for a core: stages the store under <out>/report/data/, runs every artifact, writes its
# own output database and a _lava_data.lava with record counts.
CORE = textwrap.dedent('''
    import json, shutil, sqlite3, sys
    from pathlib import Path
    from scripts.artifacts import demo
    out = Path(sys.argv[sys.argv.index("-o") + 1])
    src = Path(sys.argv[sys.argv.index("-i") + 1])
    data = out / "report" / "data" / "app" / "databases"
    data.mkdir(parents=True)
    for name in ("demo.db", "demo.db-wal", "demo.db-shm", "acct-12345.db"):
        if (src / name).exists():
            shutil.copy2(src / name, data / name)
    counts = {}
    for key in ("demo_two", "demo_star", "demo_helper", "demo_temp"):
        counts[key] = len(getattr(demo, key)(data / "demo.db"))
    counts["demo_wild"] = len(demo.demo_wild(data / "acct-12345.db"))
    with sqlite3.connect(out / "report" / "_lava_artifacts.db") as lava:
        lava.execute("CREATE TABLE IF NOT EXISTS msgs (id, b, c)")
    # An artifact that reads the core's own output database: that read must be left out.
    demo.demo_wild(out / "report" / "_lava_artifacts.db")
    lava_data = {"artifacts": {"Demo": [{"artifact_key": k, "record_count": v, "column_map": {"a": "a"}}
                                        for k, v in counts.items()]}}
    (out / "report" / "_lava_data.lava").write_text(json.dumps(lava_data))
    print("Report generation Completed")
''')


def make_store(folder):
    """demo.db in WAL mode, with the last rows only in the -wal, and a second store."""
    db_path = folder / 'demo.db'
    db = sqlite3.connect(db_path)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA wal_autocheckpoint=0')
    db.execute('CREATE TABLE msgs (id INTEGER PRIMARY KEY, body TEXT, flag INTEGER, copy_id INTEGER, note TEXT)')
    # An app collation the reader does not have, as Android contacts declares PHONEBOOK.
    db.create_collation('APPSORT', lambda a, b: (a > b) - (a < b))
    db.execute('CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, secret TEXT COLLATE APPSORT)')
    db.executemany('INSERT INTO msgs VALUES (?, ?, 1, ?, NULL)', [(i, f'm{i}', i) for i in range(1, 4)])
    db.executemany('INSERT INTO people VALUES (?, ?, ?)', [(1, 'a', PLANTED), (2, 'b', 'x')])
    db.commit()
    # Keep the WAL: copy the files while this connection is still open, so nothing is checkpointed.
    staged = folder / 'staged'
    staged.mkdir()
    for name in ('demo.db', 'demo.db-wal', 'demo.db-shm'):
        (staged / name).write_bytes((folder / name).read_bytes())
    db.close()
    with closing(sqlite3.connect(staged / 'acct-12345.db')) as other:
        other.execute('CREATE TABLE msgs (id INTEGER, extra TEXT)')
        other.execute("INSERT INTO msgs VALUES (1, 'e')")
        other.commit()
    return staged


class SyntheticRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        tree = root / 'core'
        (tree / 'scripts' / 'artifacts').mkdir(parents=True)
        (tree / 'scripts' / '__init__.py').write_text('')
        (tree / 'scripts' / 'artifacts' / '__init__.py').write_text('')
        (tree / 'scripts' / 'artifacts' / 'demo.py').write_text(ARTIFACT)
        (tree / 'scripts' / 'artifacts' / 'helper.py').write_text(HELPER)
        (tree / 'fakeleapp.py').write_text(CORE)
        cls.tree = tree
        cls.source = make_store(root)
        cls.wal_hash = hashlib.sha256((cls.source / 'demo.db-wal').read_bytes()).hexdigest()
        cls.out = root / 'out'
        cls.out.mkdir()
        cls.trace_path = root / 'trace.json'
        cls.done = subprocess.run([sys.executable, str(HERE / 'trace_sqlite.py'), '--trace', str(cls.trace_path), '--',
                                   'fakeleapp.py', '-i', str(cls.source), '-o', str(cls.out)],
                                  cwd=tree, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        cls.trace = json.loads(cls.trace_path.read_text())
        cls.artifacts = unread_columns.module_artifacts(tree, 'demo')
        cls.result = unread_columns.analyze(cls.trace, cls.out, cls.artifacts, scratch=root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def table(self, artifact, table, store='demo.db'):
        return self.result['artifacts'][artifact]['stores'][store][table]

    def test_core_ran_and_trace_written(self):
        self.assertEqual(self.done.returncode, 0, self.done.stderr)
        self.assertEqual(self.trace['core_exit'], 0)
        self.assertEqual(self.trace['callback_errors'], 0)

    def test_columns_not_read_are_listed_with_counts(self):
        t = self.table('demo_two', 'msgs')
        self.assertEqual(t['columns'], 5)
        self.assertEqual(t['read'], 2)
        self.assertEqual(sorted(t['unread']), ['copy_id', 'flag', 'note'])
        self.assertEqual(t['unread']['flag']['kinds'], ['one value on every row'])
        self.assertEqual(t['unread']['note']['kinds'], ['NULL on every row'])
        self.assertEqual(t['unread']['copy_id']['same_as'], ['id'])
        # Three rows, all of them only in the -wal: the copy was opened with its sidecars. Two
        # connections to the one file do not double them.
        self.assertEqual(t['unread']['flag']['rows'], 3)
        self.assertEqual(t['copies'], 1)
        self.assertEqual(self.result['artifacts']['demo_two']['traced_connections'], 2)

    def test_select_star_is_flagged_not_counted_as_reported(self):
        t = self.table('demo_star', 'people')
        self.assertEqual(t['unread'], {})
        self.assertTrue(t['whole_row_read'])
        lines = '\n'.join(unread_columns.report_lines('X', 'c', {'k': dict(self.result, status='ran')}))
        self.assertIn('cannot be told from the SQL', lines)

    def test_helper_read_is_credited_to_the_artifact(self):
        t = self.table('demo_helper', 'people')
        self.assertEqual(sorted(t['unread']), ['id', 'secret'])
        self.assertFalse(t['whole_row_read'])
        # secret is declared with a collation this reader lacks and is still counted.
        self.assertEqual(t['unread']['secret']['distinct'], 2)
        self.assertEqual([e for e in self.result['errors'] if 'could not read' in e], [])

    def test_core_output_database_is_left_out(self):
        self.assertEqual(self.result['skipped_connections'], 1)
        self.assertEqual(list(self.result['artifacts']['demo_wild']['stores']), ['store 1'])

    def test_a_temporary_copy_is_could_not_measure_not_output(self):
        self.assertEqual(self.result['artifacts']['demo_temp']['stores'], {})
        self.assertEqual(len([e for e in self.result['errors'] if e.startswith('demo_temp: table people')]), 1)

    def test_wildcard_store_name_is_not_written(self):
        stores = self.result['artifacts']['demo_wild']['stores']
        self.assertEqual(list(stores), ['store 1'])
        self.assertEqual(sorted(stores['store 1']['msgs']['unread']), ['extra'])

    def test_no_evidence_value_in_the_output(self):
        text = json.dumps(self.result, default=sorted) + '\n'.join(
            unread_columns.report_lines('X', 'c', {'k': dict(self.result, status='ran')}))
        self.assertNotIn(PLANTED, text)
        self.assertNotIn('acct-12345', text)
        self.assertNotIn("'e'", text)

    def test_staged_store_and_sidecars_unchanged(self):
        staged = self.out / 'report' / 'data' / 'app' / 'databases'
        self.assertEqual(hashlib.sha256((staged / 'demo.db-wal').read_bytes()).hexdigest(), self.wal_hash)

    def test_lava_rows_are_read(self):
        self.assertEqual(self.result['artifacts']['demo_two']['lava_rows'], 3)


class TracerInProcess(unittest.TestCase):
    def test_a_failing_recorder_does_not_change_the_query(self):
        real = trace_sqlite._record

        def boom(*_a, **_k):
            raise RuntimeError('planted fault')

        trace_sqlite._record = boom
        before = trace_sqlite._STATE['callback_errors']
        try:
            db = trace_sqlite.traced_connect(':memory:')
            db.execute('CREATE TABLE t (a, b)')
            db.execute('INSERT INTO t VALUES (1, 2)')
            self.assertEqual(db.execute('SELECT a, b FROM t').fetchall(), [(1, 2)])
            db.close()
        finally:
            trace_sqlite._record = real
        self.assertGreater(trace_sqlite._STATE['callback_errors'], before)

    def test_a_refused_statement_is_still_refused(self):
        db = trace_sqlite.traced_connect(':memory:')
        with self.assertRaises(sqlite3.OperationalError):
            db.execute('SELECT nope FROM missing')
        db.close()


class TracerCases(unittest.TestCase):
    def reads(self, statements, setup):
        conn_count = len(trace_sqlite._STATE['connections'])
        db = trace_sqlite.traced_connect(':memory:')
        db.executescript(setup)
        for sql in statements:
            db.execute(sql).fetchall()
        db.close()
        conn = trace_sqlite._STATE['connections'][conn_count]
        return {column for reads in conn['reads'].values() for _s, _t, column in reads}

    def test_a_row_count_reads_no_column(self):
        self.assertEqual(self.reads(['SELECT count(*) FROM t'], 'CREATE TABLE t (id INTEGER PRIMARY KEY, a);'), {''})

    def test_a_rowid_read_names_the_column(self):
        self.assertIn('id', self.reads(['SELECT rowid FROM t'], 'CREATE TABLE t (id INTEGER PRIMARY KEY, a);'))

    @unittest.skipIf(sys.version_info < (3, 11), 'Python 3.10 reuses a statement even with cached_statements=0')
    def test_statement_cache_is_off(self):
        seen = []
        db = trace_sqlite.traced_connect(':memory:')
        db.execute('CREATE TABLE t (a)')
        db.set_authorizer(lambda *a: (seen.append(a[0]), sqlite3.SQLITE_OK)[1])
        for _ in range(2):
            db.execute('SELECT a FROM t').fetchall()
        db.close()
        self.assertEqual(seen.count(sqlite3.SQLITE_READ), 2)


class Settings(unittest.TestCase):
    def test_out_dir_default_and_override(self):
        sys.path.insert(0, str(HERE.parent))
        import kit_config
        self.assertEqual(kit_config.unread_columns({'work_dir': '/w'})['out_dir'], Path('/w/unread-columns'))
        self.assertEqual(kit_config.unread_columns({'work_dir': '/w', 'unread_columns': {'out_dir': '{work_dir}/u'}})
                         ['out_dir'], Path('/w/u'))
        with self.assertRaises(kit_config.ConfigError):
            kit_config.unread_columns({'work_dir': '/w', 'unread_columns': 'x'})


class Separation(unittest.TestCase):
    def test_review_gate_does_not_use_this_tool(self):
        gate = HERE.parent / 'review-gate'
        for path in gate.glob('*.py'):
            text = path.read_text(encoding='utf-8').lower()
            for name in ('unread_columns', 'unread-columns', 'trace_sqlite'):
                self.assertNotIn(name, text, path.name)

    def test_counts_hint(self):
        self.assertEqual(unread_columns.kind({'rows': 0, 'not_null': 0, 'distinct': 0, 'same_as': []}),
                         'table empty on this key')
        self.assertEqual(unread_columns.kind({'rows': 4, 'not_null': 2, 'distinct': 1, 'same_as': []}),
                         'one value where set')
        self.assertEqual(unread_columns.kind({'rows': 4, 'not_null': 4, 'distinct': 3, 'same_as': []}), 'varies')

    def test_store_label(self):
        labels = {}
        self.assertEqual(unread_columns.store_label(Path('/x/msgstore.db'), ('*/msgstore.db*',), labels), 'msgstore.db')
        self.assertEqual(unread_columns.store_label(Path('/x/u99.db'), ('*/*.db',), labels), 'store 1')
        self.assertEqual(unread_columns.store_label(Path('/x/u98.db'), ('*/*.db',), labels), 'store 2')
        self.assertEqual(unread_columns.store_label(Path('/x/u99.db'), ('*/*.db',), labels), 'store 1')


if __name__ == '__main__':
    unittest.main()
