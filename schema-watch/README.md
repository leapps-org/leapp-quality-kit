# Schema watch

Checks new corpus keys for app stores whose tables, columns or preference names differ from what
the artifacts were built on. It only measures and files issues. It never edits code, uses no model
and never blocks a merge.

    python3 schema-watch/schema_watch.py --init
    python3 schema-watch/schema_watch.py [--file-issues [--dry-run]] [--core ALEAPP] [--jobs 3]
    python3 schema-watch/schema_watch.py --keys KEY [KEY ...] [--core ALEAPP]

## What counts as new

The state file `<work_dir>/schema-watch/state.json` maps each key it has seen to the sha256 its
registry gave. A key that is not there, or is there with a different sha256 (a re-capture), is new.
Run `--init` once: it records every registered key as seen and compares nothing, so the first daily
run does not treat the whole corpus as new. Only a run with `--file-issues` (and not `--dry-run`)
marks keys as seen, so a report-only run leaves them for the daily run. A key that could not be
checked is left unseen and is tried again the next day: a failed read, a member that could not be
copied, a cited key that could not be read for a module, or a filing that failed. `--keys` compares
the keys you name and leaves the state file alone. A registry the corpus roots catalog names but
that is not there is reported, and the run exits 2; `--init` then saves nothing.

Each key is checked against the core whose registry holds it, from `core_registries` in the config.
A key in a registry no core is mapped to is logged and skipped.

## What an artifact was built on

For each module on the core's `origin/main`, resolved once to a commit and read with `git archive`
(so no checkout is touched):

1. Its `paths` are matched against the key's member names the way the LEAPP seekers match them:
   `fnmatch` on `"root/" + name`, where `*` also crosses folders.
2. Every matched SQLite file is copied with its `-wal`, `-shm` and `-journal` to a temp folder under
   `<work_dir>/schema-watch/tmp`, opened read-only with Python's `sqlite3`, and its table names and
   `table_xinfo` column names are kept. Android `shared_prefs` XML files give their preference
   names (ElementTree) and plists their top-level keys (plistlib). Nothing is read from the corpus
   in place and nothing is read by a byte scan. Other formats are not compared.
3. The baseline is the union of those names for the same store in every key the module cites in
   `sample_data`. A store is named by its path from the first literal folder of the pattern on
   (for example `pl.solidexplorer2/databases/explorer.db`), so the same store under another root
   or user lines up.

The new key's stores are then compared with the baseline. A table, column or preference name is
"in no cited key" when none of the cited keys' copies of that store has it. A name is "missing here"
only when every compared cited key that has the store has it, so a column one phone maker adds is not
reported as missing on every other maker's key. A missing name that also appears as a word in a
string in the module's source is marked "named in the module". That is a source-token match, not
a trace of what the code reads. A store the module matches in the new key but in none of its cited
keys is listed as having no baseline. A store that could not be read (an encrypted database, for
example) is listed as not compared, and a module with nothing else to report gets the status "not
compared", never "unchanged". It files nothing.

Before comparing, SQLite's own tables (`sqlite_sequence`, `sqlite_stat1` and the rest) and
`android_metadata` are dropped, because they appear with use or with the framework, not with the
app. A table name that ends in an 8 to 16 character hex id after a letter has the id replaced, so
ExoPlayer's `ExoPlayerCacheIndex<uid>` tables line up across devices.

A module counts as changed, and gets an issue, when a store has a name in no cited key, a missing
name that is named in the module, or no baseline. Missing names the module never mentions are
listed in the report as notes and file nothing.

A module with no `sample_data` keys has no baseline and is only counted. A module that cites the
new key was built on it and is skipped.

Each key's member list and store names are cached in `<work_dir>/schema-watch/index/<key>.json`,
keyed by the registry sha256. Only the modules that have a store in a new key get their cited keys
indexed. The first index of a large tar takes a minute or two; after that it is a file read.

## Output and issues

Every run writes `report.md` and `results.json` to `<work_dir>/schema-watch/runs/<stamp>/`. They
hold table, column, preference, module and key names and counts. No row values are read.

`--file-issues` files one `audit-queue` issue in `<owner>/<core>` per changed module, covering
all of that run's new keys for it. The body ends with one hidden marker per key,
`<!-- schema-watch module=NAME key=KEY@SHA -->`, where SHA is the first 12 characters of the key's
registry sha256. A key and capture already in an open issue's marker is not filed again; a
re-captured key is. Before filing, the core checkout's `origin` must be `github.com/<owner>/<core>`. `--dry-run` prints what would be filed and writes no state.

A finding goes to GitHub only when the new key and every cited key it was compared with have
`"allow_public_metadata": true` in their registry entries, because the finding is computed from
both. A missing or false value counts as private: the finding is appended to the private queue
file instead (`private_queue` in the config, default `<work_dir>/schema-watch/private-queue.md`),
which is created readable by its owner only. An existing queue file that others can read is not
written to; the run reports it and exits 2 until the file is `chmod 600`. The `schema-watch` folder
under `work_dir`, with its index, state and run reports, is kept owner-only too.

Exit 0 means every new key was compared and every filing worked. Exit 2 means something could not
be checked (config, corpus drive, a key or a filing); the report says what.

## Daily run

`daily.sh` runs `schema_watch.py --file-issues` with the config's `python` and logs to
`<work_dir>/schema-watch/daily.log`. A missing corpus roots catalog is logged as "could not check"
and exits 2. It uses `caffeinate`, so it is written for macOS. `launchagent.example.plist` starts it
at 06:30; fill in the path and load it with `launchctl bootstrap`.

## Proof

Run on ALEAPP main on 26 November 2026, with the 95 keys ALEAPP cites indexed from the corpus:

- `emu_a15_oss_v12` against every module: 3 changed, 151 unchanged, 3 with no cited keys. One of
  the 3 is HERE WeGo: its `FlutterSharedPreferences.xml` lacks 6 preference names that its cited key
  `emu_a15_oss_v14` has, 3 of them named in the module. v12 was captured before the app was used.
- Solid Explorer on `emu_a15_oss_v17` against its cited `emu_a15_oss_v14`: unchanged, nothing filed.
  `explorer.db` has the same tables and columns in both. In v12 it is not there at all.
- `emu_a15_kdl_v1` (public metadata allowed) with `--file-issues --dry-run`: 59 unchanged, nothing
  filed. Before missing-but-unmentioned names became notes, the same run would have filed 2 issues
  in the core repository, which exercised the public route. The `emu_a15_oss` keys do not set
  `allow_public_metadata`, so their findings went to the private queue.

Indexing the 95 keys the first time took about 95 minutes on a busy machine. Later runs read the
cache; one took 5 seconds.

The tests build small tar and zip keys from real SQLite files (one table exists only in the `-wal`),
and check that the difference is found, that an unchanged store files nothing, and that a private
key never reaches GitHub.

Run them with `python3 -m unittest discover -s schema-watch`.
