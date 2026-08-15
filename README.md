# Porua AI — Backend

A backend for an AI-powered study app. A student creates a **room** for one
topic, talks to an AI tutor inside it, and uploads class files as source
material. Over time this builds a study history they can query.

Built for the Monsha Software Engineer technical assessment
([`docs/ASSESSMENT.md`](docs/ASSESSMENT.md)).

FastAPI · PostgreSQL 16 · SQLAlchemy 2 async · Alembic · `gemini-2.5-flash`
19 endpoints · 11 tables · 7 migrations · 290 tests, none of which call the model

---

## Contents

1. [Requirements, and where each one is met](#requirements-and-where-each-one-is-met)
2. [Quick start](#quick-start)
3. [Architecture](#architecture)
4. [Important decisions](#important-decisions)
5. [Prompts](#prompts)
6. [Known limitations](#known-limitations)
7. [What I would do with more time](#what-i-would-do-with-more-time)
8. [Testing](#testing)
9. [Documentation](#documentation)

---

## Requirements, and where each one is met

**"We should be able to…"**

| Requirement | Endpoint |
|---|---|
| Create and list study rooms | `POST /rooms`, `GET /rooms` |
| Open a room and see what happened inside it | `GET /rooms/{id}`, `GET /rooms/{id}/turns` |
| Start a new AI turn/chat | `POST /rooms/{id}/turns` |
| Inspect a turn: prompt, attempts, tool activity, failures, tokens, result | `GET /turns/{id}` |
| Query study history over a period | `GET /students/me/study-history` |
| Upload `.docx` / `.pdf` / `.pptx` as context | `POST /rooms/{id}/documents` |
| …and the tutor can query those | `search_room_materials` tool |
| Data persists between runs | Postgres in a named volume |

`GET /turns/{id}` is the one to look at. It returns the rendered prompt with its
version and checksum, every attempt including the failed ones, each tool call
with arguments and results, and token usage separated into prompt / output /
**thinking**.

**The rest of the brief**

| Requirement | Where |
|---|---|
| Relational DB, real relationships and constraints and indexes | 11 tables, 7 migrations. Partial indexes, functional unique indexes, `CHECK` constraints, a generated `tsvector` |
| At least 2 tools | 3 — `search_room_materials`, `query_study_history`, `evaluate_expression` |
| At least 2 skills, not renamed prompts | 2 — `quiz_builder`, `step_solver`. Each is a prompt plus code that checks its output |
| Prompts readable and versioned | [`prompts/<name>/v<N>.md`](prompts/), YAML front-matter, checksum stored per attempt |
| Reliability around a misbehaving model | [`app/reliability/`](app/reliability/) — parse → schema → tidy → content, then repair |
| Controlled tool use | Fixed registry, validated arguments, four budgets and a deadline |
| Real ingestion, bad input handled deliberately | [`app/documents/`](app/documents/) — 7 typed rejection codes |
| Swagger | <http://localhost:8000/docs> |
| Migrations · example env · tests | [`migrations/versions/`](migrations/versions/) · [`.env.example`](.env.example) · 290 tests |
| Assumptions documented | [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) |

A login flow is not required, per the brief. `X-Student-Id` is trusted as sent.

---

## Quick start

Requires Docker. Nothing else.

```bash
cp .env.example .env          # paste your Gemini key into GEMINI_API_KEY
docker compose up -d --build
docker compose exec api alembic upgrade head
```

Verified from a clean slate (`docker compose down -v` and back up).

- **Swagger:** <http://localhost:8000/docs>
- **Health:** `curl localhost:8000/healthz`

A Gemini key is only needed to run a turn. Rooms, uploads, migrations and the
whole test suite work without one.

Running outside Docker, and every other command, is in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

**Two things Swagger does not show.** There is no login, so create a student
first (`POST /students`) and send its id as the `X-Student-Id` header on every
other request. And a citation only appears once a room has an uploaded document,
so upload before asking a question about the material.

Study history needs history. Seed six weeks of it, with no model calls:

```bash
docker compose exec api python scripts/seed_demo_data.py
```

---

## Architecture

A **room** holds one topic and lives for weeks. A **turn** is one question and
answer inside it. Gemini is called during a turn, tools run, output is validated,
and failures are recorded. Files attach to the room, so every turn in it can
search them.

A turn is not a message. One turn can involve several Gemini calls: a tool
round-trip, a retry, a repair. The student still sees one answer. This is why
`turn_attempts` hangs off `turns`, and why the brief asks to inspect a turn's
model attempts, plural.

```
app/
  api/
    errors.py       one RFC 9457 error shape for the entire API
    pagination.py   cursor paging helpers
    deps.py         FastAPI dependencies — the only place identity enters
    routes/         HTTP layer: thin, no business logic
  db/               engine, session, declarative base + naming convention
  documents/        reading uploads: detect → extract → chunk
  llm/              provider boundary: one Protocol, a Gemini client, record/replay
  reliability/      parse → schema → content. Pure functions
  skills/           a prompt plus code that checks its output; sympy lives here
  tools/            what the model may ask for; the fixed registry
  models/           SQLAlchemy models (the database)
  schemas/          Pydantic models (the API contract) — deliberately separate
  services/         business logic; every function is scoped to one student
migrations/         Alembic; one migration per slice
prompts/            versioned prompt files, one directory per prompt
scripts/            seed data, record fixtures, check a document, verify a migration
docs/               brief, plan, decisions, assumptions, schema, slice write-ups
```

`reliability/` and `documents/` never touch the database or the network. They
take input and return a verdict, so their tests run in milliseconds against real
PDFs and real model output. The decision to call the model again costs money and
writes a row, so it lives in `services/turn.py`.

---

## Important decisions

Short versions. Full reasoning for these and twelve more is in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

**[The model asks for tools; it never runs anything](docs/DECISIONS.md#the-model-asks-for-tools-it-never-runs-anything)**
Five protections. The registry is a fixed dict filled at import, so there is no
`getattr` and no dispatch from model output. Arguments are validated with the
same Pydantic model that generated the declaration the model read. No tool takes
a student id, so the model cannot ask for another student's data. Four budgets
apply: 5 tool rounds, 6 calls, 8 seconds per tool, 45 seconds per turn. Repeat
calls are answered from cache.

When a budget runs out the turn still succeeds. The tools stop being offered, so
the model answers with what it already gathered.

**[`evaluate_expression` cannot run anything but arithmetic](docs/DECISIONS.md#evaluate_expression-cannot-run-anything-but-arithmetic)**
It evaluates a string written by the model, so it does not use `eval`. Filtering
`eval` does not work: `(1).__class__.__bases__[0].__subclasses__()` walks from an
integer to every loaded class. The expression is parsed to a syntax tree and
every node is checked against an allow-list of seven types. `ast.Attribute` is
not on the list, so a `.` cannot be written at all.

Size limits are a separate problem. `9 ** 9 ** 9` uses only allowed nodes and
would still hang the process, so exponents, factorials and expression length are
all capped.

**[A skill is a prompt plus code that checks it](docs/DECISIONS.md#a-skill-is-a-prompt-plus-code-that-checks-it)**
`step_solver` checks every line with sympy before the student sees it. The test
is whether two consecutive equations have the same solution set, which catches
any slip without knowing what operation was claimed. Problems it cannot verify
are declined before a model call is spent.

`quiz_builder` handles the four failures the brief names. Duplicate choices and
invalid answers are caught in code and also blocked by functional unique indexes.
The wrong count is caught in code. Predictable answer positions cannot be caught
at all, since no single quiz is wrong for putting the answer in slot B, so the
options are shuffled with a seeded generator.

**[A skill run is a turn](docs/DECISIONS.md#a-skill-run-is-a-turn)**
`skill_runs.turn_id` is `UNIQUE NOT NULL`. A quiz therefore writes
`turn_attempts` rows with `purpose='skill'`, shows up in the room timeline, and
is inspected through the same `GET /turns/{id}`.

The first version hung skill runs off rooms and stored their attempts in JSONB.
That left the project with two audit trails of different shapes when the brief
asks for one, so it was reversed before being committed.

**[Nothing unchecked reaches the student](docs/DECISIONS.md#nothing-unchecked-reaches-the-student)**
Four layers. Parse asks whether it is JSON. Schema asks whether it is the right
JSON. Tidy fixes what code can fix and never rejects. Content checks for a blank
answer, runaway length, and declining then answering anyway.

A rejection feeds a repair prompt listing the exact failures. Two repairs are
allowed, after which the turn fails with no answer. **A turn either carries a
result that passed every check, or carries nothing and says why.**

Only things code cannot correct are rejected. Code fences get stripped for free.
Truncated JSON is rejected, because closing the braces would mean inventing the
end of a sentence the student is about to read.

**[A bad upload is an HTTP status, not a status field](docs/DECISIONS.md#a-bad-upload-is-an-http-status-not-a-status-field)**
Most bad files are only discovered during extraction: an encrypted PDF, a scan
with no text layer, a `.pptx` renamed `.docx`. The first four bytes cannot tell
you. Doing that work in a background task turns each one into a status column the
client has to poll and interpret. Doing it in the request gives each one an HTTP
status with a stable code: `UNSUPPORTED_TYPE`, `FILE_TOO_LARGE`,
`CORRUPT_ARCHIVE`, `ENCRYPTED`, `NO_TEXT_LAYER`, `UNDECODABLE_TEXT`,
`EMPTY_EXTRACTION`.

**[Every model attempt is a database row](docs/DECISIONS.md#every-model-attempt-is-a-database-row-not-a-log-line)**
Failed attempts included, from the first migration that touches them. The brief's
inspection requirement is effectively a schema specification, so the endpoint
reads rows that were always there instead of reconstructing anything from logs.

**[Built for "as history grows"](docs/DECISIONS.md#cursor-paging-everywhere-never-offset)**
Cursor paging on every list endpoint. `OFFSET 5000` makes Postgres walk and
discard 5,000 rows on each request. The cursor carries `(sort_value, id)`, so
rows sharing a timestamp are never duplicated or skipped across pages.

`DELETE /rooms/{id}` sets `archived_at` and keeps the row, since a cascade would
remove turns that study history reports on. Postgres was chosen for partial
indexes, `jsonb`, and generated `tsvector` columns.

---

## Prompts

Prompts live in `prompts/<name>/v<N>.md`. The live version is a pinned constant,
and a test asserts the pin is the newest file on disk.

| Prompt | Live | Temp | For |
|---|---|---|---|
| [`tutor_system`](prompts/tutor_system/) | **v7** | 0.4 | The tutor, pinned to the student's education level and the room topic |
| [`tutor_repair`](prompts/tutor_repair/) | v1 | 0.1 | Sent after a response is rejected. Names the failures, shows the model its own output |
| [`quiz_builder`](prompts/quiz_builder/) | v1 | 0.6 | Multiple-choice questions, optionally grounded in uploaded material |
| [`step_solver`](prompts/step_solver/) | v1 | 0.2 | Worked solutions, one equation per line, for sympy to verify |

The scheme, since the brief asks for prompts to be *readable and versioned*:

- YAML front-matter records name, version, model, temperature, and a changelog
  line saying what changed and **why**.
- Jinja2 in strict mode, so a missing variable raises at render time instead of
  quietly producing a worse prompt.
- Every attempt stores `prompt_name`, `version` and a checksum of the exact text.
- **A prompt file is never edited once it has produced a stored attempt.** A new
  version is added instead, or the audit trail silently stops being true.

`tutor_system` is on v7. Each version came from a failure seen in testing, and
each file's changelog says which one. Reading them in order gives the
prompt-engineering history.

---

## Known limitations

**The tutor cannot invoke a skill.** 

- Skills are client-invoked. A student who
types *"quiz me on photosynthesis"* into the chat gets a quiz written as prose,
with none of `quiz_builder`'s checks and nothing stored in the database. So the
natural way to ask produces the worse result. This is the gap I would close
first.

**Retrieval**

- Search is lexical, not semantic. Asked for a report's submission date, the
  tutor said it could not find one; the date was on slide 1, reading `AUGUST
  2026`. A date does not contain the word "date", and no ranking change fixes
  that.
- An earlier bad answer in the room can stop the tutor searching, because it
  recaps itself instead. v5 improves this but does not fix it.

**Documents**

- Extraction gaps are silent. `.docx`: headers, footers, text boxes, footnotes,
  comments. `.pptx`: SmartArt, chart data. `.pdf`: images, and formulas rendered
  as images. The Word gaps matter most, because a cover page's date and course
  code are usually in a footer.
- Word files get no page numbers. A `.docx` does not contain them, so a citation
  names the file and stops there.
- Processing is synchronous, measured at ~1.5 MB/s, so a 20 MB file makes the
  uploader wait ~14 s. Parsing runs in a worker thread, so only the uploader
  waits.
- The original file is not kept, only its hash, name, size and extracted text.

**Model behaviour**

- A truncated reply is misread as a content failure. Thinking is billed against
  `max_output_tokens`, so a hard question can run out of room mid-JSON; the parse
  layer calls it `JSON_TRUNCATED` and sends it to a repair loop that cannot fix a
  budget problem. `finish_reason` is stored but nothing branches on it.
- Room context is the last 6 in-scope turns, not a summary, so long rooms mean
  long prompts.
- The whole prompt is sent as one `user` message, and Gemini's
  `system_instruction` field is unused. The tag wrapping inside the prompt text
  is therefore the only thing separating our rules from a student's uploaded PDF.
- Content checks are tuned to English. Word counts would need rethinking for
  Bangla, which matters for this product specifically.

**Operational**

- Turns are synchronous; no rate limiting; migrations are not run on startup.
- A tool timeout fails the whole turn, and it cannot interrupt CPU-bound work.
  `asyncio.wait_for` only cancels at an `await`, so the size limits are the real
  protection.

**Deliberately not built**, with reasons in [`docs/PLAN.md` §16](docs/PLAN.md):
web UI, OCR, formats beyond the three named, streaming, job queue, vector search,
web search, code-execution sandbox, and quiz grading.

---

## What I would do with more time

1. **Let the tutor invoke skills** — the gap above. Most of the plumbing exists;
   the blockers are a per-tool timeout, refusing a second `build_quiz` in one
   turn cleanly, and the 45-second turn deadline.
2. **Rate limiting.** Every turn is a billable model call, so the first risk is
   cost, not load. One student stuck in a retry loop can drain the key for
   everyone. The useful limit is a per-student token budget over a rolling
   window, since a single request can cost 8,000 tokens.
3. **Partition the audit tables, and expire their payloads.** `turn_attempts` is
   the table to worry about. `rendered_prompt` and `raw_response` are both
   `text`, and one turn writes one to three rows, so it grows in bytes much
   faster than the row count suggests. Monthly range partitions on `started_at`
   would keep queries against recent data off the whole history. The partition
   key has to be part of the primary key, so this is a real migration and not a
   setting. Retention goes with it: token counts, error types and prompt versions
   are worth keeping forever, but the two large text columns are not, so old
   partitions can drop the payloads and keep the metrics.
4. **Hybrid retrieval** — embeddings alongside the `tsvector`. Needs a second
   model, which is the only reason it is not built.
5. **Quiz grading** — `POST /quiz-runs/{turn_id}/answers` and a `quiz_attempts`
   table, which is where a quiz stops being a document and becomes data.

---

## Testing

290 tests. Commands in [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

**The suite runs with no Gemini API key.** `get_llm` is a FastAPI dependency, so
tests override it with a fake that records the prompts it was given — which is
how the education level and the room's earlier turns are asserted to have
genuinely reached the model, rather than trusting the template.

Fixtures come in two kinds. **Recorded** ones capture what the model actually
does, which is the only way to find out whether a prompt works. **Hand-written**
ones cover the failure modes the brief names, because you cannot ask Gemini for
truncated JSON or a quiz with duplicate options on demand. Recorded fixtures
alone would only test the model on its good days.

Each test runs in a transaction that is rolled back, so order does not matter,
and the HTTP client shares the test's session, so a test can assert on rows the
endpoint just wrote.

Migrations are verified by round trip — `upgrade head` → `downgrade base` →
`upgrade head` — plus `alembic check` for model drift. Both have caught real
bugs: an enum type never dropped on downgrade, and a column type that had
diverged from its model.

---

## Documentation

| File | Contents |
|---|---|
| [`docs/ASSESSMENT.md`](docs/ASSESSMENT.md) | The brief, as text |
| [`docs/PLAN.md`](docs/PLAN.md) | The design written before the code — priorities, tool selection, build order, and §16, everything deliberately not built |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | Every engineering decision in full, with the failures that caused them |
| [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) | Every assumption, with reasoning |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | Non-Docker setup, tests, migrations, seeding, fixtures, resetting |
| [`docs/S4-EXPLAINED.md`](docs/S4-EXPLAINED.md) · [`S5`](docs/S5-EXPLAINED.md) · [`S6`](docs/S6-EXPLAINED.md) | The agent loop, document ingestion, and skills — each walked through end to end |
