# Frontend build — produces static assets in dist/ for S3/CloudFront (or any
# static host). Per the deploy plan the frontend is NOT dockerized; this Makefile
# is the build entrypoint.
#
# The one build-time var is VITE_LIVE_BASE: the live-browser service's PUBLIC url
# (where the browser opens the live pane). It is baked in at build time — empty
# falls back to http://localhost:8877 for local dev.
#
#   make build VITE_LIVE_BASE=https://live-browser.delta.deccanexperts.ai
#
# API calls are same-origin (/api/*), so whatever serves dist/ must route /api/*
# to the backend (CloudFront behavior on AWS; nginx does it in the container
# build). Keeping /api same-origin preserves the SameSite=Lax session cookie.

VITE_LIVE_BASE ?=

.PHONY: help install build preview lint test clean

help:
	@echo "make install   - npm ci (clean install from package-lock.json)"
	@echo "make build     - build static assets into dist/  [VITE_LIVE_BASE=<live-browser url>]"
	@echo "make preview   - serve the built dist/ locally"
	@echo "make lint      - oxlint"
	@echo "make test      - vitest"
	@echo "make clean     - remove dist/ and node_modules/"

install:
	npm ci

build: install
	VITE_LIVE_BASE=$(VITE_LIVE_BASE) npm run build

preview:
	npm run preview

lint:
	npm run lint

test:
	npm run test

clean:
	rm -rf dist node_modules
