import sqlite3
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import corpus_check as corpus

class CorpusTest(unittest.TestCase):
    def setUp(self):
        self.path = "scripts/artifacts/demo.py"
        self.packet = {"context":{"changed_paths":[self.path]},
                       "diff":f"diff --git a/{self.path} b/{self.path}\n+ history.db events stamp"}
        self.check = dict(path=self.path, assertion="column_has_null", basename="history.db",
                          table="events", column="stamp")

    def test_schema_and_null_from_wal_only(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            db = root / "history.db"
            con = sqlite3.connect(db)
            self.addCleanup(con.close)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA wal_autocheckpoint=0")
            con.execute("CREATE TABLE events(stamp INTEGER)")
            con.execute("INSERT INTO events VALUES(NULL)")
            con.commit()
            archive = root / "evidence.zip"
            with zipfile.ZipFile(archive,"w") as z:
                for suffix in ("","-wal","-shm"):
                    f = Path(str(db) + suffix)
                    z.write(f, "nested/" + f.name)
            before = archive.read_bytes()
            with patch.object(corpus, "archives", return_value=[archive]):
                self.assertIn("1 true, 0 false", corpus.verify("registry.json",[self.check],self.packet))
                con.execute("INSERT INTO events VALUES(5)")
                changed = dict(self.check,assertion="column_present",column="absent")
                self.packet["diff"] += " absent"
                self.assertIn("0 true, 1 false", corpus.verify("registry.json",[changed],self.packet))
            self.assertEqual(archive.read_bytes(),before)

    def test_outside_diff_and_queries_declined(self):
        for change in ({"column":"stamp FROM table"},{"path":"../../other"},{"basename":"../history.db"},
                       {"table":"unrelated"},{"assertion":"arbitrary_sql"}):
            self.assertFalse(corpus.applicable(dict(self.check, **change), self.packet))
