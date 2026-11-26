from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import corpus_check
import kit_test_config  # noqa: F401  (placeholder settings)
import mac_review_worker as worker

class PrivateCorpusRouting(unittest.TestCase):
    def test_a_private_observation_reaches_report_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "history.db"
            with closing(sqlite3.connect(source)) as con:
                con.execute("CREATE TABLE visits(visit_time)")
                con.execute("INSERT INTO visits VALUES(NULL)")
                con.commit()
            archive = root / "sample.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.write(source, "user/history.db")
            path = "scripts/artifacts/demo.py"
            request = dict(path=path, assertion="column_has_null", basename="history.db",
                           table="visits", column="visit_time")
            summary = "Unit test review."
            packet = {"context": {"changed_paths":[path]}, "diff":
                      f"diff --git a/{path} b/{path}\n+ history.db visits visit_time"}
            pr = {"state":"open","draft":False,"number":21,"head":{"sha":"c"*40},
                  "user":{"id":12,"login":"contributor"}}
            public = {"path":path,"line":7,"body":"Shareable test finding."}
            posted = []
            def api(url, *, body=None, **kwargs):
                if body is not None:
                    posted.append(body)
                    return {}
                if "/files?" in url:
                    return [{"filename":path, "patch":"@@ -6,0 +7 @@\n+line"}]
                if url=="user":
                    return {"id":42}
                if "/issues?" in url:
                    return [{"pull_request":{},"number":21}]
                return []
            def check(registry, checks, evidence):
                with patch.object(corpus_check, "archives", return_value=[archive]):
                    return corpus_check.verify(registry, checks, evidence)
            with (patch.object(worker.pilot,"STATE",root/"review"),
                  patch.object(worker.pilot,"api",side_effect=api),
                  patch.object(worker.pilot,"get_pr",return_value=pr),
                  patch.object(worker,"checks",return_value=[("flag","SUCCESS","flag","")]),
                  patch.object(worker,"expected_workflows",return_value=set()),
                  patch.object(worker,"notify"), patch.object(worker,"review_packet",return_value=packet),
                  patch.object(worker,"analyze",return_value={"summary":summary,
                               "public_comments":[public],"corpus_checks":[request]}),
                  patch.object(corpus_check,"run_checks",side_effect=check)):
                worker.run()
                worker.run()
                self.assertEqual(len(posted),1)
                self.assertNotIn("1 true",json.dumps(posted))
                self.assertNotIn("history.db",json.dumps(posted))
                report, = (worker.pilot.STATE/"reports").glob("*.md")
                self.assertIn("1 true, 0 false",report.read_text())
                self.assertIn(public["body"],report.read_text())
