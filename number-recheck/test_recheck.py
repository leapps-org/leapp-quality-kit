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


class Owner(unittest.TestCase):
    def owner_used(self, config):
        """Run main() as far as the corpus check and return the owner it would file under."""
        seen = {}
        real = recheck.kit_config.need

        def need(data, name):
            value = real(data, name)
            seen.setdefault(name, value)
            return value
        with patch.object(recheck.kit_config, 'load', return_value=config), \
                patch.object(recheck.kit_config, 'need', side_effect=need), \
                patch('sys.argv', ['recheck.py', '--core', 'ALEAPP', '--file-issues', '--dry-run']):
            code = recheck.main()
        return code, seen

    BASE = {'github_owner': 'example-owner', 'repos_dir': '/nonexistent', 'corpus_roots': '/nonexistent/roots.json',
            'work_dir': '/nonexistent'}

    def test_a_bad_owner_map_cannot_check(self):
        code, _ = self.owner_used({**self.BASE, 'repo_owners': []})
        self.assertEqual(code, 2)

    def test_an_override_for_the_core_is_used(self):
        captured = {}
        with patch.object(recheck, 'file_issues', side_effect=lambda owner, *a: captured.setdefault('owner', owner)):
            code, seen = self.owner_used({**self.BASE, 'repo_owners': {'aleapp': 'example-org'}})
        self.assertEqual(code, 2)  # stops at the missing corpus, after the owner is resolved
        self.assertNotIn('github_owner', seen)


if __name__ == '__main__':
    unittest.main()
