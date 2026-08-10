---
name: run-porua-api
description: Start the Porua API locally with Postgres, apply migrations, seed demo data, and exercise an endpoint end-to-end. Use when asked to run, start, restart, or demo the app, to check a change works in the real API, or to open Swagger.
---

# Running the Porua API

> Until the stack exists, this file is the intended procedure — M0/M1 in `docs/PLAN.md`.
> Verify the commands against `compose.yaml` and `Makefile` before trusting them.

## Start

```bash
cp -n .env.example .env          # then set GEMINI_API_KEY if making real calls
docker compose up -d --build
docker compose exec api alembic upgrade head
docker compose exec api python -m app.seed     # demo student + rooms
```

Swagger: <http://localhost:8000/docs> · Health: `curl -s localhost:8000/healthz`

The app runs **without** a Gemini key for everything except live turns — migrations, uploads,
ingestion, history queries, and the whole test suite work offline.

## Verify it actually works

```bash
# create a room
curl -s -X POST localhost:8000/rooms \
  -H 'Content-Type: application/json' -H 'X-Student-Id: 00000000-0000-0000-0000-000000000001' \
  -d '{"title":"Grade 7 Algebra: Solving Equations","subject":"math","grade_level":7}' | jq

# upload material
curl -s -X POST localhost:8000/rooms/$ROOM/documents \
  -H 'X-Student-Id: ...' -F 'file=@tests/fixtures/docs/sample.pptx' | jq

# start a turn (needs a key)
curl -s -X POST localhost:8000/rooms/$ROOM/turns \
  -H 'Content-Type: application/json' -H 'X-Student-Id: ...' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"message":"Explain how to solve 3x + 6 = 15"}' | jq

# ← the endpoint worth demoing: full attempt/tool/failure/token detail
curl -s localhost:8000/turns/$TURN | jq
```

**The persistence check the brief asks for** ("data must persist between runs") — restart without
wiping volumes and confirm the room is still there:

```bash
docker compose restart api && curl -s localhost:8000/rooms -H 'X-Student-Id: ...' | jq
```

Note the difference: `docker compose down` preserves the volume, `down -v` destroys it. Only use
`-v` when you intend to test migrations from empty.

## Tests

```bash
docker compose exec api pytest -q                    # full suite, no API key needed
docker compose exec api pytest tests/reliability -q  # the failure-mode fixtures
```

## When something is wrong

- **API won't start** — `docker compose logs api --tail=50`. Usually a migration that did not
  run, or `DATABASE_URL` pointing at `localhost` instead of the `db` service host.
- **DB connection refused** — Postgres is slower to accept connections than to report healthy;
  check `docker compose ps` and the healthcheck before assuming a config problem.
- **Migration conflict** — see the `alembic-migration` skill.
- **Turn fails with a typed reason** — that is the design working. Read
  `GET /turns/{id}` for the attempt chain and validation failures before touching code; the
  answer is almost always already recorded there.
- **Port 8000 in use** — `lsof -i :8000`.

Do not debug by adding print statements to the LLM path. Every attempt is already persisted with
its prompt, response, and failures — read the inspection endpoint instead.
