SHELL := /bin/bash

BACKEND_HOST ?= 127.0.0.1
BACKEND_PORT ?= 8011
FRONTEND_HOST ?= 127.0.0.1
FRONTEND_PORT ?= 5173
VENV_BIN := .venv/bin
UVICORN := $(if $(wildcard $(VENV_BIN)/uvicorn),$(VENV_BIN)/uvicorn,uvicorn)
TAURI := cargo tauri

.PHONY: dev backend frontend build native-backend native-dev native-build native-smoke

dev:
	@set -e; \
	trap 'trap - INT TERM EXIT; kill $$backend_pid $$frontend_pid 2>/dev/null || true; wait 2>/dev/null || true' INT TERM EXIT; \
	$(MAKE) --no-print-directory backend & backend_pid=$$!; \
	$(MAKE) --no-print-directory frontend & frontend_pid=$$!; \
	wait $$backend_pid $$frontend_pid

backend:
	$(UVICORN) backend.app.main:app --reload --reload-dir backend --timeout-graceful-shutdown 3 --host $(BACKEND_HOST) --port $(BACKEND_PORT)

frontend:
	cd frontend && WIKI_API_TARGET=http://$(BACKEND_HOST):$(BACKEND_PORT) npm run dev -- --host $(FRONTEND_HOST) --port $(FRONTEND_PORT)

build:
	cd frontend && npm run build
	python -m compileall backend

native-backend:
	./scripts/build-native-backend.sh

native-dev: native-backend
	$(TAURI) dev --no-watch

native-build: native-backend
	$(TAURI) build --bundles app

native-smoke: native-backend
	./scripts/native-smoke.sh
