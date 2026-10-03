# PinGraph

Graph-based pin recommendation backend, inspired by Pinterest's Pixie/PinSage random walks.

## What it does

Pins are saved to boards. That forms a bipartite graph (pins on one side, boards on the other, an edge per save). To recommend pins related to pin X, run random walks with restart from X over the graph (pin -> board -> pin -> ...) and rank pins by visit count. The user feed merges related-pin results from a user's recent saves.

## Stack

- Python 3.12, FastAPI, uvicorn
- PostgreSQL 16 via asyncpg (no ORM, raw SQL)
- Redis 7 (response cache + pub/sub)
- nginx as load balancer in front of 3 API replicas
- Docker Compose for everything
- pytest for tests, Locust for load tests

## Repo layout

```
app/
  main.py          FastAPI app, lifespan (load graph, start subscriber)
  config.py        env-based settings
  db.py            asyncpg pool + queries
  graph.py         in-memory bipartite graph (pin_to_boards, board_to_pins)
  recommender.py   random walk with restart, top-k
  cache.py         Redis cache-aside helpers
  pubsub.py        publish/subscribe for new edges
  routes/          pins.py, users.py, health.py
db/schema.sql
scripts/seed.py    synthetic data with topic clusters
scripts/eval.py    precision@k against seed topics
tests/
loadtest/locustfile.py
nginx/nginx.conf
docker-compose.yml
Dockerfile
docs/results.md    measured load test numbers
PLAN.md
```

## Commands

- `docker compose up --build` : run full stack
- `docker compose up postgres redis` : deps only, for local dev
- `uvicorn app.main:app --reload` : run API locally
- `python scripts/seed.py` : seed the database
- `pytest -q` : run tests
- `locust -f loadtest/locustfile.py --host http://localhost:8080` : load test through nginx

## How to work in this repo

- Work one phase of PLAN.md at a time. Do not start the next phase until I say so.
- Before writing code for a phase, show me a short plan and wait for approval.
- After finishing a phase: run the tests, check the phase's acceptance criteria, tick the boxes in PLAN.md, and give me a short explanation of what you built and why, including one tradeoff you considered. I need to be able to explain every part of this in an interview.
- Keep it simple. No abstractions, frameworks, or dependencies that PLAN.md does not call for.
- `app/recommender.py` and `app/graph.py`: I write these myself. Review my code and suggest fixes, but do not rewrite them unless I ask.
- Type hints everywhere. Small functions. No dead code.
- Never invent performance numbers. Anything in README.md or docs/results.md must come from a run we actually did.
- Small commits with plain messages, one logical change each. Never alter commit dates.