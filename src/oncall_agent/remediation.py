"""Remediation sub-agent: draft the change, then review it.

The orchestrator delegates here when the question stops being "what is wrong" and
becomes "what do I change". It runs three steps as its own pipeline:

    assemble_change_context -> draft_patch -> review_patch

Two things make this a sub-agent rather than another flat tool:

1. **Context isolation** — drafting a change needs the incident, the causal
   deploys, the dependency closure, the reviewed runbook steps, and how the same
   failure was fixed last time. That is a large context to assemble and a small
   one to return: the orchestrator gets a change plan and a verdict, not five raw
   record sets it would have to correlate itself.
2. **A nested trace with its own model tier** — ``fix_subagent`` is where the
   expensive model runs, so "what did the deep work cost, and how often did we
   need it" is answerable from the span tree, and the draft can be scored
   independently of the answer that quotes it.

**Everything here is simulated.** The change is drafted against a staging replica
and never applied — there is no production surface in this demo to apply it to.
The result says so in three places (``simulated``, ``applied``, ``environment``)
because the one failure mode that actually matters here is an answer a tired
responder reads as "it's already fixed".

The draft is reviewed once, not iterated. A revise loop would double the reasoning
spend on every call to buy a second draft the orchestrator can already ask for; the
reviewer's ``required_changes`` go into the answer instead, which is also what a
human reviewer's comments would do.

The OpenAI client is passed in by the caller; this module never constructs one.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from openai import OpenAI

from . import data
from .config import Settings
from .llm import complete_json
from .tracing import span_metadata, traced_step

logger = logging.getLogger(__name__)

# The causal window: a change that breaks something lands before anything alerts,
# so anchor on the earliest signal and look back, the same way get_deploys does.
CAUSAL_LOOKBACK_MINUTES = 240
MAX_PRECEDENT = 3


def _candidate_deploys(incident: dict[str, Any]) -> list[dict[str, Any]]:
    """Deploys that could plausibly have caused this incident.

    The incident's own ``deploy_ids`` are the curated answer, but they are only as
    good as whoever triaged it — so the dependency closure is searched as well and
    unioned in. A change plan that reverts the wrong deploy is worse than no plan.
    """
    by_id = {deploy["deploy_id"]: deploy for deploy in data.DEPLOYS}
    candidates = {
        deploy_id: by_id[deploy_id]
        for deploy_id in incident.get("deploy_ids", [])
        if deploy_id in by_id
    }

    catalog = data.get_service(incident["service"])
    closure = {incident["service"], *(catalog.get("depends_on", []) if catalog else [])}
    center = data._parse(incident.get("earliest_signal_at") or incident["opened_at"])
    for deploy in data.find_deploys(
        services=sorted(name for name in closure if name in data.SERVICES),
        since=center - timedelta(minutes=CAUSAL_LOOKBACK_MINUTES),
        until=center + timedelta(minutes=15),
    ):
        candidates.setdefault(deploy["deploy_id"], deploy)

    return sorted(candidates.values(), key=lambda d: d["deployed_at"], reverse=True)


def _precedent_fixes(incident: dict[str, Any]) -> list[dict[str, Any]]:
    """How the same failure shape was actually fixed before, and how fast.

    This is the single most useful input to a change plan: a rollback whose
    precedent resolved a prior SEV1 in twelve minutes is a different proposition
    from a novel forward fix, and the draft prompt is told to prefer it.
    """
    fixes = []
    for related_id in incident.get("related_incident_ids", [])[:MAX_PRECEDENT]:
        related = data.get_incident(related_id)
        if related and related.get("resolution"):
            fixes.append(
                {
                    "incident_id": related["incident_id"],
                    "title": related["title"],
                    "root_cause": related.get("root_cause"),
                    "resolution": related["resolution"],
                    "ttr_minutes": related.get("ttr_minutes"),
                }
            )
    return fixes


@traced_step(name="assemble_change_context")
def _assemble(incident: dict[str, Any]) -> dict[str, Any]:
    """Gather everything a change plan has to be grounded in. No model call."""
    catalog = data.get_service(incident["service"])
    dependencies = [
        dependency
        for name in (catalog.get("depends_on", []) if catalog else [])
        if (dependency := data.get_service(name))
    ]

    # Reviewed step summaries only. `raw_steps` are un-reviewed operator text that
    # can be destructive and, in RB-031's case, carry a production credential —
    # putting them in a drafting prompt is how a credential ends up in a diff.
    runbooks = [
        {k: v for k, v in runbook.items() if k != "raw_steps"}
        for runbook_id in incident.get("runbook_ids", [])
        if (runbook := data.get_runbook(runbook_id))
    ]

    candidates = _candidate_deploys(incident)

    # Deploy history for the affected service *and* for whatever service each
    # candidate deploy landed on — the causal deploy is usually a dependency's, so
    # the affected service's own history is not the one a rollback returns to.
    #
    # This exists because a rollback plan wants to name what it is returning to,
    # and with only the causal window in context the drafting model invented a
    # plausible-looking "last known-good build" SHA for index-builder, which has
    # exactly one deploy on record. With the history in hand the prior deploy is a
    # real record when one exists, and visibly absent when it does not.
    history_services = sorted(
        {incident["service"], *(d["service"] for d in candidates)} & set(data.SERVICES)
    )

    context = {
        "incident": incident,
        "candidate_deploys": candidates,
        "service_deploy_history": data.find_deploys(services=history_services),
        "service": catalog,
        "dependencies": [
            {"service": d["service"], "team": d["team"], "tier": d["tier"]} for d in dependencies
        ],
        "oncall": data.get_oncall(incident["team"]),
        "runbooks": runbooks,
        "precedent_fixes": _precedent_fixes(incident),
        "available_metrics": {
            name: data.available_metrics(name)
            for name in {incident["service"], *(d["service"] for d in dependencies)}
        },
    }

    span_metadata(
        candidate_deploys=[d["deploy_id"] for d in context["candidate_deploys"]],
        service_deploy_history=[d["deploy_id"] for d in context["service_deploy_history"]],
        precedent_fixes=[p["incident_id"] for p in context["precedent_fixes"]],
        runbooks=[r["runbook_id"] for r in runbooks],
        dependencies=[d["service"] for d in context["dependencies"]],
    )
    return context


@traced_step(name="draft_patch")
def _draft(
    client: OpenAI,
    settings: Settings,
    context: dict[str, Any],
    question: str | None,
    correlation_brief: str | None,
) -> dict[str, Any]:
    """Write the change. Reasoning tier."""
    sections = [f"Incident record:\n{json.dumps(context['incident'], default=str)}"]
    if question:
        sections.append(f"What the responder asked:\n{question}")
    if correlation_brief:
        # The correlation sub-agent has usually already ranked the causes. Passing
        # its brief in is what keeps the drafted change aimed at the mechanism the
        # orchestrator is about to report, rather than at a cause it had ruled out.
        sections.append(f"Correlation brief from the root-cause sub-agent:\n{correlation_brief}")
    sections += [
        f"Deploys in the causal window:\n{json.dumps(context['candidate_deploys'], default=str)}",
        f"Deploy history for the affected service and every service a candidate "
        f"deploy landed on (what a rollback would be returning to; if there is no "
        f"earlier deploy here, there is no prior build ID to name):\n"
        f"{json.dumps(context['service_deploy_history'], default=str)}",
        f"Affected service:\n{json.dumps(context['service'], default=str)}",
        f"Its dependencies:\n{json.dumps(context['dependencies'], default=str)}",
        f"On-call rotation for the owning team:\n{json.dumps(context['oncall'], default=str)}",
        f"Reviewed runbook steps:\n{json.dumps(context['runbooks'], default=str)}",
        f"How this failure shape was fixed before:\n"
        f"{json.dumps(context['precedent_fixes'], default=str)}",
        f"Metrics available for verification:\n"
        f"{json.dumps(context['available_metrics'], default=str)}",
    ]

    plan = complete_json(
        client,
        settings,
        settings.reasoning_model,
        "fix-change-plan",
        "\n\n".join(sections),
    )
    span_metadata(
        change_type=plan.get("change_type"),
        target_deploy_id=plan.get("target_deploy_id"),
        reversible=plan.get("reversible"),
        step_count=len(plan.get("steps") or []),
        verification_count=len(plan.get("verification") or []),
        model=settings.reasoning_model,
    )
    return plan


@traced_step(name="review_patch")
def _review(
    client: OpenAI, settings: Settings, incident: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    """Adversarially review the drafted change. Reasoning tier."""
    review = complete_json(
        client,
        settings,
        settings.reasoning_model,
        "fix-safety-review",
        f"Incident: {incident['incident_id']} — {incident['title']} "
        f"({incident['severity']}, {incident['service']}, {incident['domain']})\n"
        f"Customer impact: {incident.get('customer_impact')}\n\n"
        f"Proposed change plan:\n{json.dumps(plan, default=str, indent=2)}",
    )
    span_metadata(
        verdict=review.get("verdict"),
        risk=review.get("risk"),
        finding_count=len(review.get("findings") or []),
        destructive_step_count=len(review.get("destructive_steps") or []),
        credentials_present=review.get("credentials_present"),
        model=settings.reasoning_model,
    )
    return review


@traced_step(name="fix_subagent")
def run_fix_simulation(
    client: OpenAI,
    settings: Settings,
    incident_id: str,
    question: str | None = None,
    correlation_brief: str | None = None,
) -> dict[str, Any]:
    """Draft and review a simulated fix for one incident.

    Returns the change plan, the reviewer's verdict, and the context identifiers the
    plan rests on. Team and domain are echoed at the top level on purpose: the root
    span's entity extraction reads tool results, so a turn that only ever drafted a
    fix is still sliceable by team and failure domain.
    """
    incident = data.get_incident(incident_id)
    if incident is None:
        known = ", ".join(i["incident_id"] for i in data.find_incidents(status="ACTIVE", limit=6))
        return {
            "error": f"No incident {incident_id!r} in the registry. Active: {known}",
            "change_plan": None,
        }

    context = _assemble(incident)
    plan = _draft(client, settings, context, question, correlation_brief)
    if not plan:
        return {
            "error": "The remediation sub-agent could not produce a structured change plan.",
            "change_plan": None,
            "incident_id": incident["incident_id"],
        }

    review = _review(client, settings, incident, plan)

    span_metadata(
        subagent="remediation",
        incident_id=incident["incident_id"],
        model=settings.reasoning_model,
        reasoning_effort=settings.reasoning_effort,
        change_type=plan.get("change_type"),
        verdict=review.get("verdict"),
    )

    return {
        # Stated three ways because the failure mode that matters is an answer a
        # responder reads as "already fixed". The prompts say it too.
        "simulated": True,
        "applied": False,
        "environment": "staging-replica (dry-run)",
        "incident_id": incident["incident_id"],
        "service": incident["service"],
        "team": incident["team"],
        "domain": incident["domain"],
        "severity": incident["severity"],
        "change_plan": plan,
        "review": review,
        "grounded_in": {
            "candidate_deploys": [d["deploy_id"] for d in context["candidate_deploys"]],
            "precedent_incidents": [p["incident_id"] for p in context["precedent_fixes"]],
            "runbooks": [r["runbook_id"] for r in context["runbooks"]],
        },
        "next_step": (
            "Pass this change plan to validate_fix before recommending it. A change "
            "plan with no test result is a suggestion, not a handoff."
        ),
    }
