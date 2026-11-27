"""Auditor spot-check: selection, planting, measurement and scoring, on made-up fixtures only."""
import contextlib
import io
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import kit_config  # noqa: E402
import spotcheck  # noqa: E402

MODULE = '''__artifacts_v2__ = {
    "thing_events": {
        "name": "Thing Events",
        "description": "Events.",
        "author": "@someone",
        "notes": ("One row per event. "
                  "Times are UTC."),
        "paths": ("*/thing.db",),
        "output_types": "standard",
    },
    "thing_users": {
        "name": "Thing Users",
        "notes": """Users of the app.
""",
        "paths": ("*/users.db",),
    },
    "thing_fmt": {
        "name": "Thing Fmt",
        "notes": f"Built {1 + 1}",
    },
}


def thing_events(context):
    return (), [], ''
'''


def make_run(root, key, rows):
    """A run output folder like run_corpus.sh writes: <key>/<report>/_lava_artifacts.db and _lava_data.lava."""
    report = root / key / 'ALEAPP_Output_x'
    report.mkdir(parents=True)
    con = sqlite3.connect(report / '_lava_artifacts.db')
    con.execute('create table thing_events (event_time INTEGER, kind TEXT, label TEXT)')
    con.executemany('insert into thing_events values (?, ?, ?)', rows)
    con.commit()
    con.close()
    meta = spotcheck.meta_of(MODULE)
    lava = {'artifacts': {'Things': [{'artifact_key': 'thing_events', 'module': 'thing', 'tablename': 'thing_events',
                                       'column_map': {'event_time': 'Event Time', 'kind': 'Kind', 'label': 'Label'}}]},
            'meta': {'modules': [{'module_name': 'thing', 'artifacts': [
                {'artifact_key': 'thing_events', 'tablename': 'thing_events', 'notes': meta['thing_events']['notes']},
                {'artifact_key': 'thing_users', 'tablename': 'thing_users', 'notes': meta['thing_users']['notes']}]}]}}
    (report / '_lava_data.lava').write_text(json.dumps(lava, indent=4), encoding='utf-8')
    return root / key


ROWS_A = [(1, 'open', 'alpha'), (2, 'close', ''), (3, 'open', None)]
ROWS_B = [(4, 'open', 'beta')]


class Selection(unittest.TestCase):
    def test_same_answer_every_time(self):
        self.assertEqual(spotcheck.chosen('ALEAPP', 'kwai', 'abc', 10), spotcheck.chosen('aleapp', 'kwai', 'abc', 10))

    def test_about_one_in_ten(self):
        hits = sum(spotcheck.chosen('ALEAPP', f'module{i}', 'abc123', 10) for i in range(2000))
        self.assertTrue(150 <= hits <= 250, hits)

    def test_every_one_always_chooses(self):
        self.assertTrue(all(spotcheck.chosen('ALEAPP', f'm{i}', 'c', 1) for i in range(20)))


class Planting(unittest.TestCase):
    def test_implicitly_joined_literal(self):
        out = spotcheck.plant_in_source(MODULE, 'thing_events', 'The Kind column is filled on every row.')
        self.assertEqual(spotcheck.meta_of(out)['thing_events']['notes'],
                         'One row per event. Times are UTC. The Kind column is filled on every row.')
        self.assertEqual(out.count('The Kind column'), 1)

    def test_triple_quoted_literal(self):
        out = spotcheck.plant_in_source(MODULE, 'thing_users', 'Planted here.')
        self.assertEqual(spotcheck.meta_of(out)['thing_users']['notes'], 'Users of the app.\n Planted here.')

    def test_refuses_an_f_string(self):
        with self.assertRaises(ValueError):
            spotcheck.plant_in_source(MODULE, 'thing_fmt', 'Planted.')

    def test_refuses_a_missing_artifact(self):
        with self.assertRaises(ValueError):
            spotcheck.plant_in_source(MODULE, 'nothing', 'Planted.')

    def test_refuses_a_sentence_that_needs_escaping(self):
        for bad in ('It is "quoted".', "It's here.", 'Back\\slash.', 'Brace {x}.'):
            with self.assertRaises(ValueError):
                spotcheck.plant_in_source(MODULE, 'thing_events', bad)

    def test_non_ascii_before_the_notes(self):
        source = MODULE.replace('"Events."', '"Événements, ü."')
        out = spotcheck.plant_in_source(source, 'thing_events', 'Planted.')
        self.assertTrue(spotcheck.meta_of(out)['thing_events']['notes'].endswith('UTC. Planted.'))


class Measuring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.runs = {'key_a': make_run(root, 'key_a', ROWS_A), 'key_b': make_run(root, 'key_b', ROWS_B)}
        self.found = spotcheck.survey('thing', self.runs, MODULE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_measures_rows_and_empties(self):
        keys = self.found['thing_events']['keys']
        self.assertEqual(keys['key_a']['rows'], 3)
        self.assertEqual(keys['key_a']['headers']['Label'], {'empty': 2, 'filled': 1, 'numeric': 0})
        self.assertNotIn('thing_users', [a for a, v in self.found.items() if v['keys']])

    def test_every_count_sentence_is_false(self):
        for sentence, fact in spotcheck.candidates('thing_events', self.found['thing_events'], 'count'):
            real = self.found['thing_events']['keys'][fact['key']]['rows']
            self.assertNotEqual(fact['stated'], real)
            self.assertIn(f"{fact['stated']:,} rows", sentence)

    def test_always_only_over_a_real_exception(self):
        sentences = [s for s, _ in spotcheck.candidates('thing_events', self.found['thing_events'], 'always')]
        self.assertEqual(sentences, ['The Label column is filled on every row.'])

    def test_meaning_skips_numbers_and_time_headers(self):
        sentences = [s for s, _ in spotcheck.candidates('thing_events', self.found['thing_events'], 'meaning')]
        self.assertEqual(sentences, ['The Kind column holds the time the item was deleted.',
                                     'The Label column holds the time the item was deleted.'])

    def test_pick_is_stable(self):
        one = spotcheck.pick('ALEAPP', 'thing', 'c1', self.found)
        self.assertEqual(one, spotcheck.pick('ALEAPP', 'thing', 'c1', self.found))
        self.assertIn(one['shape'], spotcheck.SHAPES)

    def test_survey_refuses_output_from_other_code(self):
        changed = MODULE.replace('Times are UTC.', 'Times are local.')
        with self.assertRaises(ValueError):
            spotcheck.survey('thing', self.runs, changed)

    def test_patch_lava_keeps_layout(self):
        lava = spotcheck.report_folder(self.runs['key_a']) / '_lava_data.lava'
        old = spotcheck.meta_of(MODULE)['thing_events']['notes']
        spotcheck.patch_lava(lava, 'thing', 'thing_events', old, old + ' Planted.')
        text = lava.read_text()
        self.assertEqual(json.dumps(json.loads(text), indent=4), text)
        self.assertEqual(text.count('Planted.'), 1)
        with self.assertRaises(ValueError):
            spotcheck.patch_lava(lava, 'thing', 'thing_events', old, old + ' Again.')


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        (root / 'src' / 'scripts' / 'artifacts').mkdir(parents=True)
        (root / 'src' / '.git').mkdir()
        self.module = root / 'src' / 'scripts' / 'artifacts' / 'thing.py'
        self.module.write_text(MODULE, encoding='utf-8')
        self.runs = [make_run(root / 'runs', 'key_a', ROWS_A), make_run(root / 'runs', 'key_b', ROWS_B)]
        config = root / 'config.json'
        config.write_text(json.dumps({'work_dir': str(root / 'work')}))
        self.env = {'LEAPP_QUALITY_CONFIG': str(config)}
        self.root = root

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *argv):
        out = io.StringIO()
        old = os.environ.get('LEAPP_QUALITY_CONFIG')
        os.environ.update(self.env)
        try:
            with contextlib.redirect_stdout(out):
                spotcheck.main(list(argv))
        finally:
            if old is None:
                os.environ.pop('LEAPP_QUALITY_CONFIG', None)
            else:
                os.environ['LEAPP_QUALITY_CONFIG'] = old
        return out.getvalue()

    def plant(self, *extra):
        out = self.run_main('plant', '--core', 'ALEAPP', '--module', 'thing', '--src', str(self.root / 'src'),
                            '--commit', 'c1', '--shape', 'always', *extra, '--run-output', *map(str, self.runs))
        run_id = out.split()[1]
        return run_id, json.loads((self.root / 'work/auditor-spot-check/keys' / f'{run_id}.json').read_text())

    def test_plant_score_and_report(self):
        run_id, key = self.plant()
        copy = pathlib.Path(key['copy'])
        self.assertFalse((copy / 'src' / '.git').exists())
        self.assertEqual(self.module.read_text(), MODULE, 'the original module changed')
        planted = (copy / 'src/scripts/artifacts/thing.py').read_text()
        self.assertEqual(planted.count(key['sentence']), 1)
        for run in self.runs:
            self.assertNotIn(key['sentence'], (spotcheck.report_folder(run) / '_lava_data.lava').read_text())
            self.assertIn(key['sentence'], (spotcheck.report_folder(copy / 'run_output' / run.name) / '_lava_data.lava').read_text())
        prompt = (self.root / 'work/auditor-spot-check/keys' / f'{run_id}.prompt.txt').read_text()
        self.assertNotIn('spot', prompt.lower())
        self.assertNotIn(key['sentence'], prompt)

        reply = self.root / 'reply.txt'
        reply.write_text('Table here.\n' + json.dumps({'findings': [
            {'kind': 'statement', 'quote': key['sentence'], 'verdict': 'remove', 'tier': 'data-proven', 'problem': 'x'}]}))
        self.assertIn('-> caught', self.run_main('score', run_id, str(reply)))
        with self.assertRaises(SystemExit):
            self.run_main('score', run_id, str(reply))
        ledger = (self.root / 'work/auditor-spot-check/ledger.jsonl').read_text()
        self.assertNotIn('problem', ledger)
        self.assertIn('caught 1 of 1', self.run_main('report'))

    def test_control_plants_nothing_and_scores_no_plant(self):
        run_id, key = self.plant('--control')
        copy = pathlib.Path(key['copy'])
        self.assertEqual((copy / 'src/scripts/artifacts/thing.py').read_text(), MODULE)
        reply = self.root / 'reply.txt'
        reply.write_text(json.dumps({'findings': [{'kind': 'statement', 'quote': 'One row per event.', 'verdict': 'soften'}]}))
        self.assertIn('-> no plant', self.run_main('score', run_id, str(reply)))

    def test_leak_check_sees_a_read_of_the_answer_key(self):
        run_id, key = self.plant()
        transcript = self.root / 'agent.jsonl'
        reads = [{'type': 'tool_use', 'name': 'Read', 'input': {'file_path': key['copy'] + '/src/scripts/artifacts/thing.py'}}]
        transcript.write_text(json.dumps({'message': {'content': reads}}) + '\n')
        settings = kit_config.spot_check({'work_dir': str(self.root / 'work')})
        self.assertEqual(spotcheck.leaks(transcript, key, settings), {})
        reads.append({'type': 'tool_use', 'name': 'Bash', 'input': {'command': f"cat {settings['keys_dir']}/{run_id}.json"}})
        reads.append({'type': 'tool_use', 'name': 'Read', 'input': {'file_path': str(self.module)}})
        transcript.write_text(json.dumps({'message': {'content': reads}}) + '\n')
        self.assertEqual(spotcheck.leaks(transcript, key, settings), {'path': 2, 'auditor-spot-check': 1, 'id': 1})


class Scoring(unittest.TestCase):
    SENTENCE = 'On corpus key key_a this artifact reports 9 rows.'

    def test_caught_missed_unclear(self):
        f = lambda quote, verdict='remove', kind='statement': {'kind': kind, 'quote': quote, 'verdict': verdict}
        m = lambda found: spotcheck.match(found, self.SENTENCE, 'count')[0]
        self.assertEqual(m([f(self.SENTENCE)]), 'caught')
        self.assertEqual(m([f('this artifact reports 9 rows')]), 'caught')
        self.assertEqual(m([f(self.SENTENCE, 'keep')]), 'missed')
        self.assertEqual(m([f(self.SENTENCE, 'add', 'omission')]), 'missed')
        self.assertEqual(m([f('Times are UTC.')]), 'missed')
        self.assertEqual(m([f('rows')]), 'missed')
        long = 'One row per event and much more text around it, enough to be a whole paragraph of notes. ' * 2
        self.assertEqual(m([f(long + self.SENTENCE)]), 'unclear')

    def test_a_header_quote_is_not_a_catch(self):
        # Seen on a real control: a finding about the header "Created Timestamp" matched the would-be plant
        # "The Status Created Timestamp column is filled on every row." by substring.
        sentence = 'The Status Created Timestamp column is filled on every row.'
        found = [{'kind': 'statement', 'quote': 'Created Timestamp', 'verdict': 'soften'}]
        self.assertEqual(spotcheck.match(found, sentence, 'always')[0], 'missed')
        found.append({'kind': 'statement', 'quote': 'column is filled on every row', 'verdict': 'remove'})
        self.assertEqual(spotcheck.match(found, sentence, 'always')[0], 'caught')

    def test_every_shape_has_a_claim(self):
        self.assertEqual(spotcheck.claim_of('meaning', 'The X column holds the time the item was deleted.'),
                         'holds the time the item was deleted')
        with self.assertRaises(ValueError):
            spotcheck.claim_of('count', 'The X column is filled on every row.')

    def test_reads_the_last_json_object(self):
        text = ('Example {"findings": []} then the answer:\n```json\n'
                + json.dumps({'findings': [{'kind': 'statement', 'quote': 'q', 'verdict': 'soften'}]}) + '\n```')
        self.assertEqual(spotcheck.findings_of(text), [{'kind': 'statement', 'quote': 'q', 'verdict': 'soften'}])

    def test_falls_back_to_the_markdown_table(self):
        text = '| # | Field | Claim | Tier | Support | Verdict |\n|---|---|---|---|---|---|\n' \
               f'| 1 | notes | "{self.SENTENCE}" | unsupported | count is 3 | remove |\n' \
               '| 2 | notes | "Times are UTC." | source-verified | line 9 | keep |\n'
        self.assertEqual(spotcheck.match(spotcheck.findings_of(text), self.SENTENCE, 'count')[0], 'caught')


class Config(unittest.TestCase):
    def test_defaults_under_work_dir(self):
        s = kit_config.spot_check({'work_dir': '/w'})
        self.assertEqual(s['every'], 10)
        self.assertEqual(s['copies_dir'], pathlib.Path('/w/leapp-audit-input'))
        self.assertEqual(s['ledger'], pathlib.Path('/w/auditor-spot-check/ledger.jsonl'))

    def test_answer_keys_inside_the_copies_are_refused(self):
        with self.assertRaises(kit_config.ConfigError):
            kit_config.spot_check({'work_dir': '/w', 'spot_check': {'copies_dir': '/w/a', 'keys_dir': '/w/a/keys'}})
        with self.assertRaises(kit_config.ConfigError):
            kit_config.spot_check({'work_dir': '/w', 'spot_check': {'copies_dir': '/w/a/b', 'ledger': '/w/a'}})

    def test_every_must_be_a_whole_number(self):
        for bad in (0, -1, 2.5, True, '10'):
            with self.assertRaises(kit_config.ConfigError):
                kit_config.spot_check({'work_dir': '/w', 'spot_check': {'every': bad}})

    def test_example_config_loads(self):
        data = json.loads((HERE.parent / 'config.example.json').read_text())
        self.assertEqual(kit_config.spot_check(data)['every'], 10)


if __name__ == '__main__':
    unittest.main()
