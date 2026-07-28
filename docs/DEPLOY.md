# Deploying the Browser-Gym platform

This is the whole system, not one service. Read the **Components** and **Shared
secrets** sections first — most deploy failures are a missing secret or two
services that don't agree on one.

> Two repos are involved:
> - **annotator** (this repo): frontend + backend + Postgres — the human review platform.
> - **gym** (`E Commerce Broswer Gym`): the gym service + the live-browser service — the environment under test.

## Components (5 services)

| # | Service | Repo · path | Port | What it is |
|---|---|---|---|---|
| 1 | **frontend** | annotator · `frontend/` | 8080 | React SPA served by nginx; nginx proxies `/api` → backend |
| 2 | **backend** | annotator · `backend/` | 8090 | FastAPI + Alembic; talks to Postgres, the gym, and the live-browser |
| 3 | **postgres** | — | 5432 | the annotator's own DB (sessions, versions, verifiers, leases) |
| 4 | **gym** | gym · `Dockerfile` | 8000 | the ecommerce world; serves `/_harness/*`. seed.db baked in, `SEEDDB_MODE=1` |
| 5 | **live-browser** | gym · `live_browser/Dockerfile` | 8877 | CDP screencast + input for the live pane (a real Chromium) |

```
browser ──HTTP──> frontend(nginx) ──/api──> backend ──> postgres
   │                                           │
   │                                           ├──HTTP──> gym (/_harness/*)
   └──────────WS + REST (live pane)──────────> live-browser (:8877)
```

The browser talks to the **frontend** (everything `/api` is proxied to the backend
server-side) and **directly to the live-browser** for the live pane (a high-fps
video + input channel deliberately not relayed through the backend).

## Shared secrets — these MUST agree across services

| Secret | Set on | Must equal |
|---|---|---|
| `AUTH_SECRET` | backend | — (any strong random string; **the prod backend refuses to boot without it**) |
| `DB_PASS` | Postgres + backend `DATABASE_URL` | itself |
| `GYM_HARNESS_TOKEN` (backend) ↔ `HARNESS_TOKEN` (gym) | backend + gym | **each other** — gates every `/_harness/*` call |
| `LIVE_STREAM_SECRET` | backend + live-browser | **each other** — signs live-session tickets (both fall back to `HARNESS_TOKEN` if unset, so a matching `HARNESS_TOKEN` also works) |
| `ANTHROPIC_API_KEY` | backend | — (optional; the live agent re-run) |

Generate the random ones once: `openssl rand -hex 32`.

## Connection URLs — who points at whom

| Var | Set on | Value |
|---|---|---|
| `GYM_URL` | backend | the gym service URL (e.g. `https://gym-…run.app`) |
| `LIVE_BROWSER_URL` | backend | the live-browser service URL (used server-side, e.g. at finalize) |
| `VITE_LIVE_BASE` | frontend **build arg** | the live-browser's **public** URL — the browser connects here for the live pane |
| `LIVE_ALLOWED_ORIGINS` | live-browser | the frontend's public origin(s), comma-separated (`*` dev only) — CORS + websocket Origin check |

---

## A. Local (single box) — docker-compose + the gym + live-browser

The annotator's compose brings up frontend + backend + Postgres + adminer. The
gym and live-browser run alongside (they live in the other repo).

```bash
# 1) gym (from the gym repo) — seed.db is baked into the image, SEEDDB_MODE=1
cd "../E Commerce Broswer Gym"
docker build -t browser-gym:local .
docker run -d -p 8000:8000 -e HARNESS_TOKEN=dev-token browser-gym:local

# 2) live-browser (from the gym repo)
docker build -t live-browser:local -f live_browser/Dockerfile .
docker run -d -p 8877:8877 -e LIVE_STREAM_SECRET=dev-token -e LIVE_ALLOWED_ORIGINS='*' live-browser:local

# 3) annotator (this repo)
cd ../browser-gym-annotator
GYM_HARNESS_TOKEN=dev-token LIVE_STREAM_SECRET=dev-token \
  docker compose -f infra/docker-compose.yml up -d --build
# app at http://localhost:8080  (compose defaults already point GYM_URL /
# LIVE_BROWSER_URL at host.docker.internal:8000 / :8877)
```

Workspace isolation (a gym container per attempt) needs the Docker socket, which
compose already mounts — a single-host, dev-only privilege. It does **not** work
on Cloud Run (see below).

## B. Cloud (GCP) — Cloud Run × 4 + Cloud SQL

Order matters: stand up the gym + live-browser first, then deploy the annotator
pointing at them.

```bash
# --- 1) gym + live-browser (from the gym repo) ---
cd "../E Commerce Broswer Gym"
gcloud builds submit --tag REGION-docker.pkg.dev/PROJ/REPO/gym:latest .
gcloud run deploy gym --image .../gym:latest --region REGION --allow-unauthenticated \
  --memory 2Gi --cpu 2 --set-env-vars HARNESS_TOKEN=$TOKEN
# (the gym writes screenshots/trajectories to disk — Cloud Run's FS is read-only
#  apart from /tmp; it still serves reset/state/world, which is what SQL-seed uses.)

gcloud builds submit --tag .../live-browser:latest -f live_browser/Dockerfile .
gcloud run deploy live-browser --image .../live-browser:latest --region REGION \
  --allow-unauthenticated --memory 2Gi --cpu 2 \
  --set-env-vars "LIVE_STREAM_SECRET=$LIVE_SECRET,LIVE_ALLOWED_ORIGINS=$FRONTEND_URL"
# capture GYM_URL and LIVE_BROWSER_URL from `gcloud run services describe`.

# --- 2) annotator (this repo) ---
cd ../browser-gym-annotator
AUTH_SECRET="$(openssl rand -hex 32)" \
DB_PASS="$(openssl rand -hex 24)" \
GYM_URL="$GYM_URL" LIVE_BROWSER_URL="$LIVE_BROWSER_URL" \
  ./infra/deploy-gcp.sh
```

`deploy-gcp.sh` deploys **backend + frontend + Cloud SQL** and fails fast if
`AUTH_SECRET` is missing. It sets `CORS_ORIGINS=[]` (the browser only calls `/api`
same-origin through nginx, so CORS is never exercised — no wildcard exposure).

**To enable the live pane in the hosted deploy**, the frontend must be BUILT with
the live-browser's public URL so the browser knows where to connect:

```bash
docker build -t .../frontend:latest --build-arg VITE_LIVE_BASE="$LIVE_BROWSER_URL" frontend/
# then redeploy annotator-frontend with that image
```

and the live-browser's `LIVE_ALLOWED_ORIGINS` must include the frontend origin.
Without this, the core platform (review, versioning, verifiers, gym tasks) works
fine — only the live pane is disabled.

## Known limitations / gotchas

- **Workspace isolation is single-host only.** It spins one gym container per
  attempt via the Docker socket — impossible on Cloud Run. In a cloud deploy set
  `WORKSPACE_ISOLATION=0`; the annotator then shares the one `GYM_URL`. (Isolation
  is for concurrent live annotators; a Kubernetes provider is the future fix.)
- **Split-origin auth.** The session cookie is `SameSite=Lax`, host-only, and the
  design assumes the frontend proxies `/api` same-origin (it does). Do **not** put
  the frontend and backend on different browser-facing domains without switching
  the cookie to `SameSite=None; Secure` and adding `allow_credentials`.
- **The live pane needs two things that match the backend:** `LIVE_STREAM_SECRET`
  (ticket signing) and the frontend build's `VITE_LIVE_BASE` (where to connect).
- **Branches.** The seed-db work is on the gym's `feat/sql-seed-db`; the annotator
  is on `fix/dataset-integrity`. Deploy from these until they merge to `main`.
