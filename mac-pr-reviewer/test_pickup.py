"""Check pickup across updates, offline periods and retries without a model or GitHub."""
import sys
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kit_test_config import start, use

if sys.platform == "win32":
    worker = None
else:
    import mac_review_worker as worker


@unittest.skipIf(sys.platform == "win32", "launchd worker uses POSIX file locking")
class PickupTest(unittest.TestCase):
    def setUp(self):
        use()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        start(self, patch.object(worker.pilot, "STATE", self.root))
        self.issues = start(self, patch.object(worker.pilot, "api", return_value=[]))
        self.pr = {"state": "open", "draft": False, "number": 11, "head": {"sha": "a"*40},
                   "user": {"login": "contributor", "id": 42}}
        start(self, patch.object(worker.pilot, "get_pr", return_value=self.pr))
        self.expected = start(self, patch.object(worker, "expected_workflows", return_value=set()))
        self.alert = start(self, patch.object(worker, "notify"))
        self.checks = start(self, patch.object(worker, "checks", return_value=[("flag", "SUCCESS", "Flag PR for Mac review", "")]))
        start(self, patch.object(worker, "review_packet",
                                       return_value={"context": {"delivery": "contributor"}}))
        self.model = start(self, patch.object(worker, "analyze", return_value={
            "summary": "private review", "public_comments": [], "corpus_checks": []}))

    def test_offline_pickup_update_and_idle(self):
        worker.run()
        self.model.assert_not_called()
        self.issues.return_value = [{"number": 11, "pull_request": {}}]
        self.checks.return_value = []
        worker.run()
        self.model.assert_not_called()
        self.checks.return_value = [("checks", "IN_PROGRESS", "Tests", "")]
        worker.run()
        self.model.assert_not_called()
        self.checks.return_value = [("checks", "SUCCESS", "Tests", "")]
        worker.run()
        worker.run()
        self.model.assert_called_once()
        self.issues.assert_called_with(
            worker.pilot.REPO + "/issues?state=open&labels=mac-review&per_page=100", pages=True)
        report, = (self.root / "reports").glob("*.md")
        self.assertEqual(report.read_text(), "private review")
        self.pr["head"]["sha"] = "b"*40
        worker.run()
        self.assertEqual(self.model.call_count, 2)
        self.assertEqual(len(list((self.root / "reports").glob("*.md"))), 2)

    def test_labeled_owner_pr_is_skipped_even_with_a_cached_result(self):
        self.pr["user"] = {"login": worker.pilot.OWNER_LOGIN.upper(), "id": 123}
        self.issues.return_value = [{"number": 11, "pull_request": {}}]
        worker.run()
        with worker.contextlib.closing(worker.pilot.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pickup").fetchone(), (0,))
            self.pr["head"]["sha"] = "b"*40
            with db:
                db.execute("INSERT INTO pickup VALUES(?,?,?)", (11, "b"*40, "stale result"))
        with patch.object(worker.pilot, "finish") as finish:
            worker.run()
            finish.assert_not_called()
        self.model.assert_not_called()
        self.checks.assert_not_called()
        self.alert.assert_not_called()
        self.assertFalse((self.root / "reports").exists())

    def test_flag_alone_cannot_finish_a_python_change(self):
        self.issues.return_value = [{"number": 11, "pull_request": {}}]
        self.expected.return_value = {"Python Lint Check"}
        worker.run()
        self.model.assert_not_called()


    def test_closed_and_draft_never_start_model(self):
        self.issues.return_value = [{"number": 11, "pull_request": {}}]
        self.pr["draft"] = True
        worker.run()
        self.pr.update(draft=False, state="closed")
        worker.run()
        self.model.assert_not_called()

    def test_delivery_retry_uses_saved_result(self):
        self.issues.return_value = [{"number": 11, "pull_request": {}}]
        with patch.object(worker.pilot, "finish", side_effect=OSError("offline")):
            worker.run()
        with patch.object(worker.time, "time", return_value=10**12):
            worker.run()
        self.model.assert_called_once()
        self.assertEqual(len(list((self.root / "reports").glob("*.md"))), 1)


@unittest.skipIf(sys.platform == "win32", "macOS worker uses system Ruby for YAML")
class WorkflowFilterTest(unittest.TestCase):
    def test_absent_workflow_directory_requires_a_complete_tree(self):
        error = subprocess.CalledProcessError(1, ["gh", "api"], stderr="gh: Not Found (HTTP 404)")
        pr = {"number": 1, "head": {"sha": "a" * 40}}
        for truncated, entries, missing in [(False, [], True), (True, [], False),
                                             (False, [{"path": ".github/workflows"}], False)]:
            with self.subTest(truncated=truncated, entries=entries), \
                    patch.object(worker, "ci_skipped", return_value=False), \
                    patch.object(worker.pilot, "api", side_effect=[
                        [], error, {"truncated": truncated, "tree": entries}]), \
                    patch.object(worker.pilot, "checked_pr"):
                if missing:
                    self.assertEqual(worker.expected_workflows(pr), set())
                else:
                    with self.assertRaises(subprocess.CalledProcessError):
                        worker.expected_workflows(pr)

    def test_python_change_waits_for_repository_lint(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        # Synthetic workflows shaped like a LEAPP repository's: one path-filtered lint job,
        # one job that a Python change must not wait for.
        (folder / "lint.yml").write_text(
            "name: Python Lint Check\non:\n  pull_request:\n    paths:\n      - '**.py'\n"
            "jobs: {}\n")
        (folder / "docs.yml").write_text(
            "name: Docs\non:\n  pull_request:\n    paths:\n      - 'docs/**'\njobs: {}\n")
        pr = {"number": 11, "head": {"sha": "a"*40}, "base": {"ref": "main"}}

        def contents(url, **_kwargs):
            if "/git/commits/" in url:
                return {"message": "update workflow"}
            if "/pulls/11/files?" in url:
                return [{"filename": "scripts/artifacts/example.py"}]
            if "/contents/.github/workflows?" in url:
                return [{"name": f.name} for f in folder.glob("*.yml")]
            return (folder / url.split("?")[0].rsplit("/", 1)[1]).read_text()

        with patch.object(worker.pilot, "api", side_effect=contents), patch.object(worker.pilot, "checked_pr"):
            self.assertEqual(worker.expected_workflows(pr), {"Python Lint Check"})


if __name__ == "__main__":
    unittest.main()
