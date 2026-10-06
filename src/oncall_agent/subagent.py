"""Root-cause correlation sub-agent.

A self-contained three-step agent the orchestrator delegates to whenever a
question needs precedent rather than a lookup: "what does this look like, and has
it happened before?"

Running it as a sub-agent rather than a flat search call buys two things, both of
which matter for the demo:

1. **Context isolation** — the orchestrator receives one ranked correlation brief
   instead of a dozen raw incident records and web excerpts.
2. **A nested trace** — Braintrust shows ``correlation_subagent`` as its own
   subtree (hypothesize -> gather -> synthesize), so the correlation step can be
   scored and debugged independently of the orchestrator.

The OpenAI client is passed in by the caller; this module never constructs one.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from . import data
from .config import Settings
from .exa import ExaClient, OPERATIONS_DOMAINS, SearchHit
from .llm import complete, complete_json
from .tracing import span_metadata, traced_step

logger = logging.getLogger(__name__)

MAX_HYPOTHESES = 4
MAX_SEARCHES = 3
RESULTS_PER_SEARCH = 4
PRECEDENT_PER_SEARCH = 3


@traced_step(name="plan_hypotheses")
def _plan(
    client: OpenAI, settings: Settings, question: str, incident: dict[str, Any] | None
) -> dict[str, list[str]]:
    """Turn the incident and its signals into checkable candidate causes."""
    context = question
    if incident:
        # Give the planner the record rather than a summary of it: the timeline
        # and the alert/deploy IDs are what make a hypothesis checkable.
        context = f"{question}\n\nIncident record:\n{json.dumps(incident, default=str)}"

    parsed = complete_json(
        client, settings, settings.subagent_model, "incident-hypotheses", context
    )

    def _strings(key: str, limit: int) -> list[str]:
        values = parsed.get(key, [])
        return [v.strip() for v in values if isinstance(v, str) and v.strip()][:limit]

    hypotheses = _strings("hypotheses", MAX_HYPOTHESES)
    searches = _strings("searches", MAX_SEARCHES) or [question]

    span_metadata(hypothesis_count=len(hypotheses), search_count=len(searches))
    return {"hypotheses": hypotheses, "searches": searches}


@traced_step(name="gather_precedent")
def _gather(
    exa: ExaClient | None,
    searches: list[str],
    incident: dict[str, Any] | None,
) -> dict[str, Any]:
    """Collect internal precedent and external writeups for each search term.

    Internal precedent comes first and is weighted more heavily downstream: a
    prior incident on this platform carries its own resolution time and actual
    fix, which is the most actionable thing precedent can offer.
    """
    seen_incidents: set[str] = set()
    precedent: list[dict[str, Any]] = []

    # Incidents the record already points at are precedent by definition.
    for related_id in (incident or {}).get("related_incident_ids", []):
        related = data.get_incident(related_id)
        if related and related["incident_id"] not in seen_incidents:
            seen_incidents.add(related["incident_id"])
            precedent.append({"match": "linked from the incident record", **related})

    for term in searches:
        for match in data.find_incidents(text=term, limit=PRECEDENT_PER_SEARCH):
            if match["incident_id"] in seen_incidents:
                continue
            if match["incident_id"] == (incident or {}).get("incident_id"):
                continue
            seen_incidents.add(match["incident_id"])
            precedent.append({"match": f"text search: {term}", **match})

    postmortems = []
    seen_pms: set[str] = set()
    for match in precedent:
        pm_id = match.get("postmortem_id")
        if pm_id and pm_id not in seen_pms:
            pm = data.get_postmortem(pm_id)
            if pm:
                seen_pms.add(pm_id)
                postmortems.append(pm)

    hits: list[SearchHit] = []
    if exa is not None:
        seen_urls: set[str] = set()
        for term in searches:
            try:
                results = exa.search(
                    term,
                    num_results=RESULTS_PER_SEARCH,
                    include_domains=OPERATIONS_DOMAINS,
                )
            except Exception as exc:  # network failure should degrade, not crash
                logger.warning("Exa search failed for %r: %s", term, exc)
                continue
            for hit in results:
                if hit.url and hit.url not in seen_urls:
                    seen_urls.add(hit.url)
                    hits.append(hit)

    span_metadata(
        internal_precedent=len(precedent),
        postmortems=len(postmortems),
        external_sources=len(hits),
        searches_run=len(searches),
    )
    return {"precedent": precedent, "postmortems": postmortems, "hits": hits}


@traced_step(name="synthesize_correlation")
def _synthesize(
    client: OpenAI,
    settings: Settings,
    question: str,
    hypotheses: list[str],
    gathered: dict[str, Any],
) -> str:
    """Rank the hypotheses against the gathered precedent."""
    precedent = gathered["precedent"]
    postmortems = gathered["postmortems"]
    hits: list[SearchHit] = gathered["hits"]

    if not precedent and not hits:
        return (
            "No internal precedent or external source was retrieved for this failure "
            "shape. Treat this as an absence of retrieved precedent, not as evidence "
            "that the failure mode is new."
        )

    internal = "\n\n".join(
        f"[{p['incident_id']}] {p['title']}\n"
        f"matched by: {p['match']}\n"
        f"severity={p['severity']} team={p['team']} domain={p['domain']} "
        f"status={p['status']} ttd={p['ttd_minutes']}m ttr={p['ttr_minutes']}m\n"
        f"root cause: {p.get('root_cause') or '(unresolved)'}\n"
        f"resolution: {p.get('resolution') or '(unresolved)'}"
        for p in precedent
    ) or "(none retrieved)"

    pm_text = "\n\n".join(
        f"[{pm['postmortem_id']}] on {pm['incident_id']}\n"
        f"summary: {pm['summary']}\n"
        f"contributing factors: " + "; ".join(pm["contributing_factors"]) + "\n"
        f"what didn't work: {pm['what_didnt']}"
        for pm in postmortems
    ) or "(none retrieved)"

    external = "\n\n".join(
        f"[{i}] {hit.title}\nURL: {hit.url}\n"
        f"Published: {hit.published_date or 'unknown'}\nExcerpt: {hit.text[:1000]}"
        for i, hit in enumerate(hits, start=1)
    ) or "(none retrieved)"

    return complete(
        client,
        settings,
        settings.subagent_model,
        "incident-correlation-brief",
        f"Question: {question}\n\n"
        f"Candidate causes to rank:\n"
        + "\n".join(f"- {h}" for h in hypotheses)
        + f"\n\nInternal precedent (prior incidents on this platform):\n{internal}"
        f"\n\nPostmortems:\n{pm_text}"
        f"\n\nExternal sources:\n{external}",
    )


@traced_step(name="correlation_subagent")
def run_correlation(
    client: OpenAI,
    settings: Settings,
    question: str,
    incident_id: str | None = None,
) -> dict[str, Any]:
    """Run hypothesize -> gather -> synthesize for one correlation question.

    Returns the brief plus the precedent it rests on, so the orchestrator can keep
    incident IDs and citations intact in its final answer.
    """
    incident = data.get_incident(incident_id) if incident_id else None
    if incident_id and incident is None:
        return {
            "error": f"No incident {incident_id!r} in the registry.",
            "brief": "",
            "sources": [],
        }

    planned = _plan(client, settings, question, incident)
    exa = ExaClient(api_key=settings.exa_api_key or "") if settings.web_search_enabled else None
    gathered = _gather(exa, planned["searches"], incident)
    brief = _synthesize(client, settings, question, planned["hypotheses"], gathered)

    span_metadata(
        subagent="root_cause_correlation",
        incident_id=incident_id,
        model=settings.subagent_model,
        web_search_used=exa is not None,
    )

    return {
        "brief": brief,
        "hypotheses": planned["hypotheses"],
        "searches": planned["searches"],
        "internal_precedent": [
            {
                "incident_id": p["incident_id"],
                "title": p["title"],
                "ttr_minutes": p["ttr_minutes"],
                "resolution": p.get("resolution"),
            }
            for p in gathered["precedent"]
        ],
        "sources": [
            {"n": i, "title": h.title, "url": h.url, "published_date": h.published_date}
            for i, h in enumerate(gathered["hits"], start=1)
        ],
    }
