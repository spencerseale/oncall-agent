"""Tools available to the on-call agent.

Each tool is a plain Python function plus an OpenAI function-calling schema. The
schemas are hand-written rather than derived from a framework so the tool contract
the model sees is exactly what appears in the Braintrust trace.

Tools split into five families:

* **Incident state** — ``get_incident``, ``search_incidents``, ``get_alerts``.
* **Change and signal** — ``get_deploys``, ``query_telemetry``.
* **Ownership and knowledge** — ``get_service_catalog``, ``get_runbook``,
  ``get_resolution_stats``.
* **Research** — ``correlate_root_cause`` delegates to the correlation sub-agent;
  ``search_web`` is a direct Exa call.
* **Simulated remediation** — ``propose_fix`` delegates to the remediation
  sub-agent and ``validate_fix`` to the validation sub-agent. Both run the
  reasoning model tier, and both are dry-runs against a staging replica: nothing
  in this repo applies a change to anything.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any, Callable

from openai import OpenAI

from . import data
from .config import Settings
from .exa import ExaClient
from .remediation import run_fix_simulation
from .subagent import run_correlation
from .tracing import span_metadata, traced_step
from .validation import run_fix_validation

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Incident state
# --------------------------------------------------------------------------


@traced_step(name="tool.get_incident", type="tool")
def get_incident(settings: Settings, client: OpenAI, incident_id: str) -> dict[str, Any]:
    """Read one incident record from the registry."""
    incident = data.get_incident(incident_id)
    if incident is None:
        known = ", ".join(i["incident_id"] for i in data.INCIDENTS[:6])
        return {"found": False, "error": f"No incident {incident_id!r}. Recent: {known}"}

    span_metadata(
        incident_id=incident["incident_id"],
        service=incident["service"],
        team=incident["team"],
        severity=incident["severity"],
        domain=incident["domain"],
        status=incident["status"],
    )
    return {"found": True, "incident": incident}


@traced_step(name="tool.search_incidents", type="tool")
def search_incidents(
    settings: Settings,
    client: OpenAI,
    service: str | None = None,
    team: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    domain: str | None = None,
    text: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Filter the incident registry by service, team, severity, status, or text."""
    matches = data.find_incidents(
        service=service,
        team=team,
        severity=severity,
        status=status,
        domain=domain,
        text=text,
        limit=min(max(limit, 1), 25),
    )
    span_metadata(matched=len(matches), filters={
        "service": service, "team": team, "severity": severity,
        "status": status, "domain": domain, "text": text,
    })
    return {"count": len(matches), "incidents": matches}


@traced_step(name="tool.get_alerts", type="tool")
def get_alerts(
    settings: Settings,
    client: OpenAI,
    service: str | None = None,
    incident_id: str | None = None,
) -> dict[str, Any]:
    """Alert history for a service or an incident, newest first."""
    alerts = data.find_alerts(service=service, incident_id=incident_id)
    span_metadata(matched=len(alerts), service=service, incident_id=incident_id)
    return {"count": len(alerts), "alerts": alerts}


# --------------------------------------------------------------------------
# Change and signal
# --------------------------------------------------------------------------


@traced_step(name="tool.get_deploys", type="tool")
def get_deploys(
    settings: Settings,
    client: OpenAI,
    services: list[str] | str | None = None,
    around_incident: str | None = None,
    # 24 hours, not 2. The anchored path gets a tight window around the incident's
    # earliest signal; the un-anchored path is the fallback the model reaches for
    # when it has not yet realized which incident it is on, and a two-hour window
    # ending now made that fallback assert "no deploys in the window" about an
    # incident that opened thirteen hours earlier. A false negative here sends a
    # responder down the wrong branch, which is worse than a few extra rows.
    lookback_minutes: int = 1440,
) -> dict[str, Any]:
    """Deploys to one or more services in a window, optionally centered on an incident."""
    if isinstance(services, str):
        services = [services]
    requested = list(services or [])

    center = data.NOW
    resolved: list[str] | None = requested or None
    if around_incident:
        incident = data.get_incident(around_incident)
        if incident is None:
            return {"error": f"No incident {around_incident!r}."}
        # Anchor on the earliest signal, not the incident's open time: the change
        # that caused a failure lands before anything alerts, and the gap between
        # "first signal" and "incident opened" is where a cause hides.
        center = data._parse(incident.get("earliest_signal_at") or incident["opened_at"])

        # Expand to the incident's own service plus its direct dependencies. The
        # model asks for deploys and the service catalog in the same turn, so it
        # cannot yet know what the affected service depends on — and "a tier-1
        # service is reporting its dependency's failure" is the single most common
        # shape here. Leaving the caller to get this right meant the causal deploy
        # was routinely one service outside the filter.
        catalog = data.get_service(incident["service"])
        closure = {incident["service"], *requested}
        closure.update(catalog.get("depends_on", []) if catalog else [])
        resolved = sorted(name for name in closure if name in data.SERVICES)

    since = center - timedelta(minutes=max(lookback_minutes, 5))
    # A small forward margin so a deploy that landed during the first minutes of
    # an incident is still visible — it can be a failed mitigation.
    until = center + timedelta(minutes=15)

    deploys = data.find_deploys(services=resolved, since=since, until=until)

    # An un-anchored window is the shape that produces false negatives, so say so
    # rather than returning a confident empty list. Naming the open incidents on
    # the requested services lets the model re-query with the right anchor on the
    # next turn instead of concluding that nothing shipped.
    notes: list[str] = []
    if not around_incident:
        open_incidents = [
            incident["incident_id"]
            for incident in data.find_incidents(status="ACTIVE", limit=25)
            if resolved is None or incident["service"] in resolved
        ]
        if open_incidents:
            notes.append(
                "This window is anchored on now, not on an incident. Open incidents on "
                f"these services: {', '.join(open_incidents)}. Re-run with "
                "around_incident set to anchor on the incident's earliest signal and "
                "widen the search to its dependencies."
            )
    if not deploys:
        notes.append(
            "No deploys in this window. A config or feature-flag change, or an "
            "external dependency, are the remaining candidates."
        )

    span_metadata(
        matched=len(deploys),
        services_searched=resolved,
        anchored=bool(around_incident),
        window={"since": data._iso(since), "until": data._iso(until)},
    )
    return {
        "count": len(deploys),
        "window": {"since": data._iso(since), "until": data._iso(until)},
        # Echoed so the trace shows what was actually searched, not what was asked
        # for — the two differ whenever a dependency closure was expanded.
        "services_searched": resolved or "all services",
        "deploys": deploys,
        "note": " ".join(notes) or None,
    }


@traced_step(name="tool.query_telemetry", type="tool")
def query_telemetry(
    settings: Settings,
    client: OpenAI,
    service: str,
    metric: str,
    around_incident: str | None = None,
    window_minutes: int = 60,
) -> dict[str, Any]:
    """Metric series for a service, optionally centered on an incident's start."""
    center = data.NOW
    if around_incident:
        incident = data.get_incident(around_incident)
        if incident is None:
            return {"error": f"No incident {around_incident!r}."}
        center = data._parse(incident["opened_at"])

    series = data.telemetry(
        service=service,
        metric=metric,
        center=center,
        window_minutes=min(max(window_minutes, 15), 480),
    )
    if "error" not in series:
        span_metadata(
            service=service, metric=metric, anomalous=series["anomalous"], max=series["max"]
        )
    return series


# --------------------------------------------------------------------------
# Ownership and knowledge
# --------------------------------------------------------------------------


@traced_step(name="tool.get_service_catalog", type="tool")
def get_service_catalog(
    settings: Settings,
    client: OpenAI,
    service: str | None = None,
    team: str | None = None,
) -> dict[str, Any]:
    """Service ownership, dependencies, tier, SLO, and the on-call rotation."""
    if service:
        record = data.get_service(service)
        if record is None:
            return {
                "found": False,
                "error": f"No service {service!r}. Known: {', '.join(sorted(data.SERVICES))}",
            }
        services = [record]
    else:
        services = data.find_services(team=team)
        if not services:
            return {
                "found": False,
                "error": f"No services for team {team!r}. Teams: {', '.join(data.TEAMS)}",
            }

    # Ownership is only actionable with the rotation attached — otherwise the
    # responder has the team name and still has to go look up who to page.
    rotations = {
        record["team"]: data.get_oncall(record["team"])
        for record in services
        if data.get_oncall(record["team"])
    }

    span_metadata(matched=len(services), teams=sorted(rotations))
    return {"found": True, "count": len(services), "services": services, "oncall": rotations}


@traced_step(name="tool.get_runbook", type="tool")
def get_runbook(
    settings: Settings,
    client: OpenAI,
    runbook_id: str | None = None,
    service: str | None = None,
    include_raw_steps: bool = False,
) -> dict[str, Any]:
    """Read a runbook by ID, or list the runbooks for a service."""
    if runbook_id:
        runbook = data.get_runbook(runbook_id)
        if runbook is None:
            return {
                "found": False,
                "error": f"No runbook {runbook_id!r}. Known: {', '.join(sorted(data.RUNBOOKS))}",
            }
        runbooks = [runbook]
    else:
        runbooks = data.find_runbooks(service=service)
        if not runbooks:
            return {"found": False, "error": f"No runbooks for service {service!r}."}

    # Raw steps are un-reviewed operator text: some are destructive, some are
    # stale, and RB-031 carries a hardcoded production credential. Withheld unless
    # the caller explicitly asks, the way a real system gates a paste-ready
    # command list.
    payload = []
    for runbook in runbooks:
        record = {k: v for k, v in runbook.items() if k != "raw_steps"}
        record["raw_steps_available"] = bool(runbook.get("raw_steps"))
        if include_raw_steps:
            record["raw_steps"] = runbook["raw_steps"]
        payload.append(record)

    span_metadata(
        matched=len(payload),
        include_raw_steps=include_raw_steps,
        stale=[r["runbook_id"] for r in runbooks if r["last_reviewed"] < "2025-08-19"],
    )
    return {"found": True, "count": len(payload), "runbooks": payload}


@traced_step(name="tool.get_resolution_stats", type="tool")
def get_resolution_stats(
    settings: Settings,
    client: OpenAI,
    team: str | None = None,
    domain: str | None = None,
    severity: str | None = None,
) -> dict[str, Any]:
    """Resolution timings (TTD/TTA/TTR) across resolved incidents, by team and domain."""
    stats = data.resolution_stats(team=team, domain=domain, severity=severity)
    span_metadata(team=team, domain=domain, resolved_count=stats.get("count"))
    return stats


# --------------------------------------------------------------------------
# Research
# --------------------------------------------------------------------------


@traced_step(name="tool.correlate_root_cause", type="tool")
def correlate_root_cause(
    settings: Settings,
    client: OpenAI,
    question: str,
    incident_id: str | None = None,
) -> dict[str, Any]:
    """Delegate hypothesis generation and precedent search to the correlation sub-agent."""
    return run_correlation(
        client=client, settings=settings, question=question, incident_id=incident_id
    )


@traced_step(name="tool.search_web", type="tool")
def search_web(
    settings: Settings,
    client: OpenAI,
    query: str,
    num_results: int = 5,
) -> dict[str, Any]:
    """Direct Exa web search for provider status pages and external context."""
    if not settings.web_search_enabled:
        return {"error": "Web search unavailable: EXA_API_KEY is not set.", "results": []}

    exa = ExaClient(api_key=settings.exa_api_key or "")
    try:
        hits = exa.search(query, num_results=min(num_results, 10))
    except Exception as exc:
        logger.warning("Exa search failed: %s", exc)
        return {"error": f"Search failed: {exc}", "results": []}

    span_metadata(result_count=len(hits))
    return {"count": len(hits), "results": [h.to_dict() for h in hits]}


# --------------------------------------------------------------------------
# Simulated remediation
# --------------------------------------------------------------------------


@traced_step(name="tool.propose_fix", type="tool")
def propose_fix(
    settings: Settings,
    client: OpenAI,
    incident_id: str,
    question: str | None = None,
    correlation_brief: str | None = None,
) -> dict[str, Any]:
    """Delegate drafting and reviewing the change to the remediation sub-agent."""
    return run_fix_simulation(
        client=client,
        settings=settings,
        incident_id=incident_id,
        question=question,
        correlation_brief=correlation_brief,
    )


@traced_step(name="tool.validate_fix", type="tool")
def validate_fix(
    settings: Settings,
    client: OpenAI,
    incident_id: str,
    change_plan: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Delegate designing and dry-running the test suite to the validation sub-agent."""
    return run_fix_validation(
        client=client, settings=settings, incident_id=incident_id, change_plan=change_plan
    )


# --------------------------------------------------------------------------
# Schemas exposed to the model
# --------------------------------------------------------------------------

_SERVICE_NAMES = ", ".join(sorted(data.SERVICES))
_TEAM_NAMES = ", ".join(data.TEAMS)
_DOMAIN_NAMES = ", ".join(data.DOMAINS)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_incident",
            "description": (
                "Read one incident from the registry: severity, status, timeline, "
                "customer impact, responders, resolution timings, and the IDs of its "
                "alerts, candidate deploys, related incidents, and runbooks. Start here "
                "when the user names an incident."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string", "description": "Incident ID, e.g. INC-4821."}
                },
                "required": ["incident_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_incidents",
            "description": (
                "Search the incident registry. Use to find currently active incidents, "
                "a team's history, or prior incidents matching a failure description. "
                "For 'has this happened before' reasoning prefer correlate_root_cause, "
                "which searches precedent and ranks it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": f"One of: {_SERVICE_NAMES}."},
                    "team": {"type": "string", "description": f"One of: {_TEAM_NAMES}."},
                    "severity": {"type": "string", "description": "SEV1, SEV2, or SEV3."},
                    "status": {"type": "string", "description": "ACTIVE, MITIGATED, or RESOLVED."},
                    "domain": {"type": "string", "description": f"Failure domain: {_DOMAIN_NAMES}."},
                    "text": {
                        "type": "string",
                        "description": (
                            "Free-text match against titles, root causes, and "
                            "resolutions. All words must appear."
                        ),
                    },
                    "limit": {"type": "integer", "description": "Max results, 1-25. Default 10."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_alerts",
            "description": (
                "Alert history for a service or an incident: monitor, metric, threshold, "
                "observed value, fire time, and where it routed. Use to establish the "
                "ordering of signals and to find alerts that fired without paging anyone."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": f"One of: {_SERVICE_NAMES}."},
                    "incident_id": {"type": "string", "description": "Incident ID, e.g. INC-4821."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_deploys",
            "description": (
                "Deploys to one or more services in a time window: SHA, author, summary, "
                "risk, what it touches, and whether a rollback is available. Pass the "
                "affected service AND its dependencies — a tier-1 service's errors are "
                "usually a dependency's change. When investigating a specific incident "
                "set around_incident: it anchors the window on that incident's earliest "
                "signal (a window anchored on now misses the change completely for an "
                "incident that opened hours ago) and automatically widens the search to "
                "the affected service's direct dependencies. Omit services entirely to "
                "see everything that shipped platform-wide in the window."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "services": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": f"Services to include. One of: {_SERVICE_NAMES}.",
                    },
                    "around_incident": {
                        "type": "string",
                        "description": (
                            "Incident ID to center the window on. Omit to look back from now."
                        ),
                    },
                    "lookback_minutes": {
                        "type": "integer",
                        "description": "How far back to look. Default 1440 (24 hours).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_telemetry",
            "description": (
                "Metric series for a service, with the baseline, range, and whether the "
                "window deviates from baseline. Set around_incident to center the window "
                "on the incident's start so the pre-incident baseline is visible next to "
                "the anomaly. Metrics vary by service; an unknown metric returns the list "
                "of available ones."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": f"One of: {_SERVICE_NAMES}."},
                    "metric": {
                        "type": "string",
                        "description": (
                            "e.g. http_5xx_rate, http_p99_latency_ms, db_pool_saturation, "
                            "queue_depth, replication_lag_seconds, memory_used_ratio, "
                            "throughput_rps, job_failure_count."
                        ),
                    },
                    "around_incident": {
                        "type": "string",
                        "description": "Incident ID to center the window on.",
                    },
                    "window_minutes": {
                        "type": "integer",
                        "description": "Window width, 15-480. Default 60.",
                    },
                },
                "required": ["service", "metric"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_service_catalog",
            "description": (
                "Service ownership and topology: owning team, tier, dependencies, "
                "dependents, SLO, remaining error budget, runbook IDs — plus the on-call "
                "rotation and escalation policy for the owning team. Use to decide who to "
                "page and to find the dependencies worth investigating."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {"type": "string", "description": f"One of: {_SERVICE_NAMES}."},
                    "team": {
                        "type": "string",
                        "description": f"List a whole team's services. One of: {_TEAM_NAMES}.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_runbook",
            "description": (
                "Read a runbook by ID, or list a service's runbooks. Returns the reviewed "
                "step summary, the last review date, and any warnings about the runbook "
                "itself. Set include_raw_steps only if the responder needs the verbatim "
                "commands: raw steps are un-reviewed operator text that can be "
                "destructive, stale, or contain credentials, and must never be reproduced "
                "verbatim into an answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "runbook_id": {"type": "string", "description": "Runbook ID, e.g. RB-014."},
                    "service": {"type": "string", "description": f"One of: {_SERVICE_NAMES}."},
                    "include_raw_steps": {
                        "type": "boolean",
                        "description": "Include the verbatim command list. Default false.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resolution_stats",
            "description": (
                "Aggregate resolution timings over resolved incidents, grouped by team and "
                "by failure domain: median and max TTR, median TTD, and the share of "
                "resolution time spent before anyone noticed. Use for questions about "
                "where response is slow and why — a high detection share means the problem "
                "is alerting, not repair."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "team": {"type": "string", "description": f"One of: {_TEAM_NAMES}."},
                    "domain": {"type": "string", "description": f"One of: {_DOMAIN_NAMES}."},
                    "severity": {"type": "string", "description": "SEV1, SEV2, or SEV3."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "correlate_root_cause",
            "description": (
                "Delegate to the root-cause correlation sub-agent. It generates "
                "mechanism-level candidate causes, searches internal precedent (prior "
                "incidents and postmortems) plus external sources (provider status pages, "
                "issue trackers, operational writeups), and returns a ranked correlation "
                "brief that reports each prior incident's actual fix and resolution time. "
                "Prefer this over search_web for any 'why is this happening' or 'has this "
                "happened before' question."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "A self-contained correlation question describing the failure "
                            "shape and the signals observed so far."
                        ),
                    },
                    "incident_id": {
                        "type": "string",
                        "description": (
                            "Incident to correlate against, e.g. INC-4821. Gives the "
                            "sub-agent the record and its linked precedent."
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_fix",
            "description": (
                "Delegate to the remediation sub-agent, which runs a reasoning model. "
                "It assembles the incident, the deploys in its causal window, the "
                "dependency closure, the reviewed runbook steps, and how the same "
                "failure shape was fixed before; drafts the smallest change that "
                "addresses the mechanism (steps, diff, blast radius, rollback plan, "
                "owning team, and real verification signals); then reviews that draft "
                "for destructive operations, leaked credentials, understated blast "
                "radius, and vague verification, returning an approve/revise/reject "
                "verdict. THE CHANGE IS SIMULATED against a staging replica and is "
                "never applied — say so in your answer. Call this when the responder "
                "asks what to change, how to fix it, or whether something is safe to "
                "ship, and pass validate_fix the change_plan it returns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "Incident to draft a fix for, e.g. INC-4821.",
                    },
                    "question": {
                        "type": "string",
                        "description": (
                            "What the responder actually asked, so the draft addresses "
                            "their constraint (roll back vs forward fix, for instance)."
                        ),
                    },
                    "correlation_brief": {
                        "type": "string",
                        "description": (
                            "The brief from correlate_root_cause, when you have one. "
                            "Aims the change at the cause you are about to report "
                            "instead of one you had already ruled out."
                        ),
                    },
                },
                "required": ["incident_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "validate_fix",
            "description": (
                "Delegate to the validation sub-agent, which runs a reasoning model. It "
                "designs a five-to-eight test suite for a proposed change — regression "
                "against precedent, integration across the dependency boundary, load at "
                "incident throughput, telemetry returning to baseline, and canary "
                "criteria — dry-runs it, and reports a ship / canary / hold verdict with "
                "each failing test's mechanical meaning and what the suite could not "
                "cover. THE RUN IS SIMULATED against a staging replica; nothing is "
                "applied and the incident is not resolved by it. Call this after "
                "propose_fix, passing the change_plan it returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {
                        "type": "string",
                        "description": "The incident the change is for, e.g. INC-4821.",
                    },
                    "change_plan": {
                        "type": "object",
                        "description": (
                            "The change_plan object propose_fix returned, passed through "
                            "unchanged. Without it the suite tests the incident rather "
                            "than the change, which is not the same question."
                        ),
                    },
                },
                "required": ["incident_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "General web search via Exa. Use for a provider status page, a vendor "
                "advisory, or an error signature when you need one specific external fact. "
                "For anything requiring ranked precedent, use correlate_root_cause instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                    "num_results": {
                        "type": "integer",
                        "description": "How many results to return (1-10). Default 5.",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "get_incident": get_incident,
    "search_incidents": search_incidents,
    "get_alerts": get_alerts,
    "get_deploys": get_deploys,
    "query_telemetry": query_telemetry,
    "get_service_catalog": get_service_catalog,
    "get_runbook": get_runbook,
    "get_resolution_stats": get_resolution_stats,
    "correlate_root_cause": correlate_root_cause,
    "propose_fix": propose_fix,
    "validate_fix": validate_fix,
    "search_web": search_web,
}


# Fields worth promoting to the root span so production traffic can be sliced by
# team and failure domain. Extracted from tool arguments and results rather than
# asked of the model, so they are facts about what was queried, not a guess.
_ENTITY_FIELDS = ("incident_id", "service", "team", "severity", "domain")


def entities_from(
    name: str, arguments: dict[str, Any], result: dict[str, Any]
) -> dict[str, set[str]]:
    """Collect the incidents, services, teams, severities, and domains a call touched.

    The business question behind this demo is "is response getting faster, and for
    whom" — which is unanswerable unless every trace says which team and which
    failure domain it was about. Deriving that here keeps it consistent across
    tools and out of the model's hands.
    """
    found: dict[str, set[str]] = {field: set() for field in _ENTITY_FIELDS}

    def absorb(record: Any) -> None:
        if not isinstance(record, dict):
            return
        for field in _ENTITY_FIELDS:
            value = record.get(field)
            if isinstance(value, str) and value:
                found[field].add(value)

    absorb(arguments)
    absorb(result)
    for key in ("incident", "incidents", "services", "alerts", "runbooks", "deploys"):
        value = result.get(key) if isinstance(result, dict) else None
        if isinstance(value, dict):
            absorb(value)
        elif isinstance(value, list):
            for item in value:
                absorb(item)

    # get_service_catalog reports owning teams under "oncall"; a runbook's owner
    # is "owner_team". Neither matches the common field name.
    if isinstance(result, dict):
        for team in (result.get("oncall") or {}):
            found["team"].add(team)
        for runbook in result.get("runbooks") or []:
            if isinstance(runbook, dict) and runbook.get("owner_team"):
                found["team"].add(runbook["owner_team"])

    return {field: values for field, values in found.items() if values}


def dispatch_tool(
    name: str,
    arguments: str | dict[str, Any],
    settings: Settings,
    client: OpenAI,
) -> tuple[dict[str, Any], dict[str, set[str]]]:
    """Run one tool call by name.

    Returns the JSON-serializable result plus the entities it touched. Tool
    failures are returned to the model as ``{"error": ...}`` rather than raised, so
    a bad argument becomes a recoverable turn instead of a crash.
    """
    fn = TOOL_REGISTRY.get(name)
    if fn is None:
        return {"error": f"Unknown tool {name!r}. Available: {', '.join(TOOL_REGISTRY)}"}, {}

    if isinstance(arguments, str):
        try:
            kwargs = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError as exc:
            return {"error": f"Could not parse arguments for {name}: {exc}"}, {}
    else:
        kwargs = dict(arguments)

    try:
        result = fn(settings=settings, client=client, **kwargs)
    except TypeError as exc:
        return {"error": f"Bad arguments for {name}: {exc}"}, {}
    except Exception as exc:  # pragma: no cover
        logger.exception("Tool %s failed", name)
        return {"error": f"{name} failed: {exc}"}, {}

    return result, entities_from(name, kwargs, result)
