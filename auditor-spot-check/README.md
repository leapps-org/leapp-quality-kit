# Auditor spot-check

The `leapp-notes-auditor` agent reads an artifact's notes against the code and the run output before
the PR opens. This folder checks the checker. Now and then a second auditor run gets a copy of the
module with one false sentence added to its notes. If the auditor marks that sentence soften or
remove, the run is caught. If not, it is missed. The ledger of those results is the auditor's catch
rate.

It only measures. The real module, PR and run output are never changed, and nothing here blocks a
merge.

## When a run is spot-checked

    python3 auditor-spot-check/spotcheck.py should ALEAPP kwai <commit>

It answers yes when `sha256("<CORE>:<module>:<commit>")` mod `every` is 0 (`every` is 10 unless the
config says otherwise). The same module at the same commit always gets the same answer, so a run
cannot be picked or skipped by asking twice. On ALEAPP at one commit it chose 72 of 623 modules
(11.6%).

## Making the copy

    python3 auditor-spot-check/spotcheck.py plant --core ALEAPP --module kwai \
        --src <code tree the run used> --commit <commit> \
        --run-output <run output folder per corpus key> ...

Each run output folder is named by its corpus key and holds one report folder, the layout
`notes-audit/run_corpus.sh` writes. The tool:

1. Reads each artifact's table in `_lava_artifacts.db` (from a temp copy) and counts rows, and empty
   and filled cells per column. It refuses a run output whose notes differ from the code, since then
   the output is from other code.
2. Picks one false sentence, in hash order, from three shapes. Each names only a column header, a
   corpus key or a count, never a value from the data:
   - `count`: "On corpus key K this artifact reports N rows." N is not the row count on K.
   - `always`: "The X column is filled on every row." X has at least one empty and one filled cell.
   - `meaning`: "The X column holds the time the item was deleted." X is a column with no
     number-like values and no time word in its header. This one is unsupported rather than shown
     false, so the report keeps it apart.
3. Copies the code tree (without `.git`) and the run outputs to `<copies_dir>/<CORE> audit <id>/`,
   with APFS clones where the volume allows. It adds the sentence to the end of the artifact's notes
   in the copied module and in each copied `_lava_data.lava` (the report embeds the notes, so a
   copy with only the code changed would give the plant away). It parses both back to check the
   sentence is there once and nothing else changed. Only plain string notes are planted, never an
   f-string.
4. Writes the answer key `<keys_dir>/<id>.json` and the auditor prompt `<keys_dir>/<id>.prompt.txt`.
   The prompt is the notes-audit workflow's prompt for one module, with the JSON reply spelled out.

`--control` makes the same copy and key but plants nothing. `--shape` picks the shape.

## Running the auditor and scoring it

Give the prompt to a `leapp-notes-auditor` agent (in Claude Code, the Agent tool with that agent
type). Save its reply to a file, then:

    python3 auditor-spot-check/spotcheck.py score <id> <reply file> --transcript <agent transcript .jsonl>

Caught means a `statement` finding with verdict soften or remove whose quote is the planted sentence,
or a part of it, or holds it with little else around it. A quote that holds it inside a much longer
passage is "unclear", for a person to read. Anything else is missed. On a control, the outcome is
"no plant", and any match means the scorer is reading something else as the plant.

The transcript check looks for reads of the answer keys, the ledger, this tool, the kit, or the
original code and run output (which the auditor could diff against the copy). A run with a hit is
left out of the rate.

The ledger line holds the id, module, artifact, shape, outcome, the matching finding's verdict and
tier, and the finding counts. It never holds the auditor's own text, which can describe evidence.

    python3 auditor-spot-check/spotcheck.py report

prints caught of total per shape, the rate over the two measured-false shapes, and the controls.

## Settings

The optional `spot_check` block in the kit config:

| Key | Default | What it is |
|---|---|---|
| `every` | `10` | About one run in this many is spot-checked |
| `copies_dir` | `{work_dir}/leapp-audit-input` | Where the copies the auditor reads are made |
| `keys_dir` | `{work_dir}/auditor-spot-check/keys` | Answer keys and prompts |
| `ledger` | `{work_dir}/auditor-spot-check/ledger.jsonl` | One line per scored run |

The answer keys and the ledger must sit outside `copies_dir`, or the config is refused. None of these
are committed. Delete a copy by hand once its run is scored.

## First runs

RESULTS

## Limits

- The sentence always goes at the end of one artifact's notes. Real errors sit anywhere.
- Three shapes. A wrong epoch, a wrong source path or a wrong "as stored" claim are not planted yet.
- One auditor run per copy, so a single miss says little. The rate means something after tens of runs.
- SQLite run output only: the measurements come from `_lava_artifacts.db`.
- Nothing calls `should` on its own yet. The session that runs the auditor before an artifact PR has
  to ask it.

Tests: `python3 -m unittest discover -s auditor-spot-check` (made-up fixtures, no corpus, no config).
