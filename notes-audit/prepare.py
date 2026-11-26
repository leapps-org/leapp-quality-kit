"""Prepare one round of the leapp-notes-audit workflow.

Exports the repo's origin/main at a pinned commit, picks the modules and their corpus keys,
writes the LEAPP profile and the control sentences, and writes args.json for the workflow.

  python3 prepare.py --repo ~/src/ALEAPP --round 3 --count 10
  python3 prepare.py --repo ~/src/ALEAPP --round 3 --modules wifiProfiles what3words

The corpus registry per core and the work folder come from the kit's config file.

Prints names, keys and counts only. Nothing is written inside the repo.
"""
import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import kit_config  # noqa: E402

ARCHIVES = ('.zip', '.tar', '.tar.gz', '.tgz')
PROFILE_EXT = {'aleapp': 'alprofile', 'ileapp': 'ilprofile', 'rleapp': 'rlprofile', 'dleapp': 'dlprofile', 'vleapp': 'vlprofile'}


def meta_of(path):
    tree = ast.parse(path.read_text(errors='replace'))
    return next(ast.literal_eval(n.value) for n in tree.body
                if isinstance(n, ast.Assign) and getattr(n.targets[0], 'id', '') == '__artifacts_v2__')


def rows(text):
    found = re.search(r'([\d,]+) rows?', str(text))
    return int(found.group(1).replace(',', '')) if found else 0


def usable_keys(registry):
    samples = json.loads(Path(registry).read_text())['samples']
    usable = set()
    for key, value in samples.items():
        private = key.startswith('private') or any('priv' in field.lower() and value.get(field) for field in value)
        if not private and str(value.get('match', {}).get('zip', '')).endswith(ARCHIVES):
            usable.add(key)
    return usable


def module_keys(meta, usable):
    """The two keys with the most sample_data rows, plus every usable key the text names."""
    with_rows, cited = {}, set()
    for value in meta.values():
        if not isinstance(value, dict):
            continue
        for key, sample in (value.get('sample_data') or {}).items():
            if key in usable and rows(sample) > 0:
                with_rows[key] = with_rows.get(key, 0) + rows(sample)
        text = f"{value.get('notes') or ''} {value.get('description') or ''}"
        cited |= {k for k in usable if re.search(r'(?<![\w])' + re.escape(k) + r'(?![\w])', text)}
    top = sorted(with_rows, key=lambda k: (-with_rows[k], k))[:2]
    return sorted(set(top) | cited), bool(with_rows)


def controls(module, meta, size=6):
    """Sentences of the module's own text, in hash order, for the workflow to pick unflagged controls from."""
    pool = {}
    for artifact, value in meta.items():
        if not isinstance(value, dict):
            continue
        for field in ('description', 'notes'):
            for sentence in re.split(r'(?<=[.!?])\s+', str(value.get(field) or '')):
                sentence = sentence.strip()
                if 40 <= len(sentence) <= 280 and 'http' not in sentence:
                    pool.setdefault(sentence, {'artifact': artifact, 'field': field, 'statement': sentence})
    return sorted(pool.values(), key=lambda p: hashlib.sha256((module + p['statement']).encode()).hexdigest())[:size]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True, type=Path)
    parser.add_argument('--round', required=True, help='round label, used in the folder name and the ledger')
    parser.add_argument('--modules', nargs='*', help='module names; default is the next --count unaudited modules')
    parser.add_argument('--count', type=int, default=10)
    parser.add_argument('--max-images', type=int, default=8)
    parser.add_argument('--max-notes', type=int, default=12000, help='skip modules with more notes text than this')
    parser.add_argument('--out', type=Path, help='default <notes_audit.input_dir>/<CORE> round <round>')
    a = parser.parse_args()
    try:
        config = kit_config.load()
        registries = kit_config.need(config, 'core_registries')
        paths = kit_config.notes_audit(config)
    except kit_config.ConfigError as error:
        sys.exit(f'FAIL {error}')

    repo = a.repo.expanduser().resolve()
    core = repo.name
    key = core.lower()
    if key not in registries:
        sys.exit(f'FAIL no registry for {core} in core_registries')
    registry = str(kit_config.expand(registries[key]))
    # Kept apart from the findings folder: the blind checkers read this folder, and score.py treats any read
    # of the findings folder as a leak. score.py copies the finished report there.
    out = (a.out or paths['input_dir'] / f'{core} round {a.round}').expanduser()
    if paths['findings_marker'] in str(out):
        sys.exit(f"FAIL keep the round folder out of the findings folder ({paths['findings_marker']})")
    if out.exists():
        sys.exit(f'FAIL output exists: {out}')

    subprocess.run(['git', '-C', str(repo), 'fetch', '-q', 'origin', 'main'], check=True, stdin=subprocess.DEVNULL)
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'origin/main'], check=True, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL).stdout.strip()
    src = out / 'src'
    src.mkdir(parents=True)
    archive = subprocess.run(['git', '-C', str(repo), 'archive', head], check=True, capture_output=True, stdin=subprocess.DEVNULL).stdout
    subprocess.run(['tar', '-x', '-C', str(src)], input=archive, check=True)

    usable = usable_keys(registry)
    # The ledger of audited modules is local state, kept out of git (see .gitignore).
    ledger_path = paths['ledger']
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    done = set(ledger.get(core, {}))
    metas = {}
    for path in sorted((src / 'scripts/artifacts').glob('*.py')):
        try:
            metas[path.stem] = meta_of(path)
        except (StopIteration, ValueError, SyntaxError):
            continue

    chosen, images = [], set()
    if a.modules:
        for module in a.modules:
            if module not in metas:
                sys.exit(f'FAIL no artifact block in {module}')
            keys, _ = module_keys(metas[module], usable)
            chosen.append({'module': module, 'keys': keys})
            images |= set(keys)
    else:
        candidates = []
        for module, meta in metas.items():
            if module in done:
                continue
            total = sum(len(str(v.get('notes') or '')) for v in meta.values() if isinstance(v, dict))
            keys, has_rows = module_keys(meta, usable)
            if 400 <= total <= a.max_notes and has_rows:
                candidates.append({'module': module, 'keys': keys})
        candidates.sort(key=lambda c: hashlib.sha256(f"{core}:{c['module']}".encode()).hexdigest())
        for c in candidates:
            if len(chosen) == a.count:
                break
            if len(images | set(c['keys'])) <= a.max_images:
                chosen.append(c)
                images |= set(c['keys'])
        print('candidates', len(candidates))
    no_keys = [c['module'] for c in chosen if not c['keys']]
    if no_keys:
        print('WARNING no usable sample_data key for', no_keys, '; the auditor will have no run output for them')

    plugins = [name for c in chosen for name in metas[c['module']]]
    profile = f'profile.{PROFILE_EXT[key]}'
    (out / profile).write_text(json.dumps({'leapp': key, 'format_version': 1, 'plugins': plugins}))
    (out / 'keys.txt').write_text(''.join(f'{k}\n' for k in sorted(images)))
    args = {'root': str(out), 'head': head, 'core': core, 'src': 'src',
            'modules': [{**c, 'candidates': controls(c['module'], metas[c['module']])} for c in chosen]}
    (out / 'args.json').write_text(json.dumps(args, ensure_ascii=False))
    (out / 'round.json').write_text(json.dumps({'core': core, 'round': a.round, 'head': head, 'registry': registry, 'profile': profile,
                                                'modules': [c['module'] for c in chosen], 'images': sorted(images)}, indent=1))
    print('head', head[:8], 'modules', len(chosen), 'images', len(images), 'plugins', len(plugins))
    for c in chosen:
        print(' ', c['module'], c['keys'])
    print('folder', out)


if __name__ == '__main__':
    main()
