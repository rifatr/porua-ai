# Assumptions

The brief says: *"If you make an assumption, document it. If something essential is unclear, ask
us without hesitation."*

**Nothing is outstanding.** Two items met the "essential and unclear" bar and both are resolved:

- **Gemini API key** — provided.
- **Deadline** — 15 August 2026, 23:59 GMT+6.

Everything else below was resolvable by judgment, so it is recorded as a decision with its
reasoning rather than sent back as a question. This register is the source for the README's
assumptions section.

---

## Decisions taken in place of questions

| Topic | The ambiguity | Decision | Reasoning |
|---|---|---|---|
| **Embeddings** | "The fixed model is `gemini-2.5-flash`" — does that forbid an embedding model too? | "Fixed model" constrains **generation**. Retrieval is lexical: Postgres `tsvector` + trigram, behind a `Retriever` interface so a vector backend drops in later. | Lexical retrieval fully satisfies "the AI tutor can query those as needed," stays deterministic, and tests offline. Committing to one model and explaining why beats hedging with two. |
| **Student identity** | No login required, yet "query a **student's** study history" implies many students. | Real `students` table; caller passes `X-Student-Id`; seeded demo students. Scoped at the data-access boundary so an agent tool cannot read across students. | Data design is graded and the brief wrote "a student's." Costs nothing if the single-student reading was intended. |
| **History: endpoint or tool** | Appears as an acceptance item *and* as their example tool. | Both, over one shared service layer. | The brief already specifies both. |
| **Bad-input handling** | OCR for scanned PDFs? Encrypted files? Size cap? | No OCR — typed `NO_TEXT_LAYER` rejection. Typed `ENCRYPTED`, `CORRUPT_ARCHIVE`, `FILE_TOO_LARGE` (20 MB). Each is a tested path. | "Handle bad and unsupported inputs **deliberately**" is an instruction to decide. Silently ingesting an empty document is the real failure. |
| **Streaming turns** | Unspecified. | Synchronous; returns the completed, persisted turn. SSE design noted in the README. | Streaming would obscure the retry/repair loop, which is the part worth showing. A half-streamed response that later fails validation is worse than a clean error. |
| **Quiz generation** | Named only as an example of bad LLM output. | Built, as one of the two required skills. | Sharpest available demonstration of the validation-and-repair pipeline the brief asks for. |
| **Language / grade band** | Bangladeshi company, US-curriculum examples (`AP Biology`, `Grade 7`). | English only. `education_level` is free text on the **student**, not the room, and is passed to every prompt as an explicit variable. | The brief's own examples set the range. A student has one level; a room does not — a university student and a Class 8 student can both open a room called "Algebra". A Bangla variant is a new prompt file, not a refactor. |
| **Database** | "Use a relational database," otherwise my call. | PostgreSQL 16 — `jsonb`, generated `tsvector`, partial and expression indexes, keyset pagination. | SQLite would make "how the design behaves as history grows" unanswerable. |
| **Setup path** | Unspecified. | `docker compose up`, with a documented non-Docker fallback. | "Setup instructions" is the deliverable; the method is mine. |
| **Web UI** | Explicitly optional. | Not building one. Swagger is the interface. | The time goes to the reliability suite instead — and saying so is the better answer. |
| **Repo mechanics** | Order of operations. | Add `ikramhasan` and `azmainadel` as collaborators **first**, then archive. | Archived repos cannot be modified, so the order is forced. |

---

## Decisions taken during S3 (reliability)

| Topic | The ambiguity | Decision | Reasoning |
|---|---|---|---|
| **Tutor output format** | The brief says to assume malformed data, but a chat reply is naturally prose. | The tutor returns JSON: `{answer, in_scope, concepts}`. | Prose gives the reliability layer nothing to check and leaves the "stay in the room" rule unenforceable. The cost is honest — asking for JSON is what creates the malformation problem — but both extra fields have real readers, so the container is carrying weight rather than ceremony. |
| **Provider structured output** | Gemini offers `response_schema`, which would largely remove parse failures. | Not used. JSON is requested in the prompt text instead. | It is provider-specific, and our `LLMClient` is a Protocol precisely so the provider can change. More importantly it does nothing for the content layer, which is the half the brief actually cares about. In production I would use it *and* keep the content layer. |
| **Which failures code fixes, and which cost a model call** | Unspecified. | Code recovers code fences, surrounding chatter, and literal line breaks inside strings. It rejects truncation, prose, and JSON arrays. | The line is "does recovering require guessing at meaning". Stripping a fence changes no content. Closing a truncated object means inventing the end of a sentence a student will read. Unwrapping a one-element array is tempting, but with two elements there is no way to know which was meant. |
| **How much of the prompt to enforce in code** | "Build enough reliability around it" — how much of a prompt's own guidance is worth a check? | Only what protects the student or the data. Ten content rules were written, then cut to three. Anything code can correct is corrected instead (`tidy`), and anything that is only taste was deleted. | A repair costs money, costs seconds of a student's wait, and might not work; a line of Python costs nothing and always works. Four of the ten were string operations outsourced to a language model, and four more defended nobody. |
| **The word limit** | The prompt says "at most 250 words **unless the student explicitly asks for more detail**", but the check never sees the student's message. | Rejects at 800, as a runaway guard rather than a style rule. | At 350 the check threw away well-judged 380-word answers from students who had asked for depth and paid to replace them with shorter, worse ones — actively harming the person it existed to protect. At 800 nothing a student legitimately asked for is caught. |
| **Repair budget** | "Build enough reliability around it" — how much is enough? | Two repairs, then the turn fails with no answer. Two network retries, counted separately. | Past two repairs the model is not misunderstanding the instruction, it is unable to follow it, and a third call mostly buys the student a longer wait before the same failure. |
| **Which errors are worth retrying** | Unspecified. | `ProviderUnavailable` and `EmptyResponse` are retried; `ResponseBlocked` is not. | Retrying something nondeterministic is sensible; retrying a decision is not. A safety refusal will be refused again, so a retry buys only a slower failure and two more billed calls. |
| **Storing concepts** | Not asked for by the brief at all. | A `jsonb` list on `turns` with a GIN index, not a `turn_concepts` table. | They are per-turn labels with no identity of their own — nothing renames, merges, or hangs data off them. The only questions asked of them are answered by the index without a join. A table becomes right the moment a concept needs an identity. |

---

## Decisions taken during S4 (tools)

| Topic | The ambiguity | Decision | Reasoning |
|---|---|---|---|
| **Which tools to build** | "At least 2 tools of your own" — from an unlimited field of options. | Three, chosen against a written four-part test, with every rejected option recorded next to the rule it failed. Test and list in [`PLAN.md` §8](./PLAN.md). | A list of tools is easy to write and hard to defend. The brief grades "what you chose to prioritize" and "what you deliberately left alone", so the rule used to choose is worth more than the choices. |
| **Code execution** | Genuinely the highest-value tool for a study app, and the brief also says "the model should not call arbitrary code". | Built as `evaluate_expression` — an AST allow-list covering expressions only. A general sandbox is in the README's "with more time". | It *is* code execution, scoped to the part that can be proved safe. A general sandbox needs container isolation, no network, memory and PID limits, a hard timeout and hang-proof cleanup. Miss the PID limit and a fork bomb wins. Shipping a small provably-safe thing beats a large probably-safe one, especially when two required slices are still unbuilt. |
| **The brief's example topics** | Rooms are "for a topic **such as** AP Biology or Grade 7 Algebra". | Treated as illustrations, not limits. A student can open a room about Python. | The brief says the scope is "deliberately open-ended so we can see your judgment". Using its examples to draw product boundaries would be reading them as instructions, which is the opposite of what it asked for. Nothing in the schema restricts a room's topic — `title` is free text. |

---

## Verify before submitting

`ikramhasan` and `azmainadel` are GitHub usernames read out of a PDF. Confirm both resolve to
real accounts on github.com before sending invitations — a typo means a reviewer never sees the
submission, and it is the cheapest possible thing to check.
