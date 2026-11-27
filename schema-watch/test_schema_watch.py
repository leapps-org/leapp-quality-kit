"""Schema watch on synthetic stores. Nothing here reads the corpus or calls GitHub."""
import importlib.util
import json
import pathlib
import sqlite3
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('schema_watch', pathlib.Path(__file__).resolve().with_name('schema_watch.py'))
sw = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sw)

MODULE = '''
__artifacts_v2__ = {
    "a": {"name": "A", "paths": ('*/com.example/databases/app.db*', '*/com.example/shared_prefs/p.xml'),
          "sample_data": {"old_key": "1 rows"}},
}
QUERY = "SELECT title, created FROM notes"
'''


def make_db(path, tables, wal_table=None):
    """A SQLite file; with wal_table, that table exists only in the -wal, which must travel with the copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    for table, cols in tables.items():
        con.execute(f'CREATE TABLE {table} ({", ".join(cols)})')
    con.commit()
    if wal_table:
        con.execute('PRAGMA journal_mode=WAL')
        con.execute('PRAGMA wal_autocheckpoint=0')
        con.execute(f'CREATE TABLE {wal_table} (x)')
        con.commit()
        # Keep the -wal: copy the files while this connection still holds them open.
        return con
    con.close()
    return None


def make_tar(folder, out, members):
    with tarfile.open(out, 'w') as tf:
        for rel in members:
            tf.add(folder / rel, arcname=rel)


class Matching(unittest.TestCase):
    def test_star_crosses_folders_like_the_seekers(self):
        hits = sw.match_names(['*/com.example/databases/app.db*'],
                              ['data/data/com.example/databases/app.db', 'data/data/com.example/databases/app.db-wal',
                               'data/data/com.other/databases/app.db'])
        self.assertEqual(len(hits['*/com.example/databases/app.db*']), 2)

    def test_store_id_ignores_root_and_user(self):
        p = '*/com.example/databases/app.db*'
        self.assertEqual(sw.store_id(p, 'Dump/data/user/0/com.example/databases/app.db'),
                         sw.store_id(p, 'data/data/com.example/databases/app.db'))


class Normalise(unittest.TestCase):
    def test_internal_tables_dropped_and_hex_ids_folded(self):
        m = {'patterns': ['*/x.db'], 'keys': [], 'tokens': set()}
        idx = {'matches': {'*/x.db': ['a/x.db']}, 'stores': {'a/x.db': {'kind': 'sqlite', 'tables': {
            'sqlite_sequence': ['name'], 'android_metadata': ['locale'], 'ExoPlayerCacheIndexe00349fe3ed572f': ['id'],
            'notes': ['t']}}}}
        self.assertEqual(sorted(sw.module_stores(m, idx)['x.db']['tables']), ['ExoPlayerCacheIndex<hex>', 'notes'])


class Compare(unittest.TestCase):
    def setUp(self):
        self.module = sw.parse_module(MODULE)

    def test_unchanged_store_reports_nothing(self):
        s = {'com.example/databases/app.db': {'kind': 'sqlite', 'tables': {'notes': ['created', 'title']}}}
        found = sw.compare(self.module, s, {'old_key': s})
        self.assertFalse(sw.is_change(found))

    def test_added_and_gone_columns_and_source_tokens(self):
        old = {'com.example/databases/app.db': {'kind': 'sqlite', 'tables': {'notes': ['created', 'title', 'junk']}}}
        new = {'com.example/databases/app.db': {'kind': 'sqlite', 'tables': {'notes': ['title', 'body'], 'tags': ['t']}}}
        found = sw.compare(self.module, new, {'old_key': old})
        self.assertTrue(sw.is_change(found))
        c = found['stores'][0]
        self.assertEqual(c['tables_added'], ['tags'])
        self.assertEqual(c['columns_added'], {'notes': ['body']})
        self.assertEqual(c['columns_gone'], {'notes': [{'name': 'created', 'named_in_module': True},
                                                       {'name': 'junk', 'named_in_module': False}]})

    def test_a_column_only_one_vendor_has_is_not_missing(self):
        sid = 'com.example/databases/app.db'
        oem = {sid: {'kind': 'sqlite', 'tables': {'notes': ['title', 'created', 'oem_extra'], 'oem_t': ['x']}}}
        aosp = {sid: {'kind': 'sqlite', 'tables': {'notes': ['title', 'created']}}}
        new = {sid: {'kind': 'sqlite', 'tables': {'notes': ['title']}}}
        found = sw.compare(self.module, new, {'oem_key': oem, 'aosp_key': aosp})
        c = found['stores'][0]
        self.assertEqual(c['tables_gone'], [])
        self.assertEqual(c['columns_gone'], {'notes': [{'name': 'created', 'named_in_module': True}]})

    def test_a_missing_name_the_module_never_mentions_is_a_note_only(self):
        sid = 'com.example/databases/app.db'
        old = {sid: {'kind': 'sqlite', 'tables': {'notes': ['title', 'created'], 'vendor_t': ['x']}}}
        new = {sid: {'kind': 'sqlite', 'tables': {'notes': ['title', 'created']}}}
        found = sw.compare(self.module, new, {'old_key': old})
        self.assertEqual(found['stores'][0]['tables_gone'][0]['name'], 'vendor_t')
        self.assertFalse(sw.is_change(found))

    def test_dotted_preference_name_counts_as_named(self):
        self.assertTrue(sw.named('flutter.last_location', {'flutter', 'last_location'}))
        self.assertFalse(sw.named('flutter.is_traffic_on', {'flutter', 'last_location'}))

    def test_unreadable_store_is_not_compared_and_files_nothing(self):
        old = {'com.example/databases/app.db': {'kind': 'sqlite', 'tables': {'notes': ['title']}}}
        new = {'com.example/databases/app.db': {'kind': 'unreadable', 'error': 'sqlite: file is not a database'}}
        found = sw.compare(self.module, new, {'old_key': old})
        self.assertEqual(found['not_compared'][0]['store'], 'com.example/databases/app.db')
        self.assertFalse(sw.is_change(found))


class EndToEnd(unittest.TestCase):
    """Index two tar keys built from real SQLite files, then compare."""

    def test_wal_table_is_seen_and_difference_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            old = tmp / 'old'
            make_db(old / 'data/data/com.example/databases/app.db', {'notes': ['title', 'created']})
            prefs = old / 'data/data/com.example/shared_prefs/p.xml'
            prefs.parent.mkdir(parents=True)
            prefs.write_text('<map><string name="a">x</string><boolean name="b" value="true"/></map>')
            make_tar(old, tmp / 'old.tar', ['data/data/com.example/databases/app.db',
                                            'data/data/com.example/shared_prefs/p.xml'])
            new = tmp / 'new'
            db = new / 'data/data/com.example/databases/app.db'
            con = make_db(db, {'notes': ['title', 'created']}, wal_table='attachments')
            try:
                self.assertTrue(pathlib.Path(str(db) + '-wal').stat().st_size > 0)
                with zipfile.ZipFile(tmp / 'new.zip', 'w') as zf:
                    for suffix in ('', '-wal', '-shm'):
                        zf.write(str(db) + suffix, 'data/data/com.example/databases/app.db' + suffix)
                    zf.writestr('data/data/com.example/shared_prefs/p.xml', '<map><string name="a">x</string></map>')
            finally:
                con.close()
            module = sw.parse_module(MODULE)
            reg = {'old_key': {'file': str(tmp / 'old.tar'), 'sha256': 'o'},
                   'new_key': {'file': str(tmp / 'new.zip'), 'sha256': 'n'}}
            idx = {k: sw.index_key(k, reg[k], module['patterns'], tmp / 'cache', tmp / 'work') for k in reg}
            found = sw.compare(module, sw.module_stores(module, idx['new_key']),
                               {'old_key': sw.module_stores(module, idx['old_key'])})
            by_kind = {c['kind']: c for c in found['stores']}
            self.assertEqual(by_kind['sqlite']['tables_added'], ['attachments'])
            self.assertEqual(by_kind['prefs']['names_gone'], [{'name': 'b', 'named_in_module': False}])
            # The cache is reused and the evidence files were not changed by the reads.
            again = sw.index_key('old_key', reg['old_key'], module['patterns'], tmp / 'cache', tmp / 'work')
            self.assertEqual(again['stores'], idx['old_key']['stores'])
            self.assertEqual(list((tmp / 'work').iterdir()), [])

    def test_member_with_dotdot_is_not_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            with zipfile.ZipFile(tmp / 'k.zip', 'w') as zf:
                zf.writestr('../escape.db', b'x')
            got = sw.Source(tmp / 'k.zip').copy_out(['../escape.db'], tmp / 'out')
            self.assertEqual(got, {})
            self.assertFalse((tmp / 'escape.db').exists())


RESULT = {'key': 'new_key', 'module': 'a.py', 'status': 'changed', 'baseline_keys': ['old_key', 'secret_key'],
          'found': {'stores': [{'store': 's.db', 'kind': 'sqlite', 'tables_added': ['t'], 'tables_gone': [],
                                'columns_added': {}, 'columns_gone': {}}]}}


class Done:
    def __init__(self, stdout=b'[]', code=0):
        self.returncode, self.stdout, self.stderr = code, stdout, b''


class Filing(unittest.TestCase):
    def test_private_key_never_reaches_github(self):
        reg = {'new_key': {'public': False}, 'old_key': {'public': True}}
        with tempfile.TemporaryDirectory() as tmp, patch.object(sw, 'run', side_effect=AssertionError):
            q = pathlib.Path(tmp) / 'q.md'
            filed, private, problems = sw.file_issues('o', 'ALEAPP', 'abc', [RESULT], reg, q, False, log=lambda *_: None)
            self.assertEqual((filed, private, problems), ([], ['a.py'], []))
            self.assertIn('new_key', q.read_text())

    def test_public_key_dry_run_searches_and_files_nothing(self):
        reg = {'new_key': {'public': True}, 'old_key': {'public': True}, 'secret_key': {'public': False}}
        calls = []
        with patch.object(sw, 'run', side_effect=lambda cmd, **kw: calls.append(cmd) or Done()):
            sw.file_issues('o', 'ALEAPP', 'abc', [RESULT], reg, '/nonexistent/q.md', True, log=lambda *_: None)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('create', calls[0])

    def test_body_names_only_public_baseline_keys(self):
        reg = {'new_key': {'public': True}, 'old_key': {'public': True}, 'secret_key': {'public': False}}
        body = sw.issue_body('ALEAPP', 'abc', 'a.py', [RESULT], reg)
        self.assertIn('old_key', body)
        self.assertNotIn('secret_key', body)
        self.assertIn(sw.MARKER.format(module='a.py', key='new_key'), body)

    def test_key_already_in_an_open_issue_is_not_filed_again(self):
        reg = {'new_key': {'public': True}, 'old_key': {'public': True}}
        body = json.dumps([{'number': 1, 'body': sw.MARKER.format(module='a.py', key='new_key')}]).encode()
        calls = []
        with patch.object(sw, 'run', side_effect=lambda cmd, **kw: calls.append(cmd) or Done(body)):
            sw.file_issues('o', 'ALEAPP', 'abc', [RESULT], reg, '/nonexistent/q.md', False, log=lambda *_: None)
        self.assertFalse(any('create' in c for c in calls))


class State(unittest.TestCase):
    def test_new_and_recaptured_keys(self):
        reg = {'a': {'sha256': '1'}, 'b': {'sha256': '2'}, 'c': {'sha256': '3'}}
        self.assertEqual(sw.new_keys({'seen': {'a': '1', 'b': 'old'}}, reg), ['b', 'c'])


if __name__ == '__main__':
    unittest.main()
