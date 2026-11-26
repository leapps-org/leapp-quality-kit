"""Score a finished leapp-notes-audit workflow run and write the round's findings report.

  python3 score.py "<round folder>" <workflow task output file> <workflow transcript dir>

The task output file is the one named in the workflow's completion notice (JSON with a "result").
The transcript dir is ~/.claude/projects/<project>/<session>/subagents/workflows/<run id>.

1. Leak check: every blind checker's tool calls are searched for the audit's findings, this tool's
   files, session transcripts and the repo's GitHub history. A hit means that checker was not blind.
2. Score: flagged statements the blind check confirmed, and controls it rejected.
3. Report: "Findings.md" and "results.json" in the round folder, copied to
   the findings folder (notes_audit.findings_dir in the config, by default
   <work_dir>/notes-audit-findings/<today> round <n> (<CORE>)/), and the modules are added to the
   ledger (audited.json) so the next round's default selection skips them.
Prints counts, ids and repo-text excerpts only.
"""
import collections
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import kit_config  # noqa: E402

def tool_uses(node):
    if isinstance(node, dict):
        if node.get('type') == 'tool_use':
            yield node
        for value in node.values():
            yield from tool_uses(value)
    elif isinstance(node, list):
        for value in node:
            yield from tool_uses(value)


def clip(text, size=330):
    text = re.sub(r'\s+', ' ', str(text)).replace('—', ',').replace('|', '/')
    text = re.sub('ground truth', 'known data', text, flags=re.I)
    return text if len(text) <= size else text[:size].rsplit(' ', 1)[0] + ' ...'


def main():
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    folder, result_file, transcripts = (Path(p).expanduser() for p in sys.argv[1:])
    try:
        config = kit_config.load()
        paths = kit_config.notes_audit(config)
        LEAK = kit_config.notes_audit_leak(config)
    except kit_config.ConfigError as error:
        sys.exit(f'FAIL {error}')
    rnd = json.loads((folder / 'round.json').read_text())
    payload = json.loads(result_file.read_text(errors='replace'))
    data = payload.get('result', payload)
    if data.get('root') and Path(data['root']) != folder:
        sys.exit(f"FAIL result is for {data['root']}, not {folder}")

    leaks, blind = {}, 0
    for path in sorted(transcripts.glob('agent-*.jsonl')):
        text = path.read_text(errors='replace')
        if 'Check examiner-facing statements' not in text[:6000]:
            continue
        blind += 1
        hits = collections.Counter()
        for line in text.splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            for use in tool_uses(record):
                if use.get('name') != 'StructuredOutput':
                    for found in LEAK.finditer(json.dumps(use.get('input', {}))):
                        hits[f"{use.get('name')}:{found.group(0)}"] += 1
        if hits:
            leaks[path.stem] = dict(hits)
    print('blind checkers', blind, 'with leak hits', len(leaks))
    for name, hits in leaks.items():
        print('  LEAK', name, hits)
    if blind == 0:
        print('WARNING no blind checker transcripts found; the leak check did not run')

    table = collections.Counter()
    statements, omissions, claims, keep, verbatim, flagged_total = [], [], 0, 0, 0, 0
    for r in data['results']:
        module = r['module']
        if not r.get('audit'):
            continue
        source = re.sub(r'\W+', ' ', (folder / 'src/scripts/artifacts' / f'{module}.py').read_text(errors='replace')).lower()
        claims += r['audit']['claims_checked']
        keep += r['audit']['keep']
        findings = {f['id']: f for f in r['audit']['findings']}
        omissions += [{'module': module, **f} for f in findings.values() if f['kind'] == 'omission']
        outcomes = {x['id']: x for x in (r.get('results') or [])}
        for row in r['packet']:
            out = outcomes.get(row['id'], {})
            outcome = out.get('outcome', 'missing')
            table[(row['kind'], outcome)] += 1
            f = findings.get(row['finding']) if row['finding'] else None
            if f:
                flagged_total += 1
                parts = [re.sub(r'\W+', ' ', p).strip().lower() for p in re.split(r'\.\.\.|\[\.\.\.\]', f['quote'])]
                verbatim += all(p in source for p in parts if p)
            statements.append({'module': module, 'id': row['id'], 'kind': row['kind'], 'finding': row['finding'],
                               'artifact': row['artifact'], 'field': row['field'], 'statement': row['statement'],
                               'blind': outcome, 'blind_reason': out.get('reason'), 'blind_evidence': out.get('evidence'),
                               'problem': f and f['problem'], 'replacement': f and f['replacement'],
                               'verdict': f and f['verdict'], 'tier': f and f['tier']})

    count = lambda kind: sum(v for k, v in table.items() if k[0] == kind)
    print('claims', claims, 'keep', keep, 'statement findings', flagged_total, 'quoted verbatim', verbatim, 'omissions', len(omissions))
    for kind in ('flagged', 'control'):
        print(kind, {o: table[(kind, o)] for o in ('not_supported', 'supported', 'could_not_check', 'missing')})

    lines = [f"# Notes audit round {rnd['round']} ({rnd['core']})", '',
             f"Code: main at {rnd['head'][:8]}. {len(rnd['modules'])} modules, {len(rnd['images'])} corpus images. Read-only: nothing in the repo changed.", '',
             'The auditor flagged problems. A separate checker then graded the flagged statements mixed with unflagged controls, '
             'without seeing the audit or which was which. Hand-measure a finding before fixing it: a proposed wording can itself be wrong.', '',
             f"Leak check: {blind} blind checkers, {len(leaks)} with a hit." + (' Treat those modules as not blind.' if leaks else ''),
             f"Quotes found verbatim in the module: {verbatim} of {flagged_total}.", '',
             '| Group | Statements | Not supported | Supported | Could not check |', '|---|---:|---:|---:|---:|',
             f"| Flagged by the auditor | {count('flagged')} | {table[('flagged', 'not_supported')]} | {table[('flagged', 'supported')]} | {table[('flagged', 'could_not_check')]} |",
             f"| Unflagged controls | {count('control')} | {table[('control', 'not_supported')]} | {table[('control', 'supported')]} | {table[('control', 'could_not_check')]} |", '',
             '"Supported" means the sentence is accurate. It does not close a finding about a missing scope or caveat.', '']
    groups = (('Both reviews found a problem', lambda s: s['kind'] == 'flagged' and s['blind'] == 'not_supported'),
              ('Blind check says accurate as written', lambda s: s['kind'] == 'flagged' and s['blind'] == 'supported'),
              ('Blind check could not settle', lambda s: s['kind'] == 'flagged' and s['blind'] not in ('not_supported', 'supported')),
              ('Controls the blind check rejected', lambda s: s['kind'] == 'control' and s['blind'] == 'not_supported'))
    for title, test in groups:
        group = [s for s in statements if test(s)]
        lines += [f'## {title} ({len(group)})', '']
        for s in group:
            label = s['finding'] or s['id']
            lines += [f"**{s['module']} {label}:** {clip(s['statement'], 170)}", '']
            if s['problem']:
                lines.append(f"- Audit: {clip(s['problem'])}")
            lines += [f"- Blind: {clip(s['blind_reason'], 260)}", '']
    lines += [f'## Omissions, unverified ({len(omissions)})', '',
              'A sentence check cannot grade an omission, so these are the auditor\'s alone.', '']
    lines += [f"- **{f['module']} {f['id']}:** {clip(f['problem'], 300)}" for f in omissions]
    if data.get('incomplete'):
        lines += ['', f"Incomplete modules: {', '.join(data['incomplete'])}"]
    text = '\n'.join(lines) + '\n'
    if '—' in text or re.search('ground truth', text, re.I):
        sys.exit('FAIL wording')
    (folder / 'Findings.md').write_text(text)
    (folder / 'results.json').write_text(json.dumps({'statements': statements, 'omissions': omissions, 'leaks': leaks}, indent=1))

    ledger_path = paths['ledger']
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    entry = ledger.setdefault(rnd['core'], {})
    for r in data['results']:
        if r.get('audit') and r.get('results'):
            entry[r['module']] = {'round': rnd['round'], 'head': rnd['head'][:8]}
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(json.dumps(ledger, indent=1, sort_keys=True))
    review = kit_config.expand(paths['findings_dir'].replace('{date}', date.today().isoformat())
                               .replace('{round}', str(rnd['round'])).replace('{core}', rnd['core']))
    review.mkdir(parents=True, exist_ok=True)
    for name in ('Findings.md', 'results.json'):
        shutil.copy(folder / name, review / name)
    print('report', review / 'Findings.md')


if __name__ == '__main__':
    main()
