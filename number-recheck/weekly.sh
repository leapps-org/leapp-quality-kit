#!/bin/bash
# Weekly number re-check, for a launch agent or cron entry. Runs every core listed in the config's
# "recheck_weekly_cores" with --file-issues, using the config's "python" interpreter. Uses no model.
# If the corpus roots catalog is missing (drive not mounted), it writes "could not check" to the log
# and exits 2; it never counts that as a pass. It exits 1 if any core's run exits non-zero.
# Log: <work_dir>/number-recheck/weekly.log (a config error that leaves no work_dir goes to stderr).
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
    if not isinstance(cores, list) or not all(isinstance(c, str) and c.isalnum() for c in cores):
        raise kit_config.ConfigError('"recheck_weekly_cores" must be a list of core names such as ["ALEAPP"]')
except kit_config.ConfigError as error:
    print(f'CONFIG {error}')
    sys.exit(2)
print(work)
print(roots)
print(data.get('python') or 'python3')
print(' '.join(cores))
PY
) || { echo "$(date '+%Y-%m-%d %H:%M:%S') weekly.sh COULD NOT CHECK: $SETTINGS" >&2; exit 2; }
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
status=0
for core in $CORES; do
  echo "$(stamp) $core start" >> "$LOG"
  caffeinate -i "$PY" "$HERE/recheck.py" --core "$core" --jobs 6 --file-issues >> "$LOG" 2>&1
  rc=$?
  echo "$(stamp) $core exit $rc" >> "$LOG"
  [ "$rc" -eq 0 ] || status=1
done
echo "$(stamp) weekly run end (exit $status)" >> "$LOG"
exit "$status"
