# Jenkins CI/CD — Browser-Gym **annotation platform**

For **https://browser-agent.delta.soulhq.ai** (the Deccan tasking UI in your screenshot).

This is **not** the same as deploying `cua-hub-*.delta.deccanexperts.ai` mock SPAs — see the gym repo `ci/jenkins/` for those.

## What gets deployed

| Service | Image | Public URL (delta) |
|---|---|---|
| **frontend** (nginx + SPA) | `frontend/Dockerfile` | `browser-agent.delta.soulhq.ai` |
| **backend** (FastAPI) | `backend/Dockerfile` | proxied as `/api` through frontend |
| **postgres** | managed DB | not public |

**Also required (separate deploys / already hosted):**

| Dependency | Purpose |
|---|---|
| **live-browser** | `VITE_LIVE_BASE` — e.g. `https://live-browser.delta.deccanexperts.ai` |
| **gym + bridge** (or CUA hub mode) | `GYM_URL` / `CUA_BRIDGE_URL` + `CUA_HUB_*` for realistic UIs |
| **cua-gym Postgres** | seed data for mock `?sid=` loads |

## Jenkins jobs to create

| Job | Agent label | Script |
|---|---|---|
| `browser-agent-annotator-delta` | `frontend-agent` + `backend-agent` (or one multi-capable agent) | `ci/jenkins/browser-agent-annotator-delta.groovy` |

Grant **dhiren** Read + Build on this job (your dashboard was empty — admin must create it).

## Backend env (delta) — minimum for new realistic UIs

Set on the **backend** Cloud Run / k8s deployment:

```bash
CUA_HUB_MODE=1
CUA_BRIDGE_URL=https://…          # bridged stack URL (8093 equivalent)
CUA_BRIDGE_GYM_TOKEN=…
CUA_HUB_API_ROOT=https://cua-gym-hub.delta.soulhq.ai
CUA_HUB_DOMAIN=delta.deccanexperts.ai
# optional per-app overrides if not using default host pattern
LIVE_BROWSER_URL=https://live-browser.delta.deccanexperts.ai
LIVE_STREAM_SECRET=…              # must match live-browser service
GYM_HARNESS_TOKEN=…               # must match gym/bridge
AUTH_SECRET=…
DATABASE_URL=…
```

## Frontend build arg

```bash
docker build frontend \
  --build-arg VITE_LIVE_BASE=https://live-browser.delta.deccanexperts.ai
```

Without `VITE_LIVE_BASE`, the live pane points at localhost and shows blank / offline.

## Why delta still looks “old”

Your screenshot (`shop.gym.local`, Replay step review, “Not saved (offline)”) is the **previous agent-review UI**, not the current `fix/dataset-integrity` branch (human-do, live ActionLog, five-app tab strip, `cua-hub-*` URLs). **Redeploying** `fix/dataset-integrity` + the env vars above is what updates delta.

## Alternative: GCP one-shot

```bash
AUTH_SECRET=… DB_PASS=… GYM_URL=… LIVE_BROWSER_URL=… ./infra/deploy-gcp.sh
```

See `docs/DEPLOY.md`. Map custom domain `browser-agent.delta.soulhq.ai` to the Cloud Run frontend URL after deploy.

## Credentials (match other Deccan Jenkins jobs)

- `jenkins_user_bitbucket` or GitHub credential for `deccanai-org/browser-gym-annotation`
- `AUTH_SECRET`, `DB_PASS`, harness/live secrets in Jenkins credential store
