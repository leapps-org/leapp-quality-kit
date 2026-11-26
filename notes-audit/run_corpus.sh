#!/bin/bash
# Run a prepared round's profile on each of its corpus keys.
#   run_corpus.sh "<round folder written by prepare.py>" [jobs]
# Output: <round folder>/run_output/<key>/, logs in <round folder>/logs/. Prints only keys that failed.
R="$1"; JOBS="${2:-6}"
# The interpreter that has the core's requirements installed. Override with LEAPP_PYTHON.
PY="${LEAPP_PYTHON:-python3}"
[ -f "$R/round.json" ] || { echo "FAIL not a prepared round folder: $R"; exit 1; }
USERBASE=$("$PY" -m site --user-base) || { echo "FAIL user base"; exit 1; }
CORE=$("$PY" -I -c "import json,sys; print(json.load(open(sys.argv[1]))['core'].lower())" "$R/round.json") || exit 1
REG=$("$PY" -I -c "import json,sys; print(json.load(open(sys.argv[1]))['registry'])" "$R/round.json") || exit 1
PROFILE=$("$PY" -I -c "import json,sys; print(json.load(open(sys.argv[1]))['profile'])" "$R/round.json") || exit 1
mkdir -p "$R/run_output" "$R/logs" "$R/home" || exit 1
# A private HOME keeps these runs out of the LEAPP history; PYTHONUSERBASE keeps the user site packages.
export PYTHONUSERBASE="$USERBASE" HOME="$R/home" PYTHONDONTWRITEBYTECODE=1 R PY REG CORE PROFILE
one() {
  KEY="$1"
  IMAGE=$("$PY" -I -c "import json,os,sys; r=sys.argv[1]; print(os.path.join(os.path.dirname(r), json.load(open(r))['samples'][sys.argv[2]]['match']['zip']))" "$REG" "$KEY") || { echo "FAIL registry $KEY"; return 1; }
  [ -f "$IMAGE" ] || { echo "FAIL missing image $KEY"; return 1; }
  case "$IMAGE" in *.zip) TYPE=zip ;; *.tar) TYPE=tar ;; *.tar.gz|*.tgz) TYPE=gz ;; *) echo "FAIL type $KEY"; return 1 ;; esac
  OUT="$R/run_output/$KEY"
  [ ! -e "$OUT" ] || { echo "FAIL output exists $KEY"; return 1; }
  mkdir -p "$OUT" || return 1
  (cd "$R/src" && "$PY" "$CORE.py" -t "$TYPE" -i "$IMAGE" -o "$OUT" -m "$R/$PROFILE" </dev/null > "$R/logs/$KEY.log" 2>&1)
  CODE=$?
  grep -q 'Report generation Completed' "$R/logs/$KEY.log" || echo "FAIL $KEY did not complete (exit $CODE)"
  [ "$(grep -c Traceback "$R/logs/$KEY.log")" = 0 ] || echo "WARN $KEY log has a traceback"
}
export -f one
xargs -n 1 -P "$JOBS" /bin/bash -c 'one "$0"' < "$R/keys.txt"
echo FINISHED
