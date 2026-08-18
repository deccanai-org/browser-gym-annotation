# Browser Gym — Low-Level Design

| | |
|---|---|
| **Status** | Draft v0.3.1 |
| **Date** | 2026-07-29 |
| **Repos** | `deccanai-org/browser-gym-annotation` · `deccanai-org/browser-gym` |
| **Pilot** | ~20 human golden trajectories (prompt + seed + verifier + package) |

Shared map for the team: what we build, how components talk, how the DB is set up, and the contracts each side must honor. Workstreams only — no personal ownership lists.

---

## 1. What we are building

Browser-agent training / eval data over realistic multi-app worlds (shop, mail, marketplace, calendar, food).

| Surface | Who | What |
|---|---|---|
| **Annotation (studio)** | Humans | Open seeded mocks, complete a task, run verifier, package a golden |
| **Gym + realistic UIs** | Agents / eval | Same worlds as Amazon / Gmail / eBay / Calendar / Uber Eats; bridge into the gym engine; score with gym verifiers |

Phase-1 client pilot = **human goldens on static JSON**. No agent-correction loop, no dynamic world generation.

**Hard rule:** seed JSON is frozen. Attempt start **clones** `seed_sid` → `attempt_sid`, mutates the clone, then discards or promotes. Never mutate the seed. Never generate rows at runtime.

---

## 2. Who owns what

| Component | Owns | Does not own |
|---|---|---|
| **Studio** | Login, assignment, form-builder shell, iframe tokens | Mock worlds, verifier scoring |
| **Annotation UI + `/api/bg`** | Task chrome, session clone, verify/submit/package | Signup, QC admin console |
| **Hosted mocks** | Render JSON for a SID; write every click to `mock_state_*` | Task metadata, scoring |
| **`cua-gym` Postgres** | Worlds (`mock_*`) + annotation metadata (`bg_*`) | — |
| **Pipeline** | Prompt + seed JSON + verifier draft | Running the annotation UI |
| **Gym bridge** (parallel) | UI click → gym engine → re-project → harness verify | Studio golden packaging |

---

## 3. How components interact

### 3.1 One annotation attempt (sequence)

```mermaid
sequenceDiagram
  participant A as Annotator
  participant S as Studio
  participant U as Annotation UI
  participant API as BG API /api/bg
  participant DB as cua-gym
  participant M as Mock apps

  A->>S: Login + start task
  S->>U: iframe URL (access_token, refresh_token, task_id)
  U->>API: POST /sessions {task_id}
  API->>DB: Read bg_task + bg_task_app.seed_sid
  API->>DB: Clone mock_states seed_sid → attempt_sid (per app)
  API->>DB: Insert bg_session (attempt SIDs, expires_at)
  API-->>U: session_id + apps[].url (?sid=attempt_sid)
  U->>M: Open each mock URL
  loop Human works
    A->>M: click / type
    M->>DB: INSERT mock_state_events; UPSERT mock_states
  end
  A->>U: Submit / Run verifier
  U->>API: POST /sessions/{id}/submit
  API->>DB: Load final states + events; eval bg_verifier.spec
  API->>DB: Write bg_verifier_run; set bg_session.score
  API-->>U: score 0|1 + assert details
```

### 3.2 Runtime stack

```text
Studio (login, assignment, iframe)
    │  access_token, refresh_token, task_id
    ▼
Annotation UI  (browser-agent.delta.soulhq.ai)
    │  /api/bg/sessions → clone → attempt URLs
    ▼
Hosted mocks  Amazon · Gmail · eBay · Uber Eats · Calendar
    │  every click mutates JSON under attempt_sid
    ▼
Postgres cua-gym
    mock_states / mock_state_events / mock_files
    bg_*  (task, session, verifier, golden)
    │
    ▼
verify → 0|1 → QC → package tar
```

**Parallel (gym repo, not the studio pilot path):** mock UI → `bridgeAct` → gym HTTP → `WorldState` → re-project → `/_harness/verify`. Driven by `tools/run_newui_eval.sh`. Annotation pilot still reads/writes `cua-gym`, not gym SQLite.

### 3.3 Trust boundaries

- Studio → UI: URL tokens. UI does not implement a second login.
- UI → BG API: `Authorization: Bearer <access_token>`.
- Mocks → DB: existing CUA write path (we do not redesign it).
- BG API → DB: only service that clones seeds / writes `bg_*` / runs verify.
- VPN required for platform + DB.

---

## 4. Hosts and apps

| System | Location |
|---|---|
| Annotation UI | `https://browser-agent.delta.soulhq.ai/` |
| Studio | Existing form-builder shell (embeds the UI) |
| Amazon | `https://xmazon.delta.deccanexperts.ai` |
| Gmail | `https://xmail.delta.deccanexperts.ai` |
| eBay | `https://xbay.delta.deccanexperts.ai` |
| Uber Eats | `https://xber-eats.delta.deccanexperts.ai` |
| Calendar | `https://xoogle-calendar.delta.deccanexperts.ai` |
| DB | `souldb` → `cua-gym` @ `10.0.141.72:5432` |

App keys **must** match `mock_states.mock`:

| key | App |
|---|---|
| `amazon_mock` | Amazon |
| `gmail_mock` | Gmail |
| `ebay_mock` | eBay |
| `ubereats_mock` | Uber Eats |
| `google_calendar_mock` | Calendar |

SID URL param name (`sid` vs other) — confirm once; isolate in `build_mock_url(app, path, sid)`.

---

## 5. Database setup

**Host:** same Postgres as the mocks (`cua-gym`). New tables use prefix `bg_` so they sit beside `mock_*` without colliding. Default = co-locate (no second DB) so SIDs join without cross-DB pain.

### 5.1 Already there — reuse, do not redesign

| Table | Columns (observed / expected) | Role |
|---|---|---|
| `mock_states` | `mock`, `sid`, `state` (jsonb), `updated_at` | Current world for `(mock, sid)` |
| `mock_state_events` | `mock`, `sid`, `action`, `state` (jsonb), `created_at` | Append-only trajectory |
| `mock_files` | file metadata (not blobs) | Uploads / assets referenced by mocks |

Trajectory query: events for `(mock, attempt_sid)` ordered by `created_at`. Verifiers primarily read final `mock_states.state`; events catch sequence / forbidden mutations.

### 5.2 New tables — DDL

```sql
-- App registry
CREATE TABLE bg_app (
  id               SERIAL PRIMARY KEY,
  key              TEXT NOT NULL UNIQUE,   -- = mock_states.mock
  display_name     TEXT NOT NULL,
  base_url         TEXT NOT NULL,
  opens_in_new_tab BOOLEAN NOT NULL DEFAULT TRUE,
  active           BOOLEAN NOT NULL DEFAULT TRUE
);

-- Task (what the sidebar shows)
CREATE TABLE bg_task (
  id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_key          TEXT NOT NULL UNIQUE,
  instruction       TEXT NOT NULL,
  description       TEXT,
  allowed_sites     JSONB NOT NULL DEFAULT '[]',  -- ["amazon_mock","gmail_mock"]
  constraints       JSONB NOT NULL DEFAULT '{}',  -- {max_steps, flags}
  difficulty        TEXT CHECK (difficulty IN ('easy','medium','hard')),
  failure_mode      TEXT,
  skill_tags        JSONB NOT NULL DEFAULT '[]',
  expected_behavior TEXT,
  start_state_label TEXT,
  source            TEXT NOT NULL DEFAULT 'pipeline',
  status            TEXT NOT NULL DEFAULT 'draft', -- draft|ready|archived
  created_by        TEXT,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Per-app seed binding
CREATE TABLE bg_task_app (
  id        SERIAL PRIMARY KEY,
  task_id   UUID NOT NULL REFERENCES bg_task(id) ON DELETE CASCADE,
  app_key   TEXT NOT NULL REFERENCES bg_app(key),
  seed_sid  UUID NOT NULL,
  role      TEXT NOT NULL DEFAULT 'primary',  -- primary|secondary
  start_url TEXT NOT NULL DEFAULT '/',
  UNIQUE (task_id, app_key)
);
CREATE INDEX ON bg_task_app (seed_sid);

-- One human attempt
CREATE TABLE bg_session (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id      UUID NOT NULL REFERENCES bg_task(id),
  annotator_id TEXT NOT NULL,              -- from studio token
  status       TEXT NOT NULL DEFAULT 'active',
    -- active|submitted|approved|rejected|expired|abandoned
  apps         JSONB NOT NULL,
    -- [{app_key, attempt_sid, start_url, url}]
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at   TIMESTAMPTZ NOT NULL,
  score        INT,                        -- last 0|1
  is_golden    BOOLEAN NOT NULL DEFAULT FALSE,
  notes        TEXT
);
CREATE INDEX ON bg_session (status, expires_at);
CREATE INDEX ON bg_session (task_id, annotator_id);

CREATE TABLE bg_verifier (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id     UUID NOT NULL REFERENCES bg_task(id) ON DELETE CASCADE,
  version     INT NOT NULL DEFAULT 1,
  type        TEXT NOT NULL DEFAULT 'deterministic',
  status      TEXT NOT NULL DEFAULT 'pipeline_generated',
    -- pipeline_generated|human_reviewed|approved
  spec        JSONB NOT NULL,
  reviewed_by TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (task_id, version)
);

CREATE TABLE bg_verifier_run (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id  UUID NOT NULL REFERENCES bg_session(id) ON DELETE CASCADE,
  verifier_id UUID NOT NULL REFERENCES bg_verifier(id),
  score       INT NOT NULL CHECK (score IN (0,1)),
  passed      BOOLEAN NOT NULL,
  details     JSONB NOT NULL DEFAULT '{}',
  run_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bg_assignment (
  id           SERIAL PRIMARY KEY,
  task_id      UUID NOT NULL REFERENCES bg_task(id) ON DELETE CASCADE,
  annotator_id TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'assigned', -- assigned|started|done
  assigned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE bg_golden (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id     UUID NOT NULL UNIQUE REFERENCES bg_task(id),
  session_id  UUID NOT NULL REFERENCES bg_session(id),
  verifier_id UUID NOT NULL REFERENCES bg_verifier(id),
  packaged_at TIMESTAMPTZ,
  tar_ref     TEXT
);
```

**Seed `bg_app` once** with the five hosted URLs from §4.

### 5.3 SID clone (session start)

```text
clone_seed(app_key, seed_sid) → attempt_sid:
  1. attempt_sid := gen_random_uuid()
  2. row := SELECT state FROM mock_states WHERE mock=app_key AND sid=seed_sid
     (must exist — pipeline / seeder wrote it earlier)
  3. INSERT mock_states (mock, sid, state) VALUES (app_key, attempt_sid, row.state)
  4. INSERT mock_state_events (..., action='set', state=row.state)
  5. optional: copy mock_files for seed_sid → attempt_sid
  6. return attempt_sid
```

Invariants: never UPDATE/DELETE `seed_sid` from annotation APIs; cleanup deletes only expired non-golden attempt SIDs. If mocks auto-create empty state on unknown SID, we still **pre-clone** so annotators never start empty.

### 5.4 Migration order

1. Confirm VPN + read access to `cua-gym`.  
2. Apply `bg_*` DDL.  
3. Insert five `bg_app` rows.  
4. Pipeline (or gym `seed_to_cuagym --commit`) writes `mock_states` for each task’s `seed_sid`.  
5. `POST /api/bg/tasks` registers metadata + points at those SIDs.  
6. Annotation can open sessions.

---

## 6. Contracts

### 6.1 iframe boot (studio → UI)

```text
https://browser-agent.delta.soulhq.ai/
  ?access_token=...
  &refresh_token=...
  &task_id=<bg_task.id>
  (&session_id=<bg_session.id> for resume)
```

On load: validate token → create or resume session → render sidebar → open each `apps[].url` (new tab by default) → heartbeat while focused.

Sidebar fields (must live in DB): task id, instruction, description, tags, start-state label, constraints, allowed sites.

### 6.2 Verifier spec

```json
{
  "version": 1,
  "apps": ["amazon_mock", "gmail_mock"],
  "initial_state_refs": {
    "amazon_mock": "<seed_sid>",
    "gmail_mock": "<seed_sid>"
  },
  "asserts": [
    {
      "id": "order_headphones",
      "type": "jsonpath_exists",
      "app": "amazon_mock",
      "path": "$.orders[?(@.items[?(@.sku=='SKU_HEADPHONES')])]",
      "required": true
    },
    {
      "id": "no_extra_macbook",
      "type": "jsonpath_forbidden",
      "app": "amazon_mock",
      "path": "$.orders[*].items[?(@.sku=='SKU_MACBOOK')]",
      "required": true
    },
    {
      "id": "confirmation_email",
      "type": "jsonpath_exists",
      "app": "gmail_mock",
      "path": "$.emails[?(@.subject~'Order confirmation')]",
      "required": true
    }
  ],
  "diff_policy": {
    "compare": "final_vs_initial",
    "ignore_paths": ["$.emails[*].received_at"]
  }
}
```

Pipeline authors the draft; humans review a small set before scale. UI only **runs** the spec — no authoring UI in Phase 1.

### 6.3 Package layout

```text
task_<task_key>/
  manifest.json
  prompt.txt
  verifier.json
  seed/<app>.json
  final/<app>.json
  trace/<app>.events.jsonl
  meta/session.json
  meta/verifier_run.json
```

---

## 7. API contracts (`/api/bg`)

Auth: `Authorization: Bearer <access_token>`. VPN.

### 7.1 Surface

| Method | Path | Responsibility |
|---|---|---|
| GET | `/apps` | App registry |
| POST | `/tasks` | Pipeline ingest (task + apps + verifier) |
| GET | `/tasks`, `/tasks/{id}` | List / detail |
| GET/PUT | `/tasks/{id}/verifier` | Human review of spec |
| POST | `/sessions` | Clone seeds → attempt SIDs; return open URLs |
| GET | `/sessions/{id}` | Resume |
| POST | `/sessions/{id}/heartbeat` | Extend TTL |
| DELETE | `/sessions/{id}` | Abandon |
| POST | `/sessions/{id}/submit` | Submit + verify |
| POST | `/sessions/{id}/verify` | Score 0\|1 + details |
| POST | `/tasks/{id}/golden` | Promote |
| POST | `/tasks/{id}/package` | Write tar, set `tar_ref` |

Prefer existing mock HTTP APIs for state/events; thin BG wrappers only if needed.

### 7.2 `POST /tasks` (pipeline ingest)

```json
{
  "task_key": "BG-2026-001",
  "instruction": "Return the blue backpack and email support the RMA id.",
  "description": "…",
  "allowed_sites": ["amazon_mock", "gmail_mock"],
  "constraints": { "max_steps": 40, "flags": ["multi_tab_allowed"] },
  "difficulty": "medium",
  "skill_tags": ["navigation", "form_fill"],
  "expected_behavior": "Return exact SKU only; support email sent.",
  "start_state_label": "Alice signed in; order ORD-9481 delivered",
  "apps": [
    {
      "app_key": "amazon_mock",
      "seed_sid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      "start_url": "/orders",
      "role": "primary"
    },
    {
      "app_key": "gmail_mock",
      "seed_sid": "ffffffff-1111-2222-3333-444444444444",
      "start_url": "/#/inbox",
      "role": "secondary"
    }
  ],
  "verifier": { "type": "deterministic", "spec": { "...": "§6.2" } }
}
```

→ `201 { "task_id": "…", "status": "ready" }`  
Prereq: each `seed_sid` already exists in `mock_states`.

### 7.3 `POST /sessions`

Request: `{ "task_id": "…", "ttl_minutes": 90 }` (annotator from token).

Behavior: load task apps → clone each seed → insert `bg_session` → return URLs.

```json
{
  "session_id": "…",
  "expires_at": "2026-07-29T18:00:00Z",
  "task": {
    "task_key": "BG-2026-001",
    "instruction": "…",
    "allowed_sites": ["amazon_mock", "gmail_mock"],
    "constraints": { "max_steps": 40 }
  },
  "apps": [
    {
      "app_key": "amazon_mock",
      "attempt_sid": "…",
      "start_url": "/orders",
      "url": "https://xmazon.delta.deccanexperts.ai/orders?sid=…"
    },
    {
      "app_key": "gmail_mock",
      "attempt_sid": "…",
      "start_url": "/#/inbox",
      "url": "https://xmail.delta.deccanexperts.ai/?sid=…#/inbox"
    }
  ]
}
```

### 7.4 `POST /sessions/{id}/verify`

Load latest `human_reviewed`/`approved` verifier → eval asserts on final states → write `bg_verifier_run` → set `bg_session.score`.

```json
{
  "score": 0,
  "passed": false,
  "details": {
    "order_headphones": { "pass": true },
    "no_extra_macbook": { "pass": false, "found": ["SKU_MACBOOK"] }
  }
}
```

---

## 8. End-to-end flows (summary)

**Authoring:** write frozen seed JSON into `mock_states` → `POST /tasks` with verifier → task `ready`.

**Annotation:** studio assigns → UI opens session (clone) → human works in mocks → submit → verify → QC → golden → package.

**Gym agent eval (optional):** bridge + `run_newui_eval` — does not replace the studio path for the first 20.

---

## 9. Non-functionals

- Studio tokens only in the annotation UI.  
- Session TTL default 90 min; cron expires and deletes attempt state.  
- Index `(mock, sid)` on mock tables; `bg_session(status, expires_at)`.  
- ≥500 tasks of metadata; worlds stay JSONB.  
- Secrets in deploy store, never git.  
- Logs: `session_id`, `task_key`, score — no extra PII.

---

*v0.3.1 — interaction sequence, DDL, clone steps, and concrete API payloads.*
