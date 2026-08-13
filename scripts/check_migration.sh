#!/usr/bin/env bash
#
# Prove a migration reverses, without touching your data.
#
#   ./scripts/check_migration.sh
#
# A migration that cannot be undone cannot be trusted, so the round trip has to be
# run. Running it against the development database destroys whatever is in there —
# which happened once in this project: a failed `alembic upgrade` was piped through
# `tail`, so the pipeline exited 0, `&&` did not stop the chain, and the
# `downgrade -1` that followed reversed the *previous* migration and dropped the
# `turns` table with every row in it.
#
# Two lessons, both baked in below:
#
#   set -euo pipefail   - a failed command in a pipeline stops the script
#   a scratch database  - there is nothing here that can reach real data
#
set -euo pipefail

SCRATCH="porua_migrationcheck"
URL="postgresql+asyncpg://porua:porua@db:5432/${SCRATCH}"

psql() { docker compose exec -T db psql -qU porua -d postgres "$@" >/dev/null; }
run()  { docker compose exec -T -e DATABASE_URL="$URL" api alembic "$@"; }

cleanup() {
    psql -c "DROP DATABASE IF EXISTS ${SCRATCH}" || true
}
# Runs on success, failure, and Ctrl-C alike, so a broken run does not leave the
# scratch database behind to confuse the next one.
trap cleanup EXIT

echo "Creating scratch database ${SCRATCH}..."
psql -c "DROP DATABASE IF EXISTS ${SCRATCH}"
psql -c "CREATE DATABASE ${SCRATCH}"

echo "  upgrade head"     && run upgrade head     >/dev/null
echo "  downgrade base"   && run downgrade base   >/dev/null
echo "  upgrade head"     && run upgrade head     >/dev/null
echo "  check"            && run check

echo
echo "Migrations round-trip cleanly. Your own database was never touched."
echo "Apply them with: docker compose exec api alembic upgrade head"
