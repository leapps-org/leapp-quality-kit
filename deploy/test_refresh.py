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

    def test_it_works_from_a_deleted_working_folder(self):
        gone = pathlib.Path(self.tmp.name) / 'gone'
        gone.mkdir()
        here = os.getcwd()
        os.chdir(gone)
        gone.rmdir()
        try:
            self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 0)
            self.commit('second')
            self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 0)
        finally:
            os.chdir(here)

    def test_a_git_failure_does_not_echo_the_remote(self):
        self.cfg['remote'] = 'https://someone:SECRET-TOKEN@127.0.0.1:9/none.git'
        code, message = refresh.refresh(self.cfg, sys.executable, passing)
        self.assertEqual(code, 2)
        self.assertNotIn('SECRET', message)

    def test_an_unopenable_releases_folder_cannot_check(self):
        self.cfg['releases'].parent.mkdir(parents=True, exist_ok=True)
        self.cfg['releases'].write_text('not a folder')
        self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 2)

    def test_a_held_lock_skips_the_run(self):
        import fcntl
        self.cfg['releases'].mkdir()
        with open(self.cfg['releases'] / '.lock', 'a+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 2)
        self.assertFalse(self.cfg['root'].exists())
        self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 0)  # released with the holder

    def test_a_test_that_changes_its_tree_is_not_promoted(self):
        def scribbling(folder, python):
            (folder / 'file.txt').write_text('changed by a test')
            (folder / refresh.MARKER).write_text('f' * 40 + '\n')
            return []
        tip = git(self.origin, 'rev-parse', 'HEAD')
        self.assertEqual(refresh.refresh(self.cfg, sys.executable, scribbling)[0], 0)
        self.assertEqual((self.cfg['root'] / 'file.txt').read_text(), 'first')
        self.assertEqual(self.deployed(), tip)

    def test_a_cleanup_failure_after_the_switch_still_reports_the_switch(self):
        refresh.refresh(self.cfg, sys.executable, passing)
        tip = self.commit('second')
        real = refresh.prune
        refresh.prune = lambda *a: (_ for _ in ()).throw(PermissionError('x'))
        try:
            code, message = refresh.refresh(self.cfg, sys.executable, passing)
        finally:
            refresh.prune = real
        self.assertEqual(code, 0)
        self.assertIn('switched', message)
        self.assertIn('cleanup', message)
        self.assertEqual(self.deployed(), tip)


    def test_old_releases_are_pruned_to_keep(self):
        for n in range(5):
            if n:
                self.commit(f'c{n}')
            self.assertEqual(refresh.refresh(self.cfg, sys.executable, passing)[0], 0)
        releases = [p for p in self.cfg['releases'].iterdir() if not p.name.startswith('.')]
        self.assertEqual(len(releases), 3)  # the current one and two previous
        self.assertIn(self.cfg['root'].resolve(), [p.resolve() for p in releases])


class Suites(unittest.TestCase):
    def test_a_missing_suite_is_a_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            failures = refresh.run_tests(pathlib.Path(folder), sys.executable)
        self.assertEqual(sorted(f.split(':')[0] for f in failures), sorted(refresh.SUITES))

    def test_a_suite_with_no_tests_is_a_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            for suite in refresh.SUITES:
                (root / suite).mkdir()
                (root / suite / 'test_x.py').write_text(
                    'import unittest\nclass T(unittest.TestCase):\n    def test(self):\n        pass\n')
            (root / refresh.SUITES[0] / 'test_x.py').write_text('# tests removed\n')
            failures = refresh.run_tests(root, sys.executable)
        # Python 3.12+ exits 5 when nothing ran; 3.10 and 3.11 exit 0, which the "Ran 0 tests" check catches.
        self.assertEqual(len(failures), 1)
        self.assertIn(failures[0], (f'{refresh.SUITES[0]}: no tests ran', f'{refresh.SUITES[0]}: exit 5'))

    def test_failures_carry_no_suite_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            for suite in refresh.SUITES:
                (root / suite).mkdir()
                (root / suite / 'test_x.py').write_text(
                    'import unittest\nclass T(unittest.TestCase):\n    def test(self):\n'
                    '        self.fail("SECRET-VALUE")\n')
            failures = refresh.run_tests(root, sys.executable)
        self.assertEqual(len(failures), len(refresh.SUITES))
        self.assertFalse(any('SECRET' in f for f in failures))

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

    def test_only_github_com_itself_is_looked_up(self):
        for remote in ('https://notgithub.com/o/r', 'https://github.com.example.net/o/r', '/local/path'):
            with self.subTest(remote=remote):
                warning, calls = self.check('b' * 40, 90, {'deploy': {'remote': remote}})
                self.assertIn('NOTE', warning)
                self.assertEqual(calls, [])
        for remote in ('https://github.com/o/r', 'https://github.com/o/r.git', 'git@github.com:o/r.git'):
            with self.subTest(remote=remote):
                self.assertIn('WARNING', self.check('b' * 40, 90, {'deploy': {'remote': remote}})[0])

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
