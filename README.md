# On-call Agent

A LangGraph incident-response agent for the fictional **Meridian** commerce
platform, instrumented end to end with Braintrust.

A responder gets paged. The agent reads the incident registry, the service
catalog, the deploy log, telemetry, runbooks, and past postmortems; delegates
"has this happened before" to a correlation sub-agent, "what do I change" to a
remediation sub-agent, and "does that change hold" to a validation sub-agent; and
comes back with a diagnosis, one concrete next action, an owner, and a way to
verify it worked.

The three sub-agents sit on **three model tiers**. The orchestrator routes and
attributes over tool results, so it runs the cheap tier; correlation ranks
precedent on the mid tier; drafting and testing a change run a reasoning model,
because that is where being confidently wrong costs a responder the most. The
tier is visible per span, so "what did the deep work cost, and how often did we
need it" is a question the traces answer.

Drafting and testing a fix are **simulated** — a dry-run against a staging
replica, deterministic, never applied. There is no production surface here.

The business goal is **time to resolution**, measured **across teams and failure
domains** — so every trace carries the team, service, severity, and domain it
touched, five scorers grade production traffic online, and four custom Topics
facets cluster live traffic into a reliability backlog.

## Quickstart

```bash
cp .env.example .env      # add BRAINTRUST_API_KEY and EXA_API_KEY
make install
make agent                # interactive session
```

```
› INC-4821 just paged me. What is going on and what should I do first?
```

## Commands

| Command | What it does |
| --- | --- |
| `make agent` | Interactive on-call session (REPL) |
| `make ask Q="..."` | One-shot, non-interactive |
| `make push-prompts` | Push the agent + sub-agent prompts to Braintrust |
| `make push-scorers` | Push the three LLM judges and the three code scorers |
| `make push-facets` | Push the four custom Topics facets |
| `make push-all` | All three of the above |
| `make automations` | Create/update the online scoring rules |
| `make seed` | Drive real agent traffic (`COUNT=300 WORKERS=12`) |
| `make dataset` | Promote production root spans into an eval dataset |
| `make experiment` | Replay that dataset against the current build |
| `make remote-eval` | Serve the agent to playgrounds on `:8300` |
| `make defect-report` | Per-defect failure frequency, derived from root spans |
| `make topics-status` | Show the Topics automation config |
| `make eval-status` | Show pushed prompts, scorers, and online rules |
| `make preflight` | Check the credential's org can serve models before seeding |
| `make verify-scoring` | Send one canary trace and wait until it comes back scored |
| `make bootstrap` | Seed a fresh org end to end |
| `make check-org` | Verify the API key's org owns the project |
| `make logs` | Tail traces |

### Seeding a fresh org

```bash
# put that org's key in .env first — see .env.example
make preflight     # can this credential actually run the demo?
make bootstrap     # push-all -> automations -> seed -> dataset
make experiment    # replay the dataset against this build
```

`make preflight` derives the org from the key and makes a real completion for each
model the demo needs, because a brand-new org has **no AI providers configured**
until someone adds one — and without a provider the agent, all three LLM judges,
and all three Topics facets fail together.

When a `.env` is present it is authoritative: the Makefile unsets the ambient
Braintrust/OpenAI variables and passes `--env-file` to `bt`, so a stale key
exported elsewhere in the same shell cannot quietly seed the wrong org. Details in
[CLAUDE.md](CLAUDE.md#seeding-a-fresh-org).

Individual steps, if you would rather run them by hand:

```bash
make push-all      # prompts, scorers, custom facets
make automations   # online scoring rules
make seed          # traffic, so Topics has something to cluster
make dataset       # promote root spans into an eval dataset
make experiment    # replay it against the current build
```

Package management is **uv**. Never use bare `pip` or `python` — use `uv run`,
`uv sync`, `uv add`.

## Architecture

```
cli.py            Typer CLI — REPL + one-shot `ask` (--json, --show-trace)
  └─ agent.py     OncallSession: owns the OpenAI client, drives the graph,
                  logs the root-span contract the scorers depend on
       └─ graph.py       LangGraph StateGraph: agent ⇄ tools ReAct loop
            └─ tools.py       12 tools (schemas + dispatcher)
                 ├─ data.py         synthetic incidents, services, deploys,
                 │                  telemetry, runbooks, postmortems
                 ├─ subagent.py     root-cause correlation sub-agent (mid tier)
                 │    └─ exa.py     Exa search client
                 ├─ remediation.py  draft + review the fix (reasoning tier)
                 └─ validation.py   plan + dry-run the tests (reasoning tier)
llm.py            One call site for both model families (temperature vs
                  reasoning_effort)
prompt_library.py Prompt text + the specs `make push-prompts` deploys
prompts.py        Resolves prompts: override → Braintrust → local literal
tracing.py        Braintrust setup + `traced_step` decorator
```

The trace tree, which is the point:

```
[task] oncall_agent                      ← root; carries the scorer contract
  [task] LangGraph
    [task] agent          → [llm] Chat Completion
    [task] tools
      [tool] tool.get_incident
      [tool] tool.get_alerts
      [tool] tool.get_deploys
      [tool] tool.query_telemetry
      [tool] tool.get_service_catalog
      [tool] tool.get_runbook
      [tool] tool.get_resolution_stats
      [tool] tool.search_incidents
      [tool] tool.search_web
      [tool] tool.correlate_root_cause
        [task] correlation_subagent
          [task] plan_hypotheses        → [llm] Chat Completion
          [task] gather_precedent
          [task] synthesize_correlation → [llm] Chat Completion
      [tool] tool.propose_fix
        [task] fix_subagent
          [task] assemble_change_context
          [task] draft_patch            → [llm] Chat Completion  (reasoning)
          [task] review_patch           → [llm] Chat Completion  (reasoning)
      [tool] tool.validate_fix
        [task] test_subagent
          [task] plan_test_suite        → [llm] Chat Completion  (reasoning)
          [task] execute_test_suite
          [task] summarize_test_results → [llm] Chat Completion  (reasoning)
```

## Evaluation

Six scorers grade production traffic online, and the same six run in the
offline experiment — that shared axis is what makes an experiment result
comparable to a production dashboard.

| Scorer | Kind | Role |
| --- | --- | --- |
| Diagnostic Accuracy | LLM judge | control — grounding, event ordering, dependency direction |
| Remediation Safety | LLM judge | control — credential leaks, irreversible actions (scored inverted: 1.0 is safe) |
| Resolution Readiness | LLM judge | **headroom** — the answer as a handoff, 7 requirements, lands 0.4–0.75 |
| Handoff Completeness | code | coverage floor, free, 100% of traffic |
| Defect Free | code | **failure rate** — binary, free, 100% of traffic; rate is `1 - mean` |
| Retrieval Grounded | code | zero-retrieval answers; composition-sensitive, read per slice |

Three instruments, three different questions. The judges say **how good** an
answer was. `Defect Free` says **how often** the agent fails. The
`agent_failure_mode` Topics facet says **in what ways** — a discovered taxonomy of
the agent's own defects, clustered and ranked by frequency, next to the three
facets that do the same for the platform's failures.

Two eval surfaces:

- **`make experiment`** — the pinned, reproducible comparison. Replays the
  promoted dataset, adds stock autoevals and an offline-only tool-efficiency
  proxy.
- **`make remote-eval`** — the knob-turning surface. Serves the live agent to a
  Braintrust playground so someone who will not edit Python can rewrite the
  orchestrator prompt, switch models, and watch tool selection and attribution
  react. Same `OncallSession.ask` entrypoint, so it produces the identical
  root-span contract.

## Topics

Three custom facets, all open-ended — the prompt fixes the theme and the output
shape, the model writes the value:

| Facet | Answers |
| --- | --- |
| `failure_domain` | How does incident load distribute across teams and domains? |
| `resolution_blocker` | What is stopping incidents from resolving faster? |
| `remediation_pattern` | Which fixes recur, and where? |

`failure_domain` leads because measuring effectiveness across teams and domains is
the goal. Cluster `resolution_blocker` and you get a ranked reliability backlog
mined from real traffic rather than from opinion.

Of the built-in facets only `Task` is kept — Sentiment and Issues matched 24 and 28
of 954 traces and generated no topics on this corpus. The Topics page pins built-in
facets above custom ones and that is not configurable, so dropping the dead ones is
also what moves these three up the page. See
[CLAUDE.md](CLAUDE.md#you-cannot-reorder-built-ins-below-custom-facets).

## The synthetic data

11 incidents (3 active), 13 services across 6 teams, 9 deploys, 11 alerts,
9 runbooks, 8 postmortems, and generated metric series. All fabricated, all
deterministic — no randomness and no wall clock, so demo runs reproduce.

The cross-links that make the demo work, and the deliberately unsafe runbook the
safety judge is there to catch, are documented in
[CLAUDE.md](CLAUDE.md#the-synthetic-data).
