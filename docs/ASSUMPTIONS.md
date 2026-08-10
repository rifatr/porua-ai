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
| **Language / grade band** | Bangladeshi company, US-curriculum examples (`AP Biology`, `Grade 7`). | English only. Grades 6–12 plus AP. `grade_level` is a structured field on the room, never parsed from the title. | The brief's own examples set the range. Prompts take grade as an explicit variable, so a Bangla variant is a new prompt file, not a refactor. |
| **Database** | "Use a relational database," otherwise my call. | PostgreSQL 16 — `jsonb`, generated `tsvector`, partial and expression indexes, keyset pagination. | SQLite would make "how the design behaves as history grows" unanswerable. |
| **Setup path** | Unspecified. | `docker compose up`, with a documented non-Docker fallback. | "Setup instructions" is the deliverable; the method is mine. |
| **Web UI** | Explicitly optional. | Not building one. Swagger is the interface. | The time goes to the reliability suite instead — and saying so is the better answer. |
| **Repo mechanics** | Order of operations. | Add `ikramhasan` and `azmainadel` as collaborators **first**, then archive. | Archived repos cannot be modified, so the order is forced. |

---

## Verify before submitting

`ikramhasan` and `azmainadel` are GitHub usernames read out of a PDF. Confirm both resolve to
real accounts on github.com before sending invitations — a typo means a reviewer never sees the
submission, and it is the cheapest possible thing to check.
