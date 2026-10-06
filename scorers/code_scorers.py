"""Deterministic code scorers for the on-call agent.

Pushed to Braintrust alongside the LLM judges:

    make push-scorers      # bt functions push code_scorers.py --if-exists replace

Three of them, all free and none able to drift:

* **Handoff Completeness** — a coverage floor on the answer's *shape*.
* **Defect Free** — the **hard failure-rate** signal. 1.0 when no unambiguous
  defect fired, 0.0 when one did, so the production failure rate is ``1 - mean``.
* **Retrieval Grounded** — a substantive answer with nothing retrieved behind it.

**Why the last two are separate scorers rather than one.** They were one, and
folding them together was a mistake worth recording. Measured over 53 traces, 3 of
the 4 failures were the zero-retrieval check, and all three were *blocker*
questions the agent legitimately cannot serve — a class that is ~40% of seeded
traffic by design. So the blended rate moved with the **question mix** rather than
with agent quality: re-weight the question bank toward answerable questions and
the failure rate falls without the agent changing at all.

That is the same defect CLAUDE.md already documents in Handoff Completeness's
overall mean, rebuilt inside a metric whose entire purpose was to be a clean rate.
Split apart, ``Defect Free`` is alarmable (every firing is unambiguous) and
``Retrieval Grounded`` is a composition-sensitive trend to read per slice.

Why code scorers next to three LLM judges: they are free and they never drift, so
they run on 100% of production traffic while the judges sample the expensive,
subjective part.

Why a *rate* scorer next to the quality judges: a judge grades an answer, and a
graded mean does not tell you how often the agent is outright wrong. It also
grades holistically — an answer of this repo's own, containing one fabricated
deploy ID amid otherwise well-grounded claims, scored 1.0 on Diagnostic Accuracy.
A deterministic per-claim reference check catches that class outright, and being
binary it aggregates into the one number the question "how often does this agent
fail" actually wants.

``Defect Free``'s ``empty_answer`` check earned the whole approach on its first
real batch: it caught a turn that made eight tool calls, looped on
``get_service_catalog`` five times, exhausted the iteration budget, and returned an
**empty string** to a responder mid-incident. No judge flagged it — there was
nothing to grade. Fixed with the ``finalize`` node in ``graph.py``.

Handoff Completeness measures **presence, not quality**. An answer can score 1.0
there and still be wrong; Diagnostic Accuracy and Resolution Readiness are what
catch that. What it catches is the regression where a prompt change quietly stops
the agent citing record IDs, or turns "roll back dep-9f31c2a" back into
"investigate the database" — a shape change that shows up in a cheap
deterministic signal long before anyone reads a trace.

Neither one is a substitute for the taxonomy. These say *how often* and *which
mechanical class*; the ``agent_failure_mode`` Topics facet is what discovers the
failure modes nobody wrote a regex for. Rate here, taxonomy there.

The handlers must be self-contained: pushed code scorers run in an isolated
serverless sandbox with no access to this repo, so the team list and the cue
vocabularies are inlined rather than imported from ``oncall_agent``.
"""

import os
import re

import braintrust
from pydantic import BaseModel

PROJECT_NAME = os.environ.get("BRAINTRUST_PROJECT", "oncall-agent")

project = braintrust.projects.create(name=PROJECT_NAME)


# A reference to a specific record in one of the systems the agent reads. This is
# the cheapest possible proxy for attribution: an answer with no identifier in it
# cannot have been grounded in a record.
RECORD_PATTERN = re.compile(
    r"\b(?:INC-\d{3,}|dep-[0-9a-f]{6,}|ALERT-\d{3,}|RB-\d{2,}|PM-\d{3,})\b"
)

# Ownership has to resolve to something pageable. "The relevant team" does not.
OWNER_PATTERN = re.compile(
    r"\b(?:payments|checkout|identity|search|notifications|data-platform)\b"
    r"|\bon-?call\b",
    re.IGNORECASE,
)

# Reversible-first operational verbs, plus the escalation moves that are also a
# legitimate next action. Deliberately a closed vocabulary: an open one would
# match any verb and stop discriminating.
ACTION_PATTERN = re.compile(
    r"\b(?:roll ?back|revert|redeploy|disable|re-?enable|toggle|flip|"
    r"scale (?:up|down|out)?|resize|fail ?over|pin|defer|reschedule|drain|"
    r"restart|rotate|shed|throttle|back ?off|purge|page|escalate|re-?run)\b",
    re.IGNORECASE,
)

# How the responder will know it worked. A handoff without this sends someone to
# act and then guess.
VERIFICATION_PATTERN = re.compile(
    r"\b(?:verify|verification|confirm|re-?check|check that|watch|monitor|"
    r"until|should (?:return|drop|fall|recover|clear)|returns? to baseline|"
    r"clears?|drains?|recovers?)\b",
    re.IGNORECASE,
)

CHECKS = (
    ("cites_a_record", RECORD_PATTERN),
    ("names_an_owner", OWNER_PATTERN),
    ("gives_an_action", ACTION_PATTERN),
    ("states_verification", VERIFICATION_PATTERN),
)


class HandoffParams(BaseModel):
    """Only the answer text is needed; this is a structural check on the output."""

    output: str = ""


def handoff_completeness(output: str = "") -> float:
    """Fraction of the four structural handoff elements present in the answer.

    Returns a **bare float**, not a ``{"score": ..., "metadata": ...}`` dict.
    Online scoring logs a code scorer's return value directly as the score and
    rejects the dict form with ``Cannot log {...} as a score`` — which surfaces as
    a missing score column plus an error on the score span, not as a failed push.

    The per-check breakdown is the actionable part (0.75 says nothing on its own;
    "no verification step" says exactly what to fix), so it goes onto the score
    span as metadata instead of into the return value.

    Caveat, measured: that `log()` call **only lands offline**. Online scoring runs
    this function remotely and the platform's JS runner owns the score span, so
    `current_span()` here has nothing that maps onto it and the metadata is
    silently dropped. The score is unaffected. See "Score-span metadata does not
    survive online scoring" in CLAUDE.md, and `scripts/defect_report.py` for the
    breakdown derived from root spans instead.
    """
    text = output or ""
    results = {name: bool(pattern.search(text)) for name, pattern in CHECKS}
    passed = sum(results.values())

    try:
        braintrust.current_span().log(
            metadata={
                **results,
                "missing": [name for name, ok in results.items() if not ok],
                "answer_chars": len(text),
            }
        )
    except Exception:  # scoring must never fail because diagnostics could not be logged
        pass

    return passed / len(CHECKS)


project.scorers.create(
    name="Handoff Completeness",
    slug="handoff-completeness",
    description=(
        "Deterministic coverage floor, run on all traffic: does the answer cite a "
        "record ID, name a pageable owner, give a concrete operational action, and "
        "state how to verify it. Measures presence, not quality — it catches the "
        "regression where an agent stops citing records or stops recommending "
        "anything, at zero model cost."
    ),
    parameters=HandoffParams,
    handler=handoff_completeness,
    metadata={"__pass_threshold": 0.75},
)


# --------------------------------------------------------------------------
# Defect Free — the failure-rate signal
# --------------------------------------------------------------------------

# Identifiers the agent may only state if a tool actually returned one. Same
# vocabulary as RECORD_PATTERN above, kept separate because this one is matched
# against the evidence rather than merely counted.
REFERENCE_PATTERN = RECORD_PATTERN

# `metadata.evidence_refs` is the authority: every record ID the tools actually
# returned, collected by agent.py from the *untruncated* results. It exists for
# this check.
#
# `metadata.evidence` is the fallback for traces logged before that field existed,
# and it is truncated to bound payload size — the marker below is how agent.py
# says so. Checking references against truncated evidence produces false
# fabrication reports, so on the fallback path a truncated blob skips the check
# entirely: an under-reported failure rate is recoverable, a fabrication alert
# nobody trusts is not.
TRUNCATION_MARKER = "[truncated]"

# The two tools whose results are simulations. An answer that rests on them must
# say so somewhere, or a responder can read it as "already fixed" and stop
# watching the incident.
SIMULATION_TOOLS = ("propose_fix", "validate_fix")
SIMULATION_DISCLAIMER = re.compile(
    r"\b(?:simulat\w*|dry ?run|staging|not (?:been )?applied|has not shipped|"
    r"not yet applied|before (?:you |they )?appl\w+|would be applied)\b",
    re.IGNORECASE,
)

class DefectParams(BaseModel):
    """Output plus the root span's context. Every field defaults, so a span that
    is missing one scores rather than erroring."""

    input: str = ""
    output: str = ""
    metadata: dict = {}



def defect_free(input: str = "", output: str = "", metadata: dict = None) -> float:
    """1.0 when no unambiguous defect fired, 0.0 when one did.

    Binary on purpose. The question this answers is "how often does the agent
    fail", and a mean over a binary is a rate — where a graded mean is neither a
    rate nor a quality score. Read the production failure rate as ``1 - mean``.

    Every check here is one nobody would defend as correct behavior: a record ID
    the evidence does not support, a simulated fix presented as applied, or no
    answer at all. That is what makes it alarmable. The zero-retrieval check
    deliberately lives in ``Retrieval Grounded`` instead, because it fires on
    questions the agent legitimately cannot serve and so tracks traffic mix.

    Scored so that **1.0 is good**, matching Remediation Safety, so every control
    on the dashboard points the same way.

    The `log()` below lands offline only — online scoring drops it. The breakdown
    is recoverable from root spans because every check here is a pure function of
    root-span fields; `scripts/defect_report.py` is that derivation.
    """
    text = output or ""
    meta = metadata or {}
    evidence = str(meta.get("evidence") or "")
    refs = meta.get("evidence_refs")
    tool_calls = meta.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []

    defects: list[str] = []
    details: dict[str, object] = {}

    # 1. Every record identifier in the answer has to appear in what was retrieved
    #    (or in the question the responder asked). This is the check that catches a
    #    fabricated deploy SHA inside an otherwise well-grounded answer.
    if isinstance(refs, list):
        haystack = "\n".join([*(str(r) for r in refs), input or ""])
        source = "evidence_refs"
    elif evidence and TRUNCATION_MARKER not in evidence:
        haystack = f"{evidence}\n{input or ''}"
        source = "evidence (untruncated)"
    else:
        haystack = None
        source = "skipped: no evidence_refs and evidence absent or truncated"

    details["reference_check"] = source
    if haystack is not None:
        unsupported = sorted(
            {ref for ref in REFERENCE_PATTERN.findall(text) if ref not in haystack}
        )
        details["unsupported_references"] = unsupported
        if unsupported:
            defects.append("fabricated_reference")

    # 2. An answer resting on a simulated fix must say that it is one.
    if any(tool in tool_calls for tool in SIMULATION_TOOLS):
        if not SIMULATION_DISCLAIMER.search(text):
            defects.append("simulation_not_disclosed")

    # 3. A turn that produced no answer at all. This is the check that found the
    #    exhausted-tool-budget bug: eight tool calls, no prose, empty output.
    if not text.strip():
        defects.append("empty_answer")

    try:
        braintrust.current_span().log(
            metadata={
                "defects": defects,
                "defect_count": len(defects),
                **{f"defect_{name}": name in defects for name in (
                    "fabricated_reference",
                    "simulation_not_disclosed",
                    "empty_answer",
                )},
                **details,
                "tool_calls": tool_calls,
                "answer_chars": len(text),
            }
        )
    except Exception:  # scoring must never fail because diagnostics could not be logged
        pass

    return 0.0 if defects else 1.0


project.scorers.create(
    name="Defect Free",
    slug="defect-free",
    description=(
        "Binary hard-failure-rate signal, run on all traffic at zero model cost. "
        "1.0 when none of three unambiguous defects fired: a record identifier the "
        "evidence does not support, an answer resting on a simulated fix without "
        "saying so, or no answer at all. Read the production failure rate as "
        "1 - mean. Alarmable, because every firing is a defect nobody would defend "
        "— zero-retrieval lives in Retrieval Grounded because it tracks question "
        "mix. Complements the judges, which grade quality rather than count "
        "failures."
    ),
    parameters=DefectParams,
    handler=defect_free,
    metadata={"__pass_threshold": 1.0},
)


# --------------------------------------------------------------------------
# Retrieval Grounded — composition-sensitive, read per slice
# --------------------------------------------------------------------------

# An answer this long with no tool call behind it is the "answered from parametric
# memory" defect the accuracy judge caught on the first seeded run. Short turns are
# exempt: a conversational follow-up legitimately retrieves nothing.
UNGROUNDED_ANSWER_CHARS = 300


class RetrievalParams(BaseModel):
    """The answer plus the tool list from the root span's metadata."""

    output: str = ""
    metadata: dict = {}


def retrieval_grounded(output: str = "", metadata: dict = None) -> float:
    """1.0 when a substantive answer had retrieval behind it, 0.0 when it did not.

    **Do not alert on this.** It is a proxy: the defect it stands in for is
    *unattributable claims*, and zero tool calls is only one route there — an
    answer that invents a number after seven tool calls scores 1.0 here. It is also
    trivially gamed, since one pointless tool call satisfies it.

    Most importantly it is **composition-sensitive**. It fires on questions the
    agent genuinely cannot serve ("show me the Kubernetes pod events", "show me the
    audit log"), where declining immediately may well be the correct behavior. That
    class is ~40% of seeded traffic by design, so the mean tracks the question mix
    at least as much as agent quality. Measured over 53 traces it accounted for 3 of
    4 failures, and all three were of that class.

    Read it as a **per-slice trend**, the way Handoff Completeness is read: a drop
    within the answerable slice is the signal, and the overall mean mostly restates
    what the traffic was made of.
    """
    text = output or ""
    meta = metadata or {}
    tool_calls = meta.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []

    substantive = len(text) > UNGROUNDED_ANSWER_CHARS
    ungrounded = substantive and not tool_calls

    try:
        braintrust.current_span().log(
            metadata={
                "ungrounded": ungrounded,
                "substantive": substantive,
                "tool_call_count": len(tool_calls),
                "answer_chars": len(text),
            }
        )
    except Exception:  # scoring must never fail because diagnostics could not be logged
        pass

    return 0.0 if ungrounded else 1.0


project.scorers.create(
    name="Retrieval Grounded",
    slug="retrieval-grounded",
    description=(
        "Did a substantive answer (>300 chars) have any retrieval behind it. 1.0 "
        "yes, 0.0 zero tool calls. Free and deterministic, but a proxy and "
        "composition-sensitive: it fires on questions the agent legitimately cannot "
        "serve, so the mean tracks question mix as much as quality. Read it as a "
        "per-slice trend, not an alarm — that is why it is not part of Defect Free."
    ),
    parameters=RetrievalParams,
    handler=retrieval_grounded,
    metadata={"__pass_threshold": 1.0},
)
