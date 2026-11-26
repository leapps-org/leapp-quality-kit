#!/usr/bin/env python3
"""Wait on a LEAPP PR's review gate, then exit so the session that owns the PR is woken.

Start it with the Bash tool's run_in_background after opening or pushing to a PR:

    python3 review-gate/wait_gate.py <repo> <pr>

It uses no model tokens while it waits. It only reruns review_gate.py. It never merges, and it
never passes --mac-report-read or --owner-approved, so those stay a decision of the session
or of the owner.

Exit 0: the gate passed; its output has the merge command.
Exit 3: the gate is blocked by something to act on (a finding, a failed check, a Mac report to
        read, a contributor PR to ask the owner about, a Greptile run that never started).
Exit 4: the deadline passed with checks or reviews still running.
Exit 5: the gate could not check, three times in a row.
"""
import argparse
import pathlib
import subprocess
import sys
import time

GATE = pathlib.Path(__file__).resolve().with_name('review_gate.py')
STILL_WAITING = ('Check still running:', 'Greptile is still reviewing', 'Greptile has not reviewed',
                 'Mac review has not finished', 'No checks are recorded')
GREPTILE_NOT_STARTED = 'Greptile has not reviewed'
GREPTILE_START_LIMIT = 15 * 60
GATE_TIMEOUT = 300
COULD_NOT_CHECK_LIMIT = 3


def is_waiting(reason):
    return reason.startswith(STILL_WAITING) or (reason.startswith('Status not passing:') and reason.endswith('(pending)'))


def run_gate(repo, pr):
    """Return the gate's exit code and output. A hung gate counts as could-not-check."""
    try:
        run = subprocess.run([sys.executable, '-B', str(GATE), repo, str(pr)], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=GATE_TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return 2, f'review_gate.py did not finish in {GATE_TIMEOUT} seconds'
    return run.returncode, (run.stdout + run.stderr).strip()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('repo')
    parser.add_argument('pr', type=int)
    parser.add_argument('--minutes', type=float, default=120, help='give up after this long (default 120)')
    parser.add_argument('--interval', type=float, default=300, help='seconds between checks (default 300)')
    args = parser.parse_args(argv)

    started = time.monotonic()
    deadline = started + args.minutes * 60
    could_not_check = 0
    while True:
        code, output = run_gate(args.repo, args.pr)
        if code == 0:
            print(output)
            return 0
        reasons = [line[2:] for line in output.splitlines() if line.startswith('- ')]
        if code == 1 and reasons:
            could_not_check = 0
            if not all(is_waiting(reason) for reason in reasons):
                print(output)
                return 3
            if (any(reason.startswith(GREPTILE_NOT_STARTED) for reason in reasons)
                    and time.monotonic() - started >= GREPTILE_START_LIMIT):
                print(output)
                return 3
        else:
            could_not_check += 1
            if could_not_check >= COULD_NOT_CHECK_LIMIT:
                print(f'gate exit {code}\n{output}')
                return 5
        if time.monotonic() + args.interval > deadline:
            print(f'deadline reached after {args.minutes:g} minutes\n{output}')
            return 4
        time.sleep(args.interval)


if __name__ == '__main__':
    sys.exit(main())
