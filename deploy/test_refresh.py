"""The refresh switches only to a descendant tip whose tests pass, and leaves the deployed copy alone otherwise.
Uses a throwaway local repository as the remote. Nothing here touches the network or a real deployment."""
import datetime
import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('refresh', HERE / 'refresh.py')
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)
sys.path.insert(0, str(HERE.parent / 'review-gate'))
import review_gate  # noqa: E402


def git(cwd, *args):
    env = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}
    return subprocess.run(['git', *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def passing(folder, python):
    return []


def failing(folder, python):
    return ['review-gate: exit 1: planted failure']


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.origin = base / 'origin'
        self.origin.mkdir()
        git(self.origin, 'init', '-q', '-b', 'main')
        self.commit('first')
        self.cfg = {'root': base / 'deployed', 'releases': base / 'releases', 'remote': str(self.origin),
                    'branch': 'main', 'keep': 2, 'work_dir': base / 'work'}

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, text):
        (self.origin / 'file.txt').write_text(text)
        git(self.origin, 'add', 'file.txt')
        git(self.origin, 'commit', '-q', '-m', text)
        return git(self.origin, 'rev-parse', 'HEAD')

    def deployed(self):
        return (self.cfg['root'] / refresh.MARKER).read_text().split()[0]

    def test_first_run_deploys_and_a_second_does_nothing(self):
        tip = git(self.origin, 'rev-parse', 'HEAD')
        self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 0)
        self.assertTrue(self.cfg['root'].is_symlink())
        self.assertEqual(self.deployed(), tip)
        self.assertEqual((self.cfg['root'] / 'file.txt').read_text(), 'first')
        code, message = refresh.refresh(self.cfg, sys.executable, passing)
        self.assertEqual((code, message.split()[0]), (0, 'up'))

    def test_failing_tests_keep_the_old_commit(self):
        refresh.refresh(self.cfg, sys.executable, passing)
        old = self.deployed()
        self.commit('second')
        code, message = refresh.refresh(self.cfg, sys.executable, failing)
        self.assertEqual(code, 1)
        self.assertIn('failed its tests', message)
        self.assertEqual(self.deployed(), old)
        self.assertFalse(any(p.name.startswith('.staging') for p in self.cfg['releases'].iterdir()))

    def test_passing_tests_switch_to_the_tip(self):
        refresh.refresh(self.cfg, sys.executable, passing)
        tip = self.commit('second')
        self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 0)
        self.assertEqual(self.deployed(), tip)
        self.assertEqual((self.cfg['root'] / 'file.txt').read_text(), 'second')

    def test_a_rewritten_history_is_refused(self):
        refresh.refresh(self.cfg, sys.executable, passing)
        old = self.deployed()
        git(self.origin, 'checkout', '-q', '--orphan', 'other')
        self.commit('unrelated')
        git(self.origin, 'branch', '-q', '-f', 'main', 'other')
        code, message = refresh.refresh(self.cfg, sys.executable, passing)
        self.assertEqual(code, 1)
        self.assertIn('does not descend', message)
        self.assertEqual(self.deployed(), old)

    def test_a_real_folder_at_root_is_left_alone(self):
        self.cfg['root'].mkdir()
        (self.cfg['root'] / 'keep.txt').write_text('x')
        code, message = refresh.refresh(self.cfg, sys.executable, passing)
        self.assertEqual(code, 1)
        self.assertIn('real folder', message)
        self.assertTrue((self.cfg['root'] / 'keep.txt').is_file())

    def test_an_unreachable_remote_cannot_check_and_changes_nothing(self):
        refresh.refresh(self.cfg, sys.executable, passing)
        old = self.deployed()
        self.cfg['remote'] = str(self.origin.parent / 'missing')
        self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 2)
        self.assertEqual(self.deployed(), old)

    def test_a_held_lock_skips_the_run(self):
        self.cfg['releases'].mkdir()
        (self.cfg['releases'] / '.lock').mkdir()
        self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 2)
        self.assertFalse(self.cfg['root'].exists())

    def test_old_releases_are_pruned_to_keep(self):
        for n in range(5):
            if n:
                self.commit(f'c{n}')
            self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 0)
        releases = [p for p in self.cfg['releases'].iterdir() if not p.name.startswith('.')]
        self.assertEqual(len(releases), 3)  # the current one and two previous
        self.assertIn(self.cfg['root'].resolve(), [p.resolve() for p in releases])


class Lag(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 26, 12, 0, tzinfo=datetime.timezone.utc)
    DEPLOY = {'deploy': {'remote': 'https://github.com/example-org/leapp-quality-kit', 'branch': 'main'}}

    def check(self, tip_sha, minutes_ago, data=None):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / 'DEPLOYED_COMMIT').write_text('a' * 40 + ' 2026-11-26T00:00:00+00:00\n')
            when = (self.NOW - datetime.timedelta(minutes=minutes_ago)).strftime('%Y-%m-%dT%H:%M:%SZ')
            calls = []

            def read(*args):
                calls.append(args)
                return {'sha': tip_sha, 'commit': {'committer': {'date': when}}}
            return review_gate.deploy_lag(data or self.DEPLOY, root, self.NOW, read), calls

    def test_current_or_recent_is_quiet(self):
        self.assertIsNone(self.check('a' * 40, 300)[0])
        self.assertIsNone(self.check('b' * 40, 30)[0])

    def test_behind_for_over_an_hour_warns(self):
        warning, calls = self.check('b' * 40, 90)
        self.assertIn('WARNING', warning)
        self.assertIn('repos/example-org/leapp-quality-kit/commits/main', calls[0][1])

    def test_no_marker_or_no_deploy_block_is_quiet(self):
        self.assertIsNone(review_gate.deploy_lag(self.DEPLOY, pathlib.Path('/nonexistent')))
        self.assertIsNone(self.check('b' * 40, 90, {'other': 1})[0])

    def test_a_failed_lookup_is_a_note_not_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / 'DEPLOYED_COMMIT').write_text('a' * 40 + '\n')

            def broken(*args):
                raise review_gate.CouldNotCheck('offline')
            self.assertIn('NOTE', review_gate.deploy_lag(self.DEPLOY, root, self.NOW, broken))


if __name__ == '__main__':
    unittest.main()
