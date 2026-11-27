# LEAPP quality kit

Copies of the tools I run around LEAPP artifact work that live outside the LEAPP repositories, with
my account, repositories and corpus paths moved into one config file.
The checks inside each core (`admin/scripts/check_*.py`, `validate_sample_data.py`, the test-case
harness) are in the cores. This repository holds the rest: the merge gate, the Mac PR reviewer,
the notes audit, the number re-check, the unread-column list, the auditor agent and its spot-check.

They are written for the LEAPPs (iLEAPP, ALEAPP, RLEAPP, VLEAPP, DLEAPP). Expect to read the code and
adapt it to your own tool.

| Folder | What it does | Needs |
|---|---|---|
| `review-gate/` | Decides whether a PR may merge: checks, reviewer state on the exact head commit, review threads, holds and contributor approval. A wait script reruns it, and a Claude Code hook refuses any merge command it has not passed. | `gh`, Python 3.10+ |
| `mac-pr-reviewer/` | Reviews PRs on the Mac with a sandboxed Codex model, posts short inline findings on the PR and keeps the full report local. Its review store is what the gate's Mac route reads. | macOS, `gh`, the ChatGPT app's Codex client, Python 3.10+ |
| `notes-audit/` | Audits the examiner-facing text of merged modules: one auditor per module, then a blind checker that sees the flagged statements mixed with unflagged controls. | Claude Code (Workflow tool), a corpus, a core checkout |
| `number-recheck/` | Re-runs every `sample_data` row count a core declares against `origin/main` and sorts each corpus key into matched, drifted or could not check. | A corpus with `samples.json` registries, a core checkout |
| `unread-columns/` | During an artifact's corpus run, lists for each SQLite table it reads the columns its SQL never reads, with counts only, so the session can skip or build each one. A warning list; the gate does not read it. | A corpus with `samples.json` registries, a core checkout |
| `auditor-spot-check/` | Checks the auditor: about one run in ten gets a second auditor run on a copy of the module with one false sentence planted in its notes, and a ledger records caught or missed. Never touches the real PR. | Claude Code, a core's run output |
| `agents/` | The `leapp-notes-auditor` agent: an independent read of an artifact's description, notes and headers against the code and the run output. | Claude Code |

## Setup

1. Copy `config.example.json` to `~/.config/leapp-quality-kit/config.json` (or point
   `LEAPP_QUALITY_CONFIG` at another path) and fill it in: your GitHub login and numeric user id,
   which repositories each reviewer covers, any held PRs, where your core checkouts are, where the
   corpus registries are, and a work folder for output. The optional keys (`attestations_dir`,
   `notes_audit`, `recheck_weekly_cores`, `unread_columns`, `app_review_setup`) keep local state out of the checkout
   and are described in each folder's README.
2. For the auditor, copy `agents/leapp-notes-auditor.md` into `~/.claude/agents/`.
3. For the merge guard, add it as a `PreToolUse` hook in your Claude Code settings:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /path/to/leapp-quality-kit/review-gate/merge_guard.py"}]}
    ]
  }
}
```

The guard fails closed: if the config is missing, cannot be read, lacks `github_owner` or lacks
any of the five repo lists (an empty list is fine), or if `kit_config` cannot be imported, every
merge-shaped command is denied. Ordinary commands still run.

Run the tests with `python3 -m unittest discover -s <folder>` for `review-gate`, `notes-audit`,
`number-recheck`, `mac-pr-reviewer`, `unread-columns`, `deploy` and `auditor-spot-check`. For the Mac reviewer, see `mac-pr-reviewer/README.md`.

## Keeping a deployed copy current

If the merge guard and gate run from a deployed copy, `deploy/refresh.py` keeps that copy at the tip of
`main` with no one having to remember. Run it from a launch agent or cron entry every few minutes. It
fetches, refuses a tip that does not descend from the deployed commit, exports the tip into its own
folder, runs every test suite there, and only if they all pass repoints the deployed symlink with one
rename. Any failure leaves the deployed copy where it was, writes `<work_dir>/deploy/refresh.log` and
posts a macOS notification. The previous two releases stay for a one-rename rollback. Set the `deploy`
block in the config; `python3 deploy/refresh.py --status` shows the deployed commit and the tip.

When the gate runs from a deployed copy that has been behind `main` for more than an hour, it prints a
warning (its answer does not change), so a refresh that stopped shows up at the next merge.

## The review gate

    python3 review-gate/review_gate.py <repo> <pr number>

Exit 0 prints a merge command pinned to the head commit with `--match-head-commit`. Exit 1 lists
what blocks. Exit 2 means it could not check, which counts as blocked. It never merges or posts.

Two flags record a person's decision for one exact commit: `--mac-report-read <sha>` (the private
review report was read and addressed) and `--owner-approved <sha>` (the owner approved a
contributor PR, a held PR, or a high-severity thread someone other than the reviewer resolved).

`wait_gate.py` reruns the gate every five minutes for up to two hours and exits when there is
something to act on. It never merges and never passes those two flags.

The Greptile route reads Greptile's check run and its review threads. The Mac route reads a local
SQLite store of finished reviews (`mac_review_store` in the config), which `mac-pr-reviewer/` writes.

## What is not here

- The Mac reviewer's private inbox delivery and its route for the desktop apps, probes, readers and
  website (see `mac-pr-reviewer/README.md`).
- The corpus, its registries and anything from them.

## License

MIT. See `LICENSE`.
