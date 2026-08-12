# Hand-written model responses

Every file here is a response the model *could* send and we need to be correct
for. They are written by hand because they cannot be recorded: you cannot ask
Gemini to return truncated JSON, or a quiz with duplicate options, on demand.
That is exactly why the reliability layer would otherwise be untestable.

Recorded responses live one directory up, in `recorded/`. Those capture what the
model really does. These capture what it might do. Both are needed — recorded
fixtures alone only ever test the model on its good days.

| File | Layer | Expected |
|---|---|---|
| `json_in_fences.txt` | parse | **recovered** — fences stripped, no repair call |
| `preamble_and_trailer.txt` | parse | **recovered** — the object is found inside the chatter |
| `literal_newlines.txt` | parse | **recovered** — real line breaks inside a JSON string |
| `truncated.txt` | parse | rejected `JSON_TRUNCATED` |
| `prose_not_json.txt` | parse | rejected `NO_JSON_FOUND` |
| `json_array.txt` | parse | rejected `JSON_NOT_AN_OBJECT` |
| `missing_concepts.json` | schema | rejected `MISSING` |
| `in_scope_as_string.json` | schema | rejected `BOOL_PARSING` |
| `duplicate_concepts.json` | tidy | **corrected** — deduped in code, no repair call |
| `decline_but_answers_anyway.json` | tidy + content | tags cleared in code; rejected `DECLINE_TOO_LONG` |

Half these rows say **recovered** or **corrected**, and that is the point of the
directory. A response arriving in a code fence, or with the same tag written twice,
is not worth a billable repair call — code can undo both exactly, and code always
works where a repair merely might. What is left, and only what is left, costs a
call: truncated JSON would mean inventing the end of a sentence, and a model that
declines to answer and then answers anyway cannot be shortened without losing
meaning.

That line — recover in code, reject only what code cannot correct — is drawn in
`app/reliability/parsing.py` and `app/reliability/checks.py`, and these files are
what hold it in place.

Four content rules were written and then deleted, so their fixtures went with
them: a ban on "Great question!" openings, on `#` headings, on more than four
tags, and on tags longer than a phrase. None defended the student or the data, and
the first had a false positive on "Absolutely convergent series…" — a correct
sentence in a maths room, rejected and paid to be replaced with a worse one.
