#!/usr/bin/env bash
# End-to-end demo against a running container (or local uvicorn on :8000).
# Shows the query before and after the disambiguating backfill, plus the two
# whole-batch rejection paths.
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
SAMPLES="$(cd "$(dirname "$0")/../samples" && pwd)"

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }

say "health"
curl -fsS "$BASE/health"; echo

say "1) initial fallback-day batch (expect 5 rows, 2 pending)"
curl -fsS -F "file=@$SAMPLES/01_initial_fallback_day.csv" "$BASE/upload"; echo

say "GET /traffic BEFORE backfill: only UTC-unique records are served"
curl -fsS "$BASE/traffic" | python3 -m json.tool

say "2) re-uploading the identical file -> idempotent, 0 inserted"
curl -fsS -F "file=@$SAMPLES/01_initial_fallback_day.csv" "$BASE/upload"; echo

say "3) conflict batch: same sample id, different pass_count -> HTTP 409, whole batch rejected"
curl -sS -o /tmp/conflict.json -w "HTTP %{http_code}\n" \
  -F "file=@$SAMPLES/03_conflict_same_id_different_value.csv" "$BASE/upload"
cat /tmp/conflict.json; echo

say "4) illegal timeline batch: 3 readings share one ambiguous wall time -> HTTP 422, rolled back"
curl -sS -o /tmp/illegal.json -w "HTTP %{http_code}\n" \
  -F "file=@$SAMPLES/04_illegal_timeline.csv" "$BASE/upload"
cat /tmp/illegal.json; echo

say "5) disambiguating backfill: one more reading at the repeated 01:20 wall time"
curl -fsS -F "file=@$SAMPLES/02_backfill_resolves_ambiguity.csv" "$BASE/upload"; echo

say "GET /traffic AFTER backfill: pending list empty, every UTC pinned"
curl -fsS "$BASE/traffic" | python3 -m json.tool

say "batch audit trail (rejections persisted despite rollback)"
curl -fsS "$BASE/batches" | python3 -m json.tool
