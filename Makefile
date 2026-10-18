SHELL := /bin/bash
.DEFAULT_GOAL := help

MULLIGAN_MCP_PORT ?= 8001
UV := uv --directory gateway
NPX := npx -y
JSON2TS := $(NPX) json-schema-to-typescript@16.0.0
PRISM := $(NPX) @stoplight/prism-cli@5.16.0
REDOCLY := $(NPX) @redocly/cli@2.64.1

SCHEMA := contracts/action.schema.json
PY_MODELS := gateway/mulligan/models.py
TS_TYPES := contracts/generated/types.ts
RECORDING ?= demo/recordings/scripted.json
SPEED ?= 1

.PHONY: help
help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

.PHONY: setup
setup: ## Install the gateway's Python deps (uv)
	$(UV) sync

# --- Contracts ---------------------------------------------------------------

.PHONY: types
types: ## Generate Pydantic models and TS types from contracts/action.schema.json
	$(UV) run datamodel-codegen \
		--input ../$(SCHEMA) --input-file-type jsonschema \
		--output ../$(PY_MODELS) \
		--output-model-type pydantic_v2.BaseModel \
		--target-python-version 3.12 \
		--use-standard-collections --use-union-operator --use-annotated \
		--collapse-root-models --field-constraints \
		--use-schema-description --use-field-description \
		--disable-timestamp --formatters ruff-format ruff-check
	$(JSON2TS) --input $(SCHEMA) --output $(TS_TYPES) \
		--unreachableDefinitions --additionalProperties false

.PHONY: types-check
types-check: types ## Fail if the generated types are out of date
	git diff --exit-code -- $(PY_MODELS) $(TS_TYPES)

.PHONY: lint-contracts
lint-contracts: ## Lint both OpenAPI specs (examples are checked against the schema)
	$(REDOCLY) lint contracts/openapi.yaml contracts/photon.openapi.yaml --config contracts/redocly.yaml

.PHONY: mocks
mocks: ## Mock the gateway API on :4010 and the Photon service on :4011 (Prism)
	@trap 'kill 0' INT TERM; \
	$(PRISM) mock -h 127.0.0.1 -p 4010 contracts/openapi.yaml & \
	$(PRISM) mock -h 127.0.0.1 -p 4011 contracts/photon.openapi.yaml & \
	wait

# --- Gateway -----------------------------------------------------------------

.PHONY: lint
lint: ## Ruff lint and format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: fmt
fmt: ## Format the gateway
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

.PHONY: typecheck
typecheck: ## mypy --strict on the gateway
	$(UV) run mypy

.PHONY: test
test: ## Unit tests
	$(UV) run pytest

.PHONY: test-integration
test-integration: ## Integration tests (needs `make db`)
	$(UV) run pytest -m integration

.PHONY: gateway
gateway: ## Run the gateway on the host (REST 127.0.0.1:8000, MCP 127.0.0.1:8001)
	$(UV) run python -m mulligan

.PHONY: mock-slack
mock-slack: ## Mock Slack MCP server on 127.0.0.1:8200 (UPSTREAM_MCP_SERVERS=slack=http://127.0.0.1:8200/mcp)
	$(UV) run python ../demo/upstream/slack_server.py

.PHONY: ci
ci: types-check lint-contracts lint typecheck test photon-check dashboard-check fetch-check ## Everything CI runs

# --- Stream B: Photon service and dashboard ---------------------------------

.PHONY: photon-setup
photon-setup: ## Install the Photon service's deps
	npm ci --prefix approvals-photon

.PHONY: photon
photon: ## Run the Photon service on the host (127.0.0.1:8100)
	npm --prefix approvals-photon run dev

.PHONY: photon-check
photon-check: ## Type-check and test the Photon service
	npm --prefix approvals-photon run typecheck
	npm --prefix approvals-photon test

.PHONY: dashboard-setup
dashboard-setup: ## Install the dashboard's deps
	npm ci --prefix dashboard

.PHONY: dashboard
dashboard: ## Run the dashboard dev server (127.0.0.1:5173; API defaults to the Prism mock on :4010)
	npm --prefix dashboard run dev

.PHONY: dashboard-check
dashboard-check: ## Type-check, test and build the dashboard
	npm --prefix dashboard run typecheck
	npm --prefix dashboard test
	npm --prefix dashboard run build

# --- Stack -------------------------------------------------------------------

.PHONY: db
db: ## Start both Postgres DBs
	docker compose up -d --wait mulligan-db prod-db

.PHONY: mailpit
mailpit: ## Start Mailpit (SMTP 127.0.0.1:1025, inbox http://127.0.0.1:8025)
	docker compose up -d --wait mailpit

.PHONY: up
up: ## Start the whole stack
	docker compose --profile app up -d --build --wait

.PHONY: down
down: ## Stop the stack (keeps volumes)
	docker compose --profile app down

.PHONY: nuke
nuke: ## Stop the stack and delete both databases' volumes
	docker compose --profile app down -v

.PHONY: logs
logs: ## Follow the stack's logs
	docker compose --profile app logs -f --tail=100

.PHONY: demo-reset
demo-reset: ## Reseed every system, clear the log, print checksums (gateway running, MULLIGAN_DEMO=1)
	$(UV) run python ../demo/scripts/reset.py

.PHONY: demo-replay
demo-replay: ## Replay the default recording through Mulligan (RECORDING=..., SPEED=...)
	$(UV) run python ../demo/rogue_agent.py --replay $(abspath $(RECORDING)) --speed $(SPEED)

.PHONY: demo-live
demo-live: ## Run the rogue agent live with an LLM and record it
	$(UV) run python ../demo/rogue_agent.py --live

.PHONY: teammate-edit
teammate-edit: ## A teammate's direct edits after the agent ran (kept and flagged by the rewind)
	$(UV) run python ../demo/teammate_edit.py

.PHONY: check-exposure
check-exposure: ## Every route needs a token, MCP port serves only /mcp, tunnel exposes only MCP
	$(UV) run python ../infra/deploy/check_exposure.py

.PHONY: load-check
load-check: ## 200 actions in one session, rewind under 30 s, checksum restored
	$(UV) run python ../demo/scripts/load_check.py

.PHONY: fetch-setup
fetch-setup: ## Install the Fetch agents' deps (uv)
	uv --directory fetch-agent sync

.PHONY: fetch-check
fetch-check: ## Lint and unit-test the Fetch agents
	uv --directory fetch-agent run ruff check .
	uv --directory fetch-agent run ruff format --check .
	uv --directory fetch-agent run pytest

.PHONY: fetch-worker
fetch-worker: ## Run the Fetch worker agent (port 8011)
	uv --directory fetch-agent run python -m mulligan_fetch.worker

.PHONY: fetch-guardian
fetch-guardian: ## Run the Fetch Guardian agent for ASI:One (port 8010, mailbox)
	uv --directory fetch-agent run python -m mulligan_fetch.guardian

.PHONY: tunnel
tunnel: ## Public HTTPS URL for the MCP port only (cloudflared quick tunnel, for Devin)
	cloudflared tunnel --url http://127.0.0.1:$(MULLIGAN_MCP_PORT)
