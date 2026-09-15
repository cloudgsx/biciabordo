#!/bin/sh
set -e

DB=${BICITREN_DB:-data/bicitren.sqlite}

# Renfe republishes both feeds nightly and the Cercanías feed is only a rolling
# ~30 day window, so a stale database quietly loses commuter connections.
# Rebuild on first boot, and whenever the file is older than a day.
WANT=$(python -c 'from app.gtfs.ingest import SCHEMA_VERSION; print(SCHEMA_VERSION)')
HAVE=$(python - "$DB" <<'EOF' 2>/dev/null || true
import sqlite3, sys
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    print(row[0] if row else "")
except Exception:
    print("")
EOF
)

if [ ! -f "$DB" ]; then
  echo "no timetable database, building..."
  python -m app.gtfs.ingest --db "$DB"
elif [ "$HAVE" != "$WANT" ]; then
  # The volume outlives the image: a database built by an older build is missing
  # columns the new code selects, and every request would 500 on "no such column".
  echo "timetable schema $HAVE != $WANT, rebuilding..."
  python -m app.gtfs.ingest --db "$DB"
elif [ -n "$(find "$DB" -mtime +1 2>/dev/null)" ]; then
  echo "timetable database older than 24h, rebuilding..."
  python -m app.gtfs.ingest --db "$DB"
fi

# The place gazetteer is small, static and independent of the timetable, so it
# is built once and left alone. A failure here is not fatal: place lookups fall
# back to OSM, which is merely the old behaviour.
PLACES=${BICITREN_PLACES:-data/places.sqlite}
if [ ! -f "$PLACES" ]; then
  echo "no place gazetteer, building..."
  python -m app.gazetteer --db "$PLACES" || \
    echo "gazetteer build failed; place search will fall back to OSM"
fi

exec "$@"
