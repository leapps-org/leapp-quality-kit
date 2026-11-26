# Notes audit

A read-only audit of the examiner-facing text (descriptions, notes, column headers) of a batch of
LEAPP modules, against the code and real corpus output. One `leapp-notes-auditor` reads each
module. A blind checker then grades the statements the auditor flagged, mixed with three sentences
it did not flag, without seeing the audit or knowing which is which.

The graph stops at findings. Every fix still goes through `leapp-notes-auditor` and the review gate
as a normal PR.

## When to use it

- A sweep over existing modules, about ten at a time, to find stale or overstated notes.
- After a change to a shared reader or helper that could make many modules' notes wrong.
- Not for one new artifact. That is the `leapp-notes-auditor` agent before the PR.

## Steps

1. Prepare the round. With no `--modules`, it takes the next `--count` modules nobody has audited,
   in a fixed hash order, kept to `--max-images` corpus keys:

       python3 notes-audit/prepare.py --repo ~/src/ALEAPP --round 3 --count 10

   The round folder is `<notes_audit.input_dir>/<CORE> round <n>/` (by default under `work_dir`). It holds the code at
   the pinned commit (`src/`), the profile, `keys.txt`, `round.json` and `args.json`.

2. Run the corpus (the jobs number is the parallel cap; check `uptime` first):

       LEAPP_PYTHON=python3 notes-audit/run_corpus.sh "<work_dir>/notes-audit-input/ALEAPP round 3" 6

3. Run the workflow in a Claude Code session: "run notes-audit/leapp-notes-audit.js inline
   with the args in `<round folder>/args.json`". The session reads the file and passes its text as
   `script`, with the args.json content as `args`. The workflow uses the `leapp-notes-auditor` agent,
   so copy `agents/leapp-notes-auditor.md` into `~/.claude/agents/` first.
   Ten modules took about 20 agents, 2M subagent tokens and 10 minutes.

4. Score it, with the task output file and the workflow transcript folder from the completion notice:

       python3 notes-audit/score.py "<round folder>" <task output file> <transcript dir>

   It leak-checks the blind checkers, prints the counts, writes `Findings.md` and `results.json`,
   copies them to the findings folder (`notes_audit.findings_dir`, by default
   `<work_dir>/notes-audit-findings/<today> round <n> (<CORE>)/`), and adds the modules to the
   ledger (`notes_audit.ledger`, by default `notes-audit/audited.json`; local state, not committed).

5. Triage. Hand-measure each finding before fixing it: a proposed wording can itself be wrong.

## Reading the numbers

- A flagged statement the blind check calls "not supported" is a finding both reviews reached.
- "Supported" means the sentence is accurate. It does not close a finding about a missing scope or caveat.
- A control the blind check rejects is worth reading: either the checker is noisy or the auditor missed it.
- Omissions cannot be graded by a sentence check, so they are the auditor's alone.

Measured on ALEAPP: the blind check reproduced 19 of 58 flagged statements in round 1 and 27 of 73
in round 2, and rejected 1 of 30 and 0 of 27 controls.

## Rules the agents follow

Read-only on the round folder, staged SQLite copied to a temp folder before opening, nothing
outside the round folder, no transcripts or memory, no local checkout and no GitHub history, no
evidence content in findings. The leak check looks for reads of the findings folder, this kit's
files, transcripts, a local core checkout and GitHub. Keep the round folder outside the findings
folder, or every blind checker shows a false leak.
