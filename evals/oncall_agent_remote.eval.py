"""Remote eval: drive the live agent from a Braintrust playground.

Run with:
    make remote-eval
    bt eval --language python --runner .venv/bin/python evals/oncall_agent_remote.eval.py --dev

The dev server listens on http://localhost:8300. Register it once under
**Settings → Remote evals**, then add it in a playground as **+ Task → Remote
eval**. The agent keeps running here, on this machine, against the same incident
registry and the same Exa key — Braintrust only sends parameters and receives
results.

This is the interactive sibling of `oncall_agent.eval.py`. That file is the
committed, reproducible experiment: fixed prompt, fixed scorers, comparable across
versions. This one is the knob-turning surface — an SRE who is not going to edit
Python can open a playground, rewrite the orchestrator's system prompt, point it
at a different model, and watch the whole agent (tool selection, the correlation
sub-agent, attribution, the next action it lands on) react.

**The task is the same entrypoint as the offline experiment** —
`OncallSession.ask` — so a playground run produces the identical root-span
contract that the online scorers and the offline experiment read. The playground
is changing the agent's configuration, not running a different agent.

**Serve this file explicitly, not the whole directory.** Bare `bt eval --dev`
discovers every `*.eval.py` here and serves the offline experiment alongside this
one. That evaluator has no parameters, so picking it in the playground shows no
controls and looks exactly like a broken parameter schema. `make remote-eval`
names the file.

**The two control judges are declared here**, so a playground run grades itself
with nothing selected in the UI: diagnostic accuracy and remediation safety, which
sit near 1.0 on a healthy agent, so a drop is a signal rather than a matter of
taste. Resolution readiness is left out — it is the headroom metric and the most
expensive judge, better added deliberately when that is the thing under test.

They are wired with `_pushed_scorer`, not `init_function`, because a scorer
*selected in the playground* is invoked by `bt`'s runner through
`braintrust.functions.invoke()` with no `x-bt-org-name` header. The gateway then
resolves the org from whatever credential the runner logged in with, and when that
is not the org holding this project every judge dies before its model runs:

    no provider configured for 'gpt-4o' in org '<other org>'

`_pushed_scorer` posts the invoke itself with both the credential and the org
stated, so these two are immune to that. Scorers added in the playground are
concatenated with them — and are still subject to it.

`.remote-eval.log` (gitignored) records the credential, injected org, and the
playground's project id for each dev-mode run: `bt` swallows the runner's stderr
and a playground run streams into the playground session rather than an
experiment, so it is the only window into which org a run authenticated as.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from typing import Any

import httpx
from braintrust import Eval, current_span, init_dataset

from oncall_agent.agent import OncallSession
from oncall_agent.config import Settings, load_settings
from oncall_agent.prompt_library import ORCHESTRATOR_PROMPT
from oncall_agent.prompts import system_prompt_from_messages

PROJECT = os.environ.get("BRAINTRUST_PROJECT", "oncall-agent")
DATASET = os.environ.get("ONCALL_EVAL_DATASET", "oncall-agent-root-traces")

BASE_SETTINGS = load_settings()

RUN_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".remote-eval.log")


def _log_run_context() -> None:
    """Append what this runner is about to do to `.remote-eval.log`.

    `bt` spawns a fresh runner per playground request and swallows its stderr in
    `--dev` mode, and a playground run streams into the playground session rather
    than an experiment — so when a scorer fails there is nowhere to see which
    credential and which project were involved. A file is the one channel that
    survives both.

    The org logged here is where playground-selected scorers will run: `bt`'s
    runner invokes them with this login and no `x-bt-org-name` header, so a
    mismatch against the project's org is what produces
    "no provider configured for '<model>' in org '<other org>'".
    """
    if not os.environ.get("BT_EVAL_DEV_MODE"):
        return
    try:
        from datetime import datetime, timezone

        from braintrust.logger import _internal_get_global_state

        state = _internal_get_global_state()
        token = getattr(state, "login_token", None) or ""
        request = json.loads(os.environ.get("BT_EVAL_DEV_REQUEST_JSON") or "{}")
        line = " ".join(
            [
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                f"login_org={getattr(state, 'org_name', None)!r}",
                f"key=sk-****{token[-5:]}" if token else "key=<none>",
                f"env_org={os.environ.get('BRAINTRUST_ORG_NAME')!r}",
                f"request_project_id={request.get('project_id')!r}",
                f"scorers={[s.get('name') for s in request.get('scores') or []]}",
            ]
        )
        with open(RUN_LOG, "a") as handle:
            handle.write(line + "\n")
    except Exception:  # diagnostics must never break a run
        pass


def _invoke_org() -> str | None:
    """The org whose AI providers will serve playground-selected scorers.

    `bt`'s runner logs in once, before importing this file, and invokes each
    playground scorer with that login and no org header — so this, not the org
    whose playground you are driving, is where the scorer's model is looked up.
    Recorded in experiment metadata because bt swallows the runner's stderr in
    `--dev` mode, leaving no other place to see it.
    """
    try:
        from braintrust.logger import _internal_get_global_state

        return getattr(_internal_get_global_state(), "org_name", None)
    except Exception:
        return None


_log_run_context()


# One parameter carrying both halves of "how the agent is configured": the
# orchestrator's system prompt and the model provider config it runs under. A
# `type: "prompt"` parameter is the only one that holds both — the playground
# renders it as a prompt editor with a model selector and provider params
# (temperature, top_p, max_tokens) attached, and `build()` hands them back
# together. Splitting it into a separate `type: "model"` parameter would leave two
# competing model settings on screen.
AGENT_PARAMETER: dict[str, Any] = {
    "type": "prompt",
    # No "name" key: the serializer drops it for prompt parameters, so the
    # playground labels the control with the schema key ("agent").
    "description": "The prompt and model used to drive the on-call agent",
    "default": {
        "prompt": {
            "type": "chat",
            # The user turn is here because the playground renders a chat prompt,
            # not because the agent reads it — `system_prompt_from_messages` takes
            # the system message and the question comes from the dataset row.
            "messages": [
                {"role": "system", "content": ORCHESTRATOR_PROMPT},
                {"role": "user", "content": "{{input}}"},
            ],
        },
        # Model provider config. Sampling params set in the playground's prompt
        # editor come back through `build()` and are forwarded to the agent; the
        # default pins only the model so the agent keeps its own temperature.
        "options": {"model": BASE_SETTINGS.model},
    },
}


# Keys `Prompt.build()` returns that are not model provider params. Everything
# else it hands back (temperature, top_p, max_tokens, …) is forwarded verbatim to
# the chat completion the agent node makes.
_NON_PARAM_KEYS = frozenset({"messages", "prompt", "tools", "model", "span_info"})


def _settings_for(prompt, question: str) -> Settings:
    """Resolve one playground parameter set into agent settings."""
    built = prompt.build(input=question)
    return replace(
        BASE_SETTINGS,
        model=built.get("model") or BASE_SETTINGS.model,
        system_prompt=system_prompt_from_messages(built.get("messages") or []) or None,
        model_params={k: v for k, v in built.items() if k not in _NON_PARAM_KEYS},
    )


def _question_from(row_input: Any) -> str:
    """Coerce a dataset row's `input` to the question string.

    Playground data is whatever dataset someone points at, so a row may arrive as
    an object rather than a bare string.
    """
    if isinstance(row_input, dict):
        for key in ("input", "question", "user_input", "query"):
            value = row_input.get(key)
            if isinstance(value, str):
                return value
    return row_input if isinstance(row_input, str) else str(row_input)


def _run_agent(settings: Settings, question: str) -> Any:
    """Blocking agent turn. One session per row: compiling the graph and building
    the client costs ~25ms against a multi-second agent run, and sharing a session
    across concurrently-running rows would let their transcripts collide."""
    return OncallSession(settings=settings, mode="playground").ask(question)


async def task(question: Any, hooks) -> str:
    """Run one question through the agent under the playground's configuration.

    Async so a playground run executes rows concurrently and streams progress: the
    agent itself is blocking, so it goes to a worker thread rather than stalling
    the dev server's event loop.
    """
    question = _question_from(question)
    settings = _settings_for(hooks.parameters["agent"], question)
    response = await asyncio.to_thread(_run_agent, settings, question)

    # Same evidence injection as the offline experiment: the pushed judges
    # template against `metadata.evidence`, and a playground run has to give them
    # the same block online scoring would.
    hooks.metadata["evidence"] = response.evidence
    hooks.metadata["tool_calls"] = response.tool_calls
    hooks.metadata["model"] = settings.model
    hooks.metadata.update(response.context)
    return response.answer


# --- pushed judges, invoked with an explicit org ------------------------------
#
# `init_function` builds a scorer that calls `braintrust.functions.invoke()`
# without an `org_name`, so the request carries no `x-bt-org-name` header and the
# gateway resolves the org from whatever credential the runner logged in with.
# When that lands in the wrong org, every judge dies with
#
#     no provider configured for 'gpt-4o' in org '<other org>'
#
# and the model never runs. Passing `org_name` to `invoke()` is not a fix either:
# it re-enters `login()`, which raises if the process already logged in elsewhere.
#
# So post the invoke directly, pinned to the org that owns the project and to the
# key in this environment. Same endpoint and payload the SDK uses; the difference
# is that both the credential and the org are stated rather than inferred.

API_URL = os.environ.get("BRAINTRUST_API_URL", "https://api.braintrust.dev").rstrip("/")
API_KEY = os.environ.get("BRAINTRUST_API_KEY", "")
INVOKE_API_VERSION = 1


def _project_org() -> str | None:
    """Name of the org that owns PROJECT, or None if it cannot be resolved."""
    if not API_KEY:
        return None
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        with httpx.Client(headers=headers, timeout=15) as client:
            found = client.get(
                f"{API_URL}/v1/project", params={"project_name": PROJECT, "limit": 1}
            ).json()["objects"]
            if not found:
                return None
            org_id = found[0]["org_id"]
            orgs = client.post(f"{API_URL}/api/apikey/login", json={}).json()["org_info"]
        return next((o["name"] for o in orgs if o["id"] == org_id), None)
    except Exception:
        return None


PROJECT_ORG = _project_org()


def _pushed_scorer(slug: str, display_name: str):
    """Wrap a pushed scorer as an eval scorer bound to the project's org."""

    def scorer(input=None, output=None, expected=None, metadata=None, **_):
        payload = {
            "project_name": PROJECT,
            "slug": slug,
            "input": {
                "input": input,
                "output": output,
                "expected": expected,
                "metadata": metadata,
            },
            "mode": "auto",
            "strict": True,
            "stream": False,
            "api_version": INVOKE_API_VERSION,
        }
        # Nest the judge's span under the row it graded, the way the SDK does.
        try:
            parent = current_span().export()
            if parent:
                payload["parent"] = parent
        except Exception:
            pass

        headers = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/json"}
        if PROJECT_ORG:
            headers["x-bt-org-name"] = PROJECT_ORG

        response = httpx.post(
            f"{API_URL}/function/invoke", json=payload, headers=headers, timeout=120
        )
        if response.status_code >= 400:
            # Carry the body, not just the status: the gateway names the org and
            # the missing provider there, which is the whole diagnosis.
            raise RuntimeError(
                f"{slug} invoke failed ({response.status_code}) "
                f"as org {PROJECT_ORG!r}: {response.text[:300]}"
            )
        return response.json()

    scorer.__name__ = display_name
    return scorer


def dataset_rows():
    """Local fallback data. The playground supplies its own and ignores this."""
    for row in init_dataset(project=PROJECT, name=DATASET):
        yield {"input": row["input"], "metadata": row.get("metadata") or {}}


Eval(
    PROJECT,
    # No `experiment_name`: bt folds it into the served evaluator name
    # ("oncall-agent [experiment_name=…]"), and the playground picker reads better
    # with the plain project name. Playground runs name their own experiment
    # anyway; only a local `bt eval` of this file is affected.
    data=dataset_rows,
    task=task,
    scores=[
        # The controls — the same pushed scorers online scoring and the offline
        # experiment use, invoked against the project's own org.
        _pushed_scorer("diagnostic-accuracy", "Diagnostic Accuracy"),
        _pushed_scorer("remediation-safety", "Remediation Safety"),
    ],
    parameters={"agent": AGENT_PARAMETER},
    metadata={
        "agent_version": BASE_SETTINGS.agent_version,
        "subagent_model": BASE_SETTINGS.subagent_model,
        "reasoning_model": BASE_SETTINGS.reasoning_model,
        "dataset": DATASET,
        "surface": "remote_eval",
        "invoke_org": _invoke_org(),
        "scorer_org": PROJECT_ORG,
    },
)
