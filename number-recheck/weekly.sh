#!/bin/bash
# Weekly number re-check, for a launch agent or cron entry. Runs every core listed in the config's
# "recheck_weekly_cores" with --file-issues, using the config's "python" interpreter. Uses no model.
# If the corpus roots catalog is missing (drive not mounted), it writes "could not check" to the log
# and exits 2; it never counts that as a pass. Log: <work_dir>/number-recheck/weekly.log.
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
    cores = kit_config.need(data, 'recheck_weekly_cores')
except kit_config.ConfigError as error:
    sys.exit(f'CONFIG {error}')
print(work)
print(roots)
print(data.get('python') or 'python3')
print(' '.join(cores))
PY
) || { echo "weekly.sh: $SETTINGS" >&2; exit 2; }
WORK=$(sed -n 1p <<<"$SETTINGS"); ROOTS=$(sed -n 2p <<<"$SETTINGS")
PY=$(sed -n 3p <<<"$SETTINGS"); CORES=$(sed -n 4p <<<"$SETTINGS")
LOG="$WORK/number-recheck/weekly.log"
mkdir -p "$(dirname "$LOG")" || exit 1
stamp() { date '+%Y-%m-%d %H:%M:%S'; }
echo "$(stamp) weekly run start" >> "$LOG"
if [ ! -f "$ROOTS" ]; then
  echo "$(stamp) COULD NOT CHECK: $ROOTS is not there (corpus drive not mounted?)" >> "$LOG"
  exit 2
fi
for core in $CORES; do
  echo "$(stamp) $core start" >> "$LOG"
  caffeinate -i "$PY" "$HERE/recheck.py" --core "$core" --jobs 6 --file-issues >> "$LOG" 2>&1
  echo "$(stamp) $core exit $?" >> "$LOG"
done
echo "$(stamp) weekly run end" >> "$LOG"
