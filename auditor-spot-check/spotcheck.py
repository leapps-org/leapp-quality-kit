"""Auditor spot-check: plant one false sentence in a copy of a module and see whether the auditor flags it.

About one `leapp-notes-auditor` run in ten (chosen by a hash, so the same module at the same commit always
gets the same answer) also gets a second auditor run on a copy of the module. The copy's notes for one
artifact carry one extra sentence that the module's own run output shows is false. If the auditor marks
that sentence soften or remove, the run is "caught"; otherwise "missed". The ledger of results gives the
auditor's catch rate. The real module, PR and run output are never changed, and nothing here blocks a merge.

  python3 spotcheck.py should <core> <module> <commit>
  python3 spotcheck.py plant --core ALEAPP --module kwai --src <code tree> --run-output <dir per key> ...
  python3 spotcheck.py plant ... --control          (same copy and prompt, nothing planted)
  python3 spotcheck.py score <id> <auditor output file> [--transcript <agent transcript .jsonl>]
  python3 spotcheck.py report

Each --run-output folder is named by its corpus key and holds one report folder with _lava_artifacts.db
and _lava_data.lava, the layout notes-audit/run_corpus.sh writes. Prints ids, keys, headers and counts only.
"""
import argparse
import ast
import collections
import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import kit_config  # noqa: E402

SHAPES = ('count', 'always', 'meaning')
# 'count' and 'always' are measured false against the run output. 'meaning' gives a column a meaning
# nothing in the code or the output supports: unsupported, not shown false, so it is reported apart.
MEASURED = ('count', 'always')
SAFE = re.compile(r"^[A-Za-z0-9 ,.:()/_%#-]+$")
TIME_WORDS = re.compile(r'time|date|stamp|created|modified|deleted|updated|expir|when', re.I)


def digest(text):
    return int(hashlib.sha256(text.encode('utf-8')).hexdigest(), 16)


def chosen(core, module, commit, every):
    """True for about one (core, module, commit) in `every`, the same answer every time it is asked."""
    return digest(f'{core.upper()}:{module}:{commit}') % every == 0


# ---------------------------------------------------------------- the module's own text

def artifacts_node(tree):
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], 'id', '') == '__artifacts_v2__':
            if isinstance(node.value, ast.Dict):
                return node.value
    raise ValueError('no __artifacts_v2__ dict')


def notes_node(source, artifact):
    """The AST node of one artifact's notes, if it is a plain string literal."""
    block = artifacts_node(ast.parse(source))
    for key, value in zip(block.keys, block.values):
        if isinstance(key, ast.Constant) and key.value == artifact and isinstance(value, ast.Dict):
            for k, v in zip(value.keys, value.values):
                if isinstance(k, ast.Constant) and k.value == 'notes':
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        return v
                    return None
    return None


def value_of(node):
    """A literal's value, or for anything else (an f-string, a name) a stand-in that compares by its code."""
    try:
        return ast.literal_eval(node)
    except ValueError:
        return ('<expression>', ast.dump(node))


def meta_of(source):
    """The artifact block, artifact by artifact and value by value, so one f-string does not hide the rest."""
    meta = {}
    block = artifacts_node(ast.parse(source))
    for key, value in zip(block.keys, block.values):
        if isinstance(key, ast.Constant) and isinstance(value, ast.Dict):
            meta[key.value] = {k.value: value_of(v) for k, v in zip(value.keys, value.values) if isinstance(k, ast.Constant)}
    return meta


def offset(source, line, col):
    """Character offset of an AST (line, UTF-8 byte column) position."""
    lines = source.splitlines(keepends=True)
    head = ''.join(lines[:line - 1])
    return len(head) + len(lines[line - 1].encode('utf-8')[:col].decode('utf-8'))


def plant_in_source(source, artifact, sentence):
    """Return the source with ' ' + sentence added to the end of the artifact's notes literal.

    The sentence goes in before the closing quote of the literal's last piece, so the code reads like any
    other notes edit. Refuses anything it cannot verify: the parsed notes must equal old + ' ' + sentence
    and nothing else in the artifact block may change.
    """
    if not SAFE.match(sentence):
        raise ValueError('sentence holds a character that would need escaping')
    node = notes_node(source, artifact)
    if node is None:
        raise ValueError(f'{artifact} has no plain string notes')
    end = offset(source, node.end_lineno, node.end_col_offset)
    for close in ('"""', "'''", '"', "'"):
        if source[:end].endswith(close):
            break
    else:
        raise ValueError('could not find the closing quote of the notes')
    planted = source[:end - len(close)] + ' ' + sentence + source[end - len(close):]
    before, after = meta_of(source), meta_of(planted)
    if after[artifact]['notes'] != before[artifact]['notes'] + ' ' + sentence:
        raise ValueError('the planted notes did not parse back as expected')
    after[artifact]['notes'] = before[artifact]['notes']
    if after != before:
        raise ValueError('planting changed more than the notes')
    return planted


# ---------------------------------------------------------------- the run output

def report_folder(run_output):
    found = sorted(p.parent for p in Path(run_output).glob('*/_lava_data.lava'))
    if len(found) != 1:
        raise ValueError(f'{run_output}: expected one report folder with _lava_data.lava, found {len(found)}')
    return found[0]


def lava_entries(lava, module):
    """artifact key -> {tablename, notes, column_map} for one module, from a parsed _lava_data.lava."""
    out = {}
    for m in lava.get('meta', {}).get('modules', []):
        if m.get('module_name') == module:
            for a in m.get('artifacts', []):
                out[a['artifact_key']] = {'tablename': a.get('tablename'), 'notes': a.get('notes')}
    for group in (lava.get('artifacts') or {}).values():
        for a in group:
            if a.get('module') == module and a.get('artifact_key') in out:
                out[a['artifact_key']]['column_map'] = a.get('column_map') or {}
    return out


def measure(db_path, table, column_map):
    """Rows, and per header the empty and filled counts, read from a temp copy of the LAVA database."""
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ('', '-wal', '-shm', '-journal'):
            side = Path(str(db_path) + suffix)
            if side.exists():
                shutil.copy2(side, Path(tmp) / (Path(db_path).name + suffix))
        copy = Path(tmp) / Path(db_path).name
        con = sqlite3.connect(copy.as_uri() + '?mode=ro', uri=True)
        try:
            exists = con.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()
            if not exists:
                return None
            quoted = '"' + table.replace('"', '""') + '"'
            rows = con.execute(f'select count(*) from {quoted}').fetchone()[0]
            columns = {r[1] for r in con.execute(f'pragma table_info({quoted})')}
            headers = {}
            for column, header in column_map.items():
                if column not in columns:
                    continue
                q = '"' + column.replace('"', '""') + '"'
                empty, numeric = con.execute(
                    f"select sum({q} is null or trim(cast({q} as text)) = ''), "
                    f"sum(typeof({q}) in ('integer', 'real') or trim(cast({q} as text)) glob '[0-9]*') "
                    f'from {quoted}').fetchone()
                headers[header] = {'empty': empty or 0, 'filled': rows - (empty or 0), 'numeric': numeric or 0}
            return {'rows': rows, 'headers': headers}
        finally:
            con.close()


def survey(module, run_outputs, source):
    """Per artifact with plain notes: the notes, and the measurements on every key that has its table."""
    meta = meta_of(source)
    found = {}
    for key, folder in run_outputs.items():
        report = report_folder(folder)
        lava = json.loads((report / '_lava_data.lava').read_text(encoding='utf-8'))
        for artifact, entry in lava_entries(lava, module).items():
            if artifact not in meta or notes_node(source, artifact) is None:
                continue
            if entry['notes'] != meta[artifact].get('notes'):
                raise ValueError(f'{key}: the report notes for {artifact} differ from the source; was it run on this code?')
            m = measure(report / '_lava_artifacts.db', entry['tablename'], entry.get('column_map') or {})
            slot = found.setdefault(artifact, {'keys': {}})
            if m:
                slot['keys'][key] = m
    return found


def candidates(artifact, info, shape):
    """Every false sentence of one shape this artifact's measurements allow, with the fact behind it."""
    keys = info['keys']
    out = []
    if shape == 'count':
        for key, m in sorted(keys.items()):
            if m['rows'] > 0:
                wrong = m['rows'] + max(3, m['rows'] // 3 + 2)
                out.append((f'On corpus key {key} this artifact reports {wrong:,} rows.',
                            {'key': key, 'rows': m['rows'], 'stated': wrong}))
    elif shape in ('always', 'meaning'):
        headers = sorted({h for m in keys.values() for h in m['headers']})
        for header in headers:
            if not SAFE.match(header):
                continue
            empty = sum(m['headers'].get(header, {}).get('empty', 0) for m in keys.values())
            filled = sum(m['headers'].get(header, {}).get('filled', 0) for m in keys.values())
            numeric = sum(m['headers'].get(header, {}).get('numeric', 0) for m in keys.values())
            if shape == 'always' and empty > 0 and filled > 0:
                out.append((f'The {header} column is filled on every row.', {'empty': empty, 'filled': filled}))
            if shape == 'meaning' and filled > 0 and numeric == 0 and not TIME_WORDS.search(header):
                out.append((f'The {header} column holds the time the item was deleted.', {'filled': filled, 'numeric': 0}))
    return out


def pick(core, module, commit, found, shape=None):
    """Choose artifact, shape and sentence by hash order, the first that the measurements allow."""
    shapes = [shape] if shape else sorted(SHAPES, key=lambda s: digest(f'{core}:{module}:{commit}:{s}'))
    for s in shapes:
        options = [(artifact, sentence, fact) for artifact in sorted(found)
                   for sentence, fact in candidates(artifact, found[artifact], s)]
        if options:
            options.sort(key=lambda o: digest(f'{core}:{module}:{commit}:{o[1]}'))
            artifact, sentence, fact = options[0]
            return {'shape': s, 'artifact': artifact, 'sentence': sentence, 'fact': fact}
    raise ValueError('no artifact of this module has a measurement that allows a planted sentence')


# ---------------------------------------------------------------- copying

def clone(source, target):
    """Copy a tree with APFS clones where the volume allows it (instant, no extra space), else a plain copy."""
    done = subprocess.run(['cp', '-cR', str(source), str(target)], capture_output=True, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        if Path(target).exists():
            raise OSError(f'cp -cR failed part way into {target}')
        shutil.copytree(source, target, symlinks=True)


def copy_tree(src, target):
    """Copy a code tree without its .git, so the auditor sees code and no history."""
    target.mkdir(parents=True)
    for entry in sorted(Path(src).iterdir()):
        if entry.name != '.git':
            clone(entry, target / entry.name)


def patch_lava(lava_path, module, artifact, old, new):
    """Swap the artifact's notes in a copied _lava_data.lava, in the file's own JSON layout."""
    text = lava_path.read_text(encoding='utf-8')
    data = json.loads(text)
    layout = next((ea for ea in (True, False) if json.dumps(data, indent=4, ensure_ascii=ea) == text), None)
    if layout is None:
        raise ValueError(f'{lava_path} does not round-trip as indent=4 JSON; refusing to rewrite it')
    hits = 0
    for m in data['meta']['modules']:
        if m.get('module_name') == module:
            for a in m['artifacts']:
                if a.get('artifact_key') == artifact and a.get('notes') == old:
                    a['notes'] = new
                    hits += 1
    if hits != 1:
        raise ValueError(f'{lava_path}: found the notes {hits} times, expected once')
    lava_path.write_text(json.dumps(data, indent=4, ensure_ascii=layout), encoding='utf-8')


def prompt_for(core, root, module, keys):
    """The auditor's prompt: the same inputs and rules the notes-audit workflow gives, asking for the same JSON."""
    outputs = '\n'.join(f'- {root}/run_output/{k}' for k in keys)
    return (
        f'Audit the examiner-facing text of this {core} module: every description, notes sentence and column header '
        f'of every artifact in it.\n\nRoot: {root}\nModule: {root}/src/scripts/artifacts/{module}.py, plus any shared '
        f'helpers it calls under {root}/src\nRun outputs, one per corpus key, each holding one report folder with '
        f'_lava_artifacts.db and staged files under data/:\n{outputs}\nAn artifact with zero rows on a key has no LAVA '
        'table.\n\n'
        'Read-only. Do not edit, commit, push or change anything under the root. Open LAVA databases only with a '
        'mode=ro URI built from Path.as_uri(). To open a staged SQLite file, first copy it with its -wal and -shm to a '
        'new folder you make with mktemp -d under the system temp directory, and delete that folder when done. Put any '
        'helper script in that folder too. Do not write to any scratchpad folder. Do not read anything else: no folder '
        f'outside the root, no session transcripts, no memory files about this work, no local {core} checkout, and no '
        f'{core} GitHub history, PRs or issues. Public primary sources (AOSP, Apple or vendor docs) may be fetched, '
        'with no evidence value in any request. Never quote evidence content (names, numbers, message text, '
        'identifiers, or paths holding them): describe its shape and give counts. No em-dashes. Say "known data", '
        'never the other phrase for it.\n\n'
        'Return only problems in findings, with ids F1, F2 and so on, and count the statements you would keep in keep. '
        'Set kind to "statement" when existing text is wrong, overstated or unsupported, and put in quote the exact '
        'text copied from the module (a sentence or a column header), with no paraphrase and none of your own framing. '
        'Set kind to "omission" when no existing sentence is wrong but the report leaves out something an examiner '
        'needs, such as a value parsed and dropped; quote the nearest related text.\n\n'
        'End your reply with one JSON object and nothing after it: {"claims_checked": int, "keep": int, "findings": '
        '[{"id", "artifact", "field", "quote", "kind": "statement"|"omission", "tier": "data-proven"|"source-verified"|'
        '"unsupported", "problem", "support", "verdict": "soften"|"remove"|"add", "replacement"}], "could_not_check": '
        '[str]}.')


def plant(a, settings):
    core, module = a.core.upper(), a.module
    src = Path(a.src).expanduser().resolve()
    source_file = src / 'scripts' / 'artifacts' / f'{module}.py'
    source = source_file.read_text(encoding='utf-8')
    run_outputs = {}
    # Every spelling of each original, for the leak check: a read of the originals lets the auditor diff the copy.
    originals = {str(Path(a.src).expanduser()), str(src)}
    for folder in a.run_output:
        originals.add(str(Path(folder).expanduser()))
        folder = Path(folder).expanduser().resolve()
        originals.add(str(folder))
        run_outputs[folder.name] = folder
    commit = a.commit or 'unknown'
    found = survey(module, run_outputs, source)
    choice = pick(core, module, commit, found, a.shape)

    run_id = uuid.uuid4().hex[:10]
    root = settings['copies_dir'] / f'{core} audit {run_id}'
    keys_dir = settings['keys_dir']
    keys_dir.mkdir(parents=True, exist_ok=True)
    if root.exists():
        sys.exit(f'FAIL copy exists: {root}')
    copy_tree(src, root / 'src')
    (root / 'run_output').mkdir()
    for key, folder in sorted(run_outputs.items()):
        clone(folder, root / 'run_output' / key)

    old = meta_of(source)[choice['artifact']]['notes']
    if not a.control:
        planted = plant_in_source(source, choice['artifact'], choice['sentence'])
        (root / 'src' / 'scripts' / 'artifacts' / f'{module}.py').write_text(planted, encoding='utf-8')
        for key in sorted(run_outputs):
            lava = report_folder(root / 'run_output' / key) / '_lava_data.lava'
            if choice['artifact'] in lava_entries(json.loads(lava.read_text(encoding='utf-8')), module):
                patch_lava(lava, module, choice['artifact'], old, old + ' ' + choice['sentence'])
    # Read the copy back: the sentence is in the module and every report exactly as often as it should be.
    check_copy(root, module, run_outputs, choice, a.control)

    record = {'id': run_id, 'created': date.today().isoformat(), 'core': core, 'module': module, 'commit': commit,
              'control': bool(a.control), **choice, 'keys': sorted(run_outputs), 'copy': str(root),
              'originals': sorted(originals)}
    (keys_dir / f'{run_id}.json').write_text(json.dumps(record, indent=1), encoding='utf-8')
    prompt = prompt_for(core, root, module, sorted(run_outputs))
    (keys_dir / f'{run_id}.prompt.txt').write_text(prompt, encoding='utf-8')
    print('id', run_id, 'control' if a.control else 'planted', choice['shape'], 'in', choice['artifact'])
    print('copy', root)
    print('prompt', keys_dir / f'{run_id}.prompt.txt')
    print('Give the prompt to a leapp-notes-auditor agent, save its reply to a file, then run: score', run_id, '<file>')


def check_copy(root, module, run_outputs, choice, control):
    text = (root / 'src' / 'scripts' / 'artifacts' / f'{module}.py').read_text(encoding='utf-8')
    want = 0 if control else 1
    if text.count(choice['sentence']) != want:
        raise ValueError('the module copy does not hold the planted sentence as expected')
    for key in run_outputs:
        lava = (report_folder(root / 'run_output' / key) / '_lava_data.lava').read_text(encoding='utf-8')
        entry = lava_entries(json.loads(lava), module).get(choice['artifact'])
        if entry is not None and entry['notes'].count(choice['sentence']) != want:
            raise ValueError(f'{key}: the report copy does not hold the planted sentence as expected')


# ---------------------------------------------------------------- scoring

def norm(text):
    return re.sub(r'[^a-z0-9]+', ' ', str(text).lower()).strip()


def findings_of(text):
    """The auditor's findings: the last JSON object in the reply, or failing that the rows of its markdown table."""
    decoder = json.JSONDecoder()
    last = None
    for start in [m.start() for m in re.finditer(r'\{', text)]:
        try:
            value, _ = decoder.raw_decode(text[start:])
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get('findings'), list):
            last = value
    if last is not None:
        return last['findings']
    rows = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip('|').split('|')] if line.strip().startswith('|') else []
        verdict = next((c.split()[0].lower() for c in cells if re.match(r'(?i)^(keep|soften|remove)\b', c)), None)
        if verdict:
            claim = max((c for c in cells if not re.match(r'(?i)^(keep|soften|remove)\b', c)), key=len, default='')
            rows.append({'kind': 'statement', 'quote': claim.strip('"'), 'verdict': verdict, 'tier': ''})
    return rows


CLAIMS = {
    'count': re.compile(r'reports [0-9,]+ rows'),
    'always': re.compile(r'is filled on every row'),
    'meaning': re.compile(r'holds the time the item was deleted'),
}


def claim_of(shape, sentence):
    """The false part of a planted sentence: its claim, not the header or key it names."""
    found = CLAIMS[shape].search(sentence)
    if not found:
        raise ValueError(f'no {shape} claim in the planted sentence')
    return found.group(0)


def match(findings, sentence, shape):
    """caught / missed / unclear for one planted sentence.

    Caught: a statement finding marked soften or remove whose quote holds the sentence's false claim
    ("is filled on every row", "reports N rows"), with little else around it. A quote of the header alone
    is not a catch: the header is real text the module already had. A quote that holds the claim inside
    a much longer passage is "unclear", for a person to read, since that finding may be about another sentence.
    """
    planted, claim = norm(sentence), norm(claim_of(shape, sentence))
    best, hit = 'missed', None
    for f in findings:
        if f.get('kind', 'statement') != 'statement' or str(f.get('verdict', '')).lower() not in ('soften', 'remove'):
            continue
        quote = norm(f.get('quote', ''))
        if claim not in quote:
            continue
        if len(quote) <= 2 * len(planted) + 20:
            return 'caught', f
        best, hit = 'unclear', f
    return best, hit


def tool_inputs(transcript):
    for line in Path(transcript).read_text(errors='replace').splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        stack = [record]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if node.get('type') == 'tool_use' and node.get('name') != 'StructuredOutput':
                    yield json.dumps(node.get('input', {}))
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)


def leaks(transcript, record, settings):
    """Reads that would let the auditor see the answer: the keys folder, the ledger, this tool, the originals."""
    needles = [str(settings['keys_dir']), settings['ledger'].name, 'auditor-spot-check', 'spotcheck',
               'leapp-quality-kit', record['id']] + record.get('originals', [])
    hits = collections.Counter()
    for text in tool_inputs(transcript):
        unescaped = text.replace('\\\\', '\\').replace('\\/', '/')
        for needle in needles:
            # The copy's own folder name carries the id; reading the copy is the job, not a leak.
            if needle == record['id']:
                if needle in unescaped.replace(record['copy'], ''):
                    hits['id'] += 1
            elif needle in unescaped:
                hits[needle if needle in ('auditor-spot-check', 'spotcheck', 'leapp-quality-kit') else 'path'] += 1
    return dict(hits)


def score(a, settings):
    key_file = settings['keys_dir'] / f'{a.id}.json'
    record = json.loads(key_file.read_text(encoding='utf-8'))
    ledger = settings['ledger']
    if ledger.exists() and any(json.loads(l).get('id') == a.id for l in ledger.read_text().splitlines() if l.strip()):
        sys.exit(f'FAIL {a.id} is already in the ledger')
    findings = findings_of(Path(a.output).read_text(errors='replace'))
    outcome, hit = match(findings, record['sentence'], record['shape'])
    if record['control']:
        # Nothing was planted, so any match means the scorer reads something else as the plant.
        outcome = 'no plant' if outcome == 'missed' else f'control matched ({outcome})'
    leak = leaks(a.transcript, record, settings) if a.transcript else None
    line = {'id': record['id'], 'scored': date.today().isoformat(), 'core': record['core'], 'module': record['module'],
            'commit': record['commit'], 'artifact': record['artifact'], 'shape': record['shape'],
            'control': record['control'], 'outcome': outcome, 'verdict': hit and hit.get('verdict'),
            'tier': hit and hit.get('tier'), 'findings': sum(1 for f in findings if f.get('kind', 'statement') == 'statement'),
            'omissions': sum(1 for f in findings if f.get('kind') == 'omission'),
            'leak_check': 'not run' if leak is None else ('hit' if leak else 'clean')}
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open('a', encoding='utf-8') as out:
        out.write(json.dumps(line) + '\n')
    print(record['id'], record['module'], record['shape'], 'control' if record['control'] else 'planted', '->', outcome,
          '| statement findings', line['findings'], '| leak check', line['leak_check'], leak or '')


def report(settings):
    ledger = settings['ledger']
    lines = [json.loads(l) for l in ledger.read_text().splitlines() if l.strip()] if ledger.exists() else []
    usable = [l for l in lines if l['leak_check'] != 'hit']
    print('runs', len(lines), 'leak hits left out', len(lines) - len(usable))
    for shape in SHAPES:
        runs = [l for l in usable if not l['control'] and l['shape'] == shape]
        caught = sum(l['outcome'] == 'caught' for l in runs)
        unclear = sum(l['outcome'] == 'unclear' for l in runs)
        print(f'  {shape:8} caught {caught} of {len(runs)}' + (f', unclear {unclear}' if unclear else '')
              + ('' if shape in MEASURED else '  (unsupported, not measured false; kept apart)'))
    measured = [l for l in usable if not l['control'] and l['shape'] in MEASURED]
    print('  measured-false shapes: caught', sum(l['outcome'] == 'caught' for l in measured), 'of', len(measured))
    controls = [l for l in usable if l['control']]
    print('  controls', len(controls), 'scored "no plant"', sum(l['outcome'] == 'no plant' for l in controls))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('should', help='is this auditor run spot-checked?')
    s.add_argument('core')
    s.add_argument('module')
    s.add_argument('commit')
    p = sub.add_parser('plant', help='make the module copy and the answer key')
    p.add_argument('--core', required=True)
    p.add_argument('--module', required=True)
    p.add_argument('--src', required=True, help='the code tree the run output was made with')
    p.add_argument('--run-output', required=True, nargs='+', help='one folder per corpus key, named by the key')
    p.add_argument('--commit', help='the commit the code tree is at; feeds the hash order')
    p.add_argument('--shape', choices=SHAPES, help='default: the first in hash order the measurements allow')
    p.add_argument('--control', action='store_true', help='make the copy and the key but plant nothing')
    c = sub.add_parser('score', help='score the auditor reply for one id and add it to the ledger')
    c.add_argument('id')
    c.add_argument('output', help="file holding the auditor's reply")
    c.add_argument('--transcript', help="the auditor agent's transcript .jsonl, for the leak check")
    sub.add_parser('report', help='catch rate from the ledger')
    a = parser.parse_args(argv)
    try:
        settings = kit_config.spot_check(kit_config.load())
    except kit_config.ConfigError as error:
        sys.exit(f'FAIL {error}')
    if a.command == 'should':
        print('spot-check', 'yes' if chosen(a.core, a.module, a.commit, settings['every']) else 'no')
    elif a.command == 'plant':
        try:
            plant(a, settings)
        except (ValueError, OSError) as error:
            sys.exit(f'FAIL {error}')
    elif a.command == 'score':
        score(a, settings)
    else:
        report(settings)


if __name__ == '__main__':
    main()
