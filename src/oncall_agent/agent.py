"""Agent session: owns the OpenAI client and drives the compiled graph.

This is the seam where the OpenAI client is created and injected into the graph
invocation, and where the root span's contract is logged. Nothing downstream
constructs a client.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

from . import data
from .config import Settings, load_settings
from .graph import build_graph
from .tracing import log_span, setup_tracing, span_metadata, traced

logger = logging.getLogger(__name__)

# Cap on the evidence blob attached to each root span. Large enough for a judge
# to check grounding, small enough that traces stay cheap to store and read.
# Raised once, for the remediation and validation sub-agents: a change plan plus
# its review plus eight test results is the largest single tool result here, and
# at 3,500 characters the verification signals — the part the readiness judge and
# the handoff scorer actually look for — were the part that got cut.
MAX_EVIDENCE_CHARS = 18_000
MAX_EVIDENCE_PER_TOOL = 5_000

# Tags are a membership filter, not a dimension. More than a few per trace makes
# the UI filter unreadable without telling you anything metadata does not.
MAX_ENTITY_TAGS = 3

# Every record identifier the tools actually returned, collected from the
# *untruncated* results. The `Defect Free` scorer checks the identifiers in the
# answer against this rather than against `metadata.evidence`, because evidence is
# truncated and the traces that truncate are precisely the richest ones — a
# remediation turn's evidence overflows the cap, which silently disabled the
# fabrication check on exactly the answers most likely to fabricate.
REFERENCE_PATTERN = re.compile(
    r"\b(?:INC-\d{3,}|dep-[0-9a-f]{6,}|ALERT-\d{3,}|RB-\d{2,}|PM-\d{3,})\b"
)


def _collect_evidence(messages: list[dict[str, Any]]) -> str:
    """Flatten tool results into a compact, judge-readable evidence blob."""
    call_names: dict[str, str] = {}
    for message in messages:
        for call in message.get("tool_calls") or []:
            call_names[call["id"]] = call["function"]["name"]

    chunks: list[str] = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        name = call_names.get(message.get("tool_call_id", ""), "tool")
        content = str(message.get("content", ""))
        if len(content) > MAX_EVIDENCE_PER_TOOL:
            content = content[:MAX_EVIDENCE_PER_TOOL] + " …[truncated]"
        chunks.append(f"### {name}\n{content}")

    evidence = "\n\n".join(chunks)
    if len(evidence) > MAX_EVIDENCE_CHARS:
        evidence = evidence[:MAX_EVIDENCE_CHARS] + " …[truncated]"
    return evidence


def _evidence_references(messages: list[dict[str, Any]]) -> list[str]:
    """Record IDs present in the full tool results, before evidence truncation."""
    found: set[str] = set()
    for message in messages:
        if message.get("role") == "tool":
            found.update(REFERENCE_PATTERN.findall(str(message.get("content", ""))))
    return sorted(found)


def _primary_context(entities: dict[str, list[str]]) -> dict[str, Any]:
    """Reduce the entities a turn touched to one team / domain / severity.

    Slicing "is response getting faster, and for whom" needs a single value per
    trace, not a set. An incident record is the authority when one was touched —
    it already states its own team, domain, and severity — and the unambiguous
    single-value case is the fallback. Anything genuinely ambiguous stays absent
    rather than being guessed at, so a dashboard is never quietly wrong.
    """
    incidents = entities.get("incident_id") or []
    for incident_id in incidents:
        record = data.get_incident(incident_id)
        if record:
            return {
                "incident_id": record["incident_id"],
                "service": record["service"],
                "team": record["team"],
                "severity": record["severity"],
                "domain": record["domain"],
                "incident_status": record["status"],
                "incident_ttr_minutes": record["ttr_minutes"],
            }

    primary: dict[str, Any] = {}
    for field_name in ("service", "team", "severity", "domain"):
        values = entities.get(field_name) or []
        if len(values) == 1:
            primary[field_name] = values[0]
    return primary


def build_openai_client(settings: Settings) -> OpenAI:
    """Construct the OpenAI client used for every model call in the process.

    Deliberately **not** wrapped with ``wrap_openai``: ``auto_instrument()`` in
    :mod:`.tracing` already patches the OpenAI SDK, and applying both produces
    two identical ``Chat Completion`` spans per call — double the logged payload
    for no extra signal.
    """
    return OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)


@dataclass
class AgentResponse:
    """One completed agent turn."""

    answer: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    iterations: int = 0
    # Flattened tool output the answer should be grounded in. Exposed (not just
    # logged to the span) so offline evals can feed the same evidence to the
    # pushed judges that online scoring gives them.
    evidence: str = ""
    # Record IDs the tools actually returned, untruncated. Same reasoning: the
    # `Defect Free` scorer checks the answer's references against this, and an
    # offline run has to hand it the field online scoring would.
    evidence_refs: list[str] = field(default_factory=list)
    entities: dict[str, list[str]] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "sources": self.sources,
            "tool_calls": self.tool_calls,
            "iterations": self.iterations,
            "context": self.context,
        }


class OncallSession:
    """A conversation with the on-call agent.

    Holds the transcript across turns so the interactive CLI gets real multi-turn
    behavior, and reuses one client and one compiled graph.
    """

    def __init__(self, settings: Settings | None = None, mode: str = "oneshot") -> None:
        self.settings = settings or load_settings()
        self.mode = mode
        self.tracing_enabled = setup_tracing(self.settings)
        self.client = build_openai_client(self.settings)
        self.graph = build_graph()
        self.history: list[dict[str, Any]] = []

    def reset(self) -> None:
        """Clear conversation history, keeping the client and graph."""
        self.history = []

    @traced(name="oncall_agent", type="task", notrace_io=True)
    def ask(self, question: str) -> AgentResponse:
        """Run one turn and return the answer plus provenance.

        The root span logs a deliberate contract that the online scorers depend
        on: ``input`` is the bare question, ``output`` is the bare answer string,
        and everything else (retrieved evidence, tool sequence, incident context)
        goes to metadata. Keeping output a plain string means the judge templates
        can use ``{{output}}`` directly and stay robust if the response shape
        changes.
        """
        # Deployment identity up front so a run that crashes mid-turn is still
        # attributable. Re-logged at the end with the incident context appended.
        base_tags = self.settings.trace_tags(self.mode)
        log_span(input=question, tags=base_tags)
        span_metadata(
            model=self.settings.model,
            subagent_model=self.settings.subagent_model,
            # The third tier. Logged even on turns that never reach it, so
            # "which build was this" stays answerable from any trace.
            reasoning_model=self.settings.reasoning_model,
            reasoning_effort=self.settings.reasoning_effort,
            turn=len(self.history) // 2 + 1,
            web_search_enabled=self.settings.web_search_enabled,
            agent_version=self.settings.agent_version,
            environment=self.settings.environment,
            mode=self.mode,
        )

        initial_state = {
            "messages": [*self.history, {"role": "user", "content": question}],
            "iterations": 0,
            "sources": [],
            "entities": {},
        }

        # The OpenAI client travels with the invocation, not the graph.
        final_state = self.graph.invoke(
            initial_state,
            config={
                "configurable": {
                    "openai_client": self.client,
                    "settings": self.settings,
                },
                # agent + tools per iteration, plus the finalize node and the
                # entry/exit steps. Raised from +4 when finalize was added: at the
                # cap the graph now visits one more node, and exceeding the limit
                # raises instead of degrading.
                "recursion_limit": self.settings.max_tool_iterations * 2 + 6,
            },
        )

        messages = final_state["messages"]
        self.history = messages

        answer = ""
        for message in reversed(messages):
            if message.get("role") == "assistant" and message.get("content"):
                answer = message["content"]
                break

        tool_calls = [
            call["function"]["name"]
            for message in messages
            for call in (message.get("tool_calls") or [])
        ]

        # Whether the turn ran out of tool budget and was forced to answer by the
        # finalize node. Derived rather than carried in state — hitting the cap is
        # exactly the condition that routes to finalize. Worth a metadata field
        # because an answer assembled from a half-finished investigation is worse
        # than one that completed, and you want to know how often it happens.
        iterations = final_state.get("iterations", 0)
        budget_exhausted = iterations >= self.settings.max_tool_iterations
        sources = final_state.get("sources", [])
        entities = final_state.get("entities", {})
        context = _primary_context(entities)

        # The accuracy judge grades the answer against what the tools actually
        # returned, so the evidence has to travel with the span. Truncated to
        # keep per-trace payload bounded.
        evidence = _collect_evidence(messages)
        evidence_refs = _evidence_references(messages)

        # Team and severity are also tags, because "show me every payments SEV1"
        # is a filter a responder types, not an aggregation they run.
        entity_tags = [f"team:{t}" for t in (entities.get("team") or [])[:MAX_ENTITY_TAGS]]
        if context.get("severity"):
            entity_tags.append(f"sev:{context['severity']}")

        log_span(output=answer, tags=[*base_tags, *entity_tags])
        span_metadata(
            tool_calls=tool_calls,
            tool_call_count=len(tool_calls),
            iterations=iterations,
            budget_exhausted=budget_exhausted,
            max_tool_iterations=self.settings.max_tool_iterations,
            source_count=len(sources),
            sources=sources,
            evidence=evidence,
            evidence_refs=evidence_refs,
            incidents=entities.get("incident_id", []),
            services=entities.get("service", []),
            teams=entities.get("team", []),
            domains=entities.get("domain", []),
            **context,
        )

        return AgentResponse(
            answer=answer,
            sources=sources,
            tool_calls=tool_calls,
            iterations=iterations,
            evidence=evidence,
            evidence_refs=evidence_refs,
            entities=entities,
            context=context,
        )


def ask_once(question: str, settings: Settings | None = None) -> AgentResponse:
    """Convenience one-shot helper for non-interactive use and evals."""
    return OncallSession(settings=settings, mode="oneshot").ask(question)
