# Unread columns

Lists, for each SQLite table an artifact reads, the columns its SQL never reads. It runs during
the corpus run an artifact PR already gets, and it is a warning list for the session working the
PR. It never edits a module, never files anything and has no part in the review gate.

    python3 unread-columns/unread_columns.py --core ALEAPP --modules kwai cmhTags --keys KEY1 [KEY2] [--tree PATH]

`--tree` is the core checkout to run, normally the PR's worktree (default `<repos_dir>/<core>`).
The image for each key comes from `core_registries` in the kit's config, or `--registry`.
Output goes to `<unread_columns.out_dir>/<core> <stamp>/` (default `<work_dir>/unread-columns`):
`report.md`, `results.json`, and per key the core's output, its log and the trace.
Exit 0 means the report was written, whatever it lists. Exit 2 means it could not run.

It runs the checkout's own code with your account, the same as the corpus run it sits beside, so
point it only at a checkout you would run anyway. The run folder is created readable by you only,
because it holds the core's staged copies of the evidence. A report on a checkout with local
changes says so next to the commit.

## How it measures

The core runs once per key with a profile holding every artifact of the named modules, under
`trace_sqlite.py`. That wrapper gives every SQLite connection an authorizer before the core is
imported. SQLite calls the authorizer while it prepares a statement, once for each column the
statement reads, with the table and column it resolved. So joins, aliases, views and helper
functions are worked out by SQLite, and no SQL text is parsed. The authorizer always allows the
read and catches its own errors, so it cannot change what the core does. SQLite's statement cache is
turned off on traced connections, so a statement a second artifact reuses is still seen (on
Python 3.11 and later; Python 3.10 reuses it anyway, so run the core with a newer Python).

A read is credited to the artifact function on the stack. A helper in another artifact file that
it calls counts as the artifact's read.

After the run, each store an artifact read is copied with its `-wal`, `-shm` and `-journal` into a
temporary folder and opened read-only. Its `table_xinfo` gives every column. Columns no statement
read are the unread list. Reads of the core's own output (the LAVA database, the timeline) are
left out.

For each unread column it reports counts only: rows, rows where the column is set, distinct values,
and any read column of the same table it equals on every row. Values are compared byte for byte, so
a column declared with an app's own collation is still counted. No value from the evidence is
written. The store is named by its file name only when a module path pattern names that file
literally; any other store is "store 1", "store 2", because a file name matched by a wildcard can
carry an account or contact id. Table and column names are written as the store has them. A store
that names its tables after a contact puts that in the report, so the report stays local and only
the decisions go in the PR.

The report has two parts per key. "Read by no profiled artifact" lists, per table, the columns no
artifact of those modules reads, with NULL, empty and constant columns folded into one line each.
"Per artifact" lists, per artifact, how many columns of each table it read and the columns a
sibling reads that it does not. The LAVA row count says whether the artifact produced rows on that
key. A table read on a key where the artifact wrote no rows still describes the store.

## What it cannot tell

- A column used only in a JOIN or WHERE counts as read. The tool measures unread, not unreported.
- A statement that only counts rows touches the table without reading a column, so the table is
  listed with every column unread.
- `SELECT *` reads every column. When one statement read every column of a table, the report says
  so, because the module may still drop columns in Python.
- Only SQLite opened through Python's `sqlite3` is traced. A store read with `sqlcipher3` or `apsw`,
  or any plist, protobuf, JSON, XML or LevelDB store, shows as "no SQLite read traced", which is not
  a finding.
- A store opened in memory, or attached under a name the trace could not tie to a file, is listed
  under "Could not measure".
- A table a module reads from its own temporary copy (outside the staged data) is listed under
  "Could not measure", since the copy is usually gone by the end of the run.
- The core must finish and print `Report generation Completed`, or the key is "could not check".

So far it has been run on ALEAPP only. The other cores take the same arguments, but their staging
folders and wrappers have not been exercised with it.

## Sorting the list

The session working the PR sorts every listed column without asking:

- Skip, with the reason: the same as a shown column, constant on every corpus row, NULL everywhere,
  or plain bookkeeping (row ids, sync flags, retry counters).
- Build "as stored": clear evidence such as a name, a time, a path, user text or a file name.

It asks the maintainer only about a varying column with no sourced meaning, anything that needs a
new artifact, and anything touching credentials or keys. Each decision gets one line in the PR
body. The list is a warning, never a merge blocker.

## Tests

    python3 -m unittest discover -s unread-columns

They build a small fake core and store (WAL mode, with rows only in the `-wal`) and check the
unread list, the `SELECT *` case, the helper credit, that the core's output database is left out,
that a wildcard-matched file name and a planted value never reach the output, that the staged
`-wal` is unchanged, that a failing authorizer leaves the query's result alone, and that the review
gate does not import this tool.
