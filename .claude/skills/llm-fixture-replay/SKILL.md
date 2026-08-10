---
name: llm-fixture-replay
description: How to record, replay, and hand-author Gemini response fixtures so the whole test suite runs deterministically with no API key and no spend. Use when writing tests that touch the LLM, when adding a failure mode to the reliability suite, or when a test is flaky or slow because it calls the real API.
---

# LLM fixture replay

Every test in this repo runs **without a Gemini API key**. Tests that hit the live API are
non-deterministic, slow, cost money, and — worst of all — cannot reproduce the failure modes the
assessment is actually about. You cannot ask the real model to reliably return a quiz with
duplicate choices.

## How it works

The Gemini client sits behind one interface. Tests inject a fake that resolves a request to a
stored response, keyed by `(prompt_name, prompt_version, sha256(rendered_prompt))`.

Three modes:

| Mode | Behaviour |
|---|---|
| `replay` (default, CI) | Fixture must exist. Missing fixture = test failure, never a live call. |
| `record` | Calls the real API, writes the fixture, requires a key. Run deliberately. |
| `synthetic` | Returns a hand-authored fixture by explicit name. Used for all failure-mode tests. |

## Recording a fixture

```bash
GEMINI_API_KEY=... LLM_FIXTURE_MODE=record pytest tests/path::test_name
```

Then **read the recorded file before committing it.** Scrub anything that would drift the SHA or
leak: keys, real names, absolute paths, timestamps. Keep it small — truncate long
`raw_response` bodies where the test does not depend on the tail.

## Hand-authoring failure fixtures — the important part

Most of the reliability suite uses fixtures nobody recorded, because the interesting responses
are ones the real model produces rarely and unpredictably. Write them by hand under
`tests/fixtures/llm/synthetic/`, one file per failure mode, named for what it breaks:

```
parse/fenced_json.json            JSON wrapped in ``` fences
parse/prose_preamble.json         "Sure! Here's your quiz:" before the JSON
parse/truncated_object.json       cut off mid-object (finish_reason: MAX_TOKENS)
parse/trailing_comma.json
schema/missing_field.json
schema/wrong_type.json            item_count as a string
quiz/duplicate_choices.json       differing only by case and trailing whitespace
quiz/wrong_item_count.json        asked for 5, returned 7
quiz/no_correct_answer.json
quiz/two_correct_answers.json
quiz/all_answers_position_b.json  ← asserts the balancer, not the prompt
quiz/distractor_is_substring.json
tools/invalid_arguments.json
tools/repeated_identical_call.json  must hit the dedupe path
tools/unknown_tool_name.json        model invents a tool
transport/rate_limited_429.json
transport/provider_500.json
```

Each fixture gets a one-line comment saying which requirement it defends. A fixture whose purpose
nobody remembers gets deleted in six months.

## Writing the test

Assert on the **outcome**, not the internals:

- What did the client receive? (Never a half-validated artifact.)
- What was persisted? (Every attempt — *including the failures* — with its
  `validation_failures`.)
- Did the repair loop stop at its bound? Assert the exact call count, not just "it terminated".
- On terminal failure, is the reason typed and the status correct?

The single most valuable assertion in this suite: **for every malformed fixture, no invalid
artifact is ever stored or returned.** That is the claim the whole project rests on.

## Rules

- A new failure-handling code path requires a fixture in the same commit.
- Never add a network call to make a test pass.
- If a fixture stops matching after a prompt version bump, do not edit the fixture to fit —
  re-record it under the new version key. The old fixture stays with the old version.
