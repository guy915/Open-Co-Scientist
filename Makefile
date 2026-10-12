.PHONY: presubmit lint-python lint-frontend root-config build-checked arch help setup start dev-api dev-ui dev-all dev-mcp preflight ensure-deps open-when-ready test test-app test-engine test-mcp test-sandbox-linux test-all test-frontend check check-tools attempt-envelope docker-build audit-deps test-evaluations eval-smoke e2e e2e-production lint typecheck build clean stop reset-db

ROOT := $(CURDIR)
ENGINE := $(ROOT)/engine
APP    := $(ROOT)/app
FRONTEND := $(APP)/frontend
VENV  := $(ROOT)/.venv
PY    := $(VENV)/bin/python
PIP   := $(VENV)/bin/pip
BUN ?= bun
BUN_VERSION := 1.3.14
# Homebrew Python is externally managed; the Python 3.12 MCP runtime needs its own venv.
MCP_VENV := $(ROOT)/.venv-mcp

API_URL := http://localhost:8008
UI_URL  := http://localhost:5173

help:
	@echo "Co-Scientist — root commands"
	@echo "  make setup        Create .venv, install engine (editable, dev extras), install frontend"
	@echo "  make start        One command: install missing deps, free ports, run MCP + API + UI, open browser"
	@echo "  make stop         Stop anything listening on the dev ports (8008/5173/8888)"
	@echo "  make dev-api      Run FastAPI dev server   ($(API_URL))"
	@echo "  make dev-ui       Run Vite dev server      ($(UI_URL))"
	@echo "  make dev-mcp      Run reference MCP server (optional, needs Python 3.12)"
	@echo "  make test         Run viewer backend pytest suite"
	@echo "  make test-app     Run viewer backend pytest suite"
	@echo "  make test-engine  Run engine pytest suite"
	@echo "  make test-mcp     Run reference MCP server pytest + mypy (needs Python 3.12)"
	@echo "  make presubmit    Run only gates affected by git diff origin/main...HEAD"
	@echo "  make test-all     Run backend, frontend and evaluation suites"
	@echo "  make check        Run lint, types, all suites, eval smoke, build, and browser tests"
	@echo "  make docker-build Build images and validate Compose (never deploys)"
	@echo "  make audit-deps   Audit dependency locks online (see requirements/README.md)"
	@echo "  make test-frontend Run frontend unit tests"
	@echo "  make e2e          Run the browser end-to-end suite (headless, isolated stack)"
	@echo "  make e2e-production Test built frontend assets and anonymous ownership"
	@echo "  make test-evaluations Run evaluation harness tests"
	@echo "  make eval-smoke   Run the offline evaluation smoke suite (no LLM, no network)"
	@echo "  make attempt-envelope Count one offline Express run's HTTP attempts per arm"
	@echo "  make lint         Lint backend (ruff) + frontend (gts) + import contracts"
	@echo "  make arch         Check the import contracts in .importlinter"
	@echo "  make typecheck    Typecheck backend (mypy: app, engine, evaluations)"
	@echo "  make build        Build frontend (tsc + vite build + prerender)"
	@echo "  make clean        Remove .venv, caches, frontend dist"
	@echo "  make reset-db     Drop the local SQLite store (coscientist.db)"

setup: check-tools $(VENV)/bin/activate
	@echo ">> Installing engine (editable)"
	@$(PIP) install -e "$(ENGINE)[dev]" pytest-xdist==3.8.0 markdown-it-py==4.2.0
	@# Reference MCP server is optional and pins Python 3.12, so we don't install it here.
	@echo ">> Installing frontend from bun.lock"
	@cd "$(FRONTEND)" && "$(BUN)" install --frozen-lockfile
	@test -f "$(ROOT)/.env" || cp "$(ROOT)/.env.example" "$(ROOT)/.env"
	@# dev-api runs with cwd=app/, and Settings loads ".env" relative to cwd
	@# (engine/src/co_scientist/core/config.py), so a root-only .env is invisible to it. Symlink
	@# app/.env at the root file so there is one file, not two to keep in
	@# sync -- and it happens to be exactly where docker-compose's own
	@# `env_file: .env` (relative to its app/ context) already looks.
	@test -e "$(APP)/.env" || ln -s ../.env "$(APP)/.env"
	@echo ""
	@echo "Setup complete. Next:"
	@echo "  make start      # MCP + API + UI in one command, then opens the browser"
	@echo ""
	@echo "Or run the pieces in separate terminals:"
	@echo "  make dev-api    # in one terminal"
	@echo "  make dev-ui     # in another terminal"

$(VENV)/bin/activate:
	@echo ">> Creating venv at $(VENV) (using python3.12 if available)"
	@(command -v python3.12 >/dev/null 2>&1 && python3.12 -m venv "$(VENV)") || \
	 (command -v python3.13 >/dev/null 2>&1 && python3.13 -m venv "$(VENV)") || \
	 python3 -m venv "$(VENV)"
	@$(PIP) install --upgrade pip setuptools wheel >/dev/null

start: preflight
	@echo ""
	@echo "Co-Scientist — dev URLs"
	@echo "  API   : $(API_URL)"
	@echo "  UI    : $(UI_URL)"
	@echo ""
	@$(MAKE) dev-all

preflight:
	@$(MAKE) ensure-deps
	@$(MAKE) stop

ensure-deps: check-tools
	@test -f "$(ROOT)/.env" || { echo ">> No .env found — copying .env.example"; cp "$(ROOT)/.env.example" "$(ROOT)/.env"; }
	@# See the matching comment in `setup` -- one env file, symlinked so
	@# dev-api's cwd=app/ Settings load actually sees it.
	@test -e "$(APP)/.env" || ln -s ../.env "$(APP)/.env"
	@if ! { test -x "$(PY)" && "$(PY)" -c "import uvicorn, fastapi, pydantic_settings, co_scientist" >/dev/null 2>&1; }; then \
		echo ">> Backend deps missing or broken — running setup"; \
		$(MAKE) setup; \
	elif [ ! -d "$(FRONTEND)/node_modules" ]; then \
		echo ">> Frontend deps missing — installing"; \
		cd "$(FRONTEND)" && "$(BUN)" install --frozen-lockfile; \
	fi

dev-all:
	@bash -c "trap 'kill 0' INT TERM EXIT; \
		($(MAKE) dev-mcp 2>&1 | sed 's/^/[mcp] /') & \
		($(MAKE) dev-api 2>&1 | sed 's/^/[api] /') & \
		($(MAKE) dev-ui 2>&1 | sed 's/^/[ui]  /') & \
		($(MAKE) open-when-ready 2>&1 | sed 's/^/[web] /') & \
		wait"

open-when-ready:
	@i=0; \
	until curl -sf "$(API_URL)/health" >/dev/null 2>&1 && curl -sf "$(UI_URL)" >/dev/null 2>&1; do \
		i=$$((i+1)); \
		if [ $$i -ge 240 ]; then \
			echo ">> Timed out waiting for the app — open $(UI_URL) manually once it is up"; \
			exit 0; \
		fi; \
		sleep 0.5; \
	done; \
	echo ">> App is up — opening $(UI_URL)"; \
	open "$(UI_URL)" 2>/dev/null || xdg-open "$(UI_URL)" 2>/dev/null || echo ">> Open $(UI_URL) in your browser"

stop:
	@echo ">> Stopping Co-Scientist dev servers (ports 8008/5173/8888)"
	@# Kill only listeners on the dev ports. uvicorn --reload
	@# and vite run under a supervising parent that respawns the listener, so
	@# take out the parent too when it is a python/node/bun process.
	@for port in 8008 5173 8888; do \
		for pid in $$(lsof -ti tcp:$$port -sTCP:LISTEN 2>/dev/null); do \
			ppid=$$(ps -o ppid= -p $$pid 2>/dev/null | tr -d ' '); \
			pcomm=$$(ps -o comm= -p $$ppid 2>/dev/null); \
			case "$$pcomm" in \
				*python*|*Python*|*node*|*bun*) kill $$ppid 2>/dev/null || true;; \
			esac; \
			kill $$pid 2>/dev/null || true; \
		done; \
	done
	@n=0; while [ $$n -lt 10 ]; do \
		busy=$$( { lsof -ti tcp:8008 -sTCP:LISTEN; lsof -ti tcp:5173 -sTCP:LISTEN; lsof -ti tcp:8888 -sTCP:LISTEN; } 2>/dev/null | head -1 ); \
		[ -z "$$busy" ] && break; \
		sleep 0.3; n=$$((n+1)); \
	done; \
	{ lsof -ti tcp:8008 -sTCP:LISTEN; lsof -ti tcp:5173 -sTCP:LISTEN; lsof -ti tcp:8888 -sTCP:LISTEN; } 2>/dev/null | xargs kill -9 2>/dev/null || true
	@echo ">> Dev ports are free"

dev-api:
	@echo ">> Starting FastAPI on $(API_URL)"
	@cd "$(APP)" && COSCIENTIST_DB_PATH="$(ROOT)/coscientist.db" "$(PY)" -m uvicorn co_scientist.main:app --reload --reload-dir "$(ENGINE)/src" --host 0.0.0.0 --port 8008

dev-ui: check-tools
	@echo ">> Starting Vite UI on $(UI_URL)"
	@cd "$(FRONTEND)" && "$(BUN)" run dev

dev-mcp:
	@# Single shell block on purpose: each make recipe line runs in its own
	@# shell, so an `exit 0` skip on one line would not stop the next one.
	@# The server runs from $(ENGINE) so the mcp_server package resolves as a
	@# directory package (the editable install's import map is empty).
	@if ! command -v python3.12 >/dev/null 2>&1; then \
		echo ">> python3.12 not found; MCP server skipped (literature tools fall back to LLM-only)"; \
		exit 0; \
	fi; \
	if ! test -x "$(MCP_VENV)/bin/python"; then \
		echo ">> Creating MCP venv at $(MCP_VENV) (Python 3.12)"; \
		python3.12 -m venv "$(MCP_VENV)" || { echo ">> Could not create MCP venv; skipping MCP server"; exit 0; }; \
	fi; \
	if ! "$(MCP_VENV)/bin/python" -c "import fastmcp, uvicorn" >/dev/null 2>&1; then \
		echo ">> Installing reference MCP server (first run / when deps change)"; \
		"$(MCP_VENV)/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1; \
		"$(MCP_VENV)/bin/python" -m pip install -q -e "$(ENGINE)/mcp_server" || { echo ">> MCP deps install failed (offline?); skipping MCP server"; exit 0; }; \
	fi; \
	echo ">> Starting reference MCP server on http://localhost:8888"; \
	cd "$(ENGINE)" && COSCIENTIST_MCP_ALLOW_UNAUTHENTICATED_LOCAL=1 "$(MCP_VENV)/bin/python" -m uvicorn mcp_server.server:app --host 0.0.0.0 --port 8888

test-all test-engine test-app test-mcp test-evaluations eval-smoke attempt-envelope arch e2e: export LITELLM_LOCAL_MODEL_COST_MAP := True

test:
	@$(MAKE) test-app

test-app:
	@cd "$(APP)" && "$(PY)" -m pytest -q -n 4

test-engine:
	@cd "$(ENGINE)" && "$(PY)" -m pytest -q

# Linux Landlock differs from macOS seatbelt; production confinement needs Linux coverage.
# Backend preflight prevents denial tests passing when no sandbox can launch.
test-sandbox-linux:
	@docker info >/dev/null 2>&1 || { \
		echo "Docker is not running. Start Docker Desktop and retry."; \
		exit 1; }
	@echo "Building the Linux sandbox harness..."
	@docker build -q -f "$(ENGINE)/docker/sandbox-linux.Dockerfile" \
		-t coscientist-sandbox-linux "$(ENGINE)" >/dev/null
	@echo ""
	@echo "== unprivileged (what production actually runs) =="
	@docker run --rm coscientist-sandbox-linux

# MCP imports resolve from engine/; strict mypy configuration lives in mcp_server/.
# Its dev extras omit mypy, so install it separately.
test-mcp:
	@command -v python3.12 >/dev/null 2>&1 || test -x "$(MCP_VENV)/bin/python" || \
		{ echo ">> python3.12 not found — the reference MCP server pins Python 3.12"; exit 1; }
	@if ! test -x "$(MCP_VENV)/bin/python"; then \
		echo ">> Creating MCP venv at $(MCP_VENV) (Python 3.12)"; \
		python3.12 -m venv "$(MCP_VENV)"; \
	fi
	@if ! "$(MCP_VENV)/bin/python" -c "import pytest, mypy, fastmcp" >/dev/null 2>&1; then \
		echo ">> Installing reference MCP server (dev extras + mypy)"; \
		"$(MCP_VENV)/bin/python" -m pip install -q --upgrade pip >/dev/null; \
		"$(MCP_VENV)/bin/python" -m pip install -q -e "$(ENGINE)/mcp_server[dev]" mypy types-defusedxml; \
	fi
	@cd "$(ENGINE)" && "$(MCP_VENV)/bin/python" -m pytest mcp_server/tests -q
	@cd "$(ENGINE)/mcp_server" && "$(MCP_VENV)/bin/python" -m mypy .

test-all:
	@$(MAKE) test-engine
	@$(MAKE) test-app
	@$(MAKE) test-mcp
	@$(MAKE) test-evaluations
	@$(MAKE) test-frontend

# Use isolated ports and a fresh store so browser tests cannot disturb a developer stack.
E2E := $(ROOT)/e2e
e2e: check-tools
	@test -x "$(PY)" || { echo ">> Backend venv missing — run 'make setup' first"; exit 1; }
	@test -d "$(FRONTEND)/node_modules" || { echo ">> Frontend deps missing — run 'make setup' first"; exit 1; }
	@echo ">> Running browser e2e suite (headless)"
	@cd "$(E2E)" && "$(BUN)" install --frozen-lockfile
	@"$(FRONTEND)/node_modules/.bin/tsc" --noEmit --project "$(E2E)/tsconfig.json"
	@if [ -n "$$COSCI_E2E_CHROMIUM_EXECUTABLE" ]; then \
		test -x "$$COSCI_E2E_CHROMIUM_EXECUTABLE" || { echo "COSCI_E2E_CHROMIUM_EXECUTABLE must name an executable browser"; exit 1; }; \
	else \
		cd "$(E2E)" && "$(BUN)" x playwright install chromium; \
	fi
	@node --test "$(E2E)/run.test.mjs"
	@cd "$(E2E)" && "$(BUN)" test support/shards.test.ts
	@cd "$(E2E)" && BUN="$(BUN)" node run.mjs $(E2E_ARGS)

e2e-production:
	@COSCI_E2E_PRODUCTION=1 $(MAKE) e2e

test-evaluations:
	@cd "$(ROOT)" && "$(PY)" -m pytest evaluations/tests -q

# Offline: real API, worker, gateway and MCP server, with every upstream faked
# below its HTTP client and counted. ENGINE_DIR measures another checkout.
attempt-envelope:
	@test -x "$(PY)" || { echo ">> Backend venv missing — run 'make setup' first"; exit 1; }
	@test -x "$(MCP_VENV)/bin/python" || { echo ">> MCP venv missing — run 'make test-mcp' first"; exit 1; }
	@cd "$(ROOT)" && "$(PY)" -m evaluations.attempt_envelope --engine-dir="$(or $(ENGINE_DIR),$(ENGINE))"

# Provider-backed evaluations stay opt-in; the default smoke is offline.
eval-smoke:
	@cd "$(ROOT)" && "$(PY)" -m evaluations.smoke

lint: lint-python lint-frontend

lint-python:
	@cd "$(ENGINE)" && "$(PY)" -m ruff format --check .
	@cd "$(APP)" && "$(PY)" -m ruff format --check .
	@cd "$(ROOT)" && "$(PY)" -m ruff format --check evaluations
	@cd "$(APP)" && "$(PY)" -m ruff check tests
	@cd "$(ENGINE)" && "$(PY)" -m ruff check .
	@cd "$(ROOT)" && "$(PY)" -m ruff check evaluations
	@cd "$(ROOT)" && "$(PY)" -m ruff format --check scripts/__init__.py scripts/select_targets.py scripts/presubmit.py
	@cd "$(ROOT)" && "$(PY)" -m ruff check scripts/__init__.py scripts/select_targets.py scripts/presubmit.py
	@$(MAKE) arch

lint-frontend: check-tools
	@test -d "$(FRONTEND)/node_modules" || { echo ">> Frontend deps missing — run 'make setup' first"; exit 1; }
	@echo ">> Linting frontend (gts)"
	@cd "$(FRONTEND)" && "$(BUN)" run lint

arch:
	@cd "$(APP)" && "$(PY)" -c "import sys; from importlinter.cli import lint_imports_command; sys.exit(lint_imports_command())" --config ../.importlinter --no-cache

typecheck:
	@cd "$(APP)" && "$(PY)" -m mypy .
	@cd "$(ENGINE)" && "$(PY)" -m mypy .
	@cd "$(ROOT)/evaluations" && "$(PY)" -m mypy .
	@cd "$(ROOT)" && "$(PY)" -m mypy --strict scripts/__init__.py scripts/select_targets.py scripts/presubmit.py

build: check-tools
	@cd "$(FRONTEND)" && "$(BUN)" run build

build-checked: check-tools
	@cd "$(FRONTEND)" && VITE_SENTRY_DSN=https://bundle-public@sentry.invalid/123 "$(BUN)" run build
	@node "$(FRONTEND)/scripts/check-bundle.mjs"
	@node --test "$(FRONTEND)/scripts/check-bundle.test.mjs"

root-config:
	@"$(PY)" -m json.tool wrangler.jsonc > /dev/null
	@node --test "$(FRONTEND)/worker.test.mjs"

presubmit:
	@"$(PY)" -m scripts.presubmit $(PRESUBMIT_ARGS)

CI_TOOLS := $(ROOT)/.cache/ci-tools

.PHONY: ci-guards lint-workflows
ci-guards:
	@"$(PY)" -m unittest discover -s scripts/ci/tests
	@"$(PY)" -m ruff check scripts/ci
	@"$(PY)" -m ruff format --check scripts/ci
	@"$(PY)" scripts/ci/markdown_links.py
	@BASE_SHA="$$(git rev-parse origin/main)" HEAD_SHA="$$(git rev-parse HEAD)" \
		"$(PY)" scripts/ci/git_hygiene.py --commits-only
	@"$(PY)" scripts/ci/install_tools.py gitleaks "$(CI_TOOLS)"
	@BASE_SHA="$$(git rev-parse origin/main)" HEAD_SHA="$$(git rev-parse HEAD)" \
		GITLEAKS_BIN="$(CI_TOOLS)/gitleaks" "$(PY)" scripts/ci/secret_scan.py

lint-workflows:
	@"$(PY)" scripts/ci/install_tools.py actionlint "$(CI_TOOLS)"
	@"$(PY)" scripts/ci/install_tools.py gitleaks "$(CI_TOOLS)"
	@"$(PY)" -m pip install zizmor==1.30.1
	@ACTIONLINT_BIN="$(CI_TOOLS)/actionlint" ZIZMOR_BIN="$(VENV)/bin/zizmor" \
		"$(PY)" scripts/ci/workflow_lint.py
	@ACTIONLINT_BIN="$(CI_TOOLS)/actionlint" ZIZMOR_BIN="$(VENV)/bin/zizmor" \
		GITLEAKS_BIN="$(CI_TOOLS)/gitleaks" "$(PY)" -m scripts.ci.tool_tests

clean:
	rm -rf "$(VENV)" "$(MCP_VENV)" "$(FRONTEND)/dist" "$(FRONTEND)/node_modules" "$(APP)"/.coscientist_cache "$(APP)"/cache

reset-db:
	rm -f "$(ROOT)/coscientist.db"
	@echo ">> Removed $(ROOT)/coscientist.db"

check:
	@$(MAKE) lint
	@$(MAKE) typecheck
	@$(MAKE) test-all
	@$(MAKE) eval-smoke
	@$(MAKE) build
	@$(MAKE) e2e
	@$(MAKE) e2e-production

test-frontend: check-tools
	@cd "$(FRONTEND)" && "$(BUN)" run test

check-tools:
	@command -v "$(BUN)" >/dev/null 2>&1 || { echo "Bun $(BUN_VERSION) is required; see docs/RUNNING-LOCALLY.md"; exit 1; }
	@test "$$($(BUN) --version)" = "$(BUN_VERSION)" || { echo "Use Bun $(BUN_VERSION) to match bun.lock and CI"; exit 1; }
	@command -v node >/dev/null 2>&1 || { echo "Node.js 22.13+ is required for frontend tooling"; exit 1; }
	@node -e 'const [major, minor] = process.versions.node.split(".").map(Number); if (major < 22 || (major === 22 && minor < 13)) { console.error("Node.js 22.13+ is required"); process.exit(1); }'

docker-build:
	@docker build -f Dockerfile.api -t coscientist-api-local .
	@docker build -f Dockerfile.mcp -t coscientist-mcp-local .
	@docker build -f "$(FRONTEND)/Dockerfile" -t coscientist-ui-local "$(FRONTEND)"
	@docker compose --env-file "$(APP)/.env.example" -f "$(APP)/docker-compose.yml" config --no-env-resolution --quiet

# Online advisories are separate from offline gates; retain all findings despite earlier
# failures.
audit-deps: check-tools
	@command -v uv >/dev/null 2>&1 || { echo "uv is required; see requirements/README.md"; exit 1; }
	@status=0; \
	for lock in api mcp skills; do \
		uv tool run --from pip-audit==2.10.1 pip-audit \
			-r "$(ROOT)/requirements/$$lock.txt" --disable-pip --no-deps \
			--progress-spinner off || status=1; \
	done; \
	(cd "$(FRONTEND)" && "$(BUN)" audit) || status=1; \
	(cd "$(E2E)" && "$(BUN)" audit) || status=1; \
	exit $$status
