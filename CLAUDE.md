# CLAUDE.md — On-call Incident Response Agent

Guidance for Claude Code when working in this repository.

## What this is

A LangGraph incident-response agent for a fictional commerce platform
(**Meridian**), instrumented end-to-end with Braintrust. A responder gets paged;
the agent correlates incident state, deploys, telemetry, ownership, runbooks, and
precedent, and returns a diagnosis with one concrete next action.

It exists as a **demo of an incident-management use case** whose immediate business
goal is **improving incident resolution speed**, and who needs to **measure that
effectiveness across teams and domains**. Those two sentences drive most of the
design decisions in here, and they are the standard to judge changes against:

- A change that makes the trace tree harder to read is a regression even if the
  code is fine.
- A change that removes the team/domain slicing dimension from a trace breaks the
  measurement story, which is the whole point.
- A change that makes the agent's answer less *actionable* (no owner, no
  verification step) is a product regression even when the diagnosis is correct.

## Commands

| Command | What it does |
| --- | --- |
| `make install` | `uv sync` into `.venv` |
| `make agent` | Interactive REPL |
| `make ask Q="..."` | One-shot, non-interactive |
| `make push-prompts` | Push the agent + sub-agent prompts to Braintrust |
| `make push-scorers` | Push the three LLM judges and the three code scorers |
| `make push-facets` | Push the four custom Topics facets (alias for `make topics`) |
| `make push-all` | All three pushes |
| `make automations` | Create/update online scoring rules via REST API |
| `make seed` | Drive real agent traffic so Topics can cluster |
| `make seed-failures` | Drive *deliberately failing* traffic so the failure monitor moves |
| `make dataset` | Promote production root spans into the eval dataset |
| `make experiment` | Replay that dataset as a Braintrust experiment |
| `make remote-eval` | Serve the agent to playgrounds as a remote eval (`:8300`) |
| `make defect-report` | Per-defect failure frequency, derived from root spans |
| `make topics-status` | Show the Topics automation config |
| `make eval-status` | Show pushed prompts, scorers, and configured rules |
| `make check-org` | Verify the API key's org owns the project |
| `make logs` | Tail traces |
| `make check` | Import-check every module |

Package management is **uv**. Never use bare `pip` or `python`; use `uv run`,
`uv sync`, `uv add`. The venv python is `.venv/bin/python`.

## Seeding a fresh org

```bash
# 1. put the target org's key in .env  (see .env.example)
make preflight     # can this credential actually run the demo?
make bootstrap     # push-all -> automations -> seed -> dataset
make experiment    # replay the dataset against this build
```

### `.env` is the authority, and making that true took work

The failure this guards against is seeding the wrong org. `load_dotenv()` **does
not override variables already in the environment**, so a `BRAINTRUST_API_KEY`
exported by another demo's `.env` earlier in the same shell silently wins — and you
discover it after 300 traces, four scorers, and six functions have landed somewhere
else.

Two mechanisms in the Makefile make `.env` win, both conditional on `.env`
existing (`DOTENV := $(wildcard .env)`), so behavior is unchanged without one:

- **`ENVSHIM`** — `env -u BRAINTRUST_API_KEY -u OPENAI_API_KEY -u OPENAI_BASE_URL
  -u EXA_API_KEY -u BRAINTRUST_ORG_NAME` in front of every credentialed recipe.
  With the ambient values gone, `load_dotenv()` has nothing to lose to.
- **`BTENV` / `PUSHENV`** — `--env-file .env` on every `bt` invocation. `bt` does
  not read `.env` on its own, and its credential order (explicit profile → env →
  saved config) means that once `ENVSHIM` removes the env var it would fall through
  to **a saved profile in a different org**. `PUSHENV` is the `../.env` variant for
  the two push targets, which `cd` into `prompts/` and `scorers/` first.

**`BTORG` must be `$(CURDIR)`-qualified.** `push-prompts` and `push-scorers` `cd`
into `prompts/` and `scorers/` before the recipe shell expands `$$(...)`, so a
relative `scripts/preflight.py` is not found from there, `2>/dev/null` hides the
error, and `bt` is handed `--org ""`:

```
error: failed to resolve auth context: invalid configuration: Organization '' not found
```

`PUSHENV` already accounted for the `cd` (`../.env`); `BTORG` did not. It is only
reachable when a `.env` exists — without one `BTORG` expands to nothing and `bt`
falls back to its saved profile — which is why it survived until the first real
fresh-org bootstrap and failed on the very first push.

Three things this exposed, all fixed:

1. **The standalone scripts never loaded `.env` at all.** `automations.py`,
   `topics.py`, `promote_dataset.py`, `check_org.py`, and `preflight.py` talk to the
   REST API directly and never import `oncall_agent.config`, which is the only
   place `load_dotenv()` ran. They worked purely because a shell happened to export
   the right key. Each now calls `load_dotenv()` itself.
2. **`bt`'s global flags only parse after the subcommand.** `bt --env-file .env
   prompts list` fails with `unexpected argument '--env-file' found`; `bt prompts
   list --env-file .env` works. The help text lists them under "Global options",
   which reads as though they were positional-anywhere. They are not.
3. **`bt status` does not tell you which credential a command will use.** Under
   `--env-file` with a bogus key it still cheerfully prints `org: <your org>
   / profile: <your org>` — it reports the saved *profile* context, not the
   effective credential. Use `make preflight`, which authenticates the key it was
   actually given.

Verified by putting a deliberately invalid key in `.env` while a **valid** key sat
in the ambient environment and a valid profile sat in `~/.config/bt`: `preflight`,
`eval-status`, `push-prompts`, and `push-scorers` all failed with 401 rather than
silently succeeding against the wrong org. That negative test is the only way to
know the precedence works — a passing run proves nothing, because the ambient key
would have produced the same result.

### The AI-provider prerequisite

`make preflight` (`scripts/preflight.py`) resolves the key to its org, then makes a
**real minimal completion** for each of the three model tiers (`ONCALL_MODEL`,
`ONCALL_SUBAGENT_MODEL`, `ONCALL_REASONING_MODEL`) and aborts if any fails.

This is the check that matters on a new org. A fresh org has **no AI providers
configured** until someone adds one, and without a provider every model call fails
with `no provider configured for '<model>' in org '<name>'` — which is the agent,
its three sub-agents, all three LLM judges, and all four Topics facets. Probing
every tier matters because a provider serving one but not another yields a
half-working demo, which is harder to diagnose than a clean failure.

The **reasoning tier is the one most likely to be missing**, and it fails
differently: an org with an OpenAI provider configured still needs that provider's
account to have o-series access, and the gateway reports that as a 404 rather than
as the familiar "no provider configured" message.

The probe itself has to change shape per family. A reasoning model rejects
`max_tokens` outright, and a budget of 1 is consumed by reasoning tokens before any
content is emitted — so those models get `max_completion_tokens: 16`. Only the
status code is checked, so an empty completion is still a successful probe.

The org name never has to be supplied: it is derived from the key. If the key sees
more than one org, preflight says which one it will use, because the scripts resolve
the org implicitly from the credential and would otherwise pick a default silently.

### Ordering

`make bootstrap` runs the steps in the only order that works on an empty project:
`push-all` first, because `project.prompts.create()` is what creates the project,
and `topics.py` / `automations.py` both start by resolving a project **by name** and
exit if it does not exist. `seed` then needs the scorers and facets already
attached, or its traffic lands unscored and unlabeled. `dataset` needs the traffic.

### An online scoring rule is not live the moment you create it

Correct ordering is not sufficient. `make automations` returns success as soon as
the rules are written, but the rule takes a minute or so to reach the ingestion
pipeline — and rules **never backfill**. Seeding immediately after creating them
produces a few hundred traces whose `scores` column is null forever, with nothing
anywhere to explain it: the rules are right, the traces are right, no score span is
ever created, and no error is logged.

This is exactly what happened on the first `make bootstrap` of a fresh org. Rules
were created at 19:26:29; the seed's traffic landed 19:27–19:29; all 300 traces
came back unscored. A canary sent at 19:51 against the *same* rules scored all four
immediately. Nothing was misconfigured — the traffic was simply early, and no
amount of waiting will fix traces that were missed.

So `bootstrap` gates the seed on `scripts/verify_scoring.py`: it sends one canary
turn (tagged `mode:canary`, so it stays out of the `mode:batch` dashboards) and
polls until that trace comes back scored, up to `--timeout` seconds. One wasted
agent run beats three hundred. Run it standalone with `make verify-scoring` any
time you have changed a rule and want to know it is actually in effect.

If a seed did land early, the traffic is not recoverable — re-seed. The unscored
traces are still fully usable for Topics, which reads facets rather than scores.

Cost, at `COUNT=300`: 300 agent runs, ~1500 scorer invocations (three judges plus
two free code scorers), and ~1200 facet extractions, plus the reasoning-tier calls on
the remediation-shaped share of the traffic. `make bootstrap COUNT=40` is a
sane smoke test; lower the sampling rates first if that matters
(`ONCALL_READINESS_SAMPLING=0.2`), and `ONCALL_REASONING_EFFORT=low` for the
reasoning tier.

## Architecture

```
cli.py            Typer CLI — interactive REPL + one-shot `ask` (--json, --show-trace)
  └─ agent.py     OncallSession: owns the OpenAI client, drives the graph,
                  logs the root-span contract the scorers depend on
       └─ graph.py      LangGraph StateGraph: agent ⇄ tools, plus finalize
            └─ tools.py      12 tools (schemas + dispatcher + entity extraction)
                 ├─ data.py         synthetic incident/service/deploy/telemetry/knowledge systems
                 ├─ subagent.py     root-cause correlation sub-agent      (mid tier)
                 │    └─ exa.py     Exa search client
                 ├─ remediation.py  draft the fix, then review it         (reasoning tier)
                 └─ validation.py   plan the tests, then dry-run them     (reasoning tier)
llm.py            The one call site for both model families — see "Two model
                  families, one helper"
prompt_library.py Prompt text + the deployment specs `make push-prompts` uses
prompts.py        Resolves a prompt: override → Braintrust → local literal
tracing.py        Braintrust setup + `traced_step` decorator
```

### Three model tiers, and why the orchestrator is the cheap one

| Tier | Env var | Default | Who runs it |
| --- | --- | --- | --- |
| orchestrator | `ONCALL_MODEL` | `gpt-4.1` | `graph.agent_node` |
| sub-agent | `ONCALL_SUBAGENT_MODEL` | `gpt-4o-mini` | `subagent.py` (correlation) |
| reasoning | `ONCALL_REASONING_MODEL` | `o3` | `remediation.py`, `validation.py` |

The orchestrator's job is routing and attribution over tool results: a lot of
tokens, not much reasoning. It is also the step that runs on **every** turn,
including the informational lookups that are most of the traffic. So it takes the
cheap tier, and the expensive tier is spent only on the two steps where being
confidently wrong is expensive — a plausible-looking patch, and a test suite that
passes for the wrong reason.

`ONCALL_REASONING_EFFORT` (`low` / `medium` / `high`, validated at load time) is
the cost knob on that tier. Drop it to `low` before a large seed rather than
downgrading the model: the pipeline shape stays the same, which is what the trace
tree is demonstrating.

This tiering is also a demo beat in its own right. Cost is per-span in Braintrust,
so the split makes "what did the deep work cost, and how often did we actually
need it" answerable from the trace tree — which is the question anyone deploying a
multi-agent system asks second, right after "does it work".

`ONCALL_MODEL=gpt-4.1-mini` drops the orchestrator another tier if the demo wants
a starker cost contrast; expect tool-selection quality to fall off, and watch
`diagnostic-accuracy` (the grounding control) rather than trusting the answer's
prose, since that is the pairing that caught the last two orchestrator defects.

### Two model families, one helper

Reasoning models **reject `temperature`** and take `reasoning_effort` instead, so
the two families cannot share a call site. `Settings.call_params(model)` resolves
that **from the model name**, not from which sub-agent is calling, and `llm.py`
(`complete`, `complete_json`) is the single place every sub-agent step goes
through.

Resolving it by name is what makes `ONCALL_SUBAGENT_MODEL=o4-mini` work rather
than 400 — and `ONCALL_REASONING_MODEL=gpt-4o` degrade to a normal sampled call
rather than error. Do not add a `temperature` to a call site; pass it to
`complete()` and let `call_params` drop it where it does not belong.

`complete_json` degrades an unparseable response to `{}` rather than raising: the
caller reports a thin result, which is a recoverable turn, where an exception
takes down the whole agent run.

### The OpenAI client is injected, never constructed downstream

This is a deliberate design constraint. The client is built **once** in
`agent.build_openai_client()` and passed into the graph through the invocation
config:

```python
self.graph.invoke(
    initial_state,
    config={"configurable": {"openai_client": self.client, "settings": self.settings}},
)
```

Nodes pull it back out via `graph._resolve(config)`. Do not construct an
`OpenAI()` inside a node, tool, or the sub-agent — it breaks client swapping for
tests, alternate endpoints, and evals.

The client points at the **Braintrust AI proxy** (`OPENAI_BASE_URL`) by default,
which is why `OPENAI_API_KEY` and `BRAINTRUST_API_KEY` can hold the same value.

### Sub-agent vs tool

Three of the twelve tools are **sub-agents**, not flat calls. Each runs its own
three-step pipeline, and each earns that for the same two reasons:

1. **Context isolation** — the orchestrator receives one synthesized artifact
   instead of the dozen raw record sets the step had to read.
2. **A nested subtree** — Braintrust shows the pipeline as its own subtree, so the
   step can be debugged and scored independently of the orchestrator, and its
   model tier is visible per span.

| Tool | Sub-agent | Pipeline | Tier |
| --- | --- | --- | --- |
| `correlate_root_cause` | `correlation_subagent` | hypothesize → gather precedent → synthesize | sub-agent |
| `propose_fix` | `fix_subagent` | assemble context → draft patch → review patch | reasoning |
| `validate_fix` | `test_subagent` | plan suite → execute suite → summarize results | reasoning |

**Correlation's** most useful output is not the ranking — it is that every cited
prior incident carries **its actual fix and its actual TTR**. "This is INC-4102
again; that one was resolved in 12 minutes by rolling back the batch-size change"
is worth more to a responder than any amount of hypothesis generation.

**Remediation's** is that the draft is grounded in the causal deploys, the
dependency closure, the reviewed runbook steps, *and* the precedent fix — and then
reviewed by a second reasoning pass that did not write it. The reviewer is what
turns "here is a plan" into "here is a plan and what is wrong with it": it checks
for destructive operations, leaked credentials, an understated blast radius, and
verification that is really just "monitor for errors".

**Validation's** is the verdict plus the failing test's *meaning*. `ship`,
`canary`, or `hold` is the line a responder acts on; "the load test fails because
reverting `dep-9f31c2a` reinstates the write amplification it shipped to fix" is
the sentence that stops them from being surprised in an hour.

Read together, the three are the demo's answer to the business goal. Correlation
shortens diagnosis; remediation shortens the gap between diagnosis and a change;
validation is what makes the change shippable instead of merely plausible. Time to
resolution is the sum of those three, and the trace shows each one separately.

#### The remediation draft is reviewed once, not iterated

A revise loop was the obvious next move and is deliberately absent: it doubles the
reasoning spend on every call to buy a second draft the orchestrator can already
ask for. The reviewer's `required_changes` go into the answer instead, which is
what a human reviewer's comments would do anyway.

#### Everything the two new sub-agents do is simulated

There is no production surface in this repo to change, and both results say so in
three places — `simulated: true`, `applied: false`, `environment:
"staging-replica (dry-run)"` — with the prompts and the tool descriptions
repeating it.

That redundancy is not decoration. The one failure mode that actually matters here
is an answer a tired responder reads as "it's already fixed", because a responder
who believes a fix has shipped stops watching the incident. `ORCHESTRATOR_PROMPT`
carries the rule explicitly, and `remediation-safety` is the control that would
catch a regression in it.

`remediation.py` also never passes a runbook's `raw_steps` into a drafting prompt —
only the reviewed step summaries. RB-031's raw steps contain a hardcoded
production credential, and a drafting prompt is exactly how that credential would
end up inside a diff.

#### The test run is deterministic, and that is the whole point

`execute_test_suite` makes **no model call**. Outcomes are derived, for the same
reason `data.py` has a frozen clock and generated metric series: every number in
the report has to be derivable from the evidence in the same trace, or a grounding
judge has nothing to check against.

Be precise about what is deterministic. The **suite is model-generated**, so a
rerun of the same question can produce different test names and a different
verdict. What is fixed is the mapping from a suite to its results — the same suite
always yields the same outcomes, durations included. That is the property the
report rests on, and it is why nothing here samples. If you need a fully
reproducible validation result for a screenshot, pin the orchestrator prompt and
re-run the same trace rather than expecting two runs to agree.

Three rules, in precedence order:

1. **Telemetry tests** are answered from `data.telemetry` — the real pre-change
   anomaly peak against the metric's real baseline. The post-change value is
   simulated as a return to baseline plus a hash-derived residual, and the test
   passes on the same 25% deviation threshold `data.telemetry` itself uses to call
   a window anomalous, so "the test says recovered" and "the telemetry tool says
   normal" cannot disagree. A test naming a metric that does not exist reports
   `status: "error"` — that is a real finding about the suite, not a failure of the
   change, and hiding it would make the suite look better than it is.
2. **A load test against a rollback** is answered mechanically. Reverting a change
   that shipped to improve something reinstates the condition it was shipped to
   improve, and `_OPTIMIZATION_CUES` picks that out of the deploy's own summary.
   It is **non-blocking on purpose**: a `hold` verdict here would argue for leaving
   a SEV1 burning to avoid reintroducing a write-amplification regression. The
   responder needs the tradeoff named, not the mitigation blocked.
3. **Everything else** hashes the test's identity with the incident and the change
   and compares against odds set by the change's own risk — high-risk changes fail
   more, changes with precedent fail less, load tests fail more.

The verdict is computed from those results (`hold` on any blocking failure,
`canary` on a non-blocking one, else `ship`) and handed to the report prompt as
**"deterministic, not yours to change"**. The model writes the prose; it does not
get a vote on the number.

On the flagship INC-4821 this lands on `canary`: the telemetry checks recover, the
regression against INC-4102 passes, and the load test fails non-blockingly with
the write-amplification tradeoff spelled out. That is a better demo than a clean
pass — a validation step that always says `ship` is a rubber stamp, and looks like
one.

If you tune `_ODDS_BY_RISK`, check the flagship still reads that way — the load
test rule is what carries it, so it holds across suites, unlike the hash path.

## Tracing contract — do not break this

`setup_tracing()` calls `braintrust.auto_instrument(openai=True, langchain=True)`.

**Do not also call `wrap_openai()`.** Doing both produces two identical
`Chat Completion` spans per call — double the payload for no extra signal.

### Span types

The type hierarchy is intentional and load-bearing for online scoring:

```
[task] oncall_agent                      ← root; carries the scorer contract
  [task] LangGraph
    [task] agent          → [llm] Chat Completion
    [task] finalize       → [llm] Chat Completion   (only when the budget runs out)
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
          [task] draft_patch            → [llm] Chat Completion   (reasoning)
          [task] review_patch           → [llm] Chat Completion   (reasoning)
      [tool] tool.validate_fix
        [task] test_subagent
          [task] plan_test_suite        → [llm] Chat Completion   (reasoning)
          [task] execute_test_suite
          [task] summarize_test_results → [llm] Chat Completion   (reasoning)
```

`assemble_change_context` and `execute_test_suite` make **no model call** and are
still spans: the first is where a wrong change plan gets explained (it names the
candidate deploys, the precedent, and the runbooks the draft was given), and the
second is where a verdict gets explained. A pipeline whose deterministic steps are
invisible reads as though the model decided everything.

Rules:
- The root span is **`task`**.
- Everything the model can invoke — every span under the `tools` node — is
  **`tool`**, named `tool.<tool_name>`.
- Internal pipeline steps are **`task`**.
- Use `@traced_step(name=..., type=...)` from `tracing.py`, not bare `@traced`.
  `traced_step` strips `client`, `settings`, `exa`, and `self` from the span
  input; bare `@traced` logs them as useless `<openai.OpenAI object at 0x...>`
  reprs and bloats every span.

### The root-span contract

`OncallSession.ask` logs a deliberate shape that the scorer prompts template
against. **Changing it breaks the online scorers silently.**

| Field | Value |
| --- | --- |
| `input` | the responder's question, a plain string |
| `output` | the agent's answer, a plain string |
| `metadata.evidence` | flattened tool results the answer must be grounded in (truncated) |
| `metadata.evidence_refs` | every record ID the tools returned, from the **untruncated** results — what `Defect Free` checks the answer's references against |
| `metadata.tool_calls` | ordered list of tool names invoked |
| `metadata.sources` | citation list from the sub-agent |
| `metadata.incident_id`, `.service`, `.team`, `.severity`, `.domain` | the **primary** incident context |
| `metadata.incidents`, `.services`, `.teams`, `.domains` | everything the turn touched |
| `metadata.incident_status`, `.incident_ttr_minutes` | the primary incident's real timings |
| `metadata.model`, `.subagent_model`, `.reasoning_model`, `.reasoning_effort` | the three model tiers, logged on every turn even when it never reached one |
| `metadata.turn`, `.iterations`, `.tool_call_count` | run context |
| `metadata.budget_exhausted`, `.max_tool_iterations` | whether the turn ran out of tool budget and was forced to answer by `finalize` — an answer from a half-finished investigation, and worth being able to count |
| `metadata.prompt_source` | `local`, `braintrust`, `local_fallback`, or `override` |
| `metadata.agent_version`, `.environment`, `.mode` | deployment identity (also tagged) |
| `tags` | `env:*`, `agent:v*`, `mode:*`, `team:*`, `sev:*` |

Output is a bare string rather than the `AgentResponse` dict so scorer templates
can use `{{output}}` directly and stay robust to response-shape changes. Evidence
is truncated (`MAX_EVIDENCE_CHARS`) to keep trace payloads bounded.

`MAX_EVIDENCE_PER_TOOL` was raised from 3,500 to 5,000 for the remediation and
validation sub-agents. A change plan plus its review plus eight test results is the
largest single tool result here, and at 3,500 the part that got cut was the
verification signals — which is precisely what `resolution-readiness` and
`handoff-completeness` look for. A cap that truncates the evidence a scorer grades
against makes the scorer measure the cap.

### Entity extraction is why the measurement story works

"Is response getting faster, and for whom" is unanswerable unless every trace
states which team and which failure domain it was about. So `tools.entities_from`
derives that from each tool call's **arguments and results** — not from the model,
and not by parsing the answer text.

`agent._primary_context` then reduces the set to one value per trace, because a
dashboard needs a scalar, not a set:

- If an **incident** was touched, its record is the authority. An incident already
  states its own team, domain, and severity; nothing needs to be inferred.
- Otherwise, a field is promoted only when the turn touched exactly **one** value
  for it.
- Anything genuinely ambiguous is left **absent** rather than guessed at. A
  dashboard with a gap is honest; a dashboard that is quietly wrong is not.

`metadata.incident_ttr_minutes` is carried deliberately: it lets you plot the
agent's scores against the incident's real historical resolution time, which is
the closest this demo gets to showing the business metric moving.

### Tags

Every root span carries deployment identity from `Settings.trace_tags()`:

```
env:prod   agent:v1.0.0   mode:oneshot   team:payments   sev:SEV1
```

- `env` — `ONCALL_ENV`, one of `prod` / `staging` / `dev`. **Validated at load
  time**: an unrecognized value raises rather than silently splitting traces
  across two tag spellings.
- `agent` — `ONCALL_AGENT_VERSION`, defaulting to `__version__` in
  `__init__.py` (kept in sync with `pyproject.toml`).
- `mode` — `interactive` (REPL) vs `oneshot` (`oncall ask`); `batch` for seeded
  traffic, `eval` for the offline experiment, `playground` for remote-eval runs.
- `team` and `sev` are added **only when resolved**, capped at `MAX_ENTITY_TAGS`.
  They are tags as well as metadata because "show me every payments SEV1" is a
  filter a responder types, not an aggregation they run.

The model is deliberately **not** tagged. It is already on every LLM span and in
`metadata.model`, where it can be grouped and compared; a tag would only duplicate
it as a membership test.

Tags are re-logged at the end of the turn with the entity tags appended. Logging a
field twice overwrites it, so the second call sends the **superset** — the initial
call exists so a run that crashes mid-turn is still attributable.

**Query gotcha:** `INCLUDES` is BTQL syntax and is *not* supported in SQL mode —
`WHERE tags INCLUDES 'env:prod'` fails to parse. Use BTQL pipe syntax, or `MATCH`
as the SQL-mode approximation:

```bash
bt sql "select: * | from: project_logs('<id>') | filter: tags INCLUDES 'team:payments'"
bt sql "SELECT * FROM project_logs('<id>') WHERE tags MATCH 'team:payments'"
```

## Prompts are deployed, not just checked in

`make push-prompts` pushes **seven** prompts to Braintrust: the orchestrator's
system prompt, the two correlation sub-agent prompts, and the four belonging to the
remediation and validation sub-agents (`fix-change-plan`, `fix-safety-review`,
`fix-test-plan`, `fix-test-report`).

Each `PromptSpec` names its own tier in `model`, because that field is what the
Braintrust UI runs when someone opens the prompt in a playground — a spec that
named the cheap model would make a UI run behave differently from the agent's own
call. The four reasoning specs carry `params={"reasoning_effort": ...}` and **no
temperature**, for the same reason: a deployed prompt that disagrees with the
runtime call fails with a 400 the moment someone presses Run.

**The text is defined once**, in `prompt_library.PROMPT_SPECS`. `prompts/oncall_prompts.py`
pushes from that definition rather than restating it, so the deployed prompt and
the checked-in prompt cannot drift. Do not inline prompt text into the push script.

At runtime `prompts.resolve()` picks a source, in precedence order:

1. `Settings.system_prompt` — set only by the remote eval, so a playground run
   drives the agent without a redeploy. Recorded as `override`.
2. Braintrust, when `ONCALL_PROMPT_SOURCE=braintrust` — the deployed version, so a
   UI edit takes effect on the next run. Recorded as `braintrust`.
3. The literal in `prompt_library` — the default. Recorded as `local`.

A Braintrust fetch that fails falls back to the literal and records
**`local_fallback`**, and that distinction is the reason `metadata.prompt_source`
exists at all: a prompt that silently fell back is otherwise indistinguishable
from one that loaded, and you would be comparing experiment arms that ran
different text.

The demo beat: `make push-prompts`, set `ONCALL_PROMPT_SOURCE=braintrust`, edit
the prompt in the UI, re-run, and watch `metadata.prompt_source=braintrust` with
different behavior and no code change.

**Push from inside the directory.** `cd prompts && bt functions push oncall_prompts.py`,
not `bt functions push prompts/oncall_prompts.py`. `bt` bundles Python functions
relative to the file's own directory, and a path-qualified push breaks local
imports. Same for `scorers/`. The Makefile already does this.

## Evaluation

Six scorers, all pushed to Braintrust, all used by both online scoring and the
offline experiment. That shared axis is the point: without it you cannot tell
whether a prompt change helps or hurts the metrics you alert on.

| Scorer | Slug | Kind | Scoring | Role |
| --- | --- | --- | --- | --- |
| Diagnostic Accuracy | `diagnostic-accuracy` | prompt | A=1.0 … E=0.0; E for fabrication | control |
| Remediation Safety | `remediation-safety` | prompt | A=1.0 safe, B=0.5, C=0.0 unsafe | control |
| Resolution Readiness | `resolution-readiness` | prompt | A=1.0 … E=0.0, 7-requirement rubric | **headroom** |
| Handoff Completeness | `handoff-completeness` | code | fraction of 4 structural checks | coverage floor |
| Defect Free | `defect-free` | code | 1.0 clean, 0.0 any hard defect | **failure rate** |
| Retrieval Grounded | `retrieval-grounded` | code | 1.0 retrieved, 0.0 zero tools | composition-sensitive trend |

Remediation Safety is scored **inverted**: 1.0 means safe. A dashboard showing
"Remediation Safety = 1.0" is good news. `Defect Free` follows the same
convention — 1.0 means no defect fired — so every scorer here points the same way
and a drop is always the bad direction.

**Do not put the raw comparison in a chart title.** "Failing sessions (Defect Free
= 0)" reads naturally as "zero defects", which is the opposite of what it means,
and it misled the person who had just written the scorer. Name the quantity you
are plotting ("Session failure rate") and leave the inversion in the measure
expression where nobody has to parse it. An inverted score is fine as a metric and
a trap as a label.

### Controls vs the headroom metric — don't conflate them

The first two are **controls**: a healthy agent sits near 1.0 and a drop means
something broke. Alert on them.

`resolution-readiness` is a **headroom metric** and is deliberately strict. It
grades the answer as a *handoff* against an incident-commander standard where
seven independent requirements must all hold to score 1.0. Expect production
answers in the **0.4–0.75** band. That is the point: it moves when you improve the
prompt, and it leaves visible room to improve.

This is also the scorer most directly tied to the business goal. Accuracy is
necessary but not sufficient — a correct answer that leaves the responder to work
out who to page, what to run, and how to tell whether it worked has not shortened
resolution time at all.

Do not "fix" it by loosening the rubric. A readiness score reading 1.0 everywhere
has stopped measuring anything. Two things were tuned and should not be casually
reverted:

- **Minimum-applicability floor.** Requirements that don't apply count as met, so
  without a floor a trivial ownership lookup swept every requirement to N/A and
  scored 1.0. Grade A now requires at least four requirements to have been
  applicable.
- **Mechanical band mapping.** The rubric ends in an explicit UNMET→grade lookup
  table. Without it the judge reasoned "1 unmet" and then picked the grade for 2.

Requirements 1 (a single next action) and 7 (per-claim attribution) are **never
N/A** — every answer either gives the responder something to do and says where it
came from, or it does not.

#### Measured: this judge's `choice` disagrees with its own rationale ~1 turn in 3

Do not treat a single readiness score as a fact about one answer. Over 6
consecutive production score spans, **2 disagreed with their own reasoning**:

| Rationale concludes | Recorded `choice` | Score |
| --- | --- | --- |
| "...leading to a grade of A." | A | 1.0 |
| "...minimum-applicability floor." (B) | B | 0.75 |
| "APPLICABLE: 7 UNMET: 2  Grade: C" | C | 0.5 |
| "...the answer receives a **C** grade." | **B** | 0.75 |
| "APPLICABLE: 7, UNMET: 0 ... should receive a grade of **A**." | **E** | **0.0** |
| "...therefore the grade is A." | A | 1.0 |

The last row is the flagship answer — seven of seven requirements MET by the
judge's own enumeration, scored **0**.

It is not a text-parsing bug. Rows 1 and 6 end with the same "grade of A."
phrasing and resolved correctly. With `use_cot=True` the rationale and the
`choice` are **separate fields**, and the model fills the choice independently of
the chain of thought it just wrote.

The two controls are unaffected — 6 of 6 consistent on both, checked in the same
batch. What distinguishes readiness is the bookkeeping: enumerate seven
requirements, produce two counts, then look the pair up in a table. The controls
are short rubrics with no counting and they do not drift.

Consequences, in order:

1. **This is why readiness is a trend metric and not an alarm.** CLAUDE.md said
   that already on cost grounds; the real reason is ~33% label noise. A single 0
   means nothing. Read the mean over a slice.
2. **Never demo a single readiness number.** It can read 0 on the best answer the
   agent produces.
3. Do not "fix" it by reverting the mechanical mapping table — that fixed a
   different and worse problem (the judge reasoning "1 unmet" then picking the
   grade for 2).

Unvalidated but worth trying, in this order: move the counts into the structured
output so the choice is a function of fields the model already committed to; or
split the rubric into per-requirement boolean scorers and average them, which
removes the letter-grade indirection entirely. Either needs a measured before and
after over 20+ traces per arm — the noise is ~33%, so a handful of runs proves
nothing.

### Rate, quality, and taxonomy are three different instruments

Worth stating plainly, because the obvious mistake is to expect one of them to do
another's job:

| Question | Instrument | Why not the others |
| --- | --- | --- |
| How good was this answer? | the three judges | graded, per answer, subjective — a mean of grades is not a rate |
| How often does the agent fail? | `defect-free` (rate = `1 - mean`) | judges grade holistically, so a single fabricated ID inside a good answer does not move them |
| In what ways does it fail? | `agent_failure_mode` Topics facet | a scorer only ever finds the failures someone already wrote a check for |

The judges were here first and are still the quality signal. What they cannot do
is count: `diagnostic-accuracy` at a mean of 0.97 does not tell you whether that
is one catastrophic answer in thirty or thirty slightly-hedged ones, and it scored
**1.0** on an answer of this repo's own containing a fabricated deploy ID.

And no set of scorers gives a taxonomy — a code scorer finds only the classes
someone anticipated, and a judge returns a grade rather than a label. Discovering
"the agent keeps burying the next action" without having guessed it first is what
the facet is for. See "Topics and the custom facets".

### Why code scorers sit next to three judges

`handoff-completeness` is free and never drifts, so it runs at 100% while the
judges can be sampled down. It measures **presence, not quality**: does the answer
cite a record ID, name a pageable owner, give a concrete operational action, and
state a verification signal.

An answer can score 1.0 there and still be wrong — that is what the judges are
for. What it catches is the *shape* regression: a prompt change that quietly stops
the agent citing record IDs, or turns "roll back dep-9f31c2a" back into
"investigate the database". That shows up in a deterministic signal long before
anyone reads a trace.

Its handler must stay **self-contained**. Pushed code scorers run in an isolated
serverless sandbox with no access to this repo, so the team list and cue
vocabularies are inlined rather than imported from `oncall_agent`.

**It must return a bare number.** Online scoring logs a code scorer's return value
directly as the score and rejects the `{"score": ..., "metadata": ...}` dict form
that works fine in an offline `Eval`:

```
Cannot log {"score":0.75,"metadata":{...}} as a score
```

That failure is easy to miss, because it is not a push failure and not a run
failure: the push succeeds, the invoke succeeds, the trace scores normally on
every *other* scorer, and this one simply has no score column. The error lives on
the score span, so the way to find it is to query score spans rather than root
spans:

```bash
bt sql "SELECT created, error FROM project_logs('<id>') \
        WHERE created > CURRENT_TIMESTAMP - INTERVAL 1 DAY \
          AND span_attributes.name = 'Handoff Completeness' LIMIT 5"
```

The per-check breakdown therefore goes onto the score span with
`braintrust.current_span().log(metadata=...)` instead of into the return value —
same information, in a place the platform accepts. After re-pushing a code scorer,
re-run `make automations`: the rule resolves the function by slug, and the first
trace or two may still score against the previous version.

**Expected band: don't read the absolute number as a grade.** Measured over seeded
traffic it sits around **0.40 overall**, and that is healthy, not broken — most of
that traffic is informational or blocker questions that legitimately have no action
to recommend and nothing to verify. It splits cleanly by question type:

| Traffic | Handoff Completeness |
| --- | --- |
| actionable ("what should I do first", "should I roll it back") | ~0.56 |
| informational ("show me", "who owns", "what is the trend") | ~0.27 |

Read it as a **distribution and a per-slice trend**, not a target. A drop in the
actionable slice is the signal; the overall mean mostly tracks traffic mix.

Judge prompts use **triple braces** (`{{{output}}}`). Mustache HTML-escapes double
braces, which mangles the `<`, `>`, `&`, and quoting that appear constantly in
commands, log lines, and JSON payloads.

`make push-scorers` passes `--runner $(PYTHON)`. Without it, `bt` invokes system
python, which has no `braintrust` module, and the push fails.

### Online scoring

`scripts/automations.py` creates the rules, because `bt` cannot. It uses
`PUT /v1/project_score` (create-or-replace by name) rather than `POST`, which
returns the existing rule unchanged and makes re-runs a silent no-op.

All rules target `apply_to_root_span: true` — the root span is the only one
carrying the input/output/evidence contract the scorer prompts expect. Scoring all
spans would run the judges against tool spans they aren't written for.

Sampling defaults to 1.0 everywhere. Under volume, lower in this order:

1. `ONCALL_READINESS_SAMPLING` — most expensive judge, and a trend metric rather
   than an alarm, so it tolerates sampling best.
2. `ONCALL_ACCURACY_SAMPLING` — quality control.
3. `ONCALL_SAFETY_SAMPLING` — leave at 1.0. It is a production-safety control.
4. `ONCALL_HANDOFF_SAMPLING` — leave at 1.0. Code scorer, no model cost.
5. `ONCALL_DEFECT_SAMPLING` — leave at 1.0, and for a second reason beyond cost:
   it is the failure *rate*, and a rate estimated from a sample is a worse number
   for no saving at all.
6. `ONCALL_RETRIEVAL_SAMPLING` — free code scorer, so no cost reason to lower it.

Scores take ~10-60s to appear on a trace after the run completes, and rules apply
only to **new** traffic — creating a rule does not backfill.

## Topics and the custom facets

`make topics` (`scripts/topics.py`) enables Topics and pushes four custom facets.

### Open-ended, not enum classification

**All four facets are deliberately open-ended.** The prompt fixes the *theme*,
the *granularity*, and the *output shape* — the model writes the value. This is
the point: the topic map clusters emergent free-text summaries into topics nobody
had to predict in advance. A fixed enum would only ever reproduce the categories
we guessed at authoring time, which is strictly less informative than the traffic.

Keep prompts anchored with 4-6 example outputs instead — examples control
granularity without constraining vocabulary.

The table is in **Topics page order**.

### You cannot reorder built-ins below custom facets

The page pins built-in facets **above** custom ones, in a fixed platform order, and
nothing in the automation changes that. There is no `position` field on a facet
ref, no ordering flag on `bt topics config`, and reordering the `facet_functions`
and `topic_map_functions` arrays has no effect on what renders. It is not creation
order either — `Task`'s classifier was created *after* all the custom facets and
still displayed first.

So the array order in `patch_automation` is **documentary, not functional**. It is
written in the order the page actually renders, so the code reads like the screen.

**Facet order on the page is not controllable at all**, built-in or custom. The
documented lever (the order of `FACETS`, hence the order of the config arrays) does
not move the page either: `agent_failure_mode` was **last** in `facet_functions`
*and* the newest by creation timestamp, and still did not render last. There is no
`position` field on a facet function. If a facet needs to be out of the way, the
only real options are to rename it (names are immutable, so that means delete and
recreate) or to stop attaching it.

What *is* controllable:

- **Dropping a built-in.** `KEEP_BUILTIN_FACETS` in `scripts/topics.py` lists the
  built-ins to re-send in the patch; `bt topics config enable` turns on all three
  and the ones omitted from that list are simply never patched back. This is the
  only lever that moves a custom facet up the page.
- **The relative order of the custom facets**, via the order of `FACETS`. They are
  created in that order and sent in that order, so whichever rule the platform
  applies, the two agree.

**Sentiment and Issues are deliberately dropped.** Measured over 954 traces they
matched 24 and 28 respectively and generated **zero** topics — they are noise on an
incident corpus, and they were holding the second and third slots on the page.
`Task` is kept: 946/954 matched and 434 topics labeled, so it earns its slot even
though it is generic. Re-add them by putting them back in `KEEP_BUILTIN_FACETS`.

| Facet | Output shape | Answers |
| --- | --- | --- |
| `failure_domain` | `<team> / <failure class>` | How does incident load distribute across teams and domains? |
| `resolution_blocker` | lowercase noun phrase, 3-8 words | What is stopping incidents from resolving faster? |
| `remediation_pattern` | lowercase verb phrase, 2-6 words | Which fixes recur, and where? |
| `agent_failure_mode` | lowercase noun phrase, 3-8 words | Where is the agent itself falling short, and how often? |

`failure_domain` leads the custom facets deliberately. It is the cross-team
measurement axis, and "measure effectiveness across teams and domains" is the
stated business goal — so it is the first thing after `Task` rather than the last.

`resolution_blocker` is the actionable one: cluster it and you get a ranked
reliability backlog mined from real traffic rather than from opinion. Every cluster
is a missing data source, a missing alert, or a stale runbook, sized by how often
responders hit it.

What `failure_domain` buys, concretely: it turns "is response getting faster" into
"for whom, and on what kind of failure" — the question a platform team has to
answer before it can prioritize anything.

`remediation_pattern` is the one that surprises people: cluster it and the
recurring mitigations become visible as a set. A large "roll back correlated
deploy" cluster is an argument for automated rollback; a large "escalate to
secondary oncall" cluster is an argument that the primary rotation is
under-equipped.

### `agent_failure_mode` — the facet whose subject is the agent

The first three facets describe the **platform**, and whoever owns reliability
reads them. `agent_failure_mode` describes the **agent**, and whoever owns the
agent reads it. That is why it sits last rather than interleaved: different
subject, different audience.

Clustering it turns "is the agent any good" into a ranked list of the ways it
actually falls short, sized by how often each happens — the same move
`resolution_blocker` makes for the platform.

**The line against `resolution_blocker` is load-bearing.** An agent that correctly
reports it lacks feature-flag history is a **blocker** — a thing to go build. An
agent that bluffs an answer instead is a **failure** — a thing it did wrong. Blur
those two and the facet fires on every hard question, at which point it measures
difficulty rather than quality. The prompt states the distinction twice and lists
the "handled a limit well" case as an explicit NONE.

**It is scoped to what the transcript shows.** Like the others it runs on the
`thread` preprocessor, so it sees the question and the answer but *not*
`metadata.evidence`. It reliably catches **shape** defects — no owner, no
verification signal, an answer that argues against itself, an answer whose next
action is buried. It catches **grounding** defects only when they are self-evident
from the text; checking a claim against the retrieved record is `Defect Free`'s
job and `diagnostic-accuracy`'s. Point this facet at the raw span instead of
`thread` if you want it doing grounding work, and expect a longer, costlier call.

#### It needs a better model than the other three, and that is measured

The other three facets *describe* what they see. This one makes a negative
judgment against several criteria with a NONE default, which is materially harder.
Measured over 6 known-good and 4 known-defective transcripts:

| Facet model | False positives on the 6 good | Defects caught of 4 |
| --- | --- | --- |
| `gpt-4o-mini` | 1 — "next action buried in long answer" on a good remediation answer | 4 |
| `gpt-4.1-mini` | 0 | 4 |
| `gpt-4o` | 0 | 4 |

So `agent_failure_mode` carries its own `model` key
(`ONCALL_FAILURE_FACET_MODEL`, default `gpt-4.1-mini`) and `create_facet_functions`
honors a per-facet override. The other three stay on `gpt-4o-mini`, where
CLAUDE.md's existing measurements were taken — a global model bump would have
changed all four to fix one.

A facet that finds fault in good answers measures nothing, so that single false
positive is the whole reason for the extra cost. Two earlier iterations are worth
not repeating:

- **The severity list biases the output.** With "simulated fix described as
  applied" among the example outputs, every good remediation answer got that
  label — the model pattern-matched "this answer is about a simulated fix" and
  reached for the nearest phrase. Whether a simulation was disclosed is
  mechanically checkable, so it moved to `Defect Free` and left the facet
  entirely. Examples anchor granularity; they also anchor *content*, so do not put
  a check in the examples that a regex can do.
- **Seven criteria with no NONE bias makes the model find something.** Before the
  explicit "**Default to NONE** … if you are choosing between several mild
  possibilities, the answer is NONE" paragraph, good answers drew "recommended
  action without named owner" on answers that named the owner. Facets are
  extraction prompts, and asking one to grade against a rubric is judge work —
  either bias it hard toward NONE or use a judge.

Verified live, by invoking each facet function directly rather than waiting for
the async pipeline (`POST /function/invoke` with the facet's slug):

| Trace | `failure_domain` | `resolution_blocker` | `remediation_pattern` |
| --- | --- | --- | --- |
| "Show me the Kubernetes events for notify-dispatcher around INC-4803" (agent cannot) | `notifications / third party rate limiting` | `missing raw cluster event stream` | `no_match` |
| "Is there a deploy that correlates with INC-4821? Should I roll it back?" | `payments / database connection saturation` | `no_match` | `roll back correlated deploy` |
| "Who owns index-builder?" | `search / team ownership query` | `no_match` | `no_match` |

Invoking the facet directly is the fastest way to check a prompt change — facet
extraction plus clustering runs asynchronously and takes up to an hour, so
iterating against the Topics page is not a workable loop.

### The NONE / `no_match_pattern` mechanism

Each prompt ends with an instruction to output literal `NONE` when the facet does
not apply — a fully resolved question has no blocker, an informational question has
no remediation. The facet's `function_data.no_match_pattern` is set to `"^NONE"`,
which makes Braintrust store those outputs as `no_match` and **exclude them from
topic generation**.

This matters more than it looks. Most traces have no blocker, so without the
pattern `NONE` becomes the single largest cluster and drowns everything else.

The field name is `no_match_pattern`, documented in the SDK's generated types
(`braintrust/_generated_types.py`, `BatchedFacetDataFacet`) rather than in the API
docs. Note the API does **not** validate `function_data` keys — a misspelled field
is stored silently and simply never takes effect, so verify behavior rather than
assuming a write succeeded.

### Facet mechanics — two functions, don't mix them

A custom facet needs **two** Braintrust functions:

1. **Facet function** (`function_type="facet"`) — runs the extraction prompt.
   Created with `POST /v1/function`.
2. **Topic map function** (`function_type="classifier"`,
   `function_data.type="topic_map"`, `source_facet=<facet name>`) — clusters the
   extracted labels. Created with `POST /insert-functions`.

The automation's `facet_functions` lists extractors; `topic_map_functions` lists
classifiers. Putting a facet function ID into `topic_map_functions` renders as
broken null entries with "restore facet" buttons in the UI.

Other gotchas:
- Do **not** pass custom facets to `bt topics config enable`. The CLI inserts them
  into `topic_map_functions` as raw facet refs and breaks the UI. Enable with
  built-ins only, then attach the custom facets via the API patch — and use that
  patch to drop the built-ins you do not want, since `enable` always turns on all
  three.
- `POST /api/project_automation/patch_id` **replaces** the config rather than
  merging, so the built-in global facet refs and classifiers must be read back and
  re-sent alongside the custom ones. Global refs need `function_type: "facet"` in
  the patch body or the built-ins render broken.
- The facets use the global `thread` preprocessor so they see a conversation
  transcript rather than raw span JSON.
- Clustering needs ~100 traces and runs asynchronously (usually within an hour),
  so a fresh project shows an empty Topics page for a while. That is expected, not
  a misconfiguration.
- **The ~100 threshold applies to the *labeled* pool, not the trace count.**
  `no_match` is excluded from topic generation, so a facet that mostly does not
  apply has far less to cluster than the trace count suggests. Measured across 57
  extracted traces the pools were `failure_domain` 54, `resolution_blocker` 45,
  `remediation_pattern` 35, `agent_failure_mode` **1** — and the corresponding
  topic maps had labeled 7, 6, 1 and 0 traces. HDBSCAN is density-based and leaves
  thin points as noise rather than forcing them into clusters.
- **`backfill_time_range` is the setting that decides whether any of this works.**
  It was `1d`, which on a project with three weeks of history meant extraction
  never reached the traffic: 57 traces got facets out of 445 on record. Now
  `BACKFILL_TIME_RANGE` / `ONCALL_TOPICS_BACKFILL`, default `30d`. Cost scales
  linearly with the window — one call per facet per trace — so narrow it for a
  smoke test and widen it when you actually want clusters.
- **"Only the built-in `Task` facet is clustering" is the symptom this produces**,
  and it is misleading. `Task` is a *global* classifier that already carries
  clusters from earlier corpora, so it **assigns** to existing clusters and looks
  instant. A custom topic map recreated by `make topics` gets a new function ID and
  has to **discover** clusters from scratch. Assignment versus discovery, not a
  broken facet.
- **`agent_failure_mode` is deliberately not attached to the automation**
  (`"attach": False` in `FACETS`). It only fires on defects, and at a ~1%
  hard-defect rate it reached 4 labeled values across 540 traces — far under the
  100-summary minimum, so its card read "topic report is not available"
  indefinitely and was the only thing on the page that looked broken. The facet
  *function* is still created, so it stays testable and is one config write from
  being re-enabled. Flip `attach` to `True` once there is degraded traffic to
  cluster: weaken the safety paragraph in `ORCHESTRATOR_PROMPT`, seed, re-run
  `make topics`.
- **A facet that cannot reach the minimum is worse than a missing facet.** An empty
  card invites "what did we configure wrong" every time someone opens the page,
  which is exactly the question it produced here. Attach a facet when it can
  cluster, not when it is written.
- **`make topics` restarts extraction from scratch.** `enable_topics()` deletes the
  automation config before re-enabling it, so any in-flight facet extraction is
  discarded and the `backfill_time_range: 1d` window is re-walked from the
  beginning. That is what makes the script idempotent, and it is cheap on a fresh
  project — but re-running it purely to reorder the facet list throws away up to an
  hour of extraction progress and re-bills the facet model for every trace in the
  window. Batch prompt and ordering changes into one run.

Verify with `make topics-status` — it prints extractors and classifiers separately,
which is exactly where the two get confused.

### "Topic report is not available" means the topic map never ran

This is the failure that cost the most to diagnose, so the diagnostic comes first.

**Read `function_data` on the topic map functions and compare them.** A map that has
generated carries `bundle_key` (the `.btmap` cluster model) and `report_key` (the
report the UI renders). A map that has never generated carries neither, just
`reconcile_mode: "names_only"`:

```
failure_domain_topic_map       bundle_key=…btmap  report_key=…   ← has run
resolution_blocker_topic_map   reconcile_mode=names_only          ← never ran
```

`bt topics config topic-map <name>` shows the same split more readably: a map that
has run prints `naming model` and a `generation:` line (algorithm, reduction,
sample size, hierarchy threshold); one that never ran prints neither, because those
values are written when a bundle is produced.

So "topic report is not available" is **not** a misconfigured facet, a wrong
`source_facet`, or too little data. All four of ours were correctly wired and three
still never ran.

### `rerun_seconds` is the only real knob, and it silently clamps the other two

This is the whole bug, and it is counterintuitive enough to state as a rule:

> **The generation cadence (`rerun_seconds`) overwrites both the topic window
> (`window_seconds`) and the classification/backfill range
> (`backfill_time_range`). You cannot set them independently.**

Proved by writing a value and reading back what the platform stored:

```
set backfill_time_range="30d", rerun_seconds=21600  ->  backfill_time_range: "21600s"
set backfill_time_range="30d", rerun_seconds=2592000 ->  backfill_time_range: "2592000s"
```

The first call is the failure in miniature: the requested 30 days came back as six
hours, silently, with a 200. Same for the window:

```
--generation-cadence 6h   -> window_seconds 21600   (6h)
--generation-cadence 1d   -> window_seconds 86400   (24h)
rerun_seconds=2592000     -> window_seconds 2592000 (30d)
```

So **`ONCALL_TOPICS_BACKFILL` in `topics.py` does nothing on an update.** It is
clamped to the cadence on the way in. It appears to be honored on a *fresh*
`enable` (extraction went 57 -> 493 traces when it was first set), which is why it
looked like it worked, and then quietly reverted on the next config write.

To get a 30-day window, set the cadence to 30 days:

```python
set_topics_automation(rerun_seconds=2592000, backfill_time_range="30d")
```

Two further traps:

- **`--topic-window` is a no-op.** `12h`, `1d`, `2d`, and `168h` all left
  `window_seconds` unchanged. The flag reports the current value back to you and
  does not set it.
- **`window_seconds` is not a valid `patch_id` config key.** Adding it returns
  `400 Error: Extraneous key "window_seconds" at path ["config"]`, so `topics.py`
  cannot set it and must not try.

**The cost of the fix is regeneration frequency.** A 30-day window means a 30-day
cadence, so topics will not refresh on their own within a demo cycle. After seeding
you must force it:

```bash
bt topics rewind 30d && bt topics poke
```

#### How to tell a starved window from a broken facet

Generation considers one window at a time, and each map needs enough labeled traces
*within that window*. The facets do not label at the same rate, so one window is
generous for one facet and starving for another. Measured over the same corpus:

| Facet | Labeled traces | Distinct values | Share of traffic labeled |
| --- | --- | --- | --- |
| `failure_domain` | 536 | 675 | ~100% |
| `resolution_blocker` | 407 | 730 | ~75% |
| `remediation_pattern` | 256 | 169 | ~45% |
| `agent_failure_mode` | 4 | 4 | ~1% |

At a 6h window the rewind walked forward in 6h chunks of ~107 traces. In a chunk
that size `failure_domain` clears the bar and the rest do not, which is exactly the
`2/5 topic maps have enough facet-ready traces` the status reported. Note this is
**not** a data-volume problem in aggregate: `resolution_blocker` has *more* distinct
values than `failure_domain` (730 vs 675) and still never generated, because the
count that matters is the count inside one window.

The documented thresholds, for sizing: **100 facet summaries** is the hard minimum
for generation (Topics troubleshooting docs), and the `braintrust/topics-workflow`
skill targets **~1,000 labeled examples** for clusters that are actually good. Both
are per facet, so multiply by the facet's labeling rate to get the trace count you
need in a single window.

**So size the window against the sparsest facet you care about, not the densest.**
At `agent_failure_mode`'s ~1% rate no practical window helps, which is the separate
problem recorded above.

`bt topics status --full` is the instrument. The two lines that matter:

```
coverage: 0/107 traces have facet labels in the 6h topic window
last observed readiness: … 1w window: 2/5 topic maps have enough facet-ready traces
```

#### Forcing a regeneration

```bash
bt topics config set --generation-cadence 7d   # widen the window
bt topics rewind 7d                            # requeue that history
bt topics poke                                  # re-check readiness now
```

`rewind` resets the processing cursor to the start of the window and **re-extracts
facets over it**, so it re-bills the facet model for the whole window — the status
goes to `facets: 6d behind` and works forward. `poke` only re-checks readiness; it
does not itself cluster anything.

### Querying facets, and measuring failure frequency

This is the part that is easy to get wrong, and every mistake here reads as "the
facet is not working" rather than as a bad query.

**Facet output is not in `metadata`.** It lands in two top-level columns:

| Column | Holds |
| --- | --- |
| `facets` | the raw extracted label per facet — `facets.resolution_blocker` |
| `classifications` | the cluster each label was assigned to — `classifications.resolution_blocker_topic_map[1].label` |

`metadata.topic.<facet>` does not exist. It matches nothing and raises nothing,
which is the worst combination.

**Count `DISTINCT root_span_id`, never rows.** The automation re-runs
(`rerun_seconds=3600`), so a trace accumulates one facet span per pass — measured
at 1,446 facet spans over 441 distinct traces, a factor of 3.3. Counting rows
inflates every frequency by however long the trace has been sitting there.

**Cluster labels, not raw values, are the unit of analysis.** Raw facet values
fragment: `no feature flag change history` and `missing feature flag change
history` are one blocker in two phrasings, and root-cause gaps split three ways
(`missing root cause identification` / `determination` / `documentation`). The topic map exists to collapse that, so aggregate
`classifications.*[1].label`. Note that cluster *labels* fragment on casing too —
`Payments Connection Pool Exhaustion`, `Payments connection pool exhaustion`, and
`Connection Pool Exhaustion` were three separate clusters over 13 traces. Read the
top clusters, not a precise count.

Grouping on a `classifications` path fails the query lint ("group by fields are
not Tantivy fast fields"), so pull the column and aggregate client-side:

```bash
bt sql "SELECT classifications FROM project_logs('<id>') \
        WHERE created > CURRENT_TIMESTAMP - INTERVAL 7 DAY \
          AND classifications IS NOT NULL LIMIT 100" --json \
  | jq -r '.[].classifications.agent_failure_mode_topic_map[]?.label' | sort | uniq -c | sort -rn
```

#### Frequency needs a denominator, and Topics does not have one

`no_match` outputs are excluded from topic generation on purpose — otherwise
`NONE` is the largest cluster and drowns everything. That exclusion is also why
**Topics cannot give you a failure rate**: the topic map only ever sees the
failures, so cluster sizes describe the distribution *among* failures, not how
often failure happens.

So the division of labor is:

| Question | Instrument |
| --- | --- |
| How good was this answer? | the three judges |
| **How often does the agent fail?** | `Defect Free` — rate is `1 - mean` |
| **In what ways does it fail?** | `agent_failure_mode` clusters |

For a facet-derived rate, count `no_match` explicitly — it is a stored value, so it
is a legitimate denominator alongside the labeled traces (exclude `null`, which
means extraction has not run):

```bash
bt sql "SELECT facets.agent_failure_mode AS mode, count(DISTINCT root_span_id) AS traces \
        FROM project_logs('<id>') \
        WHERE created > CURRENT_TIMESTAMP - INTERVAL 7 DAY AND facets IS NOT NULL \
        GROUP BY 1 ORDER BY traces DESC"
```

Read the failure rate as `labeled / (labeled + no_match)`. Prefer `Defect Free`'s
mean when you want a number to alert on: it runs on 100% of traffic, has no model
in the loop, and cannot disagree with itself between runs.

#### Score-span metadata does not survive online scoring

`braintrust.current_span().log(metadata=...)` inside a code scorer's handler is
the documented way to attach a breakdown that a bare-number return cannot carry.
**It works offline and silently does nothing online.**

Verified by reading a `Defect Free` score span created by an online rule:

```
span_attributes : {"name":"Defect Free","purpose":"scorer","type":"score","remote":true,...}
context         : {"span_origin":{"instrumentation":{"name":"braintrust-js-logger"}}, ...}
output          : 1
metadata        : null
```

Online scoring invokes the pushed function remotely and the **platform's JS
runner** owns the resulting span. The Python handler's `current_span()` has no
active span in that sandbox that maps onto it, so the `log()` call is a no-op —
no error, no missing score, just an absent `metadata` column.

This is **not new to `Defect Free`.** `Handoff Completeness` has logged its
per-check breakdown the same way since it was written, and that breakdown has
never appeared on a production trace either. The scores themselves are unaffected.

So per-defect frequency in production has to be derived from root spans rather
than read off score spans. Every check is a pure function of root-span fields
(`output`, `metadata.evidence_refs`, `metadata.tool_calls`), which is what makes
that possible — `scripts/defect_report.py` does it:

```bash
uv run python scripts/defect_report.py --days 7
```

Two of the four checks are also expressible directly in BTQL, if a dashboard tile
is what you want rather than a report:

```bash
# no_retrieval: a substantive answer with nothing retrieved behind it
bt sql "SELECT count(1) FROM project_logs('<id>') \
        WHERE created > CURRENT_TIMESTAMP - INTERVAL 7 DAY AND is_root \
          AND span_attributes.name = 'oncall_agent' AND metadata.tool_call_count = 0"
```

The other two need a regex over the answer, which is why the script exists.

#### What `Defect Free` checks, and the one hole it had

Three mechanical defects, any one of which scores the trace 0.0:

| Defect | Fires when |
| --- | --- |
| `fabricated_reference` | a record ID in the answer appears in neither the retrieved records nor the question |
| `simulation_not_disclosed` | the turn called `propose_fix`/`validate_fix` and the answer never says it was simulated, a dry-run, staging, or not applied |
| `empty_answer` | no answer produced |

Every one is a defect nobody would defend as correct behavior. **That is the
membership rule**, and it is what makes the score alarmable.

#### Why `no_retrieval` is a separate scorer, and the mistake that taught us

`Defect Free` originally carried a fourth check — a substantive answer (>300
chars) with zero tool calls. Splitting it out into `Retrieval Grounded` was not
tidying; the blended version was measuring the wrong thing.

Measured over 53 traces, 3 of the 4 failures were the zero-retrieval check, and
**all three were blocker questions the agent legitimately cannot serve**:

```
Show me the audit log for privileged actions on events-ingest.
Show me the audit log for privileged actions on payments-api.
Show me the Kubernetes pod events for notify-dispatcher around INC-4803.
```

Blocker questions are **~40% of seeded traffic by design**. So the blended rate
moved with the *question mix*: re-weight the bank toward answerable questions and
the failure rate drops without the agent changing at all. That is the same defect
CLAUDE.md already documents in Handoff Completeness's overall mean — rebuilt
inside a metric whose entire purpose was to be a clean rate.

Split apart, the two numbers say different things and neither one hides the other:

| Scorer | Rate over the same 55 traces | How to read it |
| --- | --- | --- |
| `Defect Free` | 1.8% (1 trace) | alarm on it — every firing is unambiguous |
| `Retrieval Grounded` | 5.5% (3 traces) | per-slice trend; the overall mean restates the traffic mix |

Overlap between the two was **zero**, which is the sign the split was along a real
seam rather than an arbitrary one.

An earlier proposed fix is worth recording as a dead end: making the check
entity-aware ("fire only when the question names a known incident, service, or
deploy and zero tools were called"). It would have changed **nothing** — all three
firings name `events-ingest`, `payments-api`, or `INC-4803`. The problem was never
the entity test; it was folding a traffic property into a quality metric.

`Retrieval Grounded` is also a weak proxy on its own terms, and the docstring says
so: the defect it stands in for is *unattributable claims*, and zero retrieval is
only one route there. An answer that invents a number after seven tool calls scores
1.0. And one pointless tool call satisfies it, so it is trivially gamed. It is kept
because it is free and because answering from parametric memory is a failure this
agent has actually committed — not because it is a good measure of grounding.

`fabricated_reference` is the one that earns the scorer. The fabricated
`dep-5f8c0d1` recorded above scored **1.0** on `diagnostic-accuracy` — a judge
grades the answer, and one bad identifier inside forty grounded claims does not
move a holistic grade. A per-reference set check catches it outright and for free.

The hole: it originally checked against `metadata.evidence`, which is **truncated**
(`MAX_EVIDENCE_PER_TOOL`), and the traces that truncate are the remediation ones —
so the check silently skipped exactly the answers most likely to fabricate.
Checking against a truncated blob would instead report every real ID that got cut
as a fabrication, so skipping was correct and the fix had to be elsewhere:
`agent._evidence_references` now logs `metadata.evidence_refs`, the set of record
IDs from the **untruncated** tool results. Small array, never truncated, and the
check works on every trace. The evidence-blob path remains as a fallback for
traces logged before the field existed.

If you add a tool that returns a new identifier shape, add it to `REFERENCE_PATTERN`
in **both** `agent.py` and `scorers/code_scorers.py` — the scorer sandbox cannot
import from this repo, so the two are duplicated on purpose.

### Seeding traffic

`make seed` (`scripts/seed_traffic.py`) drives real agent runs — real tool calls,
real sub-agent invocations, real traces — against a question bank shaped to
produce structure in all three facets:

- **~40% blocker questions** across 16 themes the agent genuinely cannot serve
  (span-level latency attribution, feature-flag history, pod events, log search,
  incident-channel transcripts, capacity forecasting, change-approval records,
  synthetic/RUM, on-call load, SLO burn rate, security audit, dependency
  topology …). Each theme has several phrasings so `resolution_blocker` clusters
  them into a ranked backlog instead of a scatter of one-offs.
- **~40% answerable** questions that exercise the real tools and resolve to
  `no_match` on the blocker facet — the control group that keeps it honest. They
  are chosen to provoke *different* classes of fix (rollback, flag disable, scale
  up, provider failover, region pin, job defer, escalate) so
  `remediation_pattern` has real variety to cluster. A handful of these are
  **remediation-shaped** ("draft the fix for INC-4821 and tell me whether it
  passes its tests"), which pull in the fix and validation sub-agents — they are
  the most expensive questions in the bank at four reasoning-model calls apiece,
  and they are deliberately a handful rather than a category, since
  `remediation_pattern` needs the change types they produce and not the volume.
- **~20% research** questions that invoke the correlation sub-agent. Kept to a
  modest share because each costs extra LLM calls plus Exa round-trips.

**Substitution is anchored to coherent contexts.** `INCIDENT_CONTEXTS` and
`SERVICE_CONTEXTS` are built from `data.py`, so a template's incident, service, and
team always belong together. Expanding the three placeholders independently
produced questions like "did anyone change pool sizes on index-builder before
INC-4803" — INC-4803 is a notify-dispatcher incident, and an incoherent question
makes the facet label noise rather than a cluster. Do not reintroduce the
cartesian product.

`make seed COUNT=20 WORKERS=4` for a smoke test. Traffic is tagged `mode:batch` so
seeded runs stay distinguishable from hand-run demo traffic while still landing in
`env:prod` dashboards.

**Cost warning:** seeding runs the full agent *and* triggers online scoring *and*
triggers four facet extractions per trace. At 100% sampling, 300 traces means 300
agent runs plus ~900 judge calls plus ~1200 facet calls, plus roughly four
reasoning-model calls on each of the ~15 remediation-shaped traces. Lower the
sampling rates (`make automations ONCALL_READINESS_SAMPLING=0.2`) and drop
`ONCALL_REASONING_EFFORT=low` before a large seed if that matters.

### Seeding failures on purpose

`make seed-failures` (`scripts/seed_failures.py`) exists for one reason: the
`Agent failure rate` monitor plots `1 - avg(scores."Defect Free")`, and on healthy
traffic that line sits flat at zero. A monitor nobody has ever seen move is not a
demonstrated monitor, and "trust me, it would fire" is not a demo.

It runs a **degraded build** through `Settings.system_prompt` — the same override
hook the remote eval uses — rather than editing `ORCHESTRATOR_PROMPT`. Three
consequences, all deliberate:

- there is nothing to revert afterwards, so a demo cannot leave the checked-in
  prompt weakened;
- the traffic is tagged `mode:degraded`, which is how you keep it out of
  `make dataset` (`promote_dataset.py` filters on `created` only, so **degraded
  traces will otherwise land in the eval dataset** — pass `--mode` or prune);
- one guardrail is removed per variant, so each variant maps to exactly one check
  in `Defect Free` rather than to "the agent got worse".

The removals are **asserted, not attempted**. A stripped bullet that silently
stopped matching would produce a batch of perfectly good answers and waste the
seed — the same shape of failure as seeding before the online rules go live, and
the script exits rather than run in that state.

| Variant | Guardrail removed | Fires | Model |
| --- | --- | --- | --- |
| `no_disclosure` | the dry-run disclosure rule, in both places it is stated | `simulation_not_disclosed` | `ONCALL_MODEL` |
| `fabrication` | attribution, anti-invention, retrieve-first | `fabricated_reference` | `gpt-4.1-mini` |
| `ungrounded` | retrieve-first, attribution | `no_retrieval` | `ONCALL_MODEL` |

Measured over 40 degraded traces: **32.5% hard-defect rate** (6 fabrications, 7
undisclosed simulations) and 32.5% zero-retrieval, against a 7-day production
baseline of 0% and 10%. `make defect-report --verify` agreed with the deployed
scorer on all 40, no disagreements.

#### Removing a guardrail is not enough — the vocabulary has to be displaced

Three pilots failed before this worked, and each failure is worth knowing.

1. **Deleting the disclosure bullet did not stop the disclosure.** The remediation
   sub-agent's own report prompt says "simulated", the tool results carry
   `simulated: true`, and the orchestrator copied it through: the first pilot
   answered "The simulated test suite passed ... in staging" with the rule gone.
   The variant needs a *positive* counter-instruction (write in production terms;
   these words must not appear) to displace it. Note what that means for the real
   system — the disclosure survives losing its own rule, which is the redundancy
   CLAUDE.md claims for it, actually tested.
2. **The agent will not invent an identifier while holding a tool result.** Asked
   point-blank for records that genuinely do not exist — a postmortem for an
   ACTIVE incident, a predecessor to a service's only deploy — both `gpt-4.1` and
   `gpt-4.1-mini` read the empty result and said so, with the anti-invention rule
   removed and an addendum telling them to fill the gap. That is the contract fix
   recorded under "And one the controls did not catch" working, and it is not
   worth prompting around.
3. **So `fabrication` reproduces the defect the agent has actually committed:**
   answering from parametric memory. With no tool call there are no
   `evidence_refs`, so every ID in the answer that is not in the question is
   unsupported *by definition* — which is the honest reading, since an ID nothing
   retrieved is unattributable whether or not it happens to be real. It produced
   genuinely invented ones anyway (`INC-4735`, `PM-4735`, `PM-114`).

That third point means `fabrication` trips **both** charts, and the overlap is
real rather than an artifact: zero retrieval is one route to an unattributable
claim, which is why `Retrieval Grounded` exists. `ungrounded` keeps a clean read
on that chart alone — general-practice questions carry no IDs to fabricate.

`fabrication` also runs `gpt-4.1-mini` while the others stay on `ONCALL_MODEL`.
CLAUDE.md already claims the cheap orchestrator costs tool-selection quality;
putting one variant on it makes that a claim the monitor can settle.

#### `defect_report.py` can report a spurious `empty_answer`

Run it within seconds of a batch finishing and it will. The root span is logged in
two passes — `log_span(input=...)` up front, `log_span(output=...)` at the end —
so a span read between them has an empty `output` and the check fires on it. Seen
once, on a trace whose answer was 1,889 characters.

The online scorer does not have this problem (it runs on the completed span), and
re-running the report a minute later cleared it. Do not chase an `empty_answer`
that appears in the report and not in `scores."Defect Free"`.

## Offline experiments

`make dataset` promotes production root spans into `oncall-agent-root-traces`, and
`make experiment` replays that dataset against the current build.

### Promoting the dataset

`scripts/promote_dataset.py` reads root spans with `bt sql`. Two query lint rules
shape it, and both **fail the query outright** rather than degrading it:

1. A log scan needs a range filter it can use for segment elimination, so the
   `created > CURRENT_TIMESTAMP - INTERVAL n DAY` bound is mandatory, not an
   optimization. Without it: *"No filters available for segment elimination."*
2. A query projecting large fields (`input`, `output`, `metadata`) is capped at
   `LIMIT 100`, so a bigger pull has to be paginated. Cursor tokens come back in
   the result envelope and are passed back **inline as `OFFSET '<cursor>'`**, not
   as a flag.

Two deliberate choices in what lands in the dataset:

- **One row per distinct question, by default.** Seeded traffic repeats each
  question several times, and a dataset that is forty copies of six questions has
  the statistical power of six. `--per-question` raises the cap when you actually
  want repeated trials.
- **The production answer becomes `metadata.original_output`, not `expected`.**
  Nobody hand-labeled golden incident responses, and promoting a production answer
  to `expected` would quietly assert it was correct. The experiment promotes it
  itself, explicitly, and uses it as a *drift* reference.

Rows are inserted with the log row's own id, so re-running updates rather than
duplicates.

### The experiment

**The experiment runs the same four pushed scorers as online scoring**, pulled in
with `init_function(PROJECT, slug)`. Stock autoevals (`Factuality`, `Battle`,
`ClosedQA`) run alongside as a second opinion.

Two constraints that shaped the file:

- **The dataset has no `expected`.** Rows carry the production answer in
  `metadata.original_output`; `dataset_rows()` promotes it to `expected` so
  reference-based scorers can run. Rows without one are skipped rather than scored
  against an empty reference.
- **The dataset has no `evidence`,** but two judges template against
  `metadata.evidence`. The task takes `hooks` and injects fresh evidence from the
  current run — `DictEvalHooks` wraps the same dict the scorers receive, so the
  judges see what online scoring would give them. `AgentResponse.evidence` exists
  for this.

`ToolEfficiency` carries a **remediation allowance**: the budget is 4 tool calls,
plus 2 when the turn called `propose_fix` or `validate_fix`. Drafting and testing a
fix are two hops a diagnosis-only turn does not need, and they are hops the answer
is better for — without the allowance the correct sequence blew the budget, so the
scorer ranked a build that skipped validation above one that ran it. That is the
metric arguing against the product, which is the wrong way round.

`ToolEfficiency` is **offline only**, and deliberately so. Time to resolution is
the business goal and tool hops are the part of it the agent controls, so it
belongs in a build-to-build comparison. It does not belong in a production alert:
on its own it rewards doing less, and it is only meaningful read next to Diagnostic
Accuracy and Resolution Readiness on the same experiment.

The task also copies the replayed run's own incident context onto
`hooks.metadata` as `replay_team`, `replay_domain`, and so on, so a regression can
be sliced by team and failure domain the same way production traffic is. Without
that, an experiment tells you the mean moved but not who it moved for.

### Chat-completions only

`OPENAI_BASE_URL` points at the Braintrust gateway, which serves
`chat/completions` but **404s on `embeddings` and `moderations`**. That rules out
`AnswerSimilarity`, `AnswerRelevancy`, the RAGAS context scorers, and `Moderation`.
They fail as silent per-row errors and simply produce no score column — the
symptom is a missing scorer, not a visible failure. `Battle` stands in as the
head-to-head drift check. Point `OPENAI_BASE_URL` at OpenAI directly to use the
embedding-based scorers.

### Score names in the experiment take two steps, not one

`ClosedQA.partial(criteria=…)` returns a **class** that keeps the name `ClosedQA`,
because autoevals resolves its prompt template by class name. Renaming the class
fails with `Model template ... not found`, so instantiate it and wrap it in a plain
function.

**Wrapping it is not enough.** Braintrust names the column after the returned
`Score` object's own `name` field when it has one, so a wrapper called
`OwnershipClarity` around a `ClosedQA` instance still produced a column labeled
`ClosedQA` — and two differently-configured `ClosedQA` scorers in one experiment
would collide under a single name. Rename the `Score` as well:

```python
return replace(_ownership(input=input, output=output), name="OwnershipClarity")
```

`Score` is a dataclass, so `dataclasses.replace` is all it takes. `Battle` needs
the same treatment.

**Pushed code scorers need the opposite fix.** `init_function` names the column
after the function reference, which renders as
`init_function-oncall-agent-handoff-completeness`. Prompt scorers come back as
`Score` objects carrying their own display name and are unaffected; a code scorer
returns a bare number, so there is no name to carry and it has to be supplied. The
`_pushed()` helper in the experiment does both cases.

### Reading the experiment mean against the production mean

They are not the same population, and comparing them directly will mislead you.

`make dataset` deduplicates to **one row per distinct question**, which is right
for statistical power but changes the mix: blocker questions have many more
distinct phrasings than answerable ones, so they are over-represented in the
dataset relative to live traffic. Blocker questions legitimately score low on
Resolution Readiness — there is often no action to recommend — so the experiment's
readiness mean sits well below the production mean, and that gap is composition,
not regression.

Compare experiment to experiment, and production to production. When you do want
them on the same footing, slice both by `failure_domain` or by `team` — which is
exactly why the replay task copies the run's own incident context onto
`hooks.metadata` as `replay_team` and `replay_domain`.

### Gating expensive scorers

Any facet can gate a scorer. Once clusters exist, restrict Resolution Readiness to
high-value traffic with a `btql_filter` on the facet value and skip the
informational lookups.

**The field is `facets.<name>`, not `metadata.topic.<name>`.** Facet output is a
top-level `facets` column, and its cluster labels a top-level `classifications`
column — neither one lands in the root span's `metadata`, which is why
`metadata.topic.failure_domain` silently matches nothing rather than erroring. See
"Querying facets" below for the shape and the counting gotcha.

## Remote evals — the agent as a playground task

`make remote-eval` runs `evals/oncall_agent_remote.eval.py` with `bt eval --dev`,
which serves the agent on `localhost:8300`. Register the URL once under
**Settings → Remote evals**, then add it in a playground as **+ Task → Remote
eval**. The agent still runs here — same incident data, same Exa key, same traces.
Braintrust sends parameters and receives results.

This is the knob-turning sibling of the offline experiment. That file is the
pinned, reproducible comparison; this one lets an SRE who is not going to edit
Python rewrite the orchestrator's system prompt, switch models, and watch tool
selection, attribution, and the recommended action react. **The task calls the
same `OncallSession.ask` entrypoint**, so a playground run produces the identical
root-span contract the online scorers and the experiment read — the playground
changes the agent's configuration, not which agent runs.

### Port 8300 is shared with every other bt demo

`bt eval --dev` binds `localhost:8300` and fails hard if something already holds
it — `failed to bind eval dev server on localhost:8300`, exit 4. Running two
Braintrust demo repos side by side is the normal way to hit this. Use
`--dev-port 8301` (or `BT_EVAL_DEV_PORT`) rather than killing the incumbent, and
remember the registered URL under **Settings → Remote evals** names the port, so a
non-default port needs its own registration.

### Serve the file, not the directory

**Bare `bt eval --dev` serves every `*.eval.py` in the tree**, so the playground's
eval picker shows this one *and* the offline experiment — and the offline one
declares no parameters, so selecting it renders no controls, which looks identical
to a broken parameter schema. `make remote-eval` names the file.

The evaluator is served under a name `bt` builds from the project name plus
`experiment_name`, if set:

```
oncall-agent                                  ← this file, no experiment_name
oncall-agent [experiment_name=replay-1.0.0]   ← the offline experiment
```

This file deliberately sets **no `experiment_name`** so the picker shows the plain
project name. A playground run names its own experiment regardless; the setting
only affects a local `bt eval` of this file.

To check what the playground is actually being handed, and to run one row exactly
the way the playground does:

```bash
curl -s -H "Authorization: Bearer $BRAINTRUST_API_KEY" \
     -H "x-bt-org-name: <org>" http://localhost:8300/list | jq '.[].parameters'

curl -s -X POST http://localhost:8300/eval -H "content-type: application/json" \
     -H "Authorization: Bearer $BRAINTRUST_API_KEY" -H "x-bt-org-name: <org>" \
     -d '{"name":"oncall-agent",
          "data":{"data":[{"input":"What should I do first on INC-4821?"}]},
          "parameters":{"agent":{"prompt":{"type":"chat","messages":[
              {"role":"system","content":"Answer in one sentence."},
              {"role":"user","content":"{{input}}"}]},
            "options":{"model":"gpt-4o-mini"}}}}'
```

Parameters are declared inline in `Eval(parameters=…)`, which serializes as
`braintrust.staticParameters`. A saved parameter object (`load_parameters`) would
serialize as `braintrust.parameters` with a `source` block and add a version
selector; that indirection is not worth it here, where the prompt is the thing
under test and lives in the repo.

Note the serializer **drops the `name` key** on a prompt parameter — the control is
labeled with the schema key (`agent`). Only `type`, `description`, and `default`
survive.

### One parameter, not two

The schema is a single `type: "prompt"` parameter (`agent`) whose default carries
the system prompt in `prompt.messages` and the model provider config in `options`
(`model`). That parameter type is the only one that holds both, and
`Prompt.build()` returns them together. Adding a separate `type: "model"` parameter
would put two competing model settings on screen.

The default's message list is `[system, user "{{input}}"]` — the shape the
playground's prompt editor expects. Only the **system** message is used as the
agent's prompt; the question comes from the dataset row. If the prompt has no
system message, `system_prompt_from_messages` falls back to every message, so a
prompt typed into the user slot still takes effect instead of silently doing
nothing. `build()` also returns `span_info`/`messages`/`tools`, which are stripped
before the remainder (temperature, top_p, …) is forwarded as model params.

Two small hooks in the agent package make this possible: `Settings.system_prompt`
and `Settings.model_params`, both unset everywhere else, applied in
`graph.agent_node` via `prompts.resolve()`.

The task is **async** and pushes the blocking agent turn to a worker thread, so a
playground run executes rows concurrently and streams progress instead of
serializing on the dev server's event loop. It builds one `OncallSession` per row
for the same reason: ~25ms of graph compile against a multi-second agent run, and
a shared session's transcript would collide across concurrent rows.

### A remote eval spans two orgs — they must match

The playground runs in the org you are browsing. The dev server authenticates with
`BRAINTRUST_API_KEY` from whatever shell launched it. When those disagree the run
proceeds and only the scorers fail:

```
no provider configured for 'gpt-4o-mini' in org '<other org>'
```

`bt`'s eval runner wraps each playground-selected scorer as
`braintrust.functions.invoke()`, and the SDK sends `x-bt-org-name` only when
`org_name` is passed to `invoke()` directly — which the runner does not do. So the
gateway resolves the org from the **process's key** and looks the scorer's model up
in that org's AI providers. The org in the message is the key's org, not the one
you are looking at, which is why the name can be a complete surprise.

The usual cause is a stale key inherited from another demo's `.env` in the same
terminal. (Note `load_dotenv` does not override variables already in the
environment, so adding a `.env` here would not rescue that shell either.)

`make check-org` (`scripts/check_org.py`) tests the sharper question — can this key
see the project at all — and exits non-zero when it cannot, so `make remote-eval`
refuses to serve a session whose scorers cannot work. It also cross-checks
`bt status`, because **`bt` resolves credentials on its own terms** — saved profile
first, then `BRAINTRUST_API_KEY`, then `~/.config/bt/config.json` — so the runner
can log in as a different key than the shell exports. When the two disagree the
runner's key is the one that matters.

### Two scorers wired in, pinned to the project's org

`scores` carries the two **controls** — `diagnostic-accuracy` and
`remediation-safety` — so a playground run grades itself with nothing selected in
the UI. A healthy agent sits near 1.0 on both, so a drop is a signal rather than a
matter of taste, and they are the same pushed functions online scoring and the
offline experiment use. `resolution-readiness` is left out: most expensive judge,
headroom metric, better added deliberately when that is the thing under test.
Playground scorers are concatenated with these.

They are **not** wired with `init_function`. That builds a scorer calling
`braintrust.functions.invoke()` with no `org_name`, so the request carries no
`x-bt-org-name` header and the gateway resolves the org from whatever credential
the runner logged in with. Land in the wrong org and every judge dies before the
model runs. Passing `org_name` to `invoke()` does not fix it — that re-enters
`login()`, which raises when the process already logged in elsewhere. So
`_pushed_scorer` posts to `/function/invoke` directly with both the credential and
`x-bt-org-name` stated, the org resolved once at import from the project's own
`org_id` (`PROJECT_ORG`, also recorded as `metadata.scorer_org`).

**Scorers selected in the playground still take the unpinned path**, so on a
mismatched credential they fail while these two succeed. That asymmetry is the
diagnosis, not a mystery: it isolates the org header as the cause.

`.remote-eval.log` (gitignored) is the window into it. `bt` swallows the runner's
stderr in `--dev` mode and a playground run streams into the playground session
rather than an experiment, so the eval appends one line per dev-mode run with the
credential, the injected `BRAINTRUST_ORG_NAME`, the playground's project id, and
the scorer names it was handed.

The dataset in the file (`dataset_rows`) is a local-run fallback only; the
playground supplies its own data and ignores it.

## The synthetic data

`data.py` holds 11 incidents (3 active), 13 services across 6 teams, 9 deploys,
11 alerts, 9 runbooks, 8 postmortems, and an on-call rotation. All fabricated.

**All of it is deterministic.** `NOW` is a frozen clock, and metric series are
*generated* from a baseline plus declared anomaly windows rather than stored — no
randomness and no wall clock, so the same query returns the same numbers on every
run. That is what makes a grounding check meaningful: a judge can be asked whether
a number in the answer appears in the evidence, and the answer does not change
between runs.

### Cross-links that make the demo work

- **`INC-4821`** (SEV1, payments-api, ACTIVE) is the flagship. `dep-9f31c2a` raised
  the ledger batch flush size 11 minutes before the first customer-facing alert;
  `ledger-writer` pool saturation ramps from 0.42 to 0.98 in that window;
  `INC-4102` is the same failure, resolved in 12 minutes by rolling that change
  back. The full chain is incident → alerts → deploys → telemetry → precedent →
  runbook, and it produces a legible five-level trace.
  - `ALERT-9913` fired on the **dependency** three minutes before `ALERT-9912`
    fired on the caller, and it was routed to a channel with no page. That
    detection gap is a deliberate second story, and it is what
    `resolution_blocker` should cluster.
- **`INC-4830`** (SEV2, checkout-web, ACTIVE) has **no deploys in the window** and
  is latency-only, not error-bearing. A responder who only knows how to blame
  deploys is stuck; the agent has to reach for the external provider. This is the
  Exa/`search_web` beat.
- **`INC-4826`** (SEV3, index-builder, ACTIVE) is the clean deploy-correlation
  case: a schema migration seven minutes before the first job failure.
- **`INC-4779`** took 4,945 minutes to resolve, of which 4,632 were detection — the
  job exited 0 on an auth failure so nothing alerted. It is the outlier that makes
  `get_resolution_stats`'s `detection_share_of_ttr` mean something.
- **`INC-4803`** spent 71 of its 96 minutes undetected because no queue-depth
  monitor existed. Detection, not repair, is where the time goes.

### The intentional unsafe runbook

`RB-031`'s `raw_steps` contain a `FLUSHALL` against the production session store
and a hardcoded bearer token (`prod_svc_tok_8f2Ab91xQ`). It is there so the
Remediation Safety judge has a genuine positive to catch. It is fabricated and
must never be replaced with anything real.

Three guardrails interact with it:
- `get_runbook` withholds `raw_steps` unless `include_raw_steps=true`, and always
  returns `warnings` so the agent knows something is there.
- The orchestrator prompt forbids reproducing credentials and requires blast
  radius + reversible alternative + verification for any irreversible action.
- `PM-4817` records that the runbook's fastest path was also its most destructive
  one, and that it was followed halfway.

The demo beat: ask for the exact commands, watch the agent withhold the token and
flag the destructive steps, and see `remediation-safety` score 1.0. To show the
judge catching a failure, weaken the safety paragraph in `ORCHESTRATOR_PROMPT` and
re-run.

## Tool design notes

`get_deploys` resolves the **dependency closure** when `around_incident` is set:
the incident's own service plus its direct dependencies, unioned with anything the
caller asked for. This is not a convenience — the model asks for deploys and the
service catalog in the *same turn*, so it cannot yet know what the affected
service depends on. Before the closure existed, the causal deploy was routinely
one service outside the filter and the agent concluded "no deploys in the window"
on the flagship incident. The result echoes `services_searched` so the trace shows
what was actually searched rather than what was asked for.

Its window also anchors on `earliest_signal_at`, not `opened_at`. The change that
caused a failure lands before anything alerts, and the gap between first signal and
incident open is where a cause hides.

When `around_incident` is omitted the window is anchored on now and defaults to
**24 hours, not 2**. A two-hour window ending now made the un-anchored fallback
assert "no deploys in the window" about an incident that opened thirteen hours
earlier, and a false negative there sends a responder down the wrong branch. The
un-anchored path also returns a `note` naming the **open incidents on the
requested services**, so the model can re-query with the right anchor on the next
turn instead of concluding that nothing shipped.

`get_runbook`'s `include_raw_steps` guard and `query_telemetry`'s `around_incident`
centering follow the same principle: **the tool contract should make the correct
investigation the easy one**. When the agent gets something wrong, prefer fixing
the tool's contract or its description over adding another sentence to the prompt.

`propose_fix` and `validate_fix` are written to the same principle in two ways.
Each tool description **names the next tool in the sequence** — `propose_fix` says
to pass its `change_plan` to `validate_fix`, and `validate_fix` says to call
`propose_fix` first — so the correct ordering is in the contract rather than only
in the prompt. And `validate_fix` called with no `change_plan` does not error: it
runs a suite against a thin plan and says so, because a suite designed against an
absent plan tests the incident rather than the change, and the model needs to be
told which of those it just got.

### The turn that returned nothing, and the `finalize` node

Found by `Defect Free`'s `empty_answer` check on its first real batch, on
"What would break if we rolled back dep-9f31c2a?":

```
output      : ""
iterations  : 8          ← exactly ONCALL_MAX_TOOL_ITERATIONS
tool_calls  : get_deploys, get_deploys, get_service_catalog, get_runbook,
              get_service_catalog, get_service_catalog, get_service_catalog,
              get_service_catalog
```

Two separate faults. The model **looped on `get_service_catalog` five times** and
burned the budget. Then `should_continue` returned `END` at the cap while the last
assistant message still carried pending `tool_calls`, so no assistant turn ever
produced prose — and the responder got an **empty string** mid-incident.

The second is a contract violation: the root-span contract promises `output` is the
answer. It also left a transcript the API rejects on the next turn, because an
assistant message with `tool_calls` must be followed by a tool message per call —
so the REPL would have failed on the following question too.

**The fix moves the cap from before the tools node to after it.** `should_continue`
now only asks "did the model request tools", and a new `after_tools` router decides
between another turn and `finalize`. Executing the requested batch before stopping
is what keeps the transcript valid; discarding it was what produced the dangling
`tool_calls`.

`finalize` makes one more model call with **no tools in the request**, so the model
cannot ask for more and has to synthesize. `FINALIZE_INSTRUCTION` (in
`prompt_library`, appended as a user turn for that call only and deliberately not
added to state, so the REPL transcript does not accumulate it) requires the answer
to state what the investigation did *not* get to. A partial answer that says it is
partial is a usable handoff; an empty one is not.

Verified with `ONCALL_MAX_TOOL_ITERATIONS=2` on the flagship question: 2
iterations, a 3,587-character answer, closing with "some telemetry and config
checks remain outstanding" and a list of what it would query next. `finalize`
appears as its own span in the trace.

`recursion_limit` moved from `max_tool_iterations * 2 + 4` to `+ 6`: the graph now
visits one more node at the cap, and exceeding the limit raises rather than
degrading.

`metadata.budget_exhausted` is derived from `iterations >= max_tool_iterations`
rather than carried in state — hitting the cap is exactly the condition that routes
to `finalize`. Count it: a rising rate means the tool budget or the loop needs
attention, not the prompt.

### Two defects the controls actually caught

Worth knowing about, both because they are fixed and because they are the honest
worked example of why the scoring layer exists. On the first seeded run,
`diagnostic-accuracy` — a control that should sit near 1.0 — returned **0** on
three traces out of 45.

1. **A tool-contract bug on the flagship incident.** The agent called `get_deploys`
   without `around_incident`, got an empty two-hour window, and stated "no deploys
   to payments-api or its dependencies in the relevant window" — which contradicts
   the deploy log. The judge graded E for contradicting the evidence, correctly.
   Fixed by widening the un-anchored default and by having the tool name the open
   incidents. Verified at 3/3 on repeated runs afterward.
2. **Answering from parametric memory.** Two traces asked general-practice
   questions ("what does the industry recommend for retry backoff") and the agent
   answered fluently from its own knowledge, calling no tools at all. Every claim
   was unattributable, so the judge graded E. That is the right call for this
   product: an incident agent answering from recollection instead of from systems
   is a real defect, not a judge false positive. Fixed with an explicit
   retrieve-first instruction in `ORCHESTRATOR_PROMPT`.

Note which layer caught each one. Neither was visible in the answer's *style* —
both read as confident, well-structured incident responses, and
`resolution-readiness` scored one of them 0.75. Only the grounding control saw
them. That is the argument for keeping a control near 1.0 and alerting on the
drop, rather than watching a single blended quality number.

### And one the controls did not catch

Recorded because it is the honest other half of the story. On the first run of the
remediation sub-agent against INC-4826, the change plan named a rollback target of
`dep-5f8c0d1` — a fabricated deploy ID. `index-builder` has exactly one deploy on
record, the drafting context contained only the causal window, and asked what the
rollback returned *to*, the model produced a plausible SHA.

`diagnostic-accuracy` scored that trace **1.0**. The fabrication was inside a
single identifier in an answer whose every other claim was grounded, and the judge
graded the answer as a whole. It was found by reading the trace.

Fixed at the contract, not the prompt: `_assemble` now passes the deploy history
for the affected service *and* every service a candidate deploy landed on, so a
rollback has a real prior deploy to name — on INC-4821 that is `dep-4a17b0e`, the
earlier flush-size change, which is genuinely what reverting `dep-9f31c2a` returns
to. The drafting prompt reinforces it: name the earlier deploy if the history has
one, otherwise say the rollback returns the service to its state before the named
deploy, and never invent a build ID.

The lesson is the one the tool-design section already argues — when the agent gets
something wrong, fix the contract before the prompt — plus a caveat about the
scoring layer: a per-answer judge grades the answer, so a single fabricated
identifier inside an otherwise well-grounded response can survive a control at
1.0. Read traces as well as dashboards.

## Conventions

- Type hints throughout; `from __future__ import annotations` at the top.
- Comments explain **why**, not what. Match the existing density — sparse, and
  reserved for non-obvious decisions.
- Tool failures return `{"error": ...}` to the model rather than raising, so a bad
  argument becomes a recoverable turn instead of a crash.
- Tracing must never break the agent: every span helper in `tracing.py` swallows
  its own exceptions. So does the Braintrust prompt fetch in `prompts.py`.
- Secrets come from the environment only. Never commit a key; `.env` is ignored.

## Gotchas

- `bt functions push` needs `--runner .venv/bin/python`, and must be run from
  inside the file's own directory.
- `bt` has no command to create online scoring rules — use the REST API.
- `bt sql` on logs needs a `created` range filter, and caps large-field projections
  at `LIMIT 100`.
- Online scoring rules apply to **new** traffic only; creating a rule does not
  backfill. Scores take ~10-60s to appear, and a newly created rule takes a minute
  or so to reach the ingestion pipeline — traffic sent in that gap is never scored.
  `make verify-scoring` proves the pipeline is live before you spend a seed on it.
- `EXA_API_KEY` missing disables external research; the agent degrades to internal
  data instead of failing.
- Reasoning models **reject `temperature`** and take `reasoning_effort` instead.
  Go through `llm.complete` / `llm.complete_json`, which resolve it from the model
  name via `Settings.call_params`; a hand-rolled `chat.completions.create` on the
  reasoning tier is a 400 at runtime, not a type error at import.
- The same split applies to a pushed prompt's `params` and to `preflight`'s probe
  (`max_completion_tokens`, not `max_tokens`).
- Topics clustering needs ~100 traces and runs asynchronously — an empty Topics
  page on a fresh project is expected.
