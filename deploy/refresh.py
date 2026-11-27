#!/usr/bin/env python3
"""Keep a deployed copy of this kit at the tip of its main branch, switching only to commits that pass the tests.

    python3 deploy/refresh.py            # one refresh; meant for a launch agent or cron entry
    python3 deploy/refresh.py --status   # print what is deployed and what main is

Settings come from the "deploy" block of the kit's config (see config.example.json):

    "deploy": {"root": "~/Tools/leapp-quality-kit", "releases": "~/Tools/leapp-quality-kit-releases",
               "remote": "https://github.com/<owner>/leapp-quality-kit", "branch": "main", "keep": 2}

"root" is a symlink to one folder under "releases", one folder per commit, each holding a DEPLOYED_COMMIT
file. A refresh:

1. fetches the branch into a cache clone under "releases";
2. stops if the deployed commit is already the tip;
3. refuses a tip that does not descend from the deployed commit (a force-push or rewritten history);
4. exports the tip into its own folder and runs every test suite there, with the real config;
5. only if every suite passes, points "root" at the new folder with one rename, so a hook that runs
   from "root" sees the old commit or the new one, never a mix;
6. keeps the "keep" previous folders for a one-rename rollback and removes older ones.

Any failure leaves "root" where it was, writes the log and posts a macOS notification. Exit codes:
0 nothing to do or switched, 1 refused or tests failed, 2 could not check (fetch, config, lock).
Log: <work_dir>/deploy/refresh.log.
"""
import argparse
import datetime
import fcntl
import os
import pathlib
import re
import shutil
import subprocess
import io
import sys
import tarfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import kit_config  # noqa: E402

RAN = re.compile(r'^Ran (\d+) tests? in ', re.M)
SUITES = ('review-gate', 'notes-audit', 'number-recheck', 'mac-pr-reviewer', 'unread-columns', 'deploy', 'auditor-spot-check')
MARKER = 'DEPLOYED_COMMIT'
TEST_TIMEOUT = 600
# git runs from here, not from the caller's folder, which may have been deleted (git then exits 128).
GIT_CWD = os.path.abspath(os.sep)


class Refused(Exception):
    """The tip cannot be deployed (exit 1)."""


class CouldNotCheck(Exception):
    """The refresh could not run (exit 2)."""


def settings(data):
    block = data.get('deploy')
    if not isinstance(block, dict):
        raise CouldNotCheck(f'set a "deploy" block in {kit_config.path_of()}')
    try:
        root = kit_config.expand(block['root'])
        releases = kit_config.expand(block['releases'])
        remote = str(block['remote'])
    except KeyError as error:
        raise CouldNotCheck(f'"deploy" needs "{error.args[0]}" in {kit_config.path_of()}') from error
    keep = block.get('keep', 2)
    if not isinstance(keep, int) or keep < 1:
        raise CouldNotCheck('"deploy.keep" must be a whole number of at least 1')
    return {'root': root, 'releases': releases, 'remote': remote, 'branch': str(block.get('branch') or 'main'),
            'keep': keep, 'work_dir': kit_config.expand(kit_config.need(data, 'work_dir'))}


def git(*args, cwd=None):
    """Run git. A failure reports the operation and exit code only: git's stderr can carry a remote URL with
    credentials, and this text reaches the log and the notification."""
    try:
        done = subprocess.run(['git', *args], cwd=cwd or GIT_CWD, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=300, check=False)
    except subprocess.TimeoutExpired as error:
        raise CouldNotCheck(f'git {args[0]} timed out') from error
    except OSError as error:
        raise CouldNotCheck(f'git could not run ({type(error).__name__})') from error
    if done.returncode != 0:
        raise CouldNotCheck(f'git {args[0]} failed (exit {done.returncode})')
    return done.stdout.strip()


def deployed_commit(root):
    """The commit "root" points at now, or None when nothing is deployed yet."""
    if not root.is_symlink():
        if root.exists():
            raise Refused(f'{root} is a real folder, not a symlink to a release; move it aside once by hand')
        return None
    try:
        return (root / MARKER).read_text(encoding='utf-8').split()[0]
    except (OSError, IndexError) as error:
        raise CouldNotCheck(f'{root} has no readable {MARKER}') from error


def fetch(cfg):
    cache = cfg['releases'] / '.cache.git'
    if not cache.exists():
        cfg['releases'].mkdir(parents=True, exist_ok=True)
        git('init', '-q', '--bare', str(cache))
    git('--git-dir', str(cache), 'fetch', '-q', '--prune', cfg['remote'],
        f"+refs/heads/{cfg['branch']}:refs/heads/{cfg['branch']}")
    return cache, git('--git-dir', str(cache), 'rev-parse', f"refs/heads/{cfg['branch']}")


def descends(cache, old, new):
    done = subprocess.run(['git', '--git-dir', str(cache), 'merge-base', '--is-ancestor', old, new], cwd=GIT_CWD,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False)
    if done.returncode not in (0, 1):
        raise CouldNotCheck(f'could not compare {old[:8]} and {new[:8]}: {done.stderr.strip()[:200]}')
    return done.returncode == 0


def export(cache, commit, target):
    data = subprocess.run(['git', '--git-dir', str(cache), 'archive', '--format=tar', commit], cwd=GIT_CWD,
                          stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=300, check=False)
    if data.returncode != 0:
        raise CouldNotCheck(f'git archive failed: {data.stderr.decode(errors="replace")[:200]}')
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data.stdout)) as archive:
        for member in archive.getmembers():
            name = pathlib.PurePosixPath(member.name)
            # Plain files and folders only, inside the target: the kit has no links or devices.
            if name.is_absolute() or '..' in name.parts or not (member.isfile() or member.isdir()):
                raise Refused(f'unexpected archive member {member.name!r}')
        if hasattr(tarfile, 'data_filter'):
            archive.extractall(target, filter='data')
        else:
            archive.extractall(target)  # members were checked above
    (target / MARKER).write_text(f'{commit} {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n', encoding='utf-8')


def run_tests(folder, python):
    """Every suite must pass in the exported folder. Returns a list of failures, empty when all pass."""
    failures = []
    for suite in SUITES:
        if not (folder / suite).is_dir():
            # A suite that disappears must not turn into a deploy with fewer tests.
            failures.append(f'{suite}: folder missing')
            continue
        try:
            done = subprocess.run([python, '-B', '-m', 'unittest', 'discover', '-s', suite], cwd=folder,
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TEST_TIMEOUT,
                                  check=False)
        except (OSError, subprocess.SubprocessError) as error:
            failures.append(f'{suite}: could not run ({type(error).__name__})')
            continue
        if done.returncode != 0:
            # Name and exit code only: suite output can echo config values, and this goes to a log and a notification.
            failures.append(f'{suite}: exit {done.returncode}')
            continue
        # unittest exits 0 when discovery finds nothing, so an emptied suite must count as a failure too.
        ran = RAN.search(done.stderr or '')
        if not ran or int(ran.group(1)) == 0:
            failures.append(f'{suite}: no tests ran')
    return failures


def switch(root, target):
    """Point root at target with one rename, so a reader sees the old link or the new one."""
    temp = root.with_name(f'.{root.name}.new')
    if temp.is_symlink() or temp.exists():
        temp.unlink()
    os.symlink(target, temp)
    os.replace(temp, root)


def prune(releases, current, keep):
    """Remove release folders beyond the current one and the newest `keep` others. Returns what was removed."""
    folders = [p for p in releases.iterdir() if p.is_dir() and not p.name.startswith('.') and (p / MARKER).is_file()]
    others = sorted((p for p in folders if p.resolve() != current.resolve()), key=lambda p: (p / MARKER).stat().st_mtime,
                    reverse=True)
    removed = []
    for old in others[keep:]:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def refresh(cfg, python, tests=run_tests):
    """One refresh. Returns (exit code, message)."""
    try:
        cfg['releases'].mkdir(parents=True, exist_ok=True)
        # An advisory lock the OS drops when this process ends, so a crashed or slow run never needs a timeout.
        lock = open(cfg['releases'] / '.lock', 'a+', encoding='utf-8')  # pylint: disable=consider-using-with
    except OSError as error:
        return 2, f'COULD NOT CHECK: the releases folder or its lock could not be opened ({type(error).__name__})'
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 2, 'another refresh is running'
        return _refresh_locked(cfg, python, tests)
    finally:
        lock.close()


def _refresh_locked(cfg, python, tests):
    try:
        current = deployed_commit(cfg['root'])
        cache, tip = fetch(cfg)
        if current == tip:
            return 0, f'up to date at {tip[:8]}'
        if current is not None and not descends(cache, current, tip):
            raise Refused(f'{tip[:8]} does not descend from the deployed {current[:8]}; not deploying a rewritten history')
        staging = cfg['releases'] / f'.staging-{tip}'
        if staging.exists():
            shutil.rmtree(staging)
        export(cache, tip, staging)
        try:
            failures = tests(staging, python)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        if failures:
            raise Refused(f'{tip[:8]} failed its tests, staying on {(current or "nothing")[:8]}: ' + '; '.join(failures))
        # Promote a fresh export of the same commit, never the tree the tests ran in.
        final = cfg['releases'] / tip
        fresh = cfg['releases'] / f'.promote-{tip}'
        for old in (fresh, final):
            if old.exists():
                shutil.rmtree(old)
        export(cache, tip, fresh)
        os.replace(fresh, final)
        switch(cfg['root'], final)
    except Refused as error:
        return 1, f'REFUSED: {error}'
    except (CouldNotCheck, OSError, subprocess.SubprocessError) as error:
        return 2, f'COULD NOT CHECK: {error}'
    message = f'switched {(current or "nothing")[:8]} -> {tip[:8]}'
    try:
        removed = prune(cfg['releases'], final, cfg['keep'])
        if removed:
            message += f', removed {len(removed)} old'
    except OSError as error:
        message += f' (cleanup of old releases failed: {type(error).__name__})'
    return 0, message


def notify(message):
    if sys.platform != 'darwin':
        return
    text = message.replace('\\', '').replace('"', "'")[:200]
    subprocess.run(['osascript', '-e', f'display notification "{text}" with title "leapp-quality-kit refresh"'],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--status', action='store_true', help='print the deployed commit and the tip; change nothing')
    args = parser.parse_args(argv)
    try:
        data = kit_config.load()
        cfg = settings(data)
    except (kit_config.ConfigError, CouldNotCheck) as error:
        print(f'COULD NOT CHECK: {error}', file=sys.stderr)
        notify(f'could not check: {error}')
        return 2
    if args.status:
        try:
            print('deployed', deployed_commit(cfg['root']))
            # Read-only: ls-remote touches neither the cache nor the lock a scheduled refresh uses.
            line = git('ls-remote', cfg['remote'], f"refs/heads/{cfg['branch']}")
            print('tip     ', line.split()[0] if line else 'not found')
        except (Refused, CouldNotCheck) as error:
            print(error)
            return 2
        return 0
    code, message = refresh(cfg, str(data.get('python') or sys.executable))
    stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log = cfg['work_dir'] / 'deploy' / 'refresh.log'
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as handle:
            handle.write(f'{stamp} {message}\n')
    except OSError:
        pass
    print(message)
    if code != 0 or message.startswith('switched'):
        notify(message)
    return code


if __name__ == '__main__':
    sys.exit(main())
