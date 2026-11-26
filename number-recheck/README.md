# Number re-check

Re-checks every `sample_data` row count a LEAPP core declares, against `origin/main`.

`recheck.py` wraps the core's own `admin/scripts/validate_sample_data.py --run KEY`, which parses
one corpus key end to end, refuses a count from a run that did not finish, caught a database error
or had a capability disabled, and compares each artifact's LAVA row count with its `sample_data`.
The wrapper adds:

- a clean detached worktree of `origin/main`, so the counts describe main and not a checkout
  someone is editing;
- every key the core cites, resolved to the `samples.json` that registers it, through the corpus
  roots catalog named by `corpus_roots` in the kit's config;
- several keys at once, each with its own HOME, TMPDIR and log;
- one report grouped by module, with each key matched, drifted or could not check. A key that could
  not be checked is never counted as a pass.

It edits no module and files no issue.

    python3 number-recheck/recheck.py --core ALEAPP [--keys k1 k2] [--jobs 6] [--out DIR]

The core checkout is read from `<repos_dir>/<core>`. Output goes to
`<work_dir>/number-recheck/<core> <stamp>/`: `report.md`, `results.json`, and per key
`keys/<key>/validate.log` plus the tool's own run log in `keys/<key>/tmp/`.

## Corpus roots catalog

`corpus_roots` points at a JSON file that lists where the registries are:

```json
{
  "version": 2,
  "roots": [
    {"path": "/path/to/corpus", "registry_pattern": "*/samples.json", "exclude_top_level": ["Vehicles"]},
    {"path": "/path/to/corpus/Vehicles", "registries": ["samples.json"]}
  ]
}
```

Each registry is `{"version": 1, "samples": {"<key>": {"match": {"zip": "<file relative to the registry>", "sha256": "..."}, ...}}}`.

## Prove it can fail before you trust it

Run it once on two keys you know match. Then, in a throwaway worktree, raise one declared count
and call `recheck.check_key` on that tree. On ALEAPP (26 November 2026) a count raised from 1 to 8
came back as status "drifted", declared 8, actual 1, with no run errors. A missing optional
dependency (`sqlcipher3`) made two keys "could not check" before it was installed, which is the
other outcome you want to see once.

## Not built yet

Issue filing (one issue per drifted module) and a weekly scheduled run. So far it has been run on
ALEAPP only.
