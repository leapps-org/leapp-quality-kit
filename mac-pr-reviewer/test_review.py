"""Exercise queue, fixture context, delivery routing and safe retries with synthetic PRs."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import review_leapp_pr as pilot
from kit_test_config import OWNER_LOGIN, start, use


class PilotTest(unittest.TestCase):
    project = "ALEAPP"

    def setUp(self):
        use(self.project)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        start(self, patch.object(pilot, "STATE", Path(tmp.name)))
        self.db = pilot.database()
        self.addCleanup(self.db.close)
        self.head = "a" * 40
        self.pr = {"number": 1, "user": {"id": 42, "login": "contributor"},
                   "state": "open", "draft": False, "head": {"sha": self.head},
                   "base": {"sha": "b" * 40, "repo": {"full_name": pilot.FULL_NAME}},
                   "changed_files": 1}
        self.reviews = []
        self.posts = []
        self.files = [{"status": "added", "patch": "@@ -0,0 +1 @@\n+test", "filename": "scripts/artifacts/demo.py"}]
        start(self, patch.object(pilot, "api", side_effect=self.api))
        self.comments = [{"path": self.files[0]["filename"], "line": 1, "body": "A test finding."}]

    def api(self, path, *, pages=False, body=None, raw=False):
        if pages:
            self.assertIn("per_page=100", path)
        if raw:
            self.assertIn("/contents/", path)
        if body is not None:
            self.posts.append(body)
            self.reviews.append(dict(body, user={"id": 27}))
            return {}
        if path == "user":
            return {"id": 27}
        if "/files?" in path:
            return self.files
        if "/reviews?" in path:
            return self.reviews
        if "/pulls?" in path:
            return [self.pr]
        if path.endswith("/pulls/1"):
            return self.pr
        if "/contents/admin?" in path:
            return [{"name":"image_manifest.json"}]
        if "image_manifest.json" in path:
            return json.dumps({"images": [{"sample_data_key": "public-image"}]})
        if "/contents/scripts/artifacts/demo.py" in path:
            return "__artifacts_v2__ = {'demo': {'sample_data': {'public-image': '1 row'}}}"
        raise AssertionError(path)

    def test_contributor_end_to_end(self):
        self.assertEqual(pilot.queue(self.db)[0]["delivery"], "contributor")
        self.assertEqual(pilot.context(1)["manifest_matches"], {"demo": ["public-image"]})
        pilot.finish(self.db, 1, self.head, "private notes", self.comments)
        report = pilot.STATE / "reports" / f"PR-1-{self.head}.md"
        self.assertIn("private notes", report.read_text())
        self.assertIn("A test finding.", report.read_text())
        self.assertEqual(report.stat().st_mode & 0o077, 0)
        self.assertEqual(self.posts[0]["comments"][0]["side"], "RIGHT")
        self.assertEqual(self.posts[0]["comments"][0]["body"],
                         "~~~text\nFinding: A test finding.\n~~~\n\n_Automated review by OpenAI Codex._")
        self.assertNotIn("private notes", json.dumps(self.posts))
        self.assertEqual(pilot.queue(self.db), [])
        self.db.execute("DELETE FROM reviews")
        self.db.commit()
        pilot.finish(self.db, 1, self.head, "retry after interruption", self.comments)
        self.assertEqual(len(self.posts), 1)
        self.pr["head"]["sha"] = "c" * 40
        self.assertEqual(len(pilot.queue(self.db)), 1)

    def test_owner_end_to_end(self):
        skip_authors = [{"id": 99, "login": OWNER_LOGIN.upper()}]
        if not pilot.OWNER_REVIEWED:
            skip_authors.append({"id": pilot.OWNER, "login": "renamed"})
        for author in skip_authors:
            with self.subTest(author=author):
                self.pr["user"] = author
                self.assertEqual(pilot.queue(self.db), [])
                self.assertEqual(pilot.context(1), {"number": 1, "head": self.head, "delivery": "skip"})
                for comments in ([], self.comments):
                    self.assertEqual(pilot.finish(self.db, 1, self.head, "notes", comments),
                                     "Skipped owner PR")
        self.assertFalse((pilot.STATE / "reports").exists())
        self.assertEqual(self.posts, [])
        self.assertIsNone(self.db.execute("SELECT result FROM reviews").fetchone())

    def check_owner_inline_findings_are_public_and_summary_stays_private(self):
        self.pr["user"] = {"id": pilot.OWNER, "login": "renamed-maintainer"}
        self.assertEqual(pilot.queue(self.db)[0]["delivery"], "private")
        self.assertEqual(pilot.context(1)["delivery"], "private")
        self.assertEqual(pilot.finish(self.db, 1, self.head, "private notes", self.comments),
                         "Recorded private review")
        report = pilot.STATE / "reports" / f"PR-1-{self.head}.md"
        self.assertIn("private notes", report.read_text())
        self.assertIn("## Findings posted on this revision", report.read_text())
        self.assertIn("A test finding.", report.read_text())
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["event"], "COMMENT")
        self.assertIn("Automated DLEAPP review.", self.posts[0]["body"])
        self.assertIn("A test finding.", self.posts[0]["comments"][0]["body"])
        self.assertNotIn("private notes", json.dumps(self.posts))
        self.assertEqual(pilot.finish(self.db, 1, self.head, "private notes", self.comments),
                         "Already delivered")
        self.assertEqual(pilot.queue(self.db), [])
        self.db.execute("DELETE FROM reviews")
        self.db.commit()
        pilot.finish(self.db, 1, self.head, "retry after interruption", self.comments)
        self.assertEqual(len(self.posts), 1)

    def check_owner_finding_off_the_diff_is_not_posted(self):
        self.pr["user"] = {"id": pilot.OWNER, "login": "renamed-maintainer"}
        pilot.finish(self.db, 1, self.head, "private notes",
                     [{"path": "scripts/artifacts/other.py", "line": 9, "body": "Off the diff."}])
        self.assertEqual(self.posts, [])
        report = pilot.STATE / "reports" / f"PR-1-{self.head}.md"
        self.assertIn("Withheld 1 inline finding", report.read_text())

    def test_stale_draft_and_closed(self):
        self.pr["draft"] = True
        self.assertEqual(pilot.queue(self.db), [])
        for state, draft, sha in [("open", True, self.head), ("closed", False, self.head),
                                  ("open", False, "c" * 40)]:
            self.pr.update(state=state, draft=draft)
            self.pr["head"]["sha"] = sha
            with self.assertRaises(ValueError):
                pilot.finish(self.db, 1, self.head, "private notes", self.comments)
        self.assertFalse(self.posts)

    def test_touched_case_is_not_a_verified_run(self):
        self.files.append({"status": "modified", "filename": "admin/test/cases/testdata.demo.json"})
        self.pr["changed_files"] = 2
        self.assertEqual(pilot.context(1)["test_files_touched"], ["demo"])
        self.assertFalse(pilot.context(1)["manifest_matches"])


class OwnerReviewedTest(PilotTest):
    """A repository in owner_mac_review_repos: the owner's PRs are reviewed, findings posted."""
    project = "DLEAPP"

    def test_owner_inline_findings_are_public_and_summary_stays_private(self):
        self.check_owner_inline_findings_are_public_and_summary_stays_private()

    def test_owner_finding_off_the_diff_is_not_posted(self):
        self.check_owner_finding_off_the_diff_is_not_posted()


if __name__ == "__main__":
    unittest.main()
