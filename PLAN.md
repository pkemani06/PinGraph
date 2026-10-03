# PinGraph build plan

One phase per Claude Code session. Each phase ends with passing tests, ticked boxes, and a commit.

## Phase 0: Scaffold (Sat, ~1 hr)

- [x] Repo layout from CLAUDE.md, requirements.txt, .gitignore, .env.example
- [x] docker-compose.yml with postgres and redis only
- [x] FastAPI app with `GET /health` returning 200

Done when: `docker compose up postgres redis` works and `/health` responds locally.

## Phase 1: Data layer (Sat/Sun)

- [ ] `db/schema.sql`: users, boards (user_id FK), pins (title, topic), board_pins (board_id, pin_id, created_at, PK on both, index on pin_id)
- [ ] `scripts/seed.py`: synthetic data with topic clusters. ~20 topics, ~10k pins each assigned one topic, ~5k boards where ~80% of a board's pins come from one topic, ~200k edges total
- [ ] `app/db.py`: asyncpg pool, `fetch_all_edges()`, `insert_edge()`, `recent_pins_for_user()`

Why topic clusters: they give ground truth. A good recommender should mostly return pins from the query pin's topic, so we can measure quality instead of eyeballing it.

Done when: seed runs in under a minute and row counts match targets.

## Phase 2: Graph + recommender (Sun/Mon) — I write this part

- [ ] `app/graph.py`: `BipartiteGraph` with `pin_to_boards` and `board_to_pins` (dict of lists), `add_edge()`, `load(edges)`
- [ ] `app/recommender.py`: `related_pins(graph, pin_id, k, n_steps, restart_prob)`. Walk pin -> random board -> random pin, count visits, restart to the query pin with probability `restart_prob`, exclude the query pin, return top k with scores
- [ ] `user_feed(graph, recent_pin_ids, k)`: run related_pins per recent pin, sum scores, exclude already-saved pins
- [ ] Tests on a tiny hand-built graph (shared-board pin ranks first, disconnected pin never appears, pin with no boards returns empty)
- [ ] `scripts/eval.py`: precision@10 by topic over 500 random pins. Record the number.
- [ ] Time a single walk. Tune `n_steps` so one call stays in the low milliseconds.

Done when: tests pass and precision@10 is clearly above the random baseline (~5% with 20 topics).

## Phase 3: API (Mon)

- [ ] Lifespan: create DB pool, load graph into memory at startup
- [ ] `GET /pins/{pin_id}/related?k=10`
- [ ] `GET /users/{user_id}/feed?k=20`
- [ ] `POST /boards/{board_id}/pins` body `{pin_id}`: insert edge in Postgres, update graph
- [ ] 404s for unknown ids, pydantic response models
- [ ] `X-Replica-Id` response header (from env var) for later load balancing checks
- [ ] API tests with httpx

Note: the walk is CPU-bound and blocks the event loop. Keep it short or run it in a threadpool. Know why.

Done when: all endpoints work end to end against seeded data.

## Phase 4: Redis cache (Tue)

- [ ] `app/cache.py`: cache-aside for related pins, key `rec:pin:{id}:{k}`, TTL 300s
- [ ] `X-Cache: hit|miss` response header
- [ ] Hit/miss counters exposed at `GET /metrics`
- [ ] Graceful fallback if Redis is down (compute, skip cache)

Done when: second request for the same pin returns `X-Cache: hit` and is measurably faster.

## Phase 5: Pub/sub sync (Tue/Wed)

Problem: each replica holds its own in-memory graph. A save handled by replica 1 leaves replicas 2 and 3 stale.

- [ ] `app/pubsub.py`: on save, write Postgres then publish `{board_id, pin_id}` to channel `edges`
- [ ] Each replica runs a subscriber task (started in lifespan) that applies `graph.add_edge()` and deletes cache keys for the affected pin
- [ ] The publishing replica also updates via the subscription, so there is one code path
- [ ] Test: two app instances, save on one, the other's graph reflects it

Tradeoffs to be able to explain: Redis pub/sub is at-most-once (a replica that is down misses messages, fixed by reloading from Postgres on startup); cached results for neighboring pins go stale until TTL expires.

Done when: the two-instance test passes.

## Phase 6: Containers + nginx (Wed)

- [ ] Dockerfile for the API
- [ ] docker-compose.yml: postgres, redis, api1, api2, api3, nginx
- [ ] `nginx/nginx.conf`: upstream with the 3 replicas, proxy on port 8080
- [ ] Verify: 30 requests through nginx show all three `X-Replica-Id` values
- [ ] Verify: save through nginx, then read from each replica shows the new edge

Done when: `docker compose up --build` brings up the full stack from a clean clone.

## Phase 7: Load testing (Thu)

- [ ] `loadtest/locustfile.py`: ~90% related-pin reads with skewed pin popularity (a few hot pins, long tail), ~10% saves
- [ ] Run A: 1 replica, cache off
- [ ] Run B: 1 replica, cache on
- [ ] Run C: 3 replicas, cache on
- [ ] Record RPS, p50, p95, error rate, cache hit rate for each in `docs/results.md`, plus machine specs and user count
- [ ] Find and fix the worst bottleneck, rerun, note what changed

Done when: results table is filled in with real numbers.

## Phase 8: Ship (Fri)

- [ ] README: what it is, architecture diagram, how to run, results table, design tradeoffs, what I'd do next (GraphSAGE embeddings, durable stream instead of pub/sub, graph partitioning)
- [ ] Push to GitHub
- [ ] Update resume bullets to match what was built and the measured numbers
- [ ] Rehearse: 60 second pitch, the random walk from memory, three tradeoffs

## Cut order if time runs short

1. User feed endpoint (keep related pins)
2. `/metrics` endpoint
3. Run A in load tests

Do not cut: recommender, cache, pub/sub, 3 replicas behind nginx, Locust results. Those are the resume claims.