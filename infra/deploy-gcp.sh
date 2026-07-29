#!/usr/bin/env bash
# Deploy the annotation platform to GCP: Cloud SQL (Postgres) + two Cloud Run
# services (backend + frontend). Idempotent — safe to re-run to ship updates.
#
#   ./infra/deploy-gcp.sh
#
# Prereqs: gcloud authed (`gcloud auth login`), billing enabled on the project.
# Override any of the vars below via the environment.
set -euo pipefail

PROJECT="${PROJECT:-mlproject-501205}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-annotator}"                     # Artifact Registry repo
SQL_INSTANCE="${SQL_INSTANCE:-annotator-db}"
DB_NAME="${DB_NAME:-browser_gym_annotator}"
DB_USER="${DB_USER:-annotator}"
DB_TIER="${DB_TIER:-db-f1-micro}"             # smallest/cheapest; bump for load
DB_PASS="${DB_PASS:-}"                         # required on first run (or set via Secret Manager)
AUTH_SECRET="${AUTH_SECRET:-}"                 # REQUIRED — signs session cookies; the prod backend refuses to boot without it
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"    # optional (M6b live agent)
GYM_URL="${GYM_URL:-}"                         # a reachable gym service URL (deploy the gym separately — see docs/DEPLOY.md)
LIVE_BROWSER_URL="${LIVE_BROWSER_URL:-}"       # a reachable live-browser service URL (for the live pane; see docs/DEPLOY.md)

AR="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}"
CONN="${PROJECT}:${REGION}:${SQL_INSTANCE}"

say() { printf "\n\033[1;34m▸ %s\033[0m\n" "$*"; }

# Fail fast on the two secrets the platform cannot run without, with a clear
# message — the prod backend's boot guard rejects a missing/default AUTH_SECRET,
# so catching it here beats a container that crash-loops on deploy.
[ -n "$AUTH_SECRET" ] || { echo "Set AUTH_SECRET (a strong random string; signs session cookies). e.g. AUTH_SECRET=\$(openssl rand -hex 32)"; exit 1; }

gcloud config set project "$PROJECT" >/dev/null

say "Enabling APIs"
gcloud services enable run.googleapis.com sqladmin.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com

say "Artifact Registry repo ($REPO)"
gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION"

say "Cloud SQL instance ($SQL_INSTANCE)"
if ! gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  [ -n "$DB_PASS" ] || { echo "Set DB_PASS on first run (a strong password)"; exit 1; }
  gcloud sql instances create "$SQL_INSTANCE" --database-version=POSTGRES_16 \
    --tier="$DB_TIER" --region="$REGION" --storage-auto-increase
  gcloud sql databases create "$DB_NAME" --instance="$SQL_INSTANCE"
  gcloud sql users create "$DB_USER" --instance="$SQL_INSTANCE" --password="$DB_PASS"
fi

# URL-encode the password so a strong password with @ / : , etc. is safe inside
# the connection string.
DB_PASS_ENC="$(python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' "$DB_PASS")"
DB_URL="postgresql+psycopg://${DB_USER}:${DB_PASS_ENC}@/${DB_NAME}?host=/cloudsql/${CONN}"

say "Build + push images (Cloud Build)"
gcloud builds submit backend  --tag "$AR/backend:latest"
gcloud builds submit frontend --tag "$AR/frontend:latest"

say "Deploy backend (Cloud Run) — runs migrations on boot"
# '|' as the env-var delimiter (^|^) — none of our values contain it — so a
# DATABASE_URL / CORS list with commas or colons can't corrupt var parsing.
#
# CORS_ORIGINS=[] (not ["*"]): the browser only ever calls /api on the FRONTEND
# origin, which nginx proxies to the backend server-side — so CORS is never
# exercised in the normal flow, and a wildcard on an --allow-unauthenticated
# service is an unnecessary exposure. Add the frontend origin here only if you
# intend direct cross-origin API calls.
BACKEND_ENV="^|^ENV=prod|AUTO_CREATE_ALL=false|RUN_MIGRATIONS=1|DATABASE_URL=${DB_URL}|AUTH_SECRET=${AUTH_SECRET}|CORS_ORIGINS=[]"
[ -n "$ANTHROPIC_API_KEY" ] && BACKEND_ENV="${BACKEND_ENV}|ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
[ -n "$GYM_URL" ]           && BACKEND_ENV="${BACKEND_ENV}|GYM_URL=${GYM_URL}"
[ -n "$LIVE_BROWSER_URL" ]  && BACKEND_ENV="${BACKEND_ENV}|LIVE_BROWSER_URL=${LIVE_BROWSER_URL}"
gcloud run deploy annotator-backend \
  --image "$AR/backend:latest" --region "$REGION" --platform managed --allow-unauthenticated \
  --add-cloudsql-instances "$CONN" --set-env-vars "$BACKEND_ENV" \
  --min-instances 1 --cpu 1 --memory 512Mi --timeout 300

BACKEND_URL="$(gcloud run services describe annotator-backend --region "$REGION" --format='value(status.url)')"
# nginx must send the backend's HOSTNAME as Host + SNI, or Cloud Run won't route /api.
BACKEND_HOST="${BACKEND_URL#https://}"; BACKEND_HOST="${BACKEND_HOST#http://}"; BACKEND_HOST="${BACKEND_HOST%%/*}"

say "Deploy frontend (Cloud Run) — proxies /api → $BACKEND_URL"
gcloud run deploy annotator-frontend \
  --image "$AR/frontend:latest" --region "$REGION" --platform managed --allow-unauthenticated \
  --set-env-vars "^|^BACKEND_ORIGIN=${BACKEND_URL}|BACKEND_HOST=${BACKEND_HOST}" --cpu 1 --memory 256Mi

FRONTEND_URL="$(gcloud run services describe annotator-frontend --region "$REGION" --format='value(status.url)')"
say "Done"
echo "  backend : $BACKEND_URL"
echo "  app     : $FRONTEND_URL"
