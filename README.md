# Browser-Use Gym — Annotator Platform

The internal annotation platform for Deccan AI's browser-use gym. An annotator is
assigned a **breaker task**, **does it by hand** in a live gym of five realistic
web apps, and every interaction they take is recorded as a trajectory. They then
build a verifier suite, ship the result, and a reviewer adjudicates it.

The product is the **golden sample**: a task, the seeded world it starts from, a
step-by-step trajectory that reaches a passing end state, the verifier suite that
scores it, and the reward. That bundle is what a customer receives.

It sits on top of the `ecommerce-browser-gym` repo (task schema, deterministic
seeding, the five mock apps, end-state verifiers) and is skinned with the Deccan
Vault design system.

> **This used to be an agent-REVIEW platform** — the annotator watched a recorded
> agent run and corrected a step. It is now **human-do**: the annotator performs
> the task themselves and their actions are the trajectory. If you find docs
> describing the old flow, they are stale.

---

## Quick start

Requires **Docker Desktop**.

```bash
docker compose -f infra/docker-compose.yml up --build
```

| Service | URL | What it is |
|---|---|---|
| **frontend** | http://localhost:8080 | the annotator app — open this |
| **backend** | http://localhost:8090 | FastAPI platform API |
| **postgres** | localhost:**5433** | the database (5433 to avoid a native :5432) |
| **adminer** | http://localhost:8081 | web DB browser |

On first boot the backend creates the schema and seeds the task catalog. You land
on **My tasks**, your assigned queue.

Doing a task also needs the **bridged gym stack** running on the host — see below.
Without it the board still loads and every task refuses to open, with a reason.

---

## The bridged gym stack

The annotator drives real mock apps, not the gym's own pages. Four things run on
the host (the annotator backend is in Docker and reaches them via
`host.docker.internal`):

| Piece | Port | What it does |
|---|---|---|
| the five mock apps | 5201–5205 | ShopGym · ValueMart · ShopMail · GymCal · GymEats |
| gym instances | 8077, 8078… | one world each — **one annotator needs one gym** |
| the bridge | 8093 | routes a click in a mock app into the real gym engine |
| the live browser | 8877 | the Chromium the annotator watches and drives |

From the `ecommerce-browser-gym` repo:

```bash
./tools/run_bridged_stack.sh
```

**Pool sizing is the thing that bites.** `GYM_URLS` is the pool, and a session
holds its gym until it is closed. Closing the browser tab never calls close, so an
abandoned session used to hold its gym for the full 90-minute TTL — two closed tabs
and a two-gym pool is dead for everyone. An idle session now yields its gym to a
newcomer after a grace period (`BRIDGE_GRACE_MIN`, default 12), and an *active*
session is never evicted. Still: **run at least one gym per concurrent annotator,
plus one** — shipping a sample replays the trajectory in a scratch world of its own.

```bash
curl -s localhost:8093/bridge/sessions   # who holds what, and how idle they are
```

---

## The annotator workflow

1. **My tasks** — your assigned queue, each row tagged with where *you* left off
   (to do / in progress / returned / in review / submitted). Assignments are real
   rows; two annotators on one task is deliberate, because that is what the QA
   agreement number measures.
2. **Do the task** — the live pane opens on the task's primary app with the tab
   strip for the rest. Work through it. Every click, fill and app switch is
   recorded; scrolls and things the page did on its own are not.
3. **Check** — run the verifier suite against the world you actually produced.
4. **Ship** — `prepare-ship` lists every unmet gate in plain language rather than
   refusing one at a time. Finalize replays the trajectory from a clean reset in a
   scratch world and refuses if it does not reproduce.
5. **QA** — a reviewer accepts it, or returns it with a note; a returned task
   comes back to the top of your board as a new attempt.

Everything persists per action; a refresh restores where you left off.

---

## What a shipped sample contains

`GET /api/export/samples/{id}`, or the whole dataset as JSONL at
`GET /api/export/dataset.jsonl`. Per step:

| | |
|---|---|
| **action** | kind, semantic locator, arguments, the actor (human or agent) |
| **observation** | url, title, viewport, scroll, visible text, and every interactive element with its bbox — content-addressed, referenced by path + sha256 |
| **screenshot** | the frame the annotator saw, same treatment |
| **state change** | the semantic world delta this step produced, diffed by entity id |
| **provenance** | which version authored it, and whether the replay verified it |

Per sample: the task, the seed it was **recorded** under, the initial world, the
verifier suite with per-check results, the reward, and the lineage of corrections.

Artifact bytes live under `ARTIFACT_ROOT` (a named Docker volume). **A submitted
sample references its screenshots forever — that volume is not disposable.**

---

## The database

One Postgres database, `browser_gym_annotator` (`annotator`/`annotator` in dev).

Roughly: `task` and `annotator` are the catalog; `task_assignment` joins them;
`review_session` is one annotator's attempt at one task; an attempt owns a
`trajectory_version` graph over `trajectory_step`s, the raw `interaction_event`
log those were folded from, `environment_checkpoint`s, a `verifier_suite`, a
`benchmark_run`, and finally a `submission` carrying a frozen snapshot. Full shape
in `backend/app/models.py`; every parent/child edge is FK-backed with a deliberate
`ondelete`.

```bash
PGPASSWORD=annotator psql -h localhost -p 5433 -U annotator -d browser_gym_annotator
```

Dev bootstraps with `create_all`. **`create_all` cannot ALTER an existing table**,
so after adding a column run the migration explicitly:

```bash
docker compose -f infra/docker-compose.yml exec -T backend alembic upgrade head
```

Production uses Alembic only (`AUTO_CREATE_ALL=false`, `RUN_MIGRATIONS=1`);
migrations live in `backend/migrations/versions/`. See `docs/DEPLOY.md`.

Reset to a clean slate (dev-only, keeps the task catalog):

```bash
curl -X POST http://localhost:8090/api/admin/reset-sessions
```

---

## Local dev

```bash
cd frontend && npm install
npm run dev          # http://localhost:5180
npx tsc --noEmit && npx vitest run
```

```bash
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --port 8090 --reload
.venv/bin/python -m pytest tests/ -q
```

---

## Layout

```
frontend/
  src/ds/                design-system primitives + tokens
  src/features/
    live-gym/            the live pane, the app tab strip, the event recorder
    task-review/         the task screen: brief, trajectory, verifiers, ship
    qa/                  the reviewer's adjudication screen
  src/lib/               API client, types, the live-browser wire
backend/app/
  api/                   the HTTP surface (routers)
  recorder.py            raw events -> candidate actions
  materialize.py         actions -> trajectory steps + world deltas
  worlddiff.py           the semantic world diff
  checkpoints.py         capture / restore / the divergence guard
  blobstore.py           where artifact bytes actually live
  versions.py            the trajectory version graph
  finalize.py            the ship gates + the frozen snapshot
  replay_surface.py      which world a replay runs in (never the annotator's)
  cua_hub.py             sid minting for the five mock apps
infra/                   docker-compose
```

## Environment variables (backend)

| Var | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | local Postgres | Postgres connection (psycopg3) |
| `ENV` | `dev` | `dev` enables auto-create and the admin reset |
| `AUTO_CREATE_ALL` | `true` | prod sets `false` + `RUN_MIGRATIONS=1` |
| `ARTIFACT_ROOT` | `/var/lib/browser-gym-annotator/artifacts` | artifact bytes — **must be durable** |
| `GYM_URL` | `http://host.docker.internal:8000` | the gym harness |
| `LIVE_BROWSER_URL` | `http://host.docker.internal:8877` | the live browser (on the host) |
| `GYM_HOST_FOR_BROWSER` | `localhost` | how the *browser* reaches the gym |
| `GYM_HARNESS_TOKEN` | — | gym auth token |
| `ANTHROPIC_API_KEY` | — | optional: the semantic/safety judge |
| `CORS_ORIGINS` | `["http://localhost:8080"]` | allowed origins |

## Prerequisites

Docker Desktop · Node 20+ · Python 3.12 · a running `ecommerce-browser-gym`
bridged stack.
