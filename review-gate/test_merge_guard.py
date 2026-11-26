"""The guard must catch every merge spelling and leave ordinary commands alone. Nothing here merges."""
import ast
import importlib.util
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = pathlib.Path(__file__).resolve().with_name('merge_guard.py')
SPEC = importlib.util.spec_from_file_location('merge_guard', PATH)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
CONFIG = {'github_owner': 'example-owner', 'owner_id': 1000,
          'greptile_repos': ['iLEAPP', 'ALEAPP', 'RLEAPP', 'VLEAPP'],
          'mac_review_repos': ['iLEAPP', 'ALEAPP', 'RLEAPP', 'VLEAPP', 'DLEAPP'],
          'owner_mac_review_repos': ['DLEAPP'],
          'app_review_repos': ['GLEAPP', 'MLEAPP', 'qnxprobe', 'dpapiprobe', 'sqlite-wal', 'cblite-parser'],
          'checks_only_repos': ['LAVA']}
guard.configure(CONFIG)
HEAD = 'a' * 40


class Done:
    def __init__(self, code, text):
        self.returncode, self.stdout, self.stderr = code, text, ''


def gate(code=0):
    text = f'ALEAPP #5 at {HEAD} (route: greptile)\n' + ('MAY MERGE' if code == 0 else 'BLOCKED\n- open finding')
    return patch.object(guard.subprocess, 'run', return_value=Done(code, text))


class Guard(unittest.TestCase):
    def test_blocked_gate_denies_every_spelling(self):
        spellings = [
            'gh pr merge 5 --repo example-owner/ALEAPP --merge',
            'gh pr merge 5 -R example-owner/ALEAPP',
            'gh pr merge --repo=example-owner/aleapp 5',
            'gh -R example-owner/ALEAPP pr merge 5' if False else 'gh pr --repo example-owner/ALEAPP merge 5',
            '/opt/homebrew/bin/gh pr merge 5 --repo example-owner/ALEAPP',
            'gh pr merge https://github.com/example-owner/ALEAPP/pull/5',
            'cd /tmp && gh pr merge 5 --repo example-owner/ALEAPP',
            'true; gh pr merge 5 --repo example-owner/ALEAPP',
            'GH_TOKEN=x gh pr merge 5 --repo example-owner/ALEAPP',
            'bash -c "gh pr merge 5 --repo example-owner/ALEAPP"',
            "bash <<'SH'\ngh pr merge 5 --repo example-owner/ALEAPP\nSH",
            'gh pr merge 5 \\\n  --repo example-owner/ALEAPP',
            'if true; then gh pr merge 5 --repo example-owner/ALEAPP; fi',
        ]
        with gate(1):
            for command in spellings:
                with self.subTest(command=command):
                    self.assertIn('did not pass', guard.check_command(command, '/tmp') or '')

    def test_pass_needs_the_head_it_checked(self):
        with gate(0):
            self.assertIn('--match-head-commit', guard.check_command('gh pr merge 5 --repo example-owner/ALEAPP', '/tmp'))
            self.assertIn('--match-head-commit', guard.check_command(
                f'gh pr merge 5 --repo example-owner/ALEAPP --match-head-commit {"b" * 40}', '/tmp'))
            self.assertIsNone(guard.check_command(
                f'gh pr merge 5 --repo example-owner/ALEAPP --merge --delete-branch --match-head-commit {HEAD}', '/tmp'))

    def test_bypass_routes_are_denied(self):
        for command in ('gh api -X PUT repos/example-owner/ALEAPP/pulls/5/merge',
                        "gh api graphql -f query='mutation{mergePullRequest(input:{}){clientMutationId}}'",
                        'gh pr merge 5 --repo example-owner/ALEAPP --auto',
                        'gh pr merge $N --repo example-owner/ALEAPP',
                        'gh pr merge 5 --repo $REPO'):
            with self.subTest(command=command), gate(0):
                self.assertTrue(guard.check_command(command, '/tmp'))

    def test_script_with_a_merge_is_denied(self):
        with tempfile.TemporaryDirectory() as folder:
            script = pathlib.Path(folder) / 'land.sh'
            script.write_text('gh pr merge 5 --repo example-owner/ALEAPP\n')
            for command in (f'bash {script}', f'/bin/bash -e {script}', f'sh "{script}"', f'cd /tmp && {script}',
                            f'source {script}', f'python3 -I {script}'):
                with self.subTest(command=command):
                    self.assertIn('contains a PR merge', guard.check_command(command, folder))
            for command in (f'cat {script}', f'grep -n merge {script}', f'cp {script} /tmp/x'):
                with self.subTest(command=command):
                    self.assertIsNone(guard.check_command(command, folder))

    def test_guard_covers_every_repo_the_gate_knows(self):
        import review_gate
        review_gate.configure(CONFIG)
        self.assertEqual(guard.REPOS, review_gate.KNOWN_REPOS)

    def test_probe_and_reader_merges_are_gated(self):
        for command in ('gh pr merge 6 --repo example-owner/dpapiprobe --merge',
                        'gh pr merge 146 --repo example-owner/qnxprobe',
                        'gh pr merge 3 --repo example-owner/SQLITE-WAL',
                        'gh pr merge https://github.com/example-owner/cblite-parser/pull/2'):
            with self.subTest(command=command), gate(1):
                self.assertIn('did not pass', guard.check_command(command, '/tmp') or '')
        self.assertTrue(guard.check_auto_merge({'enabled': True,
                                                'url': 'https://github.com/example-owner/qnxprobe/pull/1'}))

    def test_running_the_guard_itself_is_allowed(self):
        self.assertIsNone(guard.check_command(f'python3 -I -B {PATH}', '/tmp'))

    def test_other_repos_and_ordinary_commands_pass(self):
        for command in ('gh pr merge 5 --repo example-owner/appwalk --merge',
                        'gh pr merge 5 --repo other/ALEAPP',
                        'gh pr merge --help',
                        'gh pr view 5 --repo example-owner/ALEAPP',
                        'gh pr checks 5 --repo example-owner/ALEAPP',
                        'git merge main',
                        'echo "run gh pr merge later"',
                        'grep -rn "gh pr merge" docs',
                        f'python3 {guard.GATE} ALEAPP 5'):
            with self.subTest(command=command), patch.object(guard.subprocess, 'run', side_effect=AssertionError):
                self.assertIsNone(guard.check_command(command, '/tmp'))

    def test_gate_error_fails_closed(self):
        with patch.object(guard.subprocess, 'run', side_effect=OSError):
            self.assertIn('could not run', guard.check_command('gh pr merge 5 --repo example-owner/ALEAPP', '/tmp'))

    def test_auto_merge_tool(self):
        self.assertTrue(guard.check_auto_merge({'enabled': True, 'url': 'https://github.com/example-owner/iLEAPP/pull/1'}))
        self.assertIsNone(guard.check_auto_merge({'enabled': False, 'url': 'https://github.com/example-owner/iLEAPP/pull/1'}))
        self.assertIsNone(guard.check_auto_merge({'enabled': True, 'url': 'https://github.com/example-owner/appwalk/pull/1'}))


def run_main(payload, config):
    """Run the hook's main() on one payload with a given config dict; return what it printed."""
    out = io.StringIO()
    with patch.object(guard.kit_config, 'load', return_value=config), patch('sys.stdin', io.StringIO(json.dumps(payload))), \
            patch('sys.stdout', out), gate(1):
        guard.main()
    guard.configure(CONFIG)
    return out.getvalue()


class FailsClosed(unittest.TestCase):
    MERGE = {'tool_name': 'Bash', 'tool_input': {'command': 'gh pr merge 5 --repo example-owner/ALEAPP'}, 'cwd': '/tmp'}

    def test_a_missing_or_misspelled_repo_list_denies(self):
        for field in guard.REPO_LISTS:
            for broken in ({k: v for k, v in CONFIG.items() if k != field}, {**CONFIG, field: 'ALEAPP'}, {**CONFIG, field: [None]}):
                with self.subTest(field=field, broken=broken.get(field)):
                    self.assertIn('"deny"', run_main(self.MERGE, broken))

    def test_padded_or_listed_names_deny(self):
        for broken in ({**CONFIG, 'github_owner': 'example-owner '}, {**CONFIG, 'github_owner': ['example-owner']},
                       {**CONFIG, 'greptile_repos': [' ALEAPP', 'iLEAPP']}, {**CONFIG, 'greptile_repos': ['example-owner/ALEAPP']}):
            with self.subTest(broken=broken):
                self.assertIn('"deny"', run_main(self.MERGE, broken))

    def test_a_script_is_blocked_while_the_config_is_broken(self):
        broken = {k: v for k, v in CONFIG.items() if k != 'greptile_repos'}
        self.assertIn('"deny"', run_main({'tool_name': 'Bash', 'tool_input': {'command': './ship.sh'}}, broken))

    def test_no_repos_at_all_denies(self):
        empty = {**CONFIG, **{field: [] for field in guard.REPO_LISTS}}
        self.assertIn('"deny"', run_main(self.MERGE, empty))

    def test_a_broken_config_leaves_ordinary_commands_alone(self):
        broken = {k: v for k, v in CONFIG.items() if k != 'greptile_repos'}
        self.assertEqual(run_main({'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}, broken), '')

    def test_an_unimportable_kit_config_denies_merges(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = pathlib.Path(folder) / 'review-gate' / PATH.name
            copy.parent.mkdir()
            shutil.copy(PATH, copy)
            for payload, denied in ((self.MERGE, True), ({'tool_name': 'Bash', 'tool_input': {'command': 'ls'}}, False)):
                done = subprocess.run([sys.executable, '-I', '-B', str(copy)], input=json.dumps(payload), text=True,
                                      capture_output=True, check=False)
                with self.subTest(denied=denied):
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertEqual('"deny"' in done.stdout, denied)


class RepoOwners(unittest.TestCase):
    OWNED = {**CONFIG, 'app_review_repos': CONFIG['app_review_repos'] + ['quality-kit'],
             'repo_owners': {'quality-kit': 'example-org'}}

    def setUp(self):
        guard.configure(self.OWNED)

    def tearDown(self):
        guard.configure(CONFIG)

    def test_a_repo_with_its_own_owner_is_covered_only_there(self):
        self.assertEqual(guard.repo_name('example-org/quality-kit'), 'quality-kit')
        self.assertIsNone(guard.repo_name('example-owner/quality-kit'))
        self.assertIsNone(guard.repo_name('example-org/GLEAPP'))
        self.assertEqual(guard.repo_name('example-owner/GLEAPP'), 'GLEAPP')

    def test_its_merges_are_gated_in_every_spelling(self):
        for command in ('gh pr merge 2 --repo example-org/quality-kit', 'gh pr merge 2 --repo quality-kit',
                        'gh pr merge https://github.com/example-org/quality-kit/pull/2'):
            with self.subTest(command=command), gate(1):
                self.assertIn('did not pass', guard.check_command(command, '/tmp') or '')
        self.assertTrue(guard.check_auto_merge({'enabled': True, 'url': 'https://github.com/example-org/quality-kit/pull/2'}))

    def test_a_bad_owner_map_denies(self):
        for bad in ({'quality-kit': 'example org'}, [], '', 0, None, ['quality-kit']):
            with self.subTest(bad=bad):
                self.assertIn('"deny"', run_main(FailsClosed.MERGE, {**self.OWNED, 'repo_owners': bad}))

    def test_gate_and_guard_agree_on_a_mixed_case_key(self):
        import review_gate
        mixed = {**self.OWNED, 'repo_owners': {'Quality-Kit': 'example-org'}}
        guard.configure(mixed)
        review_gate.configure(mixed)
        try:
            self.assertEqual(guard.repo_name('example-org/quality-kit'), 'quality-kit')
            self.assertEqual(review_gate.owner_for('quality-kit'), 'example-org')
        finally:
            review_gate.configure(CONFIG)

    def test_the_gate_refuses_a_bad_owner_map(self):
        import review_gate
        with patch.object(review_gate.kit_config, 'load', return_value={**self.OWNED, 'repo_owners': []}):
            self.assertEqual(review_gate.main(['ALEAPP', '2']), 2)
        review_gate.configure(CONFIG)

    def test_the_gate_uses_the_same_owner(self):
        import review_gate
        review_gate.configure(self.OWNED)
        try:
            self.assertEqual(review_gate.owner_for('quality-kit'), 'example-org')
            self.assertEqual(review_gate.owner_for('ALEAPP'), 'example-owner')
            with patch.object(review_gate.kit_config, 'load', return_value=self.OWNED):
                self.assertEqual(review_gate.main(['example-owner/quality-kit', '2']), 2)
        finally:
            review_gate.configure(CONFIG)


class AppReviewerList(unittest.TestCase):
    def test_every_probe_and_reader_the_mac_reviewer_covers_is_in_the_config(self):
        """Optional: set "app_review_setup" to the reviewer's app_review.py to check its PROBES and READERS."""
        try:
            data = guard.kit_config.load()
        except guard.kit_config.ConfigError:
            self.skipTest('no kit config on this machine')
        setup = data.get('app_review_setup')
        if not setup or not guard.kit_config.expand(setup).is_file():
            self.skipTest('app_review_setup is not set or not on this machine')
        names = {}
        for node in ast.parse(guard.kit_config.expand(setup).read_text(encoding='utf-8')).body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in ('PROBES', 'READERS'):
                    names[node.targets[0].id] = set(ast.literal_eval(node.value))
        self.assertEqual(set(names), {'PROBES', 'READERS'})
        self.assertEqual(sorted((names['PROBES'] | names['READERS']) - set(data.get('app_review_repos') or [])), [])


if __name__ == '__main__':
    unittest.main()
