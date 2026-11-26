"""The notes audit's folders and leak check follow the config. Nothing here reads a corpus."""
import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import kit_config  # noqa: E402



class Paths(unittest.TestCase):
    def test_defaults_sit_under_work_dir(self):
        paths = kit_config.notes_audit({'work_dir': '/w'})
        self.assertEqual(paths['input_dir'], pathlib.Path('/w/notes-audit-input'))
        self.assertEqual(paths['findings_dir'], '/w/notes-audit-findings/{date} round {round} ({core})')
        self.assertEqual(paths['findings_marker'], 'notes-audit-findings')
        self.assertEqual(paths['ledger'], HERE / 'audited.json')

    def test_a_findings_template_with_a_dated_folder(self):
        paths = kit_config.notes_audit({'work_dir': '/w', 'notes_audit': {
            'findings_dir': '/d/LEAPP review {date}/Notes audit round {round} ({core})', 'ledger': '/s/audited.json'}})
        self.assertEqual(paths['findings_marker'], 'LEAPP review')
        self.assertEqual(paths['ledger'], pathlib.Path('/s/audited.json'))

    def test_a_template_with_no_fixed_folder_is_refused(self):
        with self.assertRaises(kit_config.ConfigError):
            kit_config.notes_audit({'work_dir': '/w', 'notes_audit': {'findings_dir': '/{date}'}})


CONFIG = {'github_owner': 'example-owner', 'work_dir': '/w', 'repos_dir': '~/Documents/GitHub',
          'notes_audit': {'input_dir': '/d/leapp-notes-audit-input',
                          'findings_dir': '/d/LEAPP review {date}/Notes audit round {round} ({core})'}}


class Leaks(unittest.TestCase):
    def test_the_leak_check_flags_findings_checkouts_and_this_tool(self):
        leak = kit_config.notes_audit_leak(CONFIG)
        for text in ('cat ~/Downloads/LEAPP review 2026-11-26/x', 'ls ~/Downloads/LEAPP\\ review\\ 2026-11-26/',
                     'ls ~/Downloads/LEAPP*', 'ls ~/Documents/GitHub/ALEAPP/README.md',
                     'cat ~/.claude/workflows/leapp-notes-audit.js', 'cat ~/Tools/leapp-quality-kit/x',
                     'cat ~/Tools/leapp-review-gate/review_gate.py',
                     'curl https://github.com/example-owner/ALEAPP', 'curl https://api.github.com/repos/example-owner/ALEAPP',
                     'curl https://raw.githubusercontent.com/example-owner/ALEAPP/main/x', 'gh pr view 5'):
            with self.subTest(text=text):
                self.assertTrue(leak.search(text))

    def test_the_round_folder_itself_is_not_a_leak(self):
        leak = kit_config.notes_audit_leak(CONFIG)
        for text in ('cat /d/leapp-notes-audit-input/ALEAPP round 3/src/scripts/artifacts/a.py',
                     'sqlite3 run_output/key/_lava_artifacts.db',
                     'cd "/d/leapp-notes-audit-input/ALEAPP round 3/run_output/key"/ALEAPP*; ls data'):
            with self.subTest(text=text):
                self.assertIsNone(leak.search(text))

    def test_a_renamed_ledger_is_a_leak(self):
        leak = kit_config.notes_audit_leak({**CONFIG, 'notes_audit': {**CONFIG['notes_audit'], 'ledger': '/s/done-modules.json'}})
        self.assertTrue(leak.search('cat /s/done-modules.json'))

    def test_a_marker_that_names_the_input_folder_is_refused(self):
        with self.assertRaises(kit_config.ConfigError):
            kit_config.notes_audit({'work_dir': '/home/Downloads', 'notes_audit': {'findings_dir': '{work_dir}/{date} notes audit'}})


if __name__ == '__main__':
    unittest.main()
