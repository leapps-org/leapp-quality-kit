# Mac PR reviewer

A local job that reviews pull requests on the LEAPP repositories with a sandboxed Codex model.
Short findings go on the PR as inline comments. The full report stays on the Mac.

It is macOS only. It needs `gh` signed in, the Codex client bundled with the ChatGPT desktop app
(the path is `CODEX` in `review_boundary.py`) signed in, and the system Ruby, which it uses to read
workflow YAML.

## How it works

1. `flag_mac_review.yml` goes in each reviewed repository's `.github/workflows/`. It adds the
   `mac-review` label to ready PRs. Apply `request-mac-review` to enroll a PR that is already open.
   The file lists the repository variables it needs.
2. Every five minutes a LaunchAgent runs `mac_review_worker.py` for one repository. It reads the
   labelled PRs and skips drafts, closed PRs and superseded commits. The owner's own PRs are
   skipped too, except in the repositories listed in `owner_mac_review_repos`.
3. It waits until the CI workflows that should run on the PR have finished. It works that out from
   the workflow files themselves (triggers, branches, paths).
4. It builds a review packet: the diff, CI state, earlier review comments, fixture context and the
   artifact guide from the base branch. Vendored files that match their pinned upstream byte for
   byte are replaced by one line saying so.
5. Codex reviews the packet as data. It runs under `sandbox-exec` in a new temporary folder, with
   commands, subagents, apps, hooks, plugins, image generation and web search turned off. The OS
   profile denies fork and running other programs. It limits file reads to system files, the Codex
   app, the scratch folder, the Codex sign-in file and its installation id file (which Codex may
   also write). Codex keeps its network access for the model call. Any tool event in its output stops the review before anything is posted.
6. The worker checks the result's shape, rechecks the PR author and revision, keeps only findings
   that sit on a line of the current patch, and posts them as one review. Each finding is quoted
   text with `@` mentions disabled.
7. The full report is written to `<state>/reports/PR-<number>-<head>.md`, and a row goes into the
   `reviews` table of `reviews.sqlite3` in the same folder. That table is what the review gate's
   Mac route reads, and `<state>` comes from the same `mac_review_store` setting.

A model can still be wrong. Read the report and keep the merge decision with a person.

## Private corpus checks

For a finding in a changed parser, the model can ask for one of four checks on real input: a file
is present, a table is present, a column is present, or a column holds a NULL. The worker accepts a
check only when the parser path, file name, table and column all appear in that file's diff. The
model never writes SQL or a command.

The checks read the repository's registry from `core_registries` (a `samples.json` whose entries
name source archives relative to its folder). They look at up to 30 registered ZIP or TAR archives,
smallest first, up to four checks per review and four conclusive files per check. ZIPs over 4 GiB,
TARs over 256 MiB and SQLite files over 64 MiB are skipped. SQLite files are copied out with their
WAL, SHM and journal and opened read-only. The results go into the private report only, never into
the posted comments.

A miss, a missing drive, an unsupported format or a timeout leaves the check inconclusive. A
schema match supports only the stated predicate, not the parser's output.

## Setup

The reviewer reads the kit's config file (see the root README). It uses `github_owner`,
`owner_id`, `mac_review_repos`, `owner_mac_review_repos`, `mac_review_store` and
`core_registries`, plus two optional keys. `mac_review_store` must contain `{repo}`, since each
repository needs its own database. A repository listed in `repo_owners` is read from that account,
as in the gate.

- `mac_review_upstream_owners`: GitHub owners whose repositories count as trusted upstreams for
  vendored files. Default: `github_owner`.
- `mac_review_guides`: per repository, the files from the base branch given to the model as
  guidance. Default: `.claude/skills/leapp-new-artifact/SKILL.md`.

Install:

    python3 -B install_worker.py            # prints what it would write
    python3 -B install_worker.py --install  # writes the worker and one LaunchAgent per repository

The installer copies the worker and `kit_config.py` to
`~/Library/Application Support/LEAPP/PR Review Worker/` and writes
`~/Library/LaunchAgents/org.<repo>.mac-review.plist`. It does not start them. Load each one with
`launchctl bootstrap gui/$(id -u) <plist>`. Each new pass reads the installed code, so run the
installer again after changing it.

`check_pr_test_data.py` is a copy of `admin/scripts/check_pr_test_data.py` from the LEAPP cores.
The worker uses its helpers to tell which changed modules have test data.

## Tests

    python3 -B -m unittest discover -s mac-pr-reviewer

They use placeholder settings and fake GitHub and model calls, and need no config file. Two manual
checks touch real things:

- `verify_macos_boundary.py` runs harmless canaries under the sandbox profile and expects reads
  and writes outside the scratch folder, fork and running another program to be denied.
- `probe_registered_corpus.py <repo>` runs one file-presence check against that repository's real
  registry and prints only PASS or an error.

`flag_mac_review.yml` was rewritten to read repository variables instead of a fixed account. It has
not been run on GitHub in this form.

## Not included

- Report delivery to a private chat inbox. My setup also renders each report as images and queues
  it into a Codex chat thread with a desktop banner. That part is built around my own app threads,
  so it is left out. Here the report file is the output: read it in `<state>/reports/`.
- The route that reviews the desktop apps, probes, readers and the website. Its review profiles are
  written for those repositories. Nothing in this kit writes a review store for the gate's
  `app_review_repos`, so leave that key empty unless you run your own reviewer for them.
- The installer that patches my existing installations in place, pinned to their file hashes.
