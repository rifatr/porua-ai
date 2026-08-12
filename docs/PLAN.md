# Porua AI Backend — Plan

**Deadline:** Sat 15 Aug 2026, 23:59 GMT+6 · **Build time:** Tue 11 – Sat 15, about 4.5 days
**Brief:** [`ASSESSMENT.md`](./ASSESSMENT.md) · **Assumptions:** [`ASSUMPTIONS.md`](./ASSUMPTIONS.md)
**Open questions:** none. Gemini key received, deadline confirmed.

> Nothing here is pre-cut. Everything is planned and ranked. If time runs short, drop from the
> bottom of the priority list in §3 and write down what you dropped and why. The brief asks you to
> prioritise, so a clear ranking *is* part of the answer.

---

## 1. What we are building

A backend for a study app called Porua.

A student makes a **room** for one topic, like "Grade 7 Algebra: Solving Equations". Inside that
room they chat with an AI tutor. They can upload their class files (Word, PDF, PowerPoint) and the
tutor can read them. When the student comes back later, the chat carries on from where it was.

Over weeks, this builds a history the student can search: what they studied, and when.

We build the API only. No login. No website. Swagger is the interface.

---

## 2. The one big idea

If you remember one thing from this plan, remember this:

> **Every call to Gemini is saved, can be inspected, and has a hard limit.**
> Nothing goes back to the user unless we checked it first.

This drives two choices:

**1. We save every model attempt in the database, even the failed ones.**
The brief asks us to "inspect a turn in enough detail to understand its prompt, model attempts,
tool activity, failures, token usage, and final saved result." That sentence is really telling us
what tables to build. If we save attempts as real rows from the start, the inspection endpoint is
just a database read. If we only write log files, we cannot build it later.

**2. Skill quality comes from code, not from clever prompt wording.**
The brief says skills must not be "renamed versions of the same prompt". So each skill has a
prompt **plus a piece of code that checks the model's work**. The maths skill checks every step
with a maths library. The quiz skill checks the quiz against rules and against database
constraints. The model cannot argue its way past code.

---

## 3. Priority list — everything, ranked

This is the master list. Build top to bottom. If you run out of time, stop where you are and
write down what is missing.

**P0 = required.** The brief asks for it directly. Missing one of these is a failed requirement.

| # | Feature | Why it is P0 |
|---|---|---|
| 1 | Postgres + Alembic migrations; data survives a restart | "Data must persist between runs" + migration files are a listed deliverable |
| 2 | Create and list rooms | Listed |
| 3 | Open a room and see what happened inside | Listed |
| 4 | Start a new AI turn (chat with the tutor) | Listed |
| 5 | Inspect one turn in full detail | Listed, and the hardest one to add later |
| 6 | Query study history over a date range | Listed |
| 7 | Upload `.docx`, `.pdf`, `.pptx` and pull the text out | Listed |
| 8 | Tool 1: search the room's uploaded files | "The AI tutor can query those as needed" |
| 9 | Tool 2: query past study history | Brief needs 2 tools; this is their own example |
| 10 | Skill 1: quiz builder | Brief needs 2 skills |
| 11 | Skill 2: step-by-step maths solver | Brief needs 2 skills; their own example |
| 12 | Read the model's JSON safely and check it against a schema | "Assume the model will return malformed data" |
| 13 | Hard limits on tool use (max calls, max time) | "The model should not call tools indefinitely" |
| 14 | Swagger docs that actually work | Listed deliverable |
| 15 | README, `.env.example`, prompts in the repo, many small commits | Listed deliverables |

**P1 = big marks.** Not spelled out as a bullet, but this is what they are grading.

| # | Feature | Why it matters |
|---|---|---|
| 16 | `turn_attempts` table: every attempt, including failures, with token counts | This is what makes #5 possible. Token usage is a stated bonus. |
| 17 | Repair loop: when output is bad, ask again with only the specific problem | The core of "build enough reliability around it" |
| 18 | Quiz content checks: duplicate options, wrong count, no correct answer, two correct answers | These are the exact four examples in the brief |
| 19 | Database constraints as a safety net for the same rules | Shows data design and reliability in one move |
| 20 | Answer-position balancing done in code with a fixed random seed | The 4th named failure. Cannot be fixed by prompting. |
| 21 | Maths steps verified with sympy before the student sees them | This is what makes the skill more than a prompt |
| 22 | Tool 3: safe calculator (`evaluate_expression`) | Proves "should not call arbitrary code" |
| 23 | Bad file uploads rejected with a clear typed reason | "Handle bad and unsupported inputs deliberately" |
| 24 | Cursor-based paging on every list endpoint | The "how does it behave as history grows" question |
| 25 | Indexes that match the actual queries | Same |
| 26 | Prompts stored as versioned files | "Keep prompts readable and versioned" |
| 27 | Fake Gemini client + saved test fixtures | Lets you test failures you cannot trigger on demand |
| 28 | Retry on 429 / 500 with backoff | Basic reliability |
| 29 | Tool arguments checked before running; tools scoped to one student | Security, and it is a good story on the call |
| 30 | Search results cite the page or slide number | Makes the search tool genuinely useful |

**P2 = good extras.** Shows depth. Drop these before P1.

| # | Feature | Why it is worth doing |
|---|---|---|
| 31 | `room_summaries`: summarise old turns so long rooms stay fast | Strongest answer to "as history grows" |
| 32 | Tool 4: `get_room_timeline` | Cheap recall of what happened in this room |
| 33 | `Idempotency-Key` so a retry does not pay for two Gemini calls | Shows you think about cost |
| 34 | Detect the model calling the same tool twice and return the cached result | Stops loops instantly |
| 35 | Seed 50,000 turns and measure query speed with `EXPLAIN` | Turns a claim into a number you can quote |
| 36 | `docs/DECISIONS.md` written as you go | Your script for the review call |
| 37 | Chunk documents on headings, not just fixed size | Better search results |
| 38 | Circuit breaker when Gemini is down | Standard practice |
| 39 | Note on deleting old `raw_response` data | Shows you thought about storage growth |

**P3 = only if you are ahead.** Drop these first.

| # | Feature |
|---|---|
| 40 | Skill 3: flashcards with spaced repetition (SM-2) |
| 41 | GitHub Actions CI |
| 42 | Strict mypy type checking |
| 43 | Table partitioning by month |
| 44 | A small web page on top of the API |

**This list is a backlog order, not a schedule.** You build slice by slice (§13), so you never sit
down and "drop item 40" — instead each slice has a core (P0 + P1) you always finish and a trim
(P2 + P3) you take only if that slice ran early. **See the core/trim table in §13**, which is the
part you actually use when running late.

Read this list for *why* something matters. Read §13 for *when* to build it and *what* to skip.

---

## 4. Tech stack — and what each piece actually does

You have not used FastAPI before, so here is what each library is for. You will be asked about
these on the call.

| Tool | What it does | Why this one |
|---|---|---|
| **FastAPI** | The web framework. You write Python functions, it turns them into HTTP endpoints and generates Swagger docs automatically. | Required by the brief |
| **Pydantic v2** | Defines the shape of data. You write a class with typed fields; Pydantic checks incoming JSON matches it and rejects it if not. FastAPI uses it for request/response bodies. | Comes with FastAPI. Also how we check Gemini's output. |
| **SQLAlchemy 2.0** | Talks to the database using Python classes instead of raw SQL strings. A `Room` class maps to a `rooms` table. | Standard choice; async support |
| **Alembic** | Version control for your database structure. Each change is a numbered file that can be applied or undone. | Migration files are a required deliverable |
| **PostgreSQL 16** | The database. | Brief says relational. Postgres gives us JSON columns, full-text search, and partial indexes. SQLite could not answer the "as history grows" question. |
| **google-genai** | Google's official Python library for calling Gemini. | Required model is `gemini-2.5-flash` |
| **PyMuPDF** | Reads PDFs. Gives you text plus which page it came from. | Page numbers let us cite sources |
| **python-docx** | Reads Word files. | Standard |
| **python-pptx** | Reads PowerPoint, including speaker notes. | Speaker notes are often the best study content |
| **sympy** | Maths library. Can check whether `3x + 6 = 15` really becomes `3x = 9`. | This is what makes the maths skill real |
| **pytest + httpx** | Tests. `httpx.AsyncClient` calls your API in tests without starting a real server. | Standard |
| **Docker Compose** | Runs the API and Postgres together with one command. | Makes setup instructions one line |

### Two FastAPI ideas you will lean on

**Dependency injection.** In FastAPI you write `def get_room(db = Depends(get_session))`. FastAPI
sees `Depends(...)`, runs `get_session()` first, and hands you the result. We use this for the
database session and for reading the student id from the request header. It means you never open
a database connection by hand inside an endpoint.

**Async.** FastAPI endpoints can be `async def`. While one request waits on the database or on
Gemini, Python can serve another request. The rule: if you call something with `await`, the whole
chain above it must be `async`. This is why we use SQLAlchemy's async engine.

---

## 5. Database design

Ten tables. Here is how they connect:

```
students ──< rooms ──< turns ──< turn_attempts ──< tool_calls
                │        │
                │        └──< messages
                │        └──< skill_runs ──< quizzes ──< quiz_questions ──< quiz_choices
                ├──< documents ──< document_chunks
                └──< room_summaries
```

Read `──<` as "one to many". One student has many rooms. One room has many turns.

Every foreign key is `ON DELETE CASCADE` from its room, so deleting a room cleans up everything
inside it. Every time column stores the timezone.

### The tables worth explaining

**`turn_attempts`** — one row per call to Gemini, including calls that failed.

Columns: `turn_id`, `attempt_no`, `purpose` (tutor / repair / skill / summarise), `prompt_name`,
`prompt_version`, `prompt_sha256`, `rendered_prompt`, `request_params`, `raw_response`,
`finish_reason`, `prompt_tokens`, `output_tokens`, `error_type`, `validation_failures`,
`started_at`, `finished_at`.

This is the most important table in the project. It is what the inspection endpoint reads.

**`quiz_choices`** — this is where we turn "the model sometimes makes bad quizzes" into a rule the
database enforces:

```sql
-- Two options in the same question cannot have the same text.
-- lower() and btrim() mean "Paris", "paris " and "PARIS" all count as the same.
CREATE UNIQUE INDEX ux_choice_text ON quiz_choices (question_id, lower(btrim(text)));

-- A question can have only one correct option.
-- The WHERE clause means the rule only applies to rows where is_correct is true.
CREATE UNIQUE INDEX ux_choice_correct ON quiz_choices (question_id) WHERE is_correct;
```

Two of the four problems the brief names now become impossible to save. Our Python code catches
them first and asks the model to fix it. The database is the safety net if the code misses one.

Wrong option count is a `CHECK` constraint. Predictable answer positions are handled in code
(§8), because that one cannot be expressed as a constraint.

**`room_summaries`** — `(room_id, up_to_turn_seq, summary, token_count)`.

When a room gets long, we cannot send 200 old messages to Gemini every time. Instead we store a
summary of everything up to turn N, and send that summary plus the last few turns. So opening a
500-turn room costs the same as opening a 5-turn room.

This is the best answer we have to "how does the design behave as history grows". It is P2 only
because the room has to get long before it matters.

**`documents`** — `UNIQUE (room_id, sha256)`. `sha256` is a fingerprint of the file contents. If a
student uploads the same file twice, we recognise it and skip re-processing.

**`document_chunks`** — the document text cut into pieces, each with the page or slide number it
came from, plus a `tsvector` column for searching.

> **What is `tsvector`?** Postgres's built-in text search. It stores a processed version of the
> text (words split, common words removed, "running" reduced to "run") so searching is fast. With
> a GIN index it is the right tool here and needs no extra service.

### Indexes, and what each one is for

| Index | Used by |
|---|---|
| `rooms (student_id, last_activity_at DESC)` | listing a student's rooms |
| `turns (room_id, seq DESC)` and `UNIQUE (room_id, seq)` | room timeline, and keeping turn numbers gap-free |
| `turn_attempts (turn_id, attempt_no)` | the inspection endpoint |
| `turns (student_id, created_at DESC)` | study history over a date range |
| `document_chunks USING GIN (tsv)` | the file search tool |
| the two `quiz_choices` indexes above | the quiz rules |

**Growth.** `turn_attempts` and `messages` grow forever. Two things follow. First, never select
`raw_response` in a list endpoint — it is large and will slow the query down. Second, splitting
these tables by month (partitioning) would help one day; we will not do it now, and the README
says why. Building it for a demo dataset would be over-engineering.

---

## 6. API endpoints

```
POST   /students                                  create a demo student
GET    /students/{id}/study-history?from&to&subject&cursor

POST   /rooms                                     title
GET    /rooms?cursor&limit&subject
GET    /rooms/{id}                                room details + recent activity
GET    /rooms/{id}/messages?cursor

POST   /rooms/{id}/turns                          talk to the tutor
GET    /rooms/{id}/turns?cursor
GET    /turns/{id}                                <-- the inspection endpoint

POST   /rooms/{id}/documents                      file upload
GET    /rooms/{id}/documents
GET    /documents/{id}                            processing status
DELETE /documents/{id}

POST   /rooms/{id}/skills/{skill_name}/runs       run a skill directly
GET    /skill-runs/{id}

GET    /healthz
```

Every list endpoint uses **cursor paging**, not page numbers.

> **Why not `?page=50`?** Page numbers use SQL `OFFSET`. To return rows 5000–5020 the database has
> to walk past the first 5000 rows every time. It gets slower as data grows. A cursor says "give
> me rows after this exact point", which stays fast forever. This is a direct answer to their
> "as history grows" requirement, so it is worth doing everywhere.

Errors all use the same JSON shape, so a client never has to guess.

`GET /turns/{id}` is the endpoint to demo on the call. It returns the whole tree: every attempt,
what prompt version was used, how many tokens, every tool call with its arguments and result,
every check that failed, and the final answer.

---

## 7. How one turn works

When a student sends a message:

```
1. Save the student message.
2. Build the context: system prompt + room summary + last few turns + student message.
3. Call Gemini, with the tool list attached.
4. Did Gemini ask to use a tool?
     yes -> check the arguments, run the tool, save a tool_calls row,
             send the result back to Gemini, go to step 4 again
     no  -> continue
5. Read the reply. Parse the JSON. Check it against the schema. Check the content rules.
6. Bad? Ask Gemini again with only the specific problem. Max 2 times.
7. Still bad? Mark the turn failed with a clear reason. Do not send junk to the user.
8. Good? Save it and return it.
```

Every call in steps 3, 4 and 6 writes a `turn_attempts` row.

### Keeping tool use under control

The brief says tools must be controlled and the model must not call code freely. Five separate
protections, because relying on one is fragile:

1. **A fixed list of tools.** Tools are registered when the app starts. If Gemini asks for a tool
   name we did not write, nothing is found and it gets an error back. There is no path from model
   output to running arbitrary code.
2. **Hard limits.** Max 5 loops, max 6 tool calls per turn, 8 seconds per tool, 45 seconds for the
   whole turn. Whichever runs out first ends the turn with a clear reason.
3. **Arguments checked first.** The arguments must fit the Pydantic model or the tool never runs.
4. **Same call twice?** Return the saved result and tell the model it already asked. Limits stop a
   loop eventually; this stops it immediately.
5. **The student id never comes from the model.** It comes from the request. So the model cannot
   ask for another student's data — the parameter simply does not exist in the tool's arguments.

Text from uploaded files and results from tools are wrapped in tags and labelled as *data, not
instructions*. Uploaded files come from outside, so we do not let them give orders to the model.

---

## 8. Tools and skills

**Tools** = things the model can call during a chat.
**Skills** = structured jobs that produce a checked result (a quiz, a solved problem).

### The test a tool has to pass

Long lists of tools are easy to write and hard to defend. Every tool below was picked with the
same four questions, and the ones that failed are listed too, with the reason. On the call, *why
these and not those* is the question — so the test matters more than the list.

**A tool earns its place only if all four are true:**

1. **The model cannot answer without it.** It needs private data, or exact computation. If the
   answer is already in the prompt, a tool that fetches it again is waste.
2. **It works inside the constraints.** One fixed model, `docker compose up`, a Gemini key and
   nothing else. A tool needing a second paid API is dead on the reviewer's machine.
3. **It can be made safe and bounded.** Timeout, argument checking, no path to arbitrary code.
4. **It can be built and tested in the time left.** A required deliverable must never be at risk
   for an optional one.

### Tools

| Tool | What it does | Why it must be a tool | Priority |
|---|---|---|---|
| `search_room_materials(query, top_k)` | Searches the room's uploaded files, returns text plus "deck.pptx, slide 12" | The model cannot see files we did not give it, and we cannot paste whole files into every prompt | P0 |
| `query_study_history(from, to)` | Counts rooms, turns and concepts over a date range | Answers "what did I study last week?" from the database instead of guessing | P0 |
| `evaluate_expression(expr)` | A calculator using sympy | Models are bad at arithmetic. Also our proof that tools cannot run arbitrary code. | P1 |

### Tools considered and rejected

| Tool | Fails | Why |
|---|---|---|
| `get_room_timeline(limit)` | 1 | The last 6 turns are already in every prompt. This would fetch back what we just sent. Was P2; cut. |
| **Web search** | 2 | Needs a paid key (Brave, Serper, Tavily). A reviewer with only a Gemini key gets a tool that never works. A curriculum tutor should also ground in the student's own materials, not the open web. |
| **Code execution sandbox** | 3, 4 | The highest-value tool on any list, and the one that cannot be done safely in the time. Needs an isolated container, no network, memory and PID limits, a hard timeout, and cleanup that survives a hang. Miss one and `while True: fork()` wins. Goes in §16 and in the README's "with more time". |
| **Wolfram Alpha** | 2 | Right idea, wrong supplier. Needs a key and costs money. sympy is a library — free, offline, deterministic. |
| **OCR / image analysis** | 2 | Needs a vision model or OCR service. The model is fixed to `gemini-2.5-flash`. |
| **Diagram generator** | 1 | The model can write Mermaid directly into its answer. No tool needed. |
| **Progress / mastery tracker** | 1 | Without quiz results, "mastery" is just counting mentions. Folded into `query_study_history` as concept counts; becomes real after S6. |
| **Text-to-speech, bash, file editor** | 1, 2 | No audio and no shell anywhere in the brief. |

**Note on the rejected examples.** The brief says a room is for "a topic **such as** AP Biology or
Grade 7 Algebra". Those are illustrations, not limits — a student can open a room about Python.
So "we do not tutor code" is *not* a reason to skip code execution. The reasons are safety and
time, which are honest ones.

**About `evaluate_expression` safety.** It *is* code execution — just the subset that can be proved
safe. We do *not* use Python's `eval`. We parse the expression into a syntax tree and walk it,
allowing only numbers, the basic operators, and a short list of functions. Anything else is
rejected: no imports, no attribute access, no huge powers like `9**9**9`. Write this carefully —
it is the most security-sensitive file in the project, and exactly the kind of thing they will ask
you to defend.

### Skills

The bar is: real value for students, extends what the model can do, not a renamed prompt.
Each skill is a prompt **plus code that checks the result**.

**`quiz_builder`** (P0, its checks are P1)

1. Ask Gemini for a quiz using a response schema.
2. Check it: right number of questions, right number of options, no duplicate options (after
   lowercasing and trimming spaces), exactly one correct answer, wrong options are not just the
   right answer reworded, every question backed by an uploaded file when files exist, language
   suits the student's education level.
3. **Balance the answer positions in code.** Shuffle the options using a fixed random seed, so the
   correct answer is spread evenly across A/B/C/D. Models tend to put the correct answer in the
   same slot. You cannot fix that by asking nicely. Because the seed is fixed, tests get the same
   result every time.
4. If checks fail, ask again — up to twice, mentioning only what was wrong, at a lower
   temperature.
5. Still failing? Drop the bad questions. Too few left? Fail the whole skill with a clear reason.
   Never show a broken quiz.

**`step_solver`** (P0, its sympy checks are P1)

1. Break the problem into steps. Each step states a claim, like `3x + 6 = 15` becomes `3x = 9`.
2. **Check every step with sympy before the student sees it.** If a step is wrong, redo it.
3. Check the final answer by putting it back into the original equation.
4. Record which steps were verified, so the inspection endpoint shows the checking happened.

This is the difference between a prompt that says "explain step by step" and a solver whose steps
are *known* to be correct.

**`flashcard_srs`** (P3) — Gemini writes the cards, then the SM-2 spaced repetition algorithm
decides when each card is due. The model writes content, the algorithm owns the schedule.

Every skill is stored as `name@version` with its prompt file, output schema, checks, repair rules,
and where it saves to. Every run creates a `skill_runs` row with the check results.

---

## 9. Making the AI reliable

Five layers. Each one has its own limit, and every attempt is saved.

| Layer | Problem it solves | Limit |
|---|---|---|
| **Network** | 429 rate limits, 500 errors, timeouts. Wait and retry, with growing gaps plus a bit of randomness. | 3 tries |
| **Parsing** | The model wrapped JSON in ``` fences, added "Sure! Here you go:" first, or got cut off mid-sentence. | 1 retry |
| **Schema** | JSON is valid but the fields are wrong or missing. Pydantic catches this. | 2 repairs |
| **Content** | JSON is perfectly valid but the quiz is bad. The checks in §8. | 2 repairs |
| **Fix-ups** | Things code should do, never the model: shuffling, ordering, cleaning up spacing. | n/a |

Also across all of them: an `Idempotency-Key` header so a client retrying does not pay for a
second Gemini call; a fixed temperature per prompt; the prompt version saved on every attempt; one
correlation id per request in the logs; and clear failure names like `SCHEMA_INVALID_AFTER_REPAIR`
or `TOOL_BUDGET_EXCEEDED` returned as proper API errors.

**The rule:** a turn either gives a checked result, or it fails openly. There is no middle state
where the user gets something we did not check.

---

## 10. Document upload and reading

```
upload -> check the file -> save it -> extract text -> split into chunks -> index -> ready
                     └────────── or fail with a clear reason ──────────┘
```

**Checking the file.** We look at the file extension *and* the first few bytes. A real PDF starts
with `%PDF`. Word and PowerPoint files are zip archives and start with `PK`. So a `.txt` renamed
to `.pdf` gets caught. Size limit is 20 MB.

**Extracting text.** PDF gives text plus page numbers. Word gives paragraphs, headings and table
cells. PowerPoint gives each slide's text, its tables, and the speaker notes.

**Chunking.** Split the text into pieces of roughly 800 tokens with 100 tokens of overlap. Never
split across a page or slide boundary. Keep the character positions so search results can point
back to the exact place.

> **Why overlap?** If a sentence gets cut in half at a chunk boundary, neither chunk makes sense
> on its own. Overlapping means the full sentence appears in at least one chunk.

**Handling bad files.** Each gets a clear reason and the right HTTP code — never a 500:

`UNSUPPORTED_TYPE` (415) · `FILE_TOO_LARGE` (413) · `CORRUPT_ARCHIVE` · `ENCRYPTED` ·
`NO_TEXT_LAYER` (a scanned PDF that is really just images) · `EMPTY_EXTRACTION` (opens fine, has
no text).

A failed upload must end as `failed` with a reason, not sit at `processing` forever.

**Known limits, written down rather than hidden:** `python-docx` cannot read headers, footers or
text boxes. PowerPoint SmartArt is not extracted. PDFs with two columns can come out interleaved.
We do not do OCR, so scanned PDFs are rejected rather than silently accepted as empty.

---

## 11. Prompts

All prompts live in `prompts/` as files, one per prompt, with a version number.

Each file starts with a small header: name, version, model, temperature, and a changelog line
saying what changed and why.

We use Jinja2 for variables, in strict mode. If a prompt expects `{{ education_level }}` and we forget
to pass it, it raises an error instead of quietly rendering an empty space.

Every saved attempt records the prompt name, version, and a checksum of the exact text. So for any
stored answer, you can find the exact prompt that produced it. This is why we **never edit a
prompt file after it has been used** — we make a new version instead.

Prompts to write: `tutor_system`, `context_summarizer`, `quiz_builder`, `quiz_repair`,
`step_solver`, `history_synthesizer`.

---

## 12. Testing

**The whole test suite runs without a Gemini API key.** A fake client returns saved responses
instead of calling the network. This is what makes the reliability layer testable.

You cannot ask the real Gemini to return a broken quiz on demand. So we write the broken responses
by hand, one file per problem:

- JSON in ``` fences · text before the JSON · response cut off halfway
- quiz with duplicate options · wrong number of questions · no correct answer · two correct answers
- **every correct answer in position B** — this one tests the balancer, not the prompt
- a tool call with bad arguments · a tool loop that must hit the limit
- repeated 429s · a 500 from Google

Plus real file fixtures: a small `.docx`, `.pdf` and `.pptx`, a corrupt zip, a password-protected
PDF, a scanned PDF, and a `.txt` renamed to `.pdf`.

The most valuable assertion in the whole suite: **for every broken response, nothing invalid is
ever saved or returned.** That is the claim the project rests on.

---

## 13. Build order

We build in **vertical slices**. Each slice is one working path all the way through: migration →
model → service → endpoint → test.

The other option is layer by layer: all the models, then all the services, then all the endpoints.
We are not doing that, for three reasons:

1. **You can stop at any time and still have something that works.** With layers, nothing runs
   until the last layer is done, and then everything breaks at once. With a fixed deadline that
   is too risky.
2. **You will understand what you built.** Each slice is small enough to think about properly.
   For the review call, "I built it this way, then hit this problem, so I changed it" is a much
   stronger answer than "that is the standard pattern".
3. **It slows you to the speed of your own thinking.** With AI tools you can generate ten
   endpoints in an hour. Anything generated faster than you can form an opinion about becomes a
   question you cannot answer later.

**The database is designed fully up front (§5) but created a few tables at a time.** One giant
migration on day one looks like a dump. Six migrations, each arriving with the feature that needs
it, look like a plan being followed.

### The slices

**Features** are numbered from §3. A slice is done when its features are done. Read this table
as the bridge between "what to build" (§3) and "when to build it" (below).

| Slice | What it delivers | Features from §3 | Tables added |
|---|---|---|---|
| **S0 Spine** | Docker Compose, settings, database session, error format, paging helper, test setup, `/healthz` | **1**, 14, 24, 25 | `students`, `rooms` |
| **S1 Rooms** | Create / list / open / delete rooms, with tests and Swagger examples | **2**, 24 | — |
| **S2 Turns** | `POST /turns` calls Gemini for real, saves the attempt, `GET /turns/{id}` shows it. **No checking yet, on purpose.** | **3**, **4**, **5**, 16, 26, 27, 33, 39 | `turns`, `messages`, `turn_attempts` |
| **S3 Reliability** | Fixtures prove S2 returns junk. Then build parse → schema → content → repair. | **12**, 17, 27, 28, 38 | — |
| **S4 Tools** | Tool registry, the five protections, `query_study_history` + `evaluate_expression` | **9**, **13**, 22, 29, 34 | `tool_calls` |
| **S5 Documents** | Upload, file checking, three extractors, chunking, search, the bad-file cases | **7**, **8**, 23, 30, 37 | `documents`, `document_chunks` |
| **S6 Skills** | `quiz_builder` and `step_solver` with all their checks | **10**, **11**, 18, 19, 20, 21 | `skill_runs`, `quizzes`, `quiz_questions`, `quiz_choices` |
| **S7 History** | Study history endpoint over a date range | **6** | — |
| **S8 Extras** | Everything left in P2, plus P3 if somehow ahead | 31, 32, 35, 40 | `room_summaries` |
| **S9 Ship** | README, demo script, rubric check, repo, collaborators, archive | **15**, 41, 42 | — |

**Bold = P0**, the features the brief asks for directly. Feature **36** (`docs/DECISIONS.md`) has no
slice because it is written continuously, a few lines at the end of each one. Features **43** and
**44** (partitioning, web UI) are not built at all — they go in the README's "what I would do next".

#### What this mapping tells you

- **S6 is the heaviest slice** — six features, two of them P0, and it holds the answer to "how is
  your skill not just a prompt?" Friday cannot slip.
- **14 of the 15 P0 features are done by the end of S7.** The last one is #15, the deliverables
  bundle, which finishes in S9. So **S8 is entirely optional**: if Saturday morning disappears, the
  submission is still complete against the brief.
- **A drop decision belongs at a slice boundary, not on Saturday.** Feature 37 is decided when S5
  starts, 31/32/35 when S8 starts. That is the moment you can see what the time actually costs.
- **Some features are spread, not owned by one slice**: 14 (Swagger), 24 and 25 (paging and
  indexes), 36 (decision log), and most of 15 — `.env.example` landed in S0, prompts start in S2,
  commits are continuous, and only the README is genuinely S9 work. If any of these first appear at
  the end, they will be thin.

Running count of P0 finished: S0 → 2, S1 → 3, S2 → 6, S3 → 7, S4 → 9, S5 → 11, S6 → 13, S7 → 14,
S9 → 15.

### How dropping actually works when you build slice by slice

The priority list in §3 is a **backlog** order. The slices are a **schedule**. They do not line up
on their own: if you are late on Friday inside S5, "drop feature 40" saves you nothing, because
feature 40 lives in S8 and you were never going to build it that day.

So every slice is split in two. Build the **core** first, always. Take the **trim** only if the
slice finished early.

| Slice | Core — always build (P0 + P1) | Trim — only if on time (P2 + P3) |
|---|---|---|
| **S0** | 1, 14, 24, 25 | — |
| **S1** | 2, 24 | — |
| **S2** | **3, 4, 5**, 16, 26, 27 | 33 idempotency · 39 retention note |
| **S3** | **12**, 17, 27, 28 | 38 circuit breaker |
| **S4** | **9, 13**, 22, 29 | 34 repeat-call dedupe |
| **S5** | **7, 8**, 23, 30 | 37 heading-aware chunking |
| **S6** | **10, 11**, 18, 19, 20, 21 | — |
| **S7** | **6** | — |
| **S8** | — | 31, 32, 35, 40 — **the whole slice is trim** |
| **S9** | **15** | 41 CI · 42 mypy |

The two rules that follow:

1. **Late inside a slice → drop that slice's trim, then move on.** Do not let a P2 item push the
   next slice's core into the following day. A skipped trim item costs one line in the README; a
   missing core item is a failed requirement.
2. **Late across days → you simply never reach S8.** No decision needed, because S8 is entirely
   trim. That is why it was placed last.

Notice that S0, S1, S6 and S7 have no trim at all. There is nothing to shave there — if those run
long, the time has to come from somewhere else, which is exactly what makes S6 (Friday) the day to
protect.

**If you are so far behind that only core work remains**, do not thin every slice to fit. Ship
fewer slices completely. A finished S5 with no S7 beats a half-built version of both, because the
brief grades "whether the whole thing hangs together as one considered piece of work" — and a
half-built feature subtracts from that, while a missing one you can explain does not.

**S0 sets the patterns everything else copies.** Error format, paging, session handling, test
setup. Get these right once and every later slice is a fill-in-the-blank. Get them wrong and you
copy the mistake eight times. This is why S0 should be done fresh, not late at night.

**S2 has no validation on purpose.** We save the attempts from the very first call, but we do not
check the output yet. Then S3 opens by proving with real fixtures that it returns junk, and builds
the checks against real observed failures.

This costs nothing — it is only the order — and it gives you the best answer available on the
call: *"I built the inspection view first because I could not debug the model without it. It
immediately showed me the failure modes, and then I wrote the checks for them."* And it is true.

### Days

| Day | Slices | You are done when |
|---|---|---|
| **Tue 11** | S0, S1, S2 | A real Gemini turn is saved and `GET /turns/{id}` shows the attempt. Unchecked, and that is the point. |
| **Wed 12** | **S3** | The broken-response fixtures all pass. Nothing unchecked reaches the user. **Protect this day.** |
| **Thu 13** | S4, start S5 | Tool limits proven by a runaway-loop test. Upload works and rejects a fake PDF. |
| **Fri 14** | Finish S5, S6 | Three file types extract with page numbers. Both skills work with their checks. |
| **Sat morning** | S7, then S8 if ahead | History endpoint works. **Stop building at midday.** |
| **Sat afternoon** | S9 | Ship. |

Commit several times per slice, at points where things work. One big commit per day looks like a
dump, and commit history is on their list.

**Checkpoint: Thursday evening.** If S5 has not started, drop P2 items 37 and 39 immediately and
keep going. Do not wait until Saturday to find out.

### Tonight (Monday, 30 minutes, then stop)

Three mechanical jobs. **Do not start S0 now** — it sets the patterns everything copies, and
that is the worst thing to do tired.

1. **Test the Gemini key** with one small call. If it is wrong or rate-limited, you want to know
   tonight, not Wednesday.
2. **Create the private repo and invite `ikramhasan` and `azmainadel`.** This removes a Saturday
   job and catches a username typo now.
3. **`git init` and commit `docs/`.** Your commit history then starts with the plan, before any
   code. That is a good shape for them to see.

### Record real Gemini responses on Tuesday

During S2, save real responses to fixture files. After that, everything runs against real recorded
behaviour: same result every time, no quota used, no network needed in tests. The *broken*
fixtures stay hand-written, because the real model will not produce those on demand.

---

## 14. Words you will need to explain

You will be asked what these mean. Short answers:

| Term | Plain meaning |
|---|---|
| **Migration** | A numbered file describing one change to the database structure, which can be applied or undone. |
| **Cursor paging** | Asking for "the next 20 rows after this point" instead of "page 50". Stays fast as data grows. |
| **Index** | An extra lookup structure so the database finds rows without reading the whole table. |
| **Partial index** | An index that only covers rows matching a condition. We use one for "only one correct answer per question". |
| **Foreign key** | A column pointing at another table's row. `ON DELETE CASCADE` means deleting the parent deletes the children. |
| **Deterministic** | Same input always gives the same output. Our shuffle is deterministic because the random seed is fixed. |
| **Idempotent** | Doing it twice has the same effect as doing it once. That is what `Idempotency-Key` gives us. |
| **Token** | Roughly a word-piece. Gemini charges by tokens, and has a limit per request. |
| **Temperature** | How random the model's output is. Lower means more predictable. We lower it on repair attempts. |
| **Schema validation** | Checking the JSON has the right fields with the right types. Pydantic does this. |
| **Semantic validation** | Checking the *content* makes sense, even though the JSON is well-formed. Our quiz checks. |
| **`tsvector`** | Postgres's processed form of text, used for fast full-text search. |
| **Chunking** | Cutting a long document into smaller pieces so we can search and send only relevant parts. |
| **Backoff** | Waiting longer between each retry instead of hammering the server. |
| **Dependency injection** | FastAPI's `Depends(...)`. It runs a function for you and passes the result into your endpoint. |

---

## 15. Questions they will probably ask

Prepare an answer for each. The strongest answers point at a commit, a test, or a number.

1. **Why Postgres and not SQLite?** JSON columns, full-text search, partial indexes, and real
   concurrency. SQLite would make the "as history grows" question unanswerable.
2. **Why is your quiz skill not just a prompt?** Because the prompt is the smallest part. Show the
   checks, the seeded balancer, and the two database constraints.
3. **How do you stop the model looping on tools?** Five protections. Show the runaway-loop test.
4. **What happens when Gemini returns garbage?** Show the fixture suite and the failed-attempt
   rows in the inspection endpoint.
5. **How does this behave with a year of history?** Cursor paging plus the indexes. If you did P2
   item 35, quote the measured numbers.
6. **What would you do with more time?** A code-execution sandbox first, then `room_summaries`,
   vector search, partitioning, OCR. Have this list ready and ordered.
7. **What did you deliberately not build, and why?** §16 answers this. Being able to answer it
   confidently is worth as much as any feature.
8. **Why these tools and not others?** §8 has the four-part test and the full rejected list. Lead
   with the test, not the list — anyone can name ten tools, few can say why they stopped at three.
   The strongest single answer here is code execution: name it as the most valuable tool you did
   not build, say exactly what a safe sandbox needs, and explain that `evaluate_expression` is the
   subset you could prove safe.
9. **You had not used FastAPI before — what surprised you?** Answer honestly. Dependency injection
   and the async rules are good, real answers.

Write `docs/DECISIONS.md` as you go: what you chose, what else you considered, why, and what would
change your mind. Writing it on Saturday from memory does not work.

---

## 16. What we are not building

| Left out | Why |
|---|---|
| Web UI | Optional in the brief. The time goes into the reliability tests instead. |
| Login / auth | Explicitly excluded. We still model students, so auth could be added without changing the schema. |
| Celery / Redis job queue | One `docker compose up` is better than an extra service. File processing uses FastAPI background tasks with a status column, so a real queue could replace it later in one module. |
| OCR for scanned PDFs | Rejecting them clearly *is* the deliberate handling they asked for. Silently saving an empty document is the real failure. |
| Streaming replies (SSE) | Streaming would hide the repair loop, which is the interesting part. |
| Vector / embedding search | "Fixed model" is read as applying to the chat model. Text search is enough here and needs no second model. The search code sits behind an interface so embeddings could drop in later. |
| Code execution sandbox | The best tool we are not building, and the first thing to add with more time. `evaluate_expression` covers the subset that can be *proved* safe — an AST allow-list over expressions. Going further needs an isolated container, no network, memory and PID limits, a hard timeout, and cleanup that survives a hang. That is a service, not a function. A large probably-safe sandbox is worth less than a small provably-safe one, and would hand the reviewer a hole to poke. |
| Web search | Needs a second paid API key. A reviewer running `docker compose up` with only a Gemini key would find a tool that never works — a broken setup instruction. A tutor should also ground in the student's own uploaded materials rather than the open web. |

---

## 17. Risks

| Risk | What we do about it |
|---|---|
| **Not enough time** | Everything is ranked in §3. Drop from the bottom, on schedule, and write down what you dropped. Thursday evening is the checkpoint. |
| **First time with FastAPI** | S0 and S1 are deliberately simple, to learn the patterns before the hard parts. Keep §14 open. Ask questions early in the week, not Friday. |
| **S0 sets a bad pattern** | Everything copies it. So build S0 fresh, and review it once before starting S2. |
| **Rate limits while developing** | Record fixtures Tuesday, then work offline. |
| **Losing a day to PDF edge cases** | Timebox S5. Write the limits down instead of chasing them. |
| **The reliability layer eating the whole week** | It is the centre of the brief, so it deserves Wednesday — but Thursday starts S4 regardless of how polished it is. |
