#!/bin/bash
# Daily schema watch, for a launch agent or cron entry. Compares the stores in any new corpus key with
# what each artifact was built on and files audit-queue issues (private keys go to the private queue
# file). Uses the config's "python" interpreter and no model. A missing config or corpus roots catalog
# is logged as "could not check" and exits 2; it is never counted as a pass.
# Log: <work_dir>/schema-watch/daily.log (a config error that leaves no work_dir goes to stderr).
exec </dev/null
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
HERE="$(cd "$(dirname "$0")" && pwd)" || exit 1
SETTINGS=$(/usr/bin/env python3 -I - "$HERE/.." <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import kit_config
try:
    data = kit_config.load()
    work = kit_config.expand(kit_config.need(data, 'work_dir'))
    roots = kit_config.expand(kit_config.need(data, 'corpus_roots'))
except kit_config.ConfigError as error:
    print(f'CONFIG {error}')
    sys.exit(2)
print(work)
print(roots)
print(data.get('python') or 'python3')
PY
) || { echo "$(date '+%Y-%m-%d %H:%M:%S') daily.sh COULD NOT CHECK: $SETTINGS" >&2; exit 2; }
WORK=$(sed -n 1p <<<"$SETTINGS"); ROOTS=$(sed -n 2p <<<"$SETTINGS"); PY=$(sed -n 3p <<<"$SETTINGS")
LOG="$WORK/schema-watch/daily.log"
mkdir -p "$(dirname "$LOG")" || exit 1
stamp() { date '+%Y-%m-%d %H:%M:%S'; }
echo "$(stamp) daily run start" >> "$LOG"
if [ ! -f "$ROOTS" ]; then
  echo "$(stamp) COULD NOT CHECK: $ROOTS is not there (corpus drive not mounted?)" >> "$LOG"
  exit 2
fi
caffeinate -i "$PY" "$HERE/schema_watch.py" --file-issues >> "$LOG" 2>&1
rc=$?
echo "$(stamp) daily run end (exit $rc)" >> "$LOG"
exit "$rc"
