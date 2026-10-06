.DEFAULT_GOAL := help
SHELL := /bin/bash

PROJECT ?= oncall-agent
PYTHON  := $(CURDIR)/.venv/bin/python
Q       ?=

# When a .env is present it is the authority on credentials.
#
# python-dotenv's load_dotenv() does NOT override variables already in the
# environment, so a BRAINTRUST_API_KEY exported by another demo's .env earlier in
# the same shell silently wins — which is exactly how you push scorers and 300
# traces into the wrong org and only notice afterward. Unsetting the ambient
# values makes .env authoritative for the Python scripts, and --env-file does the
# same for bt, whose own credential order (profile, then env, then saved config)
# would otherwise fall back to a saved profile in a different org.
#
# With no .env, both expand to nothing and the ambient environment is used as before.
DOTENV  := $(wildcard .env)
ENVSHIM := $(if $(DOTENV),env -u BRAINTRUST_API_KEY -u OPENAI_API_KEY -u OPENAI_BASE_URL -u EXA_API_KEY -u BRAINTRUST_ORG_NAME,)
BTENV   := $(if $(DOTENV),--env-file .env,)
# The two push targets cd into prompts/ and scorers/ first, so .env is one level up.
PUSHENV := $(if $(DOTENV),--env-file ../.env,)

# bt still resolves the *org* from its own saved config (~/.config/bt/config.json)
# even when the credential comes from --env-file, so a .env pointing at a different
# org than the last `bt login` fails with
# "invalid configuration: Organization '<other org>' not found". Deriving the org
# from the credential itself is the only way to keep the two in agreement.
#
# Resolved by the recipe's own shell (`$$(...)`), not by make's `$(shell ...)`.
# make expands `$(shell)` while parsing the recipe, which can run before the
# `install` prerequisite has finished building .venv — that produced a silent
# `--org ""` and a failed eval. Deferring to the shell means it runs after install,
# and only in recipes that actually invoke bt, so `make help` stays offline.
# $(CURDIR)-qualified, not relative: push-prompts and push-scorers `cd` into
# prompts/ and scorers/ before this substitution runs in the recipe shell, and a
# relative scripts/preflight.py is not found from there. 2>/dev/null then hides
# the error and bt receives --org "", failing with "Organization '' not found".
# Only reachable when a .env exists, which is why it survived until the first
# fresh-org bootstrap.
BTORG    = $(if $(DOTENV),--org "$$($(ENVSHIM) $(PYTHON) $(CURDIR)/scripts/preflight.py --print-org 2>/dev/null)",)

COUNT   ?= 300
WORKERS ?= 12
DAYS    ?= 7
# The question bank is sampled deterministically, so a second seed run with the
# same SEED replays the same questions. Vary it to add genuinely different traffic.
SEED    ?= 7
# Degraded-traffic seed (scripts/seed_failures.py). Small by default: the point is
# a visible, unambiguous failure rate on the monitor, not volume.
FAIL_COUNT   ?= 24
FAIL_WORKERS ?= 6

.PHONY: help install agent ask push-prompts push-scorers push-facets push-all \
        automations topics topics-status defect-report seed seed-failures dataset experiment remote-eval \
        preflight bootstrap verify-scoring check-org eval-status logs check clean

help: ## Show available targets
	@echo "On-call Agent — incident response, instrumented with Braintrust"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo
	@echo "Seeding a fresh org:"
	@echo "  1. put that org's key in .env   (see .env.example; .env then wins over"
	@echo "     any stale key exported in this shell)"
	@echo "  2. make preflight               # confirm the org can serve models"
	@echo "  3. make bootstrap               # push + automations + seed + dataset"
	@echo "  4. make experiment              # replay the dataset"
	@echo
	@echo "Examples:"
	@echo "  make agent"
	@echo "  make ask Q=\"INC-4821 just paged me. What should I do first?\""
	@echo "  make automations ONCALL_READINESS_SAMPLING=0.2"

install: ## Install dependencies into .venv with uv
	uv sync

agent: install ## Start the interactive on-call session
	$(ENVSHIM) uv run oncall chat

ask: install ## Ask one question non-interactively: make ask Q="..."
	@if [ -z "$(Q)" ]; then \
		echo "Usage: make ask Q=\"your question\""; exit 2; \
	fi
	@$(ENVSHIM) uv run oncall ask "$(Q)" --show-trace

# --- deploy ---------------------------------------------------------------

push-prompts: install ## Push the agent + sub-agent prompts to Braintrust
	@# Pushed from inside prompts/: bt bundles Python functions relative to the
	@# file's own directory, and a path-qualified push breaks local imports.
	cd prompts && $(ENVSHIM) BRAINTRUST_PROJECT=$(PROJECT) bt functions push oncall_prompts.py \
		--project $(PROJECT) \
		--runner $(PYTHON) \
		--if-exists replace \
		--yes $(PUSHENV) $(BTORG)

push-scorers: install ## Push the three LLM judges and the three code scorers
	@# --runner points bt at the uv venv; the default runner uses system python,
	@# which does not have the braintrust SDK installed.
	cd scorers && $(ENVSHIM) BRAINTRUST_PROJECT=$(PROJECT) bt functions push judges.py \
		--project $(PROJECT) --runner $(PYTHON) --if-exists replace --yes $(PUSHENV) $(BTORG)
	cd scorers && $(ENVSHIM) BRAINTRUST_PROJECT=$(PROJECT) bt functions push code_scorers.py \
		--project $(PROJECT) --runner $(PYTHON) --if-exists replace --yes $(PUSHENV) $(BTORG)

push-facets: topics ## Push the custom Topics facets (alias for `make topics`)

push-all: push-prompts push-scorers topics ## Push prompts, scorers, and custom facets

# --- observability --------------------------------------------------------

automations: install ## Create/update Braintrust online scoring rules (REST API)
	$(ENVSHIM) uv run python scripts/automations.py

topics: install ## Enable Topics and push the four custom facets
	$(ENVSHIM) uv run python scripts/topics.py

verify-scoring: install ## Send one canary trace and wait until it comes back scored
	@$(ENVSHIM) uv run python scripts/verify_scoring.py

defect-report: install ## Per-defect failure frequency from root spans (DAYS=7)
	@$(ENVSHIM) uv run python scripts/defect_report.py --days $(DAYS) --verify

topics-status: install ## Show the Topics automation config
	@$(ENVSHIM) uv run python scripts/topics.py --status
	@echo
	@$(ENVSHIM) bt topics status --project $(PROJECT) $(BTENV) $(BTORG) || true

seed: install ## Generate agent traffic so Topics can cluster (COUNT=300 WORKERS=12)
	$(ENVSHIM) uv run python scripts/seed_traffic.py --count $(COUNT) --workers $(WORKERS) --seed $(SEED)

seed-failures: install ## Seed deliberately failing traffic so the failure monitor moves (FAIL_COUNT=24)
	@# A degraded build, driven through the Settings.system_prompt override rather
	@# than by editing prompt_library — nothing to revert, and every trace is
	@# tagged mode:degraded so it can be excluded from `make dataset`.
	$(ENVSHIM) uv run python scripts/seed_failures.py --count $(FAIL_COUNT) --workers $(FAIL_WORKERS) --seed $(SEED)

# --- evaluation -----------------------------------------------------------

dataset: install ## Promote production root spans into the eval dataset (DAYS=7)
	$(ENVSHIM) uv run python scripts/promote_dataset.py --days $(DAYS)

experiment: install ## Replay the production-trace dataset as a Braintrust experiment
	$(ENVSHIM) bt eval --language python --runner $(PYTHON) evals/oncall_agent.eval.py $(BTENV) $(BTORG)

remote-eval: install ## Serve the agent to Braintrust playgrounds as a remote eval (:8300)
	@# Aborts if BRAINTRUST_API_KEY belongs to another org — playground scorers
	@# authenticate with it, and would fail on a missing AI provider mid-run.
	@$(ENVSHIM) uv run python scripts/check_org.py
	@# Name the file. Bare `bt eval --dev` also serves the offline experiment,
	@# which has no parameters and shows no playground controls.
	$(ENVSHIM) bt eval --language python --runner $(PYTHON) evals/oncall_agent_remote.eval.py --dev $(BTENV) $(BTORG)

preflight: install ## Check the credential's org can serve models before seeding
	@$(ENVSHIM) uv run python scripts/preflight.py

bootstrap: install ## Seed a fresh org end to end: preflight, push, automations, seed, dataset
	@$(ENVSHIM) uv run python scripts/preflight.py
	@echo
	@echo "── pushing prompts, scorers, custom facets ──"
	@$(MAKE) --no-print-directory push-all
	@echo
	@echo "── online scoring rules ──"
	@$(MAKE) --no-print-directory automations
	@echo
	@echo "── verifying online scoring is live before spending a seed on it ──"
	@$(ENVSHIM) uv run python scripts/verify_scoring.py
	@echo
	@echo "── seeding $(COUNT) traces ──"
	@$(MAKE) --no-print-directory seed
	@echo
	@echo "── promoting root spans into the eval dataset ──"
	@$(MAKE) --no-print-directory dataset
	@echo
	@echo "Done. Topics needs ~100 traces and clusters asynchronously (up to an hour)."
	@echo "Next: make experiment    # replay the dataset against this build"
	@echo "      make topics-status  # watch facet extraction and clustering"

check-org: install ## Verify BRAINTRUST_API_KEY belongs to the project's org
	@$(ENVSHIM) uv run python scripts/check_org.py

eval-status: install ## Show pushed scorers, prompts, and online scoring rules
	@echo "── Prompts ──"
	@$(ENVSHIM) bt prompts list --project $(PROJECT) $(BTENV) $(BTORG) || true
	@echo
	@echo "── Scorers ──"
	@$(ENVSHIM) bt scorers list --project $(PROJECT) $(BTENV) $(BTORG) || true
	@echo
	@echo "── Online scoring rules ──"
	@$(ENVSHIM) uv run python scripts/automations.py --list

logs: ## Tail recent agent traces in Braintrust
	$(ENVSHIM) bt view logs --project $(PROJECT) $(BTENV) $(BTORG)

# --- housekeeping ---------------------------------------------------------

check: install ## Import-check every module
	uv run python -c "import oncall_agent.cli, oncall_agent.agent, oncall_agent.graph, \
oncall_agent.tools, oncall_agent.subagent, oncall_agent.remediation, oncall_agent.validation, \
oncall_agent.llm, oncall_agent.data, oncall_agent.tracing, \
oncall_agent.prompts, oncall_agent.prompt_library; print('all modules import cleanly')"

clean: ## Remove the virtualenv and caches
	rm -rf .venv **/__pycache__ .pytest_cache
