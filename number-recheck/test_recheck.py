"""Issue filing takes its owner from the caller and files nothing in a dry run. Nothing here calls GitHub."""
import importlib.util
import json
import pathlib
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('recheck', pathlib.Path(__file__).resolve().with_name('recheck.py'))
recheck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recheck)


class Done:
    def __init__(self, stdout='[]', code=0):
        self.returncode, self.stdout, self.stderr = code, stdout, ''


DRIFT = {'module': 'a.py', 'artifact': 'a', 'key': 'k1', 'declared': '8', 'actual': '1'}


class FileIssues(unittest.TestCase):
    def test_dry_run_searches_the_owners_repo_and_files_nothing(self):
        calls = []
        with patch.object(recheck, 'run', side_effect=lambda cmd, **kw: calls.append(cmd) or Done()):
            filed = recheck.file_issues('example-owner', 'ALEAPP', 'abc', [{'status': 'drifted', 'drifts': [DRIFT]}], True)
        self.assertEqual(filed, [])
        self.assertEqual(len(calls), 1)
        self.assertIn('example-owner/ALEAPP', calls[0])
        self.assertNotIn('create', calls[0])

    def test_an_open_issue_with_the_marker_is_not_filed_again(self):
        body = json.dumps([{'number': 1, 'body': recheck.MARKER.format(module='a.py')}])
        calls = []
        with patch.object(recheck, 'run', side_effect=lambda cmd, **kw: calls.append(cmd) or Done(body)):
            recheck.file_issues('example-owner', 'ALEAPP', 'abc', [{'status': 'drifted', 'drifts': [DRIFT]}], False)
        self.assertFalse(any('create' in cmd for cmd in calls))

    def test_drift_on_an_untrusted_key_alone_files_nothing(self):
        with patch.object(recheck, 'run', side_effect=AssertionError):
            recheck.file_issues('example-owner', 'ALEAPP', 'abc', [{'status': 'could not check', 'drifts': [DRIFT]}], False)


if __name__ == '__main__':
    unittest.main()
