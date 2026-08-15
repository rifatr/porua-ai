# Running and operating this project

Everything beyond the three-command start in the [README](../README.md).

Every command below works either way. Inside Docker, prefix with `docker compose
exec api`; on your machine, run it directly with the virtualenv active and
`DATABASE_URL` exported.

---

## Running without Docker

If you would rather run the app on your machine and keep only Postgres in a
container. Verified on Python 3.14 — every dependency has a wheel, nothing needs
compiling.

```bash
docker compose up -d db              # Postgres only; port 5432 is published

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

**One gotcha.** `.env` points `DATABASE_URL` at the host `db`, which is the
service name and only resolves *inside* Docker. From your machine it must be
`localhost`:

```bash
export DATABASE_URL="postgresql+asyncpg://porua:porua@localhost:5432/porua"
```

A real environment variable takes priority over `.env`, so exporting it is enough
— no need to edit the file and risk committing a broken one.

```bash
alembic upgrade head
uvicorn app.main:app --reload        # http://localhost:8000
```

Use `--port 8001` if the container is still running on 8000.

---

## Hot reload and rebuilding

`compose.yaml` mounts the source into the container and uvicorn runs with
`--reload`, so editing a file on your machine restarts the server. You only need
to rebuild when a dependency changes:

```bash
docker compose build api && docker compose up -d
```

---

## Tests

```bash
pytest                       # all tests
pytest -q                    # quiet
pytest tests/test_turns.py   # one file
pytest -k "cursor"           # by name
pytest -x -vv                # stop at the first failure, verbose
```

Tests create and use a **separate database**, `porua_test`, built from the models
and dropped fresh each run. They never touch your development data — and never
call Gemini.

## Linting

```bash
ruff check .                 # report
ruff check . --fix           # fix what is safely fixable
```

---

## Migrations

```bash
# 1. change a model in app/models/, then draft a migration from the difference
alembic revision --autogenerate -m "add_document_chunks"

# 2. READ AND EDIT THE GENERATED FILE. Autogenerate is a draft, not an answer.
#    It reliably misses partial indexes, CHECK constraints, generated columns,
#    enum drops, and every data migration.

# 3. prove it reverses — on a THROWAWAY database, never your own
./scripts/check_migration.sh

# 4. apply it to your development database, forward only
alembic upgrade head
```

**Step 3 uses a scratch database on purpose.** A migration that cannot be undone
cannot be trusted, so the round trip has to be run — but running `downgrade`
against a database holding your own data destroys it. Once, here, a failed
`upgrade` piped through `tail` returned exit code 0, so `&&` did not stop the
chain, and the `downgrade -1` that followed reversed the *previous* migration and
dropped the `turns` table. `scripts/check_migration.sh` creates its own database,
round-trips `upgrade → downgrade base → upgrade → check` inside it, and drops it
again. Nothing it does can reach your data.

Other useful ones:

```bash
alembic current              # which revision is applied
alembic history --verbose    # the chain
```

`alembic downgrade` is deliberately absent from this list. Use the script.

`alembic check` has already caught two real bugs in this project: a column type
that had drifted from its model, and an enum type that was created but never
dropped on downgrade.

---

## Resetting the database

```bash
docker compose down -v       # -v deletes the volume, so all data goes
docker compose up -d
docker compose exec api alembic upgrade head
```

Without `-v` the volume survives, which is how *"data must persist between runs"*
is verified:

```bash
docker compose restart api
curl -s localhost:8000/rooms -H "X-Student-Id: $SID"   # your rooms are still there
```

---

## Seeding demo data

Study history cannot be judged on two turns from this morning. This writes six
weeks of them, with no Gemini calls, so it is instant and free:

```bash
docker compose exec api python scripts/seed_demo_data.py
```

It prints the student id and two commands: one reading the history endpoint
directly, one asking the tutor the same question so it reaches the data through
`query_study_history`.

Running it a second time does nothing — it prints the existing id and stops,
rather than doubling the data. To start clean:

```bash
docker compose exec db psql -U porua -d porua \
  -c "DELETE FROM students WHERE display_name = 'Demo Student'"
```

Every foreign key cascades from the student, so that removes the rooms and turns
with it. Only the seeded student is touched.

The seed deliberately includes one **failed** and one **off-topic** turn, which
history must exclude. If either ever appears in the totals, the filter has broken
— and that is the kind of bug that produces plausible numbers nobody questions.

---

## Checking a document before a demo

Runs the real ingestion pipeline against a file and prints what would happen —
which is worth doing before a demo rather than during one:

```bash
docker compose exec -T api python scripts/check_document.py /tmp/handout.pdf
```

---

## Recording Gemini fixtures

Needed after changing a prompt — a fixture is keyed by the exact request text, so
an edited prompt makes existing recordings unreachable. Costs real quota
(currently 4 calls):

```bash
docker compose exec -e LLM_FIXTURE_MODE=record api python scripts/record_fixtures.py
```

It prints, per scenario, the token usage and whether the response passed the
validation pipeline. Anything rejected there is a real prompt weakness worth a
new version, not a fluke. Delete the old files in
`tests/fixtures/llm/recorded/` when re-recording after a prompt change.

---

## Watching what the app is doing

```bash
docker compose logs -f api                    # follow
docker compose exec db psql -U porua -d porua # a SQL prompt
```
