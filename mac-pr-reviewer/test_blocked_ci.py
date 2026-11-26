"""Terminal, jobless fork runs must belong to this PR before they count."""
import unittest
from unittest.mock import patch
from kit_test_config import start
import mac_review_worker as worker


class BlockedCiTest(unittest.TestCase):
    def setUp(self):
        self.source = {"id": 44, "fork": True, "owner": {"login": "contributor"},
                       "full_name": "contributor/example"}
        self.pr = {"number": 9, "created_at": "2026-01-01T00:00:00Z",
                   "head": {"repo": self.source, "ref": "change", "sha": "b" * 40}}
        self.candidates = [self.pr]
        self.run = {"id": 1, "name": "Tests", "workflow_id": 17,
                    "created_at":"2026-01-02T00:00:00Z", "head_repository":{"id":44},
                    "head_sha":"b"*40, "head_branch":"change", "pull_requests":[],
                    "status":"completed", "conclusion":"action_required", "html_url":"test"}
        start(self, patch.object(worker.pilot, "api", side_effect=self.api))

    def api(self, path, **kwargs):
        if "/pulls?" in path:
            return self.candidates
        return {"total_count":1, "workflow_runs":[self.run]}

    def test_terminal_jobless_run_records_limit(self):
        runs = worker.fork_runs_without_check_jobs(self.pr, [])
        self.assertIn("no test execution", runs[0][1])
        self.assertEqual(self.pr["_blocked_run_time"], self.run["created_at"])

    def test_shared_branch_or_pending_run_is_not_evidence(self):
        self.candidates.append(dict(self.pr, number=11))
        self.assertFalse(worker.fork_runs_without_check_jobs(self.pr, []))
        self.candidates.pop()
        self.run["status"] = "queued"
        self.assertFalse(worker.fork_runs_without_check_jobs(self.pr, []))
        self.run.update(status="completed", pull_requests=[{"number":11}])
        self.assertFalse(worker.fork_runs_without_check_jobs(self.pr, []))
