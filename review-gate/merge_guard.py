#!/usr/bin/env python3
"""Claude Code PreToolUse hook: a PR in a covered repo merges only when review_gate.py beside this file passes.

It fails closed for a LEAPP merge it cannot resolve. Commands that do not merge are not touched.
"""
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
try:
    import kit_config  # noqa: E402
    IMPORT_ERROR = None
except Exception as error:  # pylint: disable=broad-exception-caught
    # A broken install must still block merges, so the error is kept and main() denies with it.
    kit_config = None
    IMPORT_ERROR = error

GATE = pathlib.Path(__file__).resolve().with_name('review_gate.py')
# Filled by configure() from the kit's config file: the owner's login and every repo the gate covers.
OWNER = ''
REPOS = set()
LOWER = {}
OWNERS = {}


# A GitHub login or repo name: no spaces or slashes, so a padded or pasted value cannot hide a repo.
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*')
REPO_LISTS = ('greptile_repos', 'mac_review_repos', 'owner_mac_review_repos', 'app_review_repos', 'checks_only_repos')


def configure(data):
    """Every repo list must be present (an empty list is fine). A missing or misspelled key would
    otherwise drop its repos from the guard, and their merges would pass with no gate run."""
    global OWNER, REPOS, LOWER, OWNERS
    owner = kit_config.need(data, 'github_owner')
    if not isinstance(owner, str) or not NAME.fullmatch(owner):
        raise kit_config.ConfigError(f'"github_owner" must be one GitHub login in {kit_config.path_of()}')
    OWNER = owner.lower()
    repos = set()
    for field in REPO_LISTS:
        value = data.get(field)
        if not isinstance(value, list) or not all(isinstance(name, str) and NAME.fullmatch(name) for name in value):
            raise kit_config.ConfigError(f'"{field}" must be a list of repo names in {kit_config.path_of()}')
        repos |= set(value)
    if not repos:
        raise kit_config.ConfigError(f'no repos are listed in {kit_config.path_of()}')
    owners = data.get('repo_owners') or {}
    if not isinstance(owners, dict) or not all(isinstance(k, str) and NAME.fullmatch(k) and isinstance(v, str)
                                               and NAME.fullmatch(v) for k, v in owners.items()):
        raise kit_config.ConfigError(f'"repo_owners" must map repo names to GitHub logins in {kit_config.path_of()}')
    REPOS = repos
    LOWER = {name.lower(): name for name in REPOS}
    OWNERS = {name.lower(): owner.lower() for name, owner in owners.items()}
START = r'(?:^|[;&|(\n]\s*|\b(?:then|do|else|time|command|exec|eval)\s+|(?:ba|z)?sh\s+-l?c\s+[\'"]|\$\(\s*)'
ENV = r'(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*'
GH = r'(?:\S*/)?gh\s+'
MERGE = re.compile(START + ENV + GH + r'(?:(?:-R|--repo)[ =]\S+\s+)?pr\s+(?:(?:-R|--repo)[ =]\S+\s+)?merge\b([^\n;&|)]*)', re.M)
API = re.compile(START + ENV + GH + r'api\b[^\n;&|]*?(?:/pulls/\d+/merge\b|mergePullRequest|enablePullRequestAutoMerge|enqueuePullRequest)', re.M)
PY_MERGE = re.compile(r'''["']gh["']\s*,\s*["']pr["']\s*,\s*["']merge["']''')
URL = re.compile(r'github\.com[/:]([^/\s]+)/([^/\s#?]+?)(?:\.git)?/pull/(\d+)', re.I)
# A script the command runs, by interpreter or by path. Reading or searching a file is not running it.
SCRIPT = re.compile(START + ENV + r'''(?:(?:\S*/)?(?:(?:ba|z)?sh|python[0-9.]*|source|\.)\s+(?:-\S+\s+)*)?'''
                    r'''["']?((?:~|/|\./|\.\./)?[^\s;&|()"'=]+\.(?:sh|bash|zsh|py))(?=$|[\s;&|)"'])''', re.M)
TRUSTED = (pathlib.Path(__file__).resolve(), GATE.resolve())


def deny(reason):
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                              'permissionDecisionReason': reason}}))
    return 0


def owner_for(name):
    """The account a covered repo lives under: github_owner unless repo_owners names another."""
    return OWNERS.get(name.lower(), OWNER)


def repo_name(slug):
    """The covered repo's name for owner/repo, or None for a repo this rule does not cover."""
    parts = slug.rstrip('/').removesuffix('.git').split('/')
    if len(parts) < 2:
        return None
    name = LOWER.get(parts[-1].lower())
    if name is None or parts[-2].lower() != owner_for(name):
        return None
    return name


def directory_for(command, cwd):
    found = re.findall(r'(?:^|[;&|\n]\s*)cd\s+("[^"]+"|\'[^\']+\'|[^\s;&|]+)', command, re.M)
    if not found:
        return cwd
    target = os.path.expanduser(found[-1].strip('"\''))
    return target if os.path.isabs(target) else os.path.join(cwd or '.', target)


def repo_from_git(directory):
    done = subprocess.run(['git', '-C', directory or '.', 'remote', 'get-url', 'origin'], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=10, check=False)
    if done.returncode:
        return None, False
    match = re.search(r'github\.com[/:]([^/\s]+)/([^/\s]+?)(?:\.git)?$', done.stdout.strip(), re.I)
    return (repo_name(f'{match[1]}/{match[2]}'), True) if match else (None, True)


def parse(rest, prefix):
    try:
        tokens = shlex.split(rest)
    except ValueError:
        # The closing quote of a wrapper such as bash -c "gh pr merge ...".
        try:
            tokens = shlex.split(rest.rstrip().rstrip('"\''))
        except ValueError:
            return None
    parsed = {'repo': None, 'number': None, 'head': None, 'auto': False, 'help': False, 'url': None}
    with_value = {'-b', '--body', '-F', '--body-file', '-t', '--subject', '-A', '--author-email'}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if token in ('--help', '-h'):
            parsed['help'] = True
        elif token == '--auto':
            parsed['auto'] = True
        elif token in ('-R', '--repo'):
            parsed['repo'] = tokens[index] if index < len(tokens) else None
            index += 1
        elif token.startswith('--repo='):
            parsed['repo'] = token.split('=', 1)[1]
        elif token == '--match-head-commit':
            parsed['head'] = tokens[index] if index < len(tokens) else None
            index += 1
        elif token.startswith('--match-head-commit='):
            parsed['head'] = token.split('=', 1)[1]
        elif token in with_value:
            index += 1
        elif not token.startswith('-') and parsed['number'] is None and parsed['url'] is None:
            if token.isdigit():
                parsed['number'] = int(token)
            elif URL.search(token):
                parsed['url'] = token
            elif token.startswith('#') and token[1:].isdigit():
                parsed['number'] = int(token[1:])
            else:
                parsed['number'] = token
    outer = re.search(r'(?:-R|--repo)[ =](\S+)', prefix)
    if outer and not parsed['repo']:
        parsed['repo'] = outer.group(1)
    return parsed


def check_merge(rest, prefix, command, cwd):
    parsed = parse(rest, prefix)
    if parsed is None:
        return 'The merge command could not be parsed, so it is blocked.'
    if parsed['help']:
        return None
    number = parsed['number']
    repo = None
    if parsed['url']:
        match = URL.search(parsed['url'])
        repo, number = repo_name(f'{match[1]}/{match[2]}'), int(match[3])
        if repo is None:
            return None
    elif parsed['repo']:
        if '$' in parsed['repo']:
            return 'Name the repo literally so the review gate can check this merge.'
        bare = parsed['repo']
        repo = repo_name(bare if '/' in bare else f'{owner_for(bare)}/{bare}')
        if repo is None:
            return None
    else:
        try:
            repo, known = repo_from_git(directory_for(command, cwd))
        except (OSError, subprocess.SubprocessError):
            return f'The repo for this merge could not be read. Add --repo {OWNER}/<repo>.'
        if repo is None:
            return None if known else f'The repo for this merge could not be read. Add --repo {OWNER}/<repo>.'
    if not isinstance(number, int):
        return f'Name the {repo} PR number literally so the review gate can check this merge.'
    if parsed['auto']:
        return 'Auto-merge is blocked on LEAPP repos because it can merge before the reviews are in.'
    try:
        done = subprocess.run([sys.executable, '-I', '-B', str(GATE), repo, str(number)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=50, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return f'The review gate could not run ({type(error).__name__}), so this merge is blocked.'
    output = done.stdout.strip()[-1500:]
    if done.returncode != 0:
        return f'Review gate did not pass. Do not merge.\n{output}'
    head = re.search(r' at ([0-9a-f]{40}) \(route:', done.stdout)
    if not head:
        return 'The review gate gave no commit, so this merge is blocked.'
    if parsed['head'] != head[1]:
        return f'Rerun the merge with --match-head-commit {head[1]}'
    return None


def script_text(path_text, cwd):
    path = pathlib.Path(os.path.expanduser(path_text))
    if not path.is_absolute():
        path = pathlib.Path(cwd or '.') / path
    try:
        if path.resolve() in TRUSTED:
            return ''
        if path.is_file() and path.stat().st_size <= 1_000_000:
            return path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        pass
    return ''


def check_command(command, cwd):
    if GATE.name in command and 'gh pr merge' not in command:
        return None
    joined = command.replace('\\\n', ' ')
    if API.search(joined):
        return 'Merging through the GitHub API is blocked on all repos. Use gh pr merge so the review gate runs.'
    for match in MERGE.finditer(joined):
        problem = check_merge(match.group(1), match.group(0)[:match.start(1) - match.start()], joined, cwd)
        if problem:
            return problem
    for script in {m.group(1) for m in SCRIPT.finditer(joined)}:
        text = script_text(script, cwd).replace('\\\n', ' ')
        if text and (MERGE.search(text) or API.search(text) or PY_MERGE.search(text)):
            return f'{script} contains a PR merge. Run the merge as its own gh pr merge command so the review gate can check it.'
    return None


def check_auto_merge(tool_input):
    if tool_input.get('enabled') is not True:
        return None
    match = URL.search(str(tool_input.get('url', '')))
    if match is None:
        return 'The PR for this auto-merge could not be read, so it is blocked.'
    if repo_name(f'{match[1]}/{match[2]}'):
        return 'Auto-merge is blocked on LEAPP repos because it can merge before the reviews are in.'
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return deny('The merge guard could not read this tool call.')
    tool = payload.get('tool_name', '')
    tool_input = payload.get('tool_input') or {}
    command = str(tool_input.get('command', ''))
    # Without a working config the guard cannot tell which repos are covered, so it blocks anything
    # that could merge: a merge-shaped command, the auto-merge tool, or a script it would have to read.
    could_merge = tool != 'Bash' or 'merge' in command.lower() or bool(SCRIPT.search(command.replace('\\\n', ' ')))
    if IMPORT_ERROR is not None:
        if could_merge:
            return deny(f'The merge guard could not load kit_config ({type(IMPORT_ERROR).__name__}), '
                        'so this command is blocked until the kit is fixed.')
        return 0
    try:
        configure(kit_config.load())
    except kit_config.ConfigError as error:
        if could_merge:
            return deny(f'The merge guard has no usable config ({error}), so this command is blocked '
                        'until the config is fixed.')
        return 0
    try:
        if tool == 'Bash':
            problem = check_command(str(tool_input.get('command', '')), payload.get('cwd'))
        elif tool == 'mcp__ccd_pr__set_auto_merge':
            problem = check_auto_merge(tool_input)
        else:
            return 0
    except Exception as error:  # pylint: disable=broad-exception-caught
        # A guard bug must not stop ordinary shell commands, and must not let a merge through.
        if tool != 'Bash' or 'merge' in str(tool_input.get('command', '')).lower():
            return deny(f'The merge guard failed ({type(error).__name__}), so this merge is blocked.')
        return 0
    return deny(problem) if problem else 0


if __name__ == '__main__':
    sys.exit(main())
