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
| **S3** Reliability | Parse → schema → content checks → repair loop, with retries | ✅ Done |
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

Needed after changing a prompt — a fixture is keyed by the exact request text, so an edited prompt
makes existing recordings unreachable. Costs real quota (currently 4 calls):

```bash
docker compose exec -e LLM_FIXTURE_MODE=record api python scripts/record_fixtures.py
```

It prints, per scenario, the token usage and whether the response passed the validation pipeline.
Anything rejected there is a real prompt weakness worth a new version, not a fluke. Delete the old
files in `tests/fixtures/llm/recorded/` when re-recording after a prompt change.

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
  llm/              the provider boundary: one Protocol, a Gemini client, record/replay
  reliability/      parse → schema → content. Pure functions, no I/O, no database
  models/           SQLAlchemy models (the database)
  prompts/          the loader; the prompt text itself lives in prompts/ at the root
  schemas/          Pydantic models (the API contract) — deliberately separate
  services/         business logic; every function is scoped to one student
migrations/         Alembic; one migration per slice
prompts/            versioned prompt files, one directory per prompt
tests/
docs/               the brief, the plan, the assumptions register
```

`reliability/` never calls the model and never touches the database — it only answers "is this
response acceptable, and if not, exactly what is wrong with it". Deciding to *call again* is a
decision about the turn, because it costs money and writes a row, so it lives in
`services/turn.py`. That split is why the checks run in microseconds and the loop is readable.

`models/` and `schemas/` are kept apart on purpose: the API contract should not shift every time a
column is added, and internal columns should never be exposed by accident.

---

## Important decisions

Fuller reasoning for each is in [`docs/PLAN.md`](docs/PLAN.md). These are the ones I would defend.

### Nothing unchecked reaches the student

The brief's instruction — *"assume the Gemini model will sometimes ignore instructions, return
malformed data, or produce structurally valid but poor content"* — names three different problems,
so `app/reliability/` answers them in three separate layers:

| Layer | Question | Does what |
|---|---|---|
| **parse** | Is it JSON? | Strips code fences and surrounding chatter. Rejects truncation, prose, arrays. |
| **schema** | Is it the *right* JSON? | Pydantic. Rejects a missing `concepts`, `in_scope` sent as `"yes"`. |
| **tidy** | Can code just fix it? | Removes duplicate tags, blanks, tags on an off-topic turn. **Never rejects.** |
| **content** | What is left? | Three rules: blank answer, runaway length, declining and then answering anyway. |

A rejected response is not thrown away. Its failures are written to the attempt row and fed into
`tutor_repair/v1.md`, which names the exact problems and shows the model its own rejected output.
Two repairs are allowed. After that the turn **fails with no answer** rather than returning
something unchecked.

**The rule: a turn either carries a result that passed every check, or it carries nothing and says
why.** There is no middle state. `tests/test_reliability.py::test_nothing_invalid_is_ever_saved_or_returned`
is the assertion the whole project rests on.

Three decisions inside this are worth defending:

**Reject only what code cannot correct.** A repair costs real money, costs the student several
seconds, and *might not work*. A line of Python costs nothing and *always* works. So a ```` ```json ````
fence is stripped for free, Markdown written with real line breaks is re-read as sent, and a
duplicate tag is deduped in code — none of them cost a call. Truncated JSON is rejected, because
closing the braces means inventing the end of a sentence the student is about to read. The line is
"does fixing this require guessing at meaning".

This was got wrong first: the content layer had ten rules, of which four were string operations
outsourced to a language model and four more were only taste. It has three. Each defends the
student or the data; there is no fourth thing a model is needed for.

**A check must not fight the person it protects.** The word limit rejects at 800, not the 250 the
prompt asks for, because the prompt itself allows more *"unless the student explicitly asks for
more detail"* — and the check never sees the student's message. At 350 it threw away well-judged
answers from students who asked for depth and paid to replace them with worse ones. At 800 it is a
runaway guard that catches a model which has lost the plot and never argues with a student. A
banned-openings rule went entirely for the same reason: it rejected *"Absolutely convergent
series…"*, a correct sentence in a maths room.

**Retries and repairs are separate budgets, both spent in the same loop.** A rate limit re-sends the
*same* prompt; a bad answer sends a *different* one. Two rate limits and two bad answers are
different problems, and one counter would hide which you had. They share a loop rather than sitting
in a client wrapper because a retry hidden in a wrapper writes no row — a turn that took four
seconds because of two 429s would look identical to one that was merely slow, and the inspection
endpoint would be quietly lying.

`ResponseBlocked` is the one error never retried: retrying something nondeterministic is sensible,
retrying a decision is not.

### The tutor returns JSON, not prose

`tutor_system` v2 returns `{answer, in_scope, concepts}`. v1 returned prose, and every field here
exists because something reads it:

- **`in_scope`** — v1's prompt already told the tutor to decline off-topic questions, but the
  verdict was buried in the prose, so nothing could act on it. An algebra room asked about football
  still fed that exchange back into the next prompt as context. Now off-topic turns are filtered out
  of the context window, and S7 can exclude them from study history.
- **`concepts`** — short topic tags. This is what lets study history answer *"what did I study"*
  rather than only *"which rooms did I open"*. Stored as `jsonb` with a GIN index rather than a join
  table: concepts here are per-turn labels with no identity of their own — nothing renames or merges
  them — so a table would add a join and answer no question the index cannot.

The cost is honest: asking for JSON creates the malformed-output problem in the first place. Gemini's
own `response_schema` would remove most parse failures, and in production I would use it. I did not
here for two reasons — it is provider-specific, and it does nothing about the content layer, which
is the half the brief actually cares about.

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

Live in `prompts/<name>/v<N>.md`. Currently two:

| Prompt | Temp | What it is for |
|---|---|---|
| `tutor_system/v2.md` | 0.4 | The tutor. Pinned to the student's education level and the room topic. |
| `tutor_repair/v1.md` | 0.1 | Sent only after a response was rejected. Names the exact failures and shows the model its own rejected output. |

`tutor_system/v1.md` is kept, unedited, because stored attempts still reference its checksum.

The repair prompt runs colder on purpose: it is not being asked to be creative, it is being asked
to comply. It is also deliberately short — it does not repeat the tutor's full rules, only the
output format, the specific failures, and the original question.

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

- **S0 to S3 are built.** No documents, tools or skills yet.
- **Room context is the last 6 in-scope turns**, not a summary. Long rooms therefore mean long
  prompts. The fix is a `room_summaries` table; it is designed, not built.
- **`tests/fixtures/llm/recorded/` is empty.** A fixture is keyed by the exact request, prompt text
  included, so moving to `tutor_system` v2 made the four recordings taken against v1 unreachable
  rather than merely stale. Re-record with one command — see [Recording Gemini
  fixtures](#recording-gemini-fixtures). Nothing in the test suite depends on them; they exist to
  check the prompt against real model behaviour.
- **The content checks are tuned to English.** Word counts and the filler-opening list would both
  need rethinking for Bangla, which matters for this product specifically.
- **A repaired turn costs the student the extra wait.** Two repairs can mean three sequential Gemini
  calls before an answer appears. Streaming a first draft and correcting it is not possible while
  the guarantee is "nothing unchecked reaches the student".
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
UI, no OCR for scanned PDFs, no streaming responses, no job queue, no vector search, no web search,
no code-execution sandbox. Tools were picked against a four-part test — the test and the full list
of what it rejected are in [`docs/PLAN.md` §8](docs/PLAN.md).

---

## Testing

Commands are under [Everyday commands](#everyday-commands).

**The suite runs with no Gemini API key.** `get_llm` is a FastAPI dependency, so tests override it
with a fake that records the prompts it was given — which is how we assert that the education level
and the room's earlier turns genuinely reached the model, rather than trusting the template.

Fixtures come in two kinds, and the reliability layer would be untestable without both:

- **Recorded** — `tests/fixtures/llm/recorded/`, written by `scripts/record_fixtures.py` against
  the real API. These capture what the model actually does, which is the only way to find out
  whether a prompt works. The script now runs each response through the real validation pipeline and
  prints the verdict, so "does the model comply with this prompt" is answered with evidence.
- **Hand-written** — `tests/fixtures/llm/synthetic/`, one file per failure mode, with a
  [README](tests/fixtures/llm/synthetic/README.md) mapping each to its expected outcome. These
  cannot be recorded: you cannot ask Gemini for truncated JSON, or a quiz with duplicate options, on
  demand. That is precisely why the failure modes the brief names need them.

Recorded fixtures alone would only ever test the model on its good days.

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

1. **Finish S4–S7.** Tools, documents, skills and study history.
2. **Rolling room summaries.** Long rooms currently mean long context. A `room_summaries` table
   storing a summary up to turn N would make opening a 500-turn room cost the same as a 5-turn one.
   Designed in [`docs/PLAN.md` §5](docs/PLAN.md), not built.
3. **A real code-execution sandbox.** The highest-value tool for a study app, and the one I
   deliberately did not build. `evaluate_expression` already runs model-written code, but only the
   subset I can prove safe: an AST allow-list of numbers, operators and a few functions, with no
   imports, no attribute access and no `9**9**9`. Going further — statements, loops, a real
   interpreter — needs an isolated container with no network, memory and PID limits, a hard
   timeout, and cleanup that survives a hang. Miss the PID limit and `while True: fork()` takes the
   host down. That is a separate service, not a function. I would rather ship a small provably-safe
   thing than a large probably-safe one, but with a week this is what I would build first.
4. **Measure instead of assert.** Seed 50,000 turns and run `EXPLAIN` on the cursor queries, so
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
