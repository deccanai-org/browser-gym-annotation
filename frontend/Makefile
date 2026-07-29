# Frontend build — produces static assets in dist/ for S3/CloudFront (not dockerized).
# Same shape as the other Deccan frontends: DEV_ARGS / PROD_ARGS + build-dev / build-prod.
#
# This app reads ONE build-time var: VITE_LIVE_BASE — the live-browser service's PUBLIC
# url (where the browser opens the live pane), baked in at build time. API calls are
# same-origin (/api/*), so whatever serves dist/ must route /api/* to the backend
# (a CloudFront behavior on AWS); that keeps the SameSite=Lax session cookie working.
#
# NOTE 1: this project uses npm (package-lock.json, no yarn.lock), so the targets use
#   `npm ci` / `npm run build` — the frozen-lockfile equivalent of the yarn convention.
#   If the team standard is yarn, add a yarn.lock and I'll switch these to yarn.
# NOTE 2: the URLs below follow the delta=dev / bare=prod convention — confirm they match
#   the actual deployed live-browser hostnames before shipping.

DEV_ARGS = VITE_ENV=development \
           VITE_DOMAIN=deccanexperts.ai \
           VITE_LIVE_BASE=https://live-browser.delta.deccanexperts.ai

PROD_ARGS = VITE_ENV=production \
            VITE_DOMAIN=deccanexperts.ai \
            VITE_LIVE_BASE=https://live-browser.deccanexperts.ai


# Build Targets for Different Environments
build-dev:
	@echo "Building frontend for dev ❤️ ..."
	npm ci
	$(DEV_ARGS) npm run build

build-prod:
	@echo "Building frontend for production ❤️ ..."
	npm ci
	$(PROD_ARGS) npm run build

# --- utility ---
preview:
	npm run preview

lint:
	npm run lint

clean:
	rm -rf dist node_modules
