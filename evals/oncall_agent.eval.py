"""Braintrust experiment: replay production questions through the agent.

Run with:
    make experiment
    bt eval --language python --runner .venv/bin/python evals/oncall_agent.eval.py

The dataset `oncall-agent-root-traces` is promoted from production root spans by
`make dataset` (`scripts/promote_dataset.py`), so `input` is a question a
responder actually asked. It has **no `expected` field** — nobody hand-labeled
golden incident responses, and pretending otherwise would make every number here
a claim the data does not support.

That shapes the scorer choice. Rather than invent ground truth, we treat the
answer the agent gave in production (`metadata.original_output`) as the reference
and re-run the same question against today's build. The experiment then measures
*drift*: does the current build still answer these the way production did, and is
the answer still accurate, safe, and actionable?

Three families of scorer run here, on purpose:

1. **The same four pushed scorers that grade production online** — diagnostic
   accuracy, remediation safety, resolution readiness, handoff completeness —
   pulled in with `init_function`. Without these the experiment could not tell you
   whether a change helps or hurts the metrics you actually alert on, because
   there would be no shared axis between an experiment and a production dashboard.
2. **Stock autoevals** as an independent second opinion, measuring drift against
   the answer production already gave.
3. **One local code scorer** (`ToolEfficiency`) that exists only offline. Time to
   resolution is the business goal, and tool hops are the part of latency the
   agent controls; an answer that reaches the same conclusion in three calls
   instead of seven is strictly better. It is not an online scorer because it
   rewards brevity, and a metric that rewards doing less does not belong in a
   production alert without a quality metric next to it.

Two of the pushed judges template against `metadata.evidence`. Dataset rows do
not carry it, so the task injects fresh evidence from the current run via
`hooks.metadata` — `DictEvalHooks` wraps the same dict the scorers receive, so the
judges see exactly the evidence block online scoring would give them.

**Every scorer here is chat-completions-only, by necessity.** This deployment
reaches models through the Braintrust gateway (`OPENAI_BASE_URL`), which serves
`chat/completions` but returns 404 for `embeddings` and `moderations`. That rules
out the embedding-based autoevals (AnswerSimilarity, AnswerRelevancy, and the
RAGAS context scorers) and Moderation. They fail as silent per-row errors and
simply produce no score column — the symptom is a missing scorer, not a visible
failure. `Battle` stands in as the head-to-head drift check. Point
`OPENAI_BASE_URL` at OpenAI directly to use the embedding-based scorers.
"""

from __future__ import annotations

import os
from dataclasses import replace

from autoevals import Battle, ClosedQA, Factuality, Score
from braintrust import Eval, init_dataset, init_function

from oncall_agent.agent import OncallSession
from oncall_agent.config import load_settings

PROJECT = os.environ.get("BRAINTRUST_PROJECT", "oncall-agent")
DATASET = os.environ.get("ONCALL_EVAL_DATASET", "oncall-agent-root-traces")

# A responder who has to wait through eight tool calls has not been helped much.
# Anything at or below this is treated as free; the score decays past it.
EFFICIENT_TOOL_CALLS = 4

# Drafting and validating a fix is two extra hops that a diagnosis-only turn does
# not need, and they are hops the answer is better for. Without this allowance the
# scorer punishes the remediation path for existing: the correct sequence
# (investigate → correlate → propose_fix → validate_fix) blew the budget, so
# ToolEfficiency ranked a build that skipped validation above one that ran it.
# That is the metric arguing against the product, which is the wrong way round.
REMEDIATION_TOOLS = frozenset({"propose_fix", "validate_fix"})
REMEDIATION_ALLOWANCE = 2

settings = load_settings()

# One session per process, reset between rows. Building the graph per row would
# dominate the runtime.
_session = OncallSession(settings=settings, mode="eval")


def task(question: str, hooks) -> str:
    """Re-run one production question through the current agent build.

    The fresh evidence is pushed onto `hooks.metadata` so the pushed judges see
    the same `metadata.evidence` block they get from online scoring. Without this,
    Diagnostic Accuracy and Resolution Readiness would grade every answer against
    an empty evidence section and score it as ungrounded.

    `evidence_refs` and `tool_calls` go on unprefixed for the same reason: they are
    the root-span contract `Defect Free` reads, and without them its reference and
    simulation-disclosure checks silently skip and the experiment reports a
    flattering 1.0. The `replay_`-prefixed copies are for slicing, not for scorers.
    """
    _session.reset()
    response = _session.ask(question)
    hooks.metadata["evidence"] = response.evidence
    hooks.metadata["evidence_refs"] = response.evidence_refs
    hooks.metadata["tool_calls"] = response.tool_calls
    hooks.metadata["replay_tool_calls"] = response.tool_calls
    hooks.metadata["replay_tool_call_count"] = len(response.tool_calls)
    # The replayed run's own incident context, so a regression can be sliced by
    # team and failure domain the same way production traffic is.
    hooks.metadata.update({f"replay_{k}": v for k, v in response.context.items()})
    return response.answer


def dataset_rows():
    """Yield dataset rows with `expected` bound to the production answer.

    The stored rows have `expected: null`; the answer production actually gave
    lives in `metadata.original_output`. Promoting it to `expected` here — rather
    than at promotion time — keeps the dataset honest about what it holds while
    still letting the reference-based autoevals run. Rows with no recorded output
    are skipped rather than scored against an empty reference, which would look
    like a failure instead of missing data.
    """
    for row in init_dataset(project=PROJECT, name=DATASET):
        metadata = row.get("metadata") or {}
        original = metadata.get("original_output")
        if not original:
            continue
        yield {
            "input": row["input"],
            "expected": original,
            "metadata": {
                **metadata,
                "reference": "production_output",
                "dataset_row_id": row.get("id"),
            },
        }


# Reference-free domain check: is the answer routed to someone who can act?
#
# ClosedQA.partial() returns a *class* that keeps the name "ClosedQA" — autoevals
# resolves its prompt template by class name, so renaming the class breaks it with
# "Model template ... not found". Instantiate it and wrap it in a plain function
# instead; Braintrust names the score after the function.
_ownership = ClosedQA.partial(
    criteria=(
        "Does the response identify who should act — a specific owning team, a named "
        "on-call responder, or an explicit escalation path — rather than leaving "
        "ownership implicit or saying that 'the relevant team' should investigate? "
        "A response that correctly states it does not have the data to determine "
        "ownership, and says so plainly, should be judged favorably."
    )
)()


def OwnershipClarity(input, output, **_):
    # Rename the returned Score, not just the wrapping function. Braintrust names
    # the column after the Score object's own `name` when it has one, so a plain
    # wrapper alone still produced a column labeled "ClosedQA" — and two
    # differently-configured ClosedQA scorers would collide under one name.
    return replace(_ownership(input=input, output=output), name="OwnershipClarity")


# Head-to-head against the answer production gave. This replaces the embedding-
# based drift scorers (AnswerSimilarity, AnswerRelevancy) and Moderation, none of
# which can run here: this deployment reaches models through the Braintrust
# gateway, which serves `chat/completions` but 404s on `embeddings` and
# `moderations`. Including them produced silent per-row errors and no score.
_battle = Battle.partial(
    instructions=(
        "Help an on-call responder resolve an incident faster: an accurate diagnosis "
        "grounded in the available evidence, claims attributed to specific incident, "
        "deploy, alert, or runbook IDs, one concrete next action with a named owner and "
        "a verification step, and honest acknowledgment of what the evidence does not "
        "establish."
    )
)()


def BetterThanProduction(output, expected, **_):
    return replace(_battle(output=output, expected=expected), name="BetterThanProduction")


def _pushed(slug: str, display_name: str):
    """Pull in a pushed scorer under a readable column name.

    `init_function` names the experiment column after the function reference, which
    for a code scorer renders as `init_function-oncall-agent-handoff-completeness`.
    Prompt scorers come back as `Score` objects carrying their own display name and
    are unaffected; a code scorer returns a bare number, so the name has to be
    supplied here.
    """
    scorer = init_function(PROJECT, slug)

    def wrapper(input=None, output=None, expected=None, metadata=None, **_):
        result = scorer(input=input, output=output, expected=expected, metadata=metadata)
        if isinstance(result, Score):
            return replace(result, name=display_name)
        return Score(name=display_name, score=result)

    wrapper.__name__ = display_name
    return wrapper


def ToolEfficiency(metadata=None, **_):
    """How economically the answer was reached, from the replayed run's tool count.

    Offline only. Time to resolution is the business goal and tool hops are the
    part of it the agent controls, so this belongs in a comparison between builds.
    It does not belong in a production alert: on its own it rewards doing less,
    and it is only meaningful read next to Diagnostic Accuracy and Resolution
    Readiness on the same experiment.
    """
    count = (metadata or {}).get("replay_tool_call_count")
    if count is None:
        return None

    called = set((metadata or {}).get("replay_tool_calls") or [])
    budget = EFFICIENT_TOOL_CALLS + (
        REMEDIATION_ALLOWANCE if called & REMEDIATION_TOOLS else 0
    )

    if count <= budget:
        return 1.0
    # Linear decay to zero at twice the budget, so twice the allowance scores 0.
    return max(0.0, 1.0 - (count - budget) / budget)


Eval(
    PROJECT,
    # The model is in the name because the orchestrator tier is a thing you
    # compare across runs, and two experiments called "replay-1.0.0" are
    # indistinguishable in the picker even though they ran different agents.
    experiment_name=f"replay-{settings.agent_version}-{settings.model}",
    data=dataset_rows,
    task=task,
    scores=[
        # The same scorers that grade production online — this is what makes an
        # experiment result comparable to a production dashboard.
        init_function(PROJECT, "diagnostic-accuracy"),
        init_function(PROJECT, "remediation-safety"),
        init_function(PROJECT, "resolution-readiness"),
        # Wrapped only because they are code scorers — see `_pushed`.
        _pushed("handoff-completeness", "Handoff Completeness"),
        _pushed("defect-free", "Defect Free"),
        _pushed("retrieval-grounded", "Retrieval Grounded"),
        # Autoevals, reference-based: does today's build still match production?
        Factuality,
        BetterThanProduction,
        # Reference-free: is the answer routed to someone who can act on it?
        OwnershipClarity,
        # Offline-only efficiency proxy for time to resolution.
        ToolEfficiency,
    ],
    metadata={
        "agent_version": settings.agent_version,
        "model": settings.model,
        "subagent_model": settings.subagent_model,
        "reasoning_model": settings.reasoning_model,
        "prompt_source": settings.prompt_source,
        "dataset": DATASET,
        "reference": "production_output",
    },
)
