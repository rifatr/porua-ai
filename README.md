# Porua AI — Backend

A backend for an AI-powered study app. A student creates a **room** for one topic, has short
conversations with an AI tutor inside it, and uploads class files as source material. Over time
this builds a study history they can query.

Built for the Monsha Software Engineer technical assessment. The brief is in
[`docs/ASSESSMENT.md`](docs/ASSESSMENT.md).

---

## Status

This is built in vertical slices — each one is a working path from migration through to endpoint
and tests, rather than a layer at a time. The reasoning is in
[`docs/PLAN.md` §13](docs/PLAN.md).

| Slice | What it delivers | State |
|---|---|---|
| **S0** Spine | Compose, settings, error format, cursor paging, test harness, health checks | ✅ Done |
| **S1** Rooms | Create / list / open / delete rooms | ✅ Done |
| **S2** Turns | Talk to Gemini, record every attempt, inspect a turn | ✅ Done |
| **S3** Reliability | Parse → schema → content checks → repair loop | ⬜ Not started |
| **S4** Tools | Tool registry with hard limits, study-history and calculator tools | ⬜ Not started |
| **S5** Documents | Upload and extract `.docx` / `.pdf` / `.pptx`, search over them | ⬜ Not started |
| **S6** Skills | Quiz builder and step-by-step maths solver, both verified in code | ⬜ Not started |
| **S7** History | Study history over a date range | ⬜ Not started |

**Everything below describes what is actually built and running.** Anything not yet built is
marked as such rather than described as if it exists.

---

## Quick start

Requires Docker. Nothing else — Python, Postgres and every dependency live in containers.

```bash
cp .env.example .env          # then paste your Gemini key into GEMINI_API_KEY
docker compose up -d --build
docker compose exec api alembic upgrade head
```

That's it. Verified from a clean slate (`docker compose down -v` and back up).

- **Swagger:** <http://localhost:8000/docs>
- **Health:** `curl localhost:8000/healthz`

**Hot reload is on.** `compose.yaml` mounts the source into the container and uvicorn runs
with `--reload`, so editing a file on your machine restarts the server. You only need to rebuild
when a dependency changes:

```bash
docker compose build api && docker compose up -d
```

Migrations are run explicitly rather than on container start, so you can see what the database did
and roll it back by hand.

**A Gemini key is only needed to run a turn.** Everything else — rooms, students, migrations —
works without one, and the whole test suite runs offline by design (see [Testing](#testing)).

### Try it

```bash
# 1. Create a student. There is no login (the brief excludes it), so the id
#    returned here goes in the X-Student-Id header on every other request.
SID=$(curl -s -X POST localhost:8000/students \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"Rifat","education_level":"Class 8"}' | jq -r .id)

# 2. Create a room — a plain database write, no AI involved, instant.
curl -s -X POST localhost:8000/rooms \
  -H 'Content-Type: application/json' -H "X-Student-Id: $SID" \
  -d '{"title":"Grade 7 Algebra: Solving Equations"}' | jq

# 3. List rooms. Cursor-paged; feed next_cursor back as ?cursor= for the next page.
curl -s "localhost:8000/rooms?limit=2" -H "X-Student-Id: $SID" | jq

# 4. Ask the tutor something. This is the slow, billable call — expect seconds.
curl -s -X POST "localhost:8000/rooms/$ROOM_ID/turns" \
  -H 'Content-Type: application/json' -H "X-Student-Id: $SID" \
  -d '{"message":"How do I solve 3x + 6 = 15?"}' | jq

# 5. Inspect that turn in full: the prompt sent, its version and checksum, every
#    model attempt including failures, and token usage.
curl -s "localhost:8000/turns/$TURN_ID" -H "X-Student-Id: $SID" | jq

# 6. Delete a room, twice. Both return 204 — it is idempotent — and the row survives.
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE "localhost:8000/rooms/$ROOM_ID" -H "X-Student-Id: $SID"
curl -s "localhost:8000/rooms/$ROOM_ID" -H "X-Student-Id: $SID" | jq .archived_at
```

Step 5 is the one worth looking at. A real turn against `gemini-2.5-flash` reports something like:

```json
"tokens": { "prompt": 496, "output": 278, "thought": 1167, "total": 1941 }
```

Thinking cost four times the visible answer. It is billed and never appears in the reply, which is
why it is counted separately rather than folded into `output`.

---

## Running without Docker

If you would rather run the app on your machine and keep only Postgres in a container. Verified on Python 3.14 — every dependency has a wheel, nothing needs compiling.

```bash
docker compose up -d db              # Postgres only; port 5432 is published

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

**One gotcha.** `.env` points `DATABASE_URL` at the host `db`, which is the service name and only resolves *inside* Docker. From your machine it must be `localhost`:

```bash
export DATABASE_URL="postgresql+asyncpg://porua:porua@localhost:5432/porua"
```

A real environment variable takes priority over `.env`, so exporting it is enough — no need to edit the file and risk committing a broken one.

```bash
alembic upgrade head
uvicorn app.main:app --reload        # http://localhost:8000
```

Use `--port 8001` if the container is still running on 8000.

---

## Everyday commands

Every command works either way. Inside Docker, prefix with `docker compose exec api`; on your machine, run it directly with the virtualenv active and `DATABASE_URL` exported.

### Tests

```bash
pytest                       # all 54
pytest -q                    # quiet
pytest tests/test_turns.py   # one file
pytest -k "cursor"           # by name
pytest -x -vv                # stop at the first failure, verbose
```

Tests create and use a **separate database**, `porua_test`, built from the models and dropped fresh each run. They never touch your development data — and never call Gemini.

### Linting

```bash
ruff check .                 # report
ruff check . --fix           # fix what is safely fixable
```

### Migrations

```bash
# 1. change a model in app/models/, then draft a migration from the difference
alembic revision --autogenerate -m "add_document_chunks"

# 2. READ AND EDIT THE GENERATED FILE. Autogenerate is a draft, not an answer.
#    It reliably misses partial indexes, CHECK constraints, generated columns,
#    enum drops, and every data migration.

# 3. apply it
alembic upgrade head

# 4. prove it reverses — a migration that cannot be undone cannot be tested
alembic downgrade -1
alembic upgrade head

# 5. confirm the models and the migrations now agree
alembic check                # must say: No new upgrade operations detected
```

Other useful ones:

```bash
alembic current              # which revision is applied
alembic history --verbose    # the chain
alembic downgrade base       # unwind everything
```

`alembic check` has already caught two real bugs in this project: a column type that had drifted
from its model, and an enum type that was created but never dropped on downgrade.

### Resetting the database

```bash
docker compose down -v       # -v deletes the volume, so all data goes
docker compose up -d
docker compose exec api alembic upgrade head
```

Without `-v` the volume survives, which is how *"data must persist between runs"* is verified:

```bash
docker compose restart api
curl -s localhost:8000/rooms -H "X-Student-Id: $SID"   # your rooms are still there
```

### Recording Gemini fixtures

Only needed after changing a prompt, and it costs real quota:

```bash
docker compose exec -e LLM_FIXTURE_MODE=record api python scripts/record_fixtures.py
```

### Watching what the app is doing

```bash
docker compose logs -f api                    # follow
docker compose exec db psql -U porua -d porua # a SQL prompt
```

---

## Rooms and turns

Worth stating plainly, because the whole data model follows from it:

- A **room** is a place — one topic, created once, alive for weeks. Creating one is a database
  write. No AI call, no cost, cannot really fail.
- A **turn** is one question-and-answer inside that room. This is where Gemini is called, tools
  may run, output is validated, and failures are recorded.

One room holds many turns. Uploaded files attach to the **room**, so every turn in it can search
them.

A turn is *not* a message. A single turn may involve several Gemini calls — a tool round-trip, a
retry, a repair after malformed JSON — while the student sees one answer. That is why the brief
asks to inspect a turn's *"model attempts"* (plural), and why `turn_attempts` hangs off `turns`
rather than off messages.

---

## Layout

```
app/
  api/
    errors.py       one RFC 9457 error shape for the entire API
    pagination.py   cursor paging helpers
    deps.py         FastAPI dependencies — the only place identity enters
    routes/         HTTP layer: thin, no business logic
  db/               engine, session, declarative base + naming convention
  models/           SQLAlchemy models (the database)
  schemas/          Pydantic models (the API contract) — deliberately separate
  services/         business logic; every function is scoped to one student
migrations/         Alembic; one migration per slice
tests/
docs/               the brief, the plan, the assumptions register
```

`models/` and `schemas/` are kept apart on purpose: the API contract should not shift every time a
column is added, and internal columns should never be exposed by accident.

---

## Important decisions

Fuller reasoning for each is in [`docs/PLAN.md`](docs/PLAN.md). These are the ones I would defend.

### Every model attempt is a database row, not a log line

The brief asks to *"inspect a turn in enough detail to understand its prompt, model attempts, tool
activity, failures, token usage, and final saved result."* That sentence is really a schema
specification. Attempts — **including the ones that fail** — are stored as first-class rows from
the first migration that touches them, so the inspection endpoint is a query rather than a
retrofit. Log files could not answer it later.

### Soft delete, never hard delete

`DELETE /rooms/{id}` sets `archived_at`; it does not remove the row. A hard delete would cascade to
the room's turns, messages and uploaded files, punching a hole in the study history the brief asks
us to query — *"what did I study in July"* would silently come back wrong.

Consequences, deliberately chosen:

- `GET /rooms` hides archived rooms; `?include_archived=true` shows them.
- `GET /rooms/{id}` still works for an archived room. Deleting hides a room; it does not erase it.
- **Study history will include archived rooms.** A reviewer could reasonably expect the opposite,
  so: deleting a room must not rewrite what you studied in July.

### `education_level` on the student, free text, no CHECK constraint

Grade drives *reading level* — vocabulary, sentence length, how much to spell out — which is a
property of **who is asking**, not of the topic. The topic's depth is already in the room title. So
it lives on `students`, not `rooms`.

It is free text with no constraint on its values, and that is a deliberate contrast with a column
like `subject`:

> A controlled vocabulary exists to make **filtering and grouping** reliable.
> Nothing filters on this column — it is **prompt input**.

"Class 8" and "class 8" fragmenting costs nothing, because no query groups by it. A closed set
would look rigorous and be wrong: it would have no valid value for a university student, and
encoding one country's system ("Grade 7", "AP") would exclude Class/SSC/HSC, Year/GCSE/A-Level, and
every adult learner. It is `NOT NULL`, though — every tutor prompt reads it.

### Cursor paging everywhere, never `OFFSET`

`OFFSET 5000` makes Postgres walk and discard 5,000 rows on every request, so it degrades exactly
as a student's history grows — which is the property the brief asks about. Every list endpoint
takes an opaque `cursor` instead.

The cursor carries `(sort_value, id)`, not just the timestamp. Without the id tie-breaker, rows
sharing a timestamp can be duplicated across pages or skipped entirely. There is a test that walks
every page and asserts neither happens.

### Another student's room returns 404, not 403

A 403 confirms the id exists. There is no reason to give that away, and no feature needs the
distinction.

### Authorisation lives in the service layer, not the endpoint

Every service function takes the resolved `Student` object and filters on `student_id`. Identity
enters the system in exactly one place — the `current_student` dependency, which reads the
`X-Student-Id` header.

This matters more later than it does now: in S4 the agent tools call this same service layer.
Because the student comes from the request context rather than from a function argument, **there is
no parameter the language model could set to reach another student's data.**

### Columns are added by the slice that reads them

Four columns were designed and then removed during review — `subject`, `status`, `rooms.grade_level`
and a grade `CHECK` constraint — because nothing read them yet. The test applied was: *does a
feature in the plan read this column?* If not, it waits for the slice that does.

One migration per slice, rather than one large one on day one or nine reactive ones.

### Postgres, not SQLite

The brief leaves the choice open but grades *"how the design behaves as history grows"*. Postgres
gives partial indexes, `jsonb` for model-attempt payloads, and generated `tsvector` columns for
document search in S5. SQLite would make that criterion unanswerable.

Already in use: the room list is served by a **partial index** —

```sql
CREATE INDEX ix_rooms_student_active ON rooms (student_id, last_activity_at DESC)
    WHERE archived_at IS NULL;
```

Archived rooms never enter the index, so it stays small however many rooms a student retires.

### One error shape, including FastAPI's own

All errors follow RFC 9457 `application/problem+json`, including FastAPI's built-in 422s, which are
rewritten so a client never meets two different error shapes. The `code` field is stable and
machine-readable; `detail` is written for humans and may change. Clients should switch on `code`.

---

## Prompts

Live in `prompts/<name>/v<N>.md`. Currently one: `tutor_system/v1.md`.

The scheme, since the brief asks for prompts to be *"readable and versioned"*:

- One file per prompt under `prompts/<name>/vN.md`, with YAML front-matter recording name, version,
  model, temperature, and a changelog line saying what changed and **why**.
- Jinja2 in strict mode, so a missing variable raises at render time instead of quietly producing a
  worse prompt.
- Every stored attempt records `prompt_name`, `version` and a checksum of the exact text, so any
  saved answer can be traced back to the prompt that produced it.
- **A prompt file is never edited once it has produced a stored attempt** — a new version is added
  instead. Editing in place would silently invalidate that audit trail.

---

## Known limitations

Current, honest:

- **S0 to S2 are built.** No validation of model output yet (that is S3, deliberately — see
  *Important decisions*), and no documents, tools or skills.
- **Room context is the last 6 turns**, not a summary. Long rooms therefore mean long prompts.
  The fix is a `room_summaries` table; it is designed, not built.
- **Turns are synchronous.** A request blocks for the several seconds Gemini takes.
- **No authentication.** The brief excludes a login flow. `X-Student-Id` is trusted as sent, so
  anyone can act as any student by changing a header. Identity is modelled properly, so adding real
  auth means replacing one dependency rather than reshaping the schema.
- **No rate limiting** on any endpoint.
- **Migrations are not run automatically** on startup — one explicit command after `up`.
- **`pip install -e .` in the Dockerfile runs before the source is copied**, so it installs
  dependencies but registers no package. Imports work via `PYTHONPATH=/code`. Harmless, but it is a
  wart.

Planned and already decided against (with reasons in [`docs/PLAN.md` §16](docs/PLAN.md)): no web
UI, no OCR for scanned PDFs, no streaming responses, no job queue, no vector search.

---

## Testing

Commands are under [Everyday commands](#everyday-commands).

**The suite runs with no Gemini API key.** `get_llm` is a FastAPI dependency, so tests override it
with a fake that records the prompts it was given — which is how we assert that the education level
and the room's earlier turns genuinely reached the model, rather than trusting the template.

Real responses are recorded separately into `tests/fixtures/llm/recorded/` by
`scripts/record_fixtures.py`, so S3 can build its checks against how the model actually behaves. That is not only about cost — you cannot ask the real model to return a
quiz with duplicate options on demand, so the failure modes the brief names have to be written by
hand as fixtures. Testing the reliability layer at all depends on it.

Two properties worth knowing:

- Each test runs inside a transaction that is rolled back afterwards, so tests cannot see each
  other's rows and execution order does not matter.
- The HTTP client shares the test's session, so a test can assert on rows the endpoint just wrote.

The migration is verified by round trip — `upgrade head` → `downgrade base` → `upgrade head` — plus
`alembic check` for drift between the models and the migration. Both caught real bugs: an enum type
that was never dropped on downgrade, and a column type that had diverged from its model.

---

## What I would do with more time

In priority order:

1. **Finish S2–S7.** The reliability layer is the centre of the brief and the most interesting part
   of the problem.
2. **Rolling room summaries.** Long rooms currently mean long context. A `room_summaries` table
   storing a summary up to turn N would make opening a 500-turn room cost the same as a 5-turn one.
   Designed in [`docs/PLAN.md` §5](docs/PLAN.md), not built.
3. **Measure instead of assert.** Seed 50,000 turns and run `EXPLAIN` on the cursor queries, so
   "this scales" is a number rather than a claim.
4. **Hybrid retrieval.** Document search is lexical (Postgres full-text). Adding embeddings behind
   the same `Retriever` interface would improve recall on paraphrased questions.
5. **Monthly partitioning** of `turn_attempts` and `messages`, the two tables that grow without
   bound, and a retention job for the large `raw_response` payloads.
6. **Real auth**, replacing the `X-Student-Id` header.

---

## Assumptions

Every assumption made, with its reasoning, is recorded in
[`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md). Two questions met the brief's *"essential and
unclear"* bar and were asked; the rest were resolved as documented decisions.
