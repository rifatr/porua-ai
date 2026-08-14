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
| **S4** Tools | Tool registry with hard limits, study-history and calculator tools | ✅ Done |
| **S5** Documents | Upload and extract `.docx` / `.pdf` / `.pptx`, full-text search over them | ✅ Done |
| **S6** Skills | Quiz builder and step-by-step maths solver, both verified in code | ✅ Done |
| **S7** History | Study history over a date range | ✅ Done — pulled into S4 |

S7 came early because the study-history *query* is the expensive part and S4's tool
needed it. The endpoint is fifteen lines on top of the same service, so building both at once cost
almost nothing and removed a slice from the last day.

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
#    model attempt including failures, tool calls, and token usage.
curl -s "localhost:8000/turns/$TURN_ID" -H "X-Student-Id: $SID" | jq

# 6. Upload a handout. Read, chunked and indexed before this returns, so a 201
#    means the tutor can already search it.
curl -s -X POST "localhost:8000/rooms/$ROOM_ID/documents" \
  -H "X-Student-Id: $SID" -F "file=@algebra-handout.pdf" | jq

# 7. Upload something that is not really a PDF. 415, with a code you can branch on.
echo "not a pdf" > fake.pdf
curl -s -X POST "localhost:8000/rooms/$ROOM_ID/documents" \
  -H "X-Student-Id: $SID" -F "file=@fake.pdf" | jq '{status, code, detail}'

# 8. Now ask something the handout covers. The tutor searches it and cites the page.
curl -s -X POST "localhost:8000/rooms/$ROOM_ID/turns" \
  -H 'Content-Type: application/json' -H "X-Student-Id: $SID" \
  -d '{"message":"What method does my handout use for simultaneous equations?"}' | jq

# 9. Build a quiz. Checked in code, then stored — and it becomes a turn in the room.
curl -s -X POST "localhost:8000/rooms/$ROOM_ID/skills/quiz_builder/runs" \
  -H 'Content-Type: application/json' -H "X-Student-Id: $SID" \
  -d '{"topic":"solving equations","question_count":5}' | jq

# 10. Solve one, with every step checked by sympy before you see it.
curl -s -X POST "localhost:8000/rooms/$ROOM_ID/skills/step_solver/runs" \
  -H 'Content-Type: application/json' -H "X-Student-Id: $SID" \
  -d '{"problem":"3x + 6 = 15"}' | jq '.result, .checks'

# 11. Delete a room, twice. Both return 204 — it is idempotent — and the row survives.
curl -s -o /dev/null -w "%{http_code}\n" -X DELETE "localhost:8000/rooms/$ROOM_ID" -H "X-Student-Id: $SID"
curl -s "localhost:8000/rooms/$ROOM_ID" -H "X-Student-Id: $SID" | jq .archived_at
```

Step 5 is the one worth looking at. A real turn against `gemini-2.5-flash` reports something like:

```json
"tokens": { "prompt": 496, "output": 278, "thought": 1167, "total": 1941 }
```

Thinking cost four times the visible answer. It is billed and never appears in the reply, which is
why it is counted separately rather than folded into `output`.

Step 10 prints the verification, which is the part worth seeing — each step with whether a
computer algebra system agreed with it:

```json
{"position": 1, "expression": "3x = 9", "verified": true},
{"position": 2, "expression": "x = 3",  "verified": true}
```

Follow either run's `turn_id` to `GET /turns/{id}` and you get the prompts, the raw replies and the
token counts, with `purpose: "skill"` on each attempt — the same endpoint that explains a
conversation.

Step 8 is the other one worth looking at. Inspect that turn and the `tool_calls` array shows what
the model asked for and what came back — including the finished citation string it was handed, so
you can see that "lecture-3.pptx, page 4" was given to it rather than composed by it.

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
pytest                       # all tests
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

# 3. prove it reverses — on a THROWAWAY database, never your own
./scripts/check_migration.sh

# 4. apply it to your development database, forward only
alembic upgrade head
```

**Step 3 uses a scratch database on purpose.** A migration that cannot be undone cannot be trusted,
so the round trip has to be run — but running `downgrade` against a database holding your own data
destroys it. Once, here, a failed `upgrade` piped through `tail` returned exit code 0, so `&&` did
not stop the chain, and the `downgrade -1` that followed reversed the *previous* migration and
dropped the `turns` table. `scripts/check_migration.sh` creates its own database, round-trips
`upgrade → downgrade base → upgrade → check` inside it, and drops it again. Nothing it does can
reach your data.

Other useful ones:

```bash
alembic current              # which revision is applied
alembic history --verbose    # the chain
```

`alembic downgrade` is deliberately absent from this list. Use the script.

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

### Seeding demo data

Study history cannot be judged on two turns from this morning. This writes six weeks of them, with
no Gemini calls, so it is instant and free:

```bash
docker compose exec api python scripts/seed_demo_data.py
```

It prints the student id and two commands: one reading the history endpoint directly, one asking
the tutor the same question so it reaches the data through `query_study_history`.

Running it a second time does nothing — it prints the existing id and stops, rather than doubling
the data. To start clean:

```bash
docker compose exec db psql -U porua -d porua \
  -c "DELETE FROM students WHERE display_name = 'Demo Student'"
```

Every foreign key cascades from the student, so that removes the rooms and turns with it. Only the
seeded student is touched.

The seed deliberately includes one **failed** and one **off-topic** turn, which history must
exclude. If either ever appears in the totals, the filter has broken — and that is the kind of bug
that produces plausible numbers nobody questions.

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
  documents/        reading uploads: detect → extract → chunk. No database, no I/O
  llm/              the provider boundary: one Protocol, a Gemini client, record/replay
  reliability/      parse → schema → content. Pure functions, no I/O, no database
  skills/           a prompt plus code that checks its output; sympy lives here
  tools/            what the model may ask for; the fixed registry
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

`documents/` follows the same rule as `reliability/`: three modules that take bytes and return
data, with no session and no network. That is what lets the file tests run against real PDFs,
Word files and slide decks without a database — `tests/documents.py` builds each fixture in
memory, so what makes a file "a scan" or "corrupt" is visible in the code rather than hidden in a
committed binary.

---

## Important decisions

Fuller reasoning for each is in [`docs/PLAN.md`](docs/PLAN.md). These are the ones I would defend.

### The model asks for tools; it never runs anything

The brief asks that *"the model should not call arbitrary code or call tools indefinitely"*. Five
independent protections, because relying on one is fragile:

| # | Protection | Where |
|---|---|---|
| 1 | **A fixed list.** A dict lookup, filled at import. No `getattr`, no import, no dispatch from model output. | `app/tools/registry.py` |
| 2 | **Arguments validated first.** Using the same Pydantic model that generated the declaration the model read, so the two cannot drift. | `registry.validate_arguments` |
| 3 | **Identity from the request.** No tool takes a student id, so the model cannot ask for another student's data. | `app/tools/base.py` |
| 4 | **Four budgets and a deadline.** 5 tool rounds, 6 tool calls, 8s per tool, 45s per turn. | `app/services/turn.py` |
| 5 | **Repeat calls answered from cache**, with a note telling the model to stop. | `app/services/turn.py` |

Two of these are worth more than the others.

**Protection 1 is not a check that could be incomplete** — there is no mechanism by which an
unlisted name becomes callable. "We validate the tool name against a list" sounds similar and is
much weaker.

**Protection 3 works by absence.** A `student_id` argument that gets checked can be forgotten on
the next tool; a parameter that does not exist cannot. A test asserts it stays that way for every
tool added later.

**Running out of budget does not fail the turn.** The tools simply stop being *offered*, so the
model has to answer with what it already gathered. A student who is waiting gets an answer rather
than an error, and the loop still terminates. `tests/test_tools.py` has the runaway-loop case.

### `evaluate_expression` cannot run anything but arithmetic

It takes a string written by a language model and evaluates it, which is the most dangerous shape a
function can have. It does **not** use `eval`, because `eval` cannot be made safe by filtering:

```python
eval("(1).__class__.__bases__[0].__subclasses__()")   # integer → every loaded class → files
```

Instead the expression is parsed to a syntax tree and every node checked against an allow-list of
seven types. `ast.Attribute` is not among them, so a `.` cannot be written at all — and every
escape of that kind begins with one. The claim is not "we thought of the bad inputs", it is "only
these seven node types can execute".

Size limits sit alongside, because `9 ** 9 ** 9` is made entirely of *allowed* nodes and would
still hang the process. Worth knowing: the 8-second tool timeout does **not** help there —
`asyncio.wait_for` cannot interrupt synchronous CPU work. The limits are the real defence.

`tests/test_calculator.py` runs eleven real attacks, including both subclass walks.

### A skill is a prompt plus code that checks it

The brief sets the bar: skills must *"provide real student value, extend the LLM's capabilities,
and not be renamed versions of the same prompt."* That last clause is the design constraint, and
the answer is that the prompt is the smallest part of each one.

**`step_solver`** asks for working, then checks every line with sympy before the student sees it.
The rule is not "does this look like the previous line minus 6" — it is **do these two equations
have the same solution set**, which catches any slip without knowing what operation was claimed:

```
3x + 6 = 15  →  3x = 9      ✓
3x + 6 = 15  →  3x = 8      ✗  arithmetic slip
2(x-4) = 10  →  2x - 4 = 10 ✗  forgot to distribute
x² - 4 = 0   →  x = 2       ✗  dropped a root
```

A failing step is sent back to be redone with the failing line named. And the skill **declines**
what it cannot verify — `UNCHECKABLE_PROBLEM`, before spending a model call — because without its
checks it would be the renamed prompt the brief warns about, and the tutor already handles word
problems perfectly well.

sympy never sees the raw string. `sympify` runs Python and has had sandbox escapes, and this input
comes from a student *by way of* a language model, so an allow-list runs first — the same posture
as `evaluate_expression`. Three hostile inputs are tested.

**The allow-list stops code, not cost, and that needed a separate answer.** `9^9^9` is six
characters, every one of them permitted, and evaluating it asks Python for a 370-million-digit
integer on the event loop with nothing able to interrupt it. `x^99999` parses in a millisecond and
then never leaves `solveset`. Neither is an escape; both are a hang, which for a synchronous API is
the same outcome. So powers are bounded before any arithmetic happens — the expression is parsed
once with `evaluate=False`, the tree is checked, and only what survives is parsed for real. The two
limits differ because the costs do: a number to a number costs digits, a *variable* to a number
costs polynomial degree, and degree is far more expensive than it looks (measured: degree 20 takes
0.4s, degree 50 takes 2.2s, degree 100 takes 14.6s).

Symbol count is the other gate, and it exists because parsing proves sympy could *read* a string,
not that the string is maths. "Give me a quiz on TCP" is letters and spaces, so it passes the
allow-list and implicit multiplication turns it into a product of fourteen symbols — a valid
expression nobody asked to solve. It used to cost three model calls and then fail as
`CHECKS_FAILED`, which is the wrong reason as well as the slow one. Real school algebra has one or
two unknowns, and above one the checker was already dropping to a weaker fallback, so refusing past
three says out loud what was happening quietly.

**`quiz_builder`** handles the four failures the brief names, in four different places, and where
each one lives is the interesting part:

| Failure | Caught by | Why there |
|---|---|---|
| duplicate choices | code, then `ux_choice_text` | code names it for a retry; the index makes it unstorable |
| wrong count | code only | a fact about a *set* of rows — a constraint would need a trigger |
| invalid answer | code, then `ux_choice_correct` | same pair of reasons as duplicates |
| **predictable positions** | **code alone, by shuffling** | see below |

The last row cannot be a check. No single quiz is wrong for putting the answer in slot B — it is a
bias across many — and instructing the model to vary it does not work, because it is not
disobeying anything. So the options are shuffled afterwards with a seeded generator and the
property becomes true by construction. The seed comes from the request via `hashlib`, not the
built-in `hash()`, which Python randomises per process: that would have been stable within a run
and different on the next one, which is a test that passes until it does not.

Both database indexes were verified by inserting bad rows by hand:

```
ERROR: duplicate key value violates unique constraint "ux_choice_text"     -- "  paris " vs "Paris"
ERROR: duplicate key value violates unique constraint "ux_choice_correct"  -- a second right answer
```

If the checks cannot be satisfied in three attempts the run **fails** rather than degrading. A
quiz with two identical options is worse than no quiz, and the failed row keeps every prompt,
every reply and exactly which rule rejected each one.

### What I would change here next

**The repair prompt is weaker than the tutor's, and it is the same repo.**
`prompts/tutor_repair/v1.md` is a separate versioned file that shows the model its own rejected
output between markers, labelled as data. A skill instead pastes an instruction from Python onto
the end of the original prompt, so the model is told *"question 1 has two options reading
'paris'"* without being shown question 1 — it regenerates rather than repairs, and mostly gets
away with it. Two consequences follow. `prompt_sha256` on a repair attempt describes only the base
file, so the checksum no longer identifies the text that was actually sent (`rendered_prompt` still
holds the truth, so nothing is lost — but the column claims something false, which is worse than
absent for an audit trail). And PLAN §11 lists `quiz_repair` as a prompt to write; it shipped as a
string constant. The fix is two prompt files and threading `previous_output` through
`skills/base.generate`, which is the shape the tutor already proves works.

**A quiz can be built but not taken.** There is no answer submission and no
scoring, so `is_correct` and `explanation` ship inside the questions. That is not a
leak while the only consumer is someone inspecting what was generated — for them
`is_correct` is the evidence that exactly one option is correct and that the
shuffle moved it — but it is a shape with no room for the feature it implies.
Grading is `POST /quiz-runs/{turn_id}/answers` taking `[{question_id, choice_id}]`,
a run response that withholds the key until then, and a `quiz_attempts` table. The
table is the interesting part: it is where a quiz stops being a document and
becomes data, so "what is this student weak at" turns into a query instead of a
count of what they happened to mention. The inspection endpoint keeps the answers
regardless — it already shows raw prompts and raw replies.

**Three checks from PLAN §8 are not implemented**: distractors that are reworded copies of the
answer, every question backed by an uploaded file when the room has one, and language suited to the
education level. The prompt asks for all three; nothing verifies them.

**`source` is an unverified claim.** The model writes it, and nothing checks it against the
citations that were actually offered — yet the turn's summary tells the student *"3 of them from
your uploaded material"*. A set-membership test against the passages handed to the prompt would
make it true rather than asserted, and would also bound the only unchecked model string going into
a `String(300)` column.

**The API should be one endpoint, not four.** `POST /rooms/{id}/turns` with a body discriminated on
`skill`, returning `content` discriminated on `type` — which is what "a skill run is a turn" says
the surface should look like. Per-skill routes exist because a shared endpoint taking an untyped
`dict` could not document each skill's arguments in Swagger; a discriminated union solves that
without the route sprawl, and would replace `result: dict | None` with a typed shape. Not done
because it is a surface refactor with no new capability, and it would have meant rewriting a
passing test file on the deadline.

### A skill run is a turn

`skill_runs.turn_id` is `UNIQUE NOT NULL`. Asking for a quiz is asking the room a question, so a
run creates a turn and everything follows from that: the model calls are `turn_attempts` rows with
`purpose='skill'`, the quiz appears in the room's timeline where the student asked for it,
`GET /turns/{id}` inspects it in the same detail as a conversation, and study history counts it as
work done.

The first version hung `skill_runs` off `rooms` with its attempts in a JSONB column. It worked, and
it was wrong: the project then had **two audit trails of different shapes**, so "inspect the model
attempts" meant one thing for a chat and another for a quiz — while the brief asks for one. It was
reversed before being committed. The tell was that `AttemptPurpose.SKILL` had been sitting in the
enum since S2 and my design made it unreachable.

`terminal_states_are_complete` then forces a good detail rather than needing a workaround: a
succeeded turn must carry an answer, so every skill returns a one-line summary — *"Here is a
5-question quiz on photosynthesis, 3 of them from your uploaded material"*. That is what belongs in
a timeline; the quiz itself is a click away.

### Tools and structured output cannot be used together

Verified against the API before designing around it:

```
400 INVALID_ARGUMENT
"Function calling with a response mime type: 'application/json' is unsupported"
```

So each call is either gathering (tools, no schema) or answering (schema, no tools);
`LLMRequest.__post_init__` refuses the combination rather than letting it fail at the provider.

The consequence is the interesting part: **constrained decoding is unavailable exactly while the
model can call tools.** During a tool-using turn the parse → schema → content pipeline is the only
thing between model output and the student. `response_schema` did not make that pipeline redundant;
it narrowed it to where it is the sole defence.

### A bad upload is an HTTP status, not a status field

Uploads are read, chunked and indexed inside the request, and the reason is the brief's own
requirement to handle bad input deliberately.

Extraction is where most bad files are *discovered*. A password-protected PDF, a scan with no text
layer, a `.pptx` renamed to `.docx` — none of these can be detected from the first four bytes; you
find out when a parser tries. Do that work in a background task and every one of them becomes a
status column the client polls and then has to interpret. Do it in the request and each is an HTTP
status with a stable code:

| Status | `code` | Cause |
|---|---|---|
| 415 | `UNSUPPORTED_TYPE` | Not a supported type, or the contents disagree with the name |
| 413 | `FILE_TOO_LARGE` | Over 20 MB |
| 422 | `CORRUPT_ARCHIVE` | Damaged, or not really the format it claims |
| 422 | `ENCRYPTED` | Password-protected |
| 422 | `NO_TEXT_LAYER` | A scan — images of text, which we do not OCR |
| 422 | `UNDECODABLE_TEXT` | Fonts carry no character map; the "text" is glyph numbers |
| 422 | `EMPTY_EXTRACTION` | Opens fine, contains no words |

The last three are the group worth arguing about. None of them yields usable text, and it would be
easy to collapse them into one error. They are kept apart because the student's next action
differs: a scan needs OCR we do not do, an undecodable file needs re-exporting from the original,
and an empty one never had anything in it. And each is **rejected** rather than stored — an
accepted scan sits in the room looking searchable, matches nothing, and gives no clue why.

`UNDECODABLE_TEXT` was found by uploading a real 700 KB PDF, which returned a 500. Some PDFs embed
fonts without a `ToUnicode` map, so there is no way back from a glyph to a character and the
extractor returns raw font indices: `\x00\x02\x01\x04\x03`. Those bytes then hit Postgres, which
cannot store `\x00` in a `text` column at all. Two fixes, not one — everything extracted is now
stripped of characters that cannot survive the database, *and* a document that is mostly such
characters is refused rather than indexed as noise.

The file was a 148-page competitive-programming book built by old LaTeX, using **98 Type 3 fonts**.
A Type 3 glyph is a small drawing program, so the file contains pictures of letters rather than
letters. It scored 0.416 across the document, its only pages under the threshold were blank, and
`text`, `blocks` and `words` extraction all returned the same indices — there is no fallback,
because there is nothing there. Rejecting it is the correct answer; reading it would need OCR.

`scripts/check_document.py` runs exactly this pipeline against a file and prints what would
happen, which is worth doing before a demo rather than during one:

```bash
docker compose exec -T api python scripts/check_document.py /tmp/handout.pdf
```

The stripping has a trap worth knowing about. The obvious implementation is "drop every character
in Unicode category `C`", and it is wrong here: category `Cf` holds the zero-width joiner, which
Bangla, Hindi and Arabic need to render correctly. `র‍্য` and `র্য` are different words. Only `Cc`
and `Cs` are dropped, and a test asserts the joiner survives.

The file type is decided from the first four bytes as well as the extension, so a `.txt` renamed
to `.pdf` never reaches a parser. That check has a real limit, stated rather than glossed over:
`.docx` and `.pptx` are both ZIP archives, so the signature proves the file is a ZIP and cannot
prove which Office format it holds. A PowerPoint named `.docx` is caught one step later, by the
extractor, as `CORRUPT_ARCHIVE`. `tests/test_documents.py` covers every row above.

### Chunks never span a page, so a citation is always true

Each chunk carries the page it came from, and that is what the tutor cites: *"your slides put it
this way (lecture-3.pptx, page 4)"*. A chunk spanning pages 3 and 4 could only claim one of them,
so half its text would be cited to the wrong place. Pages are therefore chunked independently even
when that leaves a short one.

Chunks overlap by 40 of their 250 words. A boundary landing mid-explanation would leave neither
side making sense; overlapping means any short passage survives whole in at least one chunk. The
cost is about 15% more rows.

The `tsvector` is a **generated column** — Postgres maintains it from `text`, so there is no code
path that updates one without the other, because there is no code path that updates it at all.

### Search tries for all the words, then any of them

`websearch_to_tsquery` is used rather than `to_tsquery`, because the search string comes from a
language model: `to_tsquery` demands operator syntax and raises on anything else, which would make
`photosynthesis and light` a 500. But it joins terms with **AND**, and that turned out to be the
bug that made the whole feature look broken in a real room:

```
'protections prevent arbitrary tool calls'
  -> 'protect' & 'prevent' & 'arbitrari' & 'tool' & 'call'
```

The uploaded document said "protections" and "arbitrary" and never said "prevent". One ordinary
English word the model chose reduced five good matches to zero. The tutor then read that silence
as "your materials do not cover this" and answered from general knowledge — fluently, plausibly,
and without the handout it was holding. Nothing errored. The only visible symptom was a vague
answer.

So search runs strict first and falls back to matching any term, ranked. Precision when the words
really are all there, recall when they are not. The cost is that a broad pass can return passages
that merely share a word, which no rank threshold fixes honestly — measured on real data, a good
query's average rank and a nonsense query's best rank overlap, so any fixed cut-off would be a
magic number tuned to one document. Relevance is therefore judged where the meaning is understood
rather than where the words are counted: `tutor_system` v5 tells the model to read each passage,
use it only if it answers the question, and say the material does not cover it otherwise. A forced
citation the student can look up and not find is worse than no citation.

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

The cost is honest: asking for JSON creates the malformed-output problem in the first place. So the
request also carries a `response_schema`, and Gemini is constrained to emit that shape and nothing
else. This was a reversal — it was left out at first as provider-specific — and a real failure
changed it: the model answered in prose, began restating the whole answer as JSON, and ran out of
budget partway through the restatement. Constrained decoding makes that draft impossible rather
than merely discouraged.

Two things kept it honest. The schema is a field on `LLMRequest`, not something the Gemini client
knows about, so a provider without structured output ignores it and `LLMClient` stays a Protocol.
And it does nothing whatever for the content layer, which is the half the brief actually cares
about — so every check below still runs on output the provider has already guaranteed the shape of.

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

- **The tutor cannot invoke a skill.** Skills are client-invoked — `POST
  /rooms/{id}/skills/{name}/runs` — and a client renders them as actions, which is what the
  optional web interface would have been. The gap is real and worth stating plainly: a student who
  types "quiz me on photosynthesis" into the chat gets a quiz *written as prose*, with none of the
  checks in `app/skills/quiz_builder.py` — no duplicate detection, no single-correct-answer rule,
  no position shuffling, nothing stored. The reliable path exists and the conversational path
  competes with it.

  Telling the tutor to redirect was considered and dropped: with no UI it has nothing to point at,
  and *"use `POST /rooms/{id}/skills/quiz_builder/runs`"* is not a sentence to say to a Class 8
  student. The real fix is skills as model-callable tools, and what blocks it is named in [what I
  would do with more time](#what-i-would-do-with-more-time).
- **Document processing is synchronous.** Measured: a text-heavy PDF parses at roughly 1.5 MB per
  second, so 4.4 MB takes ~3 s and a 20 MB worst case around 14 s. The caller waits that long. The
  trade is deliberate — extraction is where most bad files are *discovered*, and doing it in the
  request means an encrypted PDF is a `422 ENCRYPTED` rather than a status the client has to poll
  for. Parsing runs in a worker thread (`asyncio.to_thread`), so it is only the *uploader* who
  waits; the event loop keeps serving everyone else. At 500 MB the whole trade would flip, and
  `app/services/document.py` says what would change.
- **Word files get no page numbers.** A `.docx` does not contain them — page breaks are computed by
  whatever renders the file. So a citation from a Word document names the file and stops there.
  Inventing a number would be worse than omitting one.
- **Extraction has gaps, and every one of them is silent.** No error, no warning — just a document
  that does not contain what the student can see on screen, first noticed when the tutor says the
  material does not cover something it visibly does. The full list is in `app/documents/extract.py`;
  the short version:

  | Format | Not extracted |
  |---|---|
  | `.docx` | headers, footers, text boxes, footnotes, endnotes, comments, chart data |
  | `.pptx` | SmartArt, chart data, WordArt, images |
  | `.pdf` | images, and formulas or figures rendered as images; two-column layouts can interleave |

  The Word row is the one that bites. A cover page's date, course code or supervisor is very often
  in a footer, and none of it reaches us — which is a plausible reason a real thesis progress report
  uploaded during testing had no findable submission date. Text inside **grouped** PowerPoint shapes
  *was* being dropped too, found by testing rather than reading: a group is a shape holding shapes
  and has no text of its own, so iterating the top level lost whole labelled diagrams. That one is
  fixed, with a test covering nested groups.
- **Search is full-text, not semantic**, and the sharpest way to see the limit is a real failure:
  asked for a report's submission date, the tutor answered honestly that it could not find one. The
  date was there, on slide 1, reading `AUGUST 2026`. The query terms were *report, summary,
  submission, date* — and **a date does not contain the word "date"**. No ranking change fixes that;
  lexical search matches words and has no concept that `AUGUST 2026` *is* a date. The same applies
  to "how plants eat" versus "photosynthesis". Embeddings are the fix and need a second model — see
  §16 of the plan.
- **The original file is not kept.** We store its SHA-256, name, size and extracted text. Nothing
  reads the bytes back, so keeping them would mean a volume or object store with no reader.
- **The tool timeout cannot interrupt CPU-bound work.** `asyncio.wait_for` cancels at an
  `await`, and `evaluate_expression` is synchronous. Its real protection is the size limits on
  exponents, factorials and expression length, not the 8-second timeout.
- **A tool timeout fails the whole turn.** Cancelling a database query mid-flight leaves the
  session in a state not worth continuing to write through, so the turn ends rather than
  carrying on. Deliberate, and the reason is in `app/services/turn.py`.
- **Room context is the last 6 in-scope turns**, not a summary. Long rooms therefore mean long
  prompts. The fix is a `room_summaries` table; it is designed, not built.
- **An earlier answer in the room can stop the tutor searching.** Found by testing, not by
  guessing. If a question was already answered badly — in a room where the materials tool was not
  yet reaching the documents — that answer sits in the conversation context, and asking the same
  question again makes the model recap itself rather than look anything up: *"we just talked about
  this"*. Its own earlier guess has become a source. Verified precisely: the same question in a
  fresh room with the same file searches and answers correctly, and a **new** question in the
  contaminated room also searches correctly, so it is not that history suppresses tool use in
  general — only that a repeated question is answered from the transcript. `tutor_system` v5 tells
  the model to search before claiming it has covered something, which helps and does not fully
  win against three prior answers. The real fix is to stop treating past answers as equal in
  standing to retrieved material: mark stored turns with whether they were grounded in a document,
  and drop ungrounded ones from context when the same topic comes round again.
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
- **A truncated reply is misread as a content failure.** `gemini-2.5-flash` bills thinking
  against `max_output_tokens`, so the two share one allowance and a question needing a lot of
  reasoning can run out of room mid-JSON. The parse layer sees an unclosed brace, calls it
  `JSON_TRUNCATED` and sends it to the repair loop — which cannot fix a budget problem, so both
  repairs are spent reproducing it. `finish_reason` is stored on every attempt but nothing
  branches on it yet. The fix is below, under [what I would do with more
  time](#what-i-would-do-with-more-time).
- **No rate limiting** on any endpoint.
- **Migrations are not run automatically** on startup — one explicit command after `up`.
- **`pip install -e .` in the Dockerfile runs before the source is copied**, so it installs
  dependencies but registers no package. Imports work via `PYTHONPATH=/code`. Harmless, but it is a
  wart.

Planned and already decided against (with reasons in [`docs/PLAN.md` §16](docs/PLAN.md)): no web
UI, no OCR for scanned or undecodable PDFs, no formats beyond the three the brief names, no
streaming responses, no job queue, no vector search, no web search, no code-execution sandbox, and skills the tutor cannot call. Tools were picked against a four-part test — the test and the full list
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

1. **Let the tutor invoke skills.** The one gap that contradicts this project's own claim. A
   student who types "quiz me" gets an unchecked quiz written as prose, while the checked builder
   sits behind an endpoint they have to know about — so the natural request routes to the worse
   path. The plumbing is mostly there after S6: a skill invoked mid-turn already attaches its
   attempts to the tutor's turn with `purpose='skill'`, and `skill_runs.turn_id` already points at
   it, so the tool would return a *reference* — "5 questions created" — and the quiz itself would
   never re-enter the model's context. Three things block it, and they are small but real:
   `tool_timeout_seconds` is 8 while a skill makes 1–3 model calls of its own, so timeouts need to
   be per-tool; `UNIQUE(turn_id)` means a second `build_quiz` in one turn must be refused cleanly
   rather than hitting the constraint; and the 45-second turn deadline becomes the tightest budget
   in the project. Roughly an afternoon, and it is the first thing I would spend one on.
2. **Give thinking its own budget, and route `MAX_TOKENS` to the retry path.** Thinking is billed
   against `max_output_tokens` on `gemini-2.5-flash`, so one allowance covers both. A multi-step
   question can spend 1,300–2,000 tokens reasoning and leave too little to write with — one
   observed turn spent three attempts and 8,679 tokens failing to deliver an answer it had got
   right on the first one. Raising the ceiling to 4,096 buys headroom, but it is a stopgap: the
   split between thinking and answering is still unmanaged, so a harder question moves the same
   failure rather than removing it. Two structural changes are outstanding. Set
   `thinking_config.thinking_budget`, so the visible answer is *guaranteed* room instead of
   getting whatever reasoning leaves behind. And check `finish_reason` before validating, so a
   truncated reply retries with a larger budget rather than entering the repair loop — which
   argues with the model about answer length, a thing that is neither the cause nor within its
   power to fix. `EmptyResponse` already takes the retry path for this exact root cause; today
   truncation is the same bug arriving through the one door that cannot handle it. Sizing the
   budget properly is a measurement rather than a guess, and wants a spread of real questions
   behind it — which is why it is here and not done.
3. **Rolling room summaries.** Long rooms currently mean long context. A `room_summaries` table
   storing a summary up to turn N would make opening a 500-turn room cost the same as a 5-turn one.
   Designed in [`docs/PLAN.md` §5](docs/PLAN.md), not built.
4. **A real code-execution sandbox.** The highest-value tool for a study app, and the one I
   deliberately did not build. `evaluate_expression` already runs model-written code, but only the
   subset I can prove safe: an AST allow-list of numbers, operators and a few functions, with no
   imports, no attribute access and no `9**9**9`. Going further — statements, loops, a real
   interpreter — needs an isolated container with no network, memory and PID limits, a hard
   timeout, and cleanup that survives a hang. Miss the PID limit and `while True: fork()` takes the
   host down. That is a separate service, not a function. I would rather ship a small provably-safe
   thing than a large probably-safe one, but with a week this is what I would build first.
5. **Read every document a student actually owns.** Today six kinds of file are rejected with a
   clear reason, which is honest but is still a student who cannot use the product. There are two
   separate gaps and they need different work.

   *Content we can see but cannot read* — scanned handouts (`NO_TEXT_LAYER`) and PDFs whose fonts
   record shapes rather than characters (`UNDECODABLE_TEXT`). Both are pictures of text, so both
   need OCR, and the same code path serves them: render each page with PyMuPDF, read the image,
   store the result as chunks exactly as now. Three ways to do the reading:

   | | Quality on maths and Bangla | Cost | New dependency |
   |---|---|---|---|
   | **Tesseract** | Weak. Formulas are hopeless; the Bangla pack is mediocre | Free | A binary and language packs, ~100 MB in the image |
   | **Gemini vision** | Strong. Reads formulas and layout, and Bangla well | ~1k tokens per page, one-off at upload | **None** — same model, same key |
   | **Cloud OCR** (Document AI, Textract) | Strongest | Per page, billed | Another key and account |

   I would use **Gemini vision**. The brief fixes the model and we already hold the key, so it adds
   no service, no image weight and no second vendor — and for a product serving Bangla and school
   maths it is the only one of the three that is actually good at both. A 148-page book is a
   one-off ~150k tokens, paid once at upload rather than per turn, which is the same economics as
   the text path today. The honest catch is latency: OCR is far slower than parsing, so this is the
   change that finally forces upload into a background job with a status column — the trade-off
   `app/services/document.py` already names as the point where synchronous processing stops paying.

   *Content we hold and do not read* — the silent gaps listed under [known
   limitations](#known-limitations). Word headers, footers, text boxes and footnotes are the ones
   worth doing first, because a cover page's date and course code live there and a student
   reasonably expects a question about them to work. `python-docx` walks the body only, so this
   means reading the package's other XML parts directly — `word/header1.xml`, `word/footnotes.xml`
   — which is a morning's work and closes the gap that has already produced one wrong-looking
   answer in testing. PowerPoint SmartArt and chart data are the same shape of problem, one layer
   deeper into the XML.

   *Formats we do not accept at all* — `.doc` (the old binary Word format, which needs a converter,
   not a parser), `.odt`, `.rtf`, `.txt`, `.md`, `.epub`. None of these need OCR. They need more
   extractors behind the same `Page` interface, which is why extraction was built as one function
   per format returning a shared shape.

6. **Measure instead of assert.** Seed 50,000 turns and run `EXPLAIN` on the cursor queries, so
   "this scales" is a number rather than a claim.
7. **Hybrid retrieval**, and I would move it up this list on the strength of one real failure.
   Asked for a report's submission date, the tutor said honestly that it could not find one — while
   slide 1 read `AUGUST 2026`. The query was *report summary submission date*, and **a date does not
   contain the word "date"**. Lexical search matches words; it has no concept that `AUGUST 2026` is
   a date, that "how plants eat" is photosynthesis, or that "the method they used" is a methodology
   section. Those are not ranking bugs to tune away — they are the boundary of the technique. The
   fix is embeddings alongside the `tsvector`, both queried and their results merged, which is why
   search sits behind one service function rather than being inlined into the tool. It needs a
   second model, which is the only reason it is not built.
8. **Stop past answers outranking the materials.** A question answered badly once tends to be
   answered badly again: the earlier reply sits in the room's context and the model recaps itself
   rather than searching — *"we just talked about this"*. Verified precisely, in
   [known limitations](#known-limitations). Prompting helps and does not win. The fix is to record
   on each turn whether it was grounded in a document, and drop ungrounded answers from context when
   the same topic returns, so the tutor's own guess never competes with the student's handout.
9. **Move prompt activation next to the prompts.** Which version is live is a constant in
   `app/services/turn.py`, so adding `v7.md` and forgetting that line leaves the new prompt inert —
   which happened, cost several hours, and is now caught by a test rather than prevented. Putting
   `status: active` in the prompt's own front-matter makes adding and switching one edit in one
   directory. Pinning stays deliberate; it just stops living in a file nobody thinks to open.
10. **Monthly partitioning** of `turn_attempts` and `messages`, the two tables that grow without
   bound, and a retention job for the large `raw_response` payloads.
11. **Real auth**, replacing the `X-Student-Id` header.

---

## Assumptions

Every assumption made, with its reasoning, is recorded in
[`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md). Two questions met the brief's *"essential and
unclear"* bar and were asked; the rest were resolved as documented decisions.
