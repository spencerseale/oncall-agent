"""Generate realistic agent traffic to populate Topics and the trace log.

Topic generation needs ~100 facet summaries before it will cluster, so a fresh
project shows an empty Topics page. This drives real agent runs — real tool calls,
real sub-agent invocations, real traces — against a question bank built to produce
interesting structure in all three custom facets.

The bank is deliberately shaped:

* **Blocker questions** (~40%) ask for things the agent genuinely cannot provide:
  span-level latency attribution, feature-flag history, pod events, log lines,
  incident-channel transcripts, capacity forecasts, change-approval records, SLA
  credit exposure. Each theme has many phrasings across incidents and services,
  so ``resolution_blocker`` clusters them into a ranked reliability backlog rather
  than a scatter of one-offs.
* **Answerable questions** (~40%) exercise the real tools and resolve to
  ``no_match`` on the blocker facet — the control group that keeps it honest. They
  are chosen to provoke *different* classes of fix (rollback, flag disable, scale
  up, provider failover, region pin, job defer, escalate) so
  ``remediation_pattern`` has real variety to cluster.
* **Research questions** (~20%) invoke the correlation sub-agent and Exa. Kept to
  a modest share because each costs extra LLM calls plus web round-trips.

Every question is anchored to a team, so ``failure_domain`` gets a genuine
cross-team distribution rather than one dominant cluster.

Seeded traffic is tagged ``mode:batch`` so it stays distinguishable from hand-run
demo traffic while still landing in ``env:prod`` dashboards.

Usage:
    uv run python scripts/seed_traffic.py --count 300
    uv run python scripts/seed_traffic.py --count 20 --workers 4   # smoke test
    uv run python scripts/seed_traffic.py --dry-run                # print only
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from oncall_agent import data
from oncall_agent.agent import OncallSession
from oncall_agent.config import load_settings

# --------------------------------------------------------------------------
# Question bank
# --------------------------------------------------------------------------

# Substitution contexts, built from the synthetic data rather than hand-listed, so
# a template's incident, service, and team always belong together. Expanding the
# three placeholders independently produced questions like "did anyone change pool
# sizes on index-builder before INC-4803" — INC-4803 is a notify-dispatcher
# incident, and an incoherent question makes the facet label noise rather than a
# cluster.
INCIDENT_CONTEXTS = [
    {"inc": incident["incident_id"], "svc": incident["service"], "team": incident["team"]}
    for incident in data.INCIDENTS
]

SERVICE_CONTEXTS = [
    {"svc": record["service"], "team": record["team"]} for record in data.SERVICES.values()
]

# Things the agent genuinely cannot answer. Each theme gets several phrasings so
# the facet has enough volume per theme to form a real cluster. `{inc}` expands
# over incidents, `{svc}` over services, `{team}` over teams.
BLOCKER_THEMES: dict[str, list[str]] = {
    "span_level_attribution": [
        "Break down the p99 on {inc} by downstream call — which span is actually slow?",
        "Show me the slowest trace for {svc} right now with per-span timings.",
        "For {inc}, which downstream dependency contributes the most to tail latency?",
        "Give me a flame graph view of a failing request on {svc}.",
    ],
    "customer_impact_detail": [
        "Exactly how many customers were affected by {inc}?",
        "What is the revenue impact of {inc} so far, by region?",
        "Which specific accounts hit errors during {inc}?",
        "How many of the affected sessions on {inc} eventually succeeded on retry?",
    ],
    "feature_flag_history": [
        "What feature flags changed on {svc} in the last hour?",
        "Show me the flag audit log around {inc}.",
        "Who last modified the flags on {svc}, and when?",
        "Was any flag rolled out to 100% before {inc} opened?",
    ],
    "config_change_history": [
        "Show me every configuration change to {svc} this week.",
        "Did anyone change pool sizes or timeouts on {svc} before {inc}?",
        "What is the diff between the current and previous config on {svc}?",
    ],
    "pod_and_container_events": [
        "Which pods restarted on {svc} during {inc}?",
        "Were there any OOMKills on {svc} in the last hour?",
        "Show me the Kubernetes events for {svc} around {inc}.",
        "How many replicas of {svc} are running right now?",
    ],
    "log_search": [
        "Show me the error log lines from {svc} at the time {inc} opened.",
        "Grep {svc} logs for connection timeout exceptions during {inc}.",
        "What is the most common stack trace on {svc} right now?",
        "Pull the last 50 error lines from {svc}.",
    ],
    "incident_channel_transcript": [
        "What did the responders discuss in the incident channel during {inc}?",
        "Summarize the {team} team's chat during {inc}.",
        "Who said they were taking over mitigation on {inc}?",
    ],
    "capacity_forecasting": [
        "At current growth, when will {svc} run out of headroom?",
        "Do we have enough capacity on {svc} for next month's peak?",
        "Forecast the queue depth on {svc} over the next six hours.",
        "How much should we over-provision {svc} to survive a 3x spike?",
    ],
    "external_dependency_slo": [
        "What is our contractual SLA with the payment processor, and are they in breach?",
        "Show me the third-party provider's historical uptime for the last quarter.",
        "Which of our external dependencies has the worst reliability record?",
        "What is the SLA credit exposure from {inc}?",
    ],
    "long_horizon_metric_history": [
        "Show me the {svc} error rate trend over the last 90 days.",
        "How has the {team} team page volume changed year over year?",
        "Plot queue depth on notify-dispatcher for the past six months.",
        "What is the long-term latency trend for {svc}?",
    ],
    "change_approval_records": [
        "Was the deploy involved in {inc} reviewed and approved? By whom?",
        "Show me the change-approval record for the last high-risk deploy to {svc}.",
        "Did {inc} involve a change that skipped the review process?",
    ],
    "synthetic_and_rum": [
        "What did the synthetic checkout probe see during {inc}?",
        "Show me real-user monitoring data for {svc} in the last hour.",
        "Did our external uptime checks catch {inc} before the internal alert?",
    ],
    "oncall_load": [
        "How many pages did the {team} primary get last week?",
        "Which team has the worst on-call page load right now?",
        "How much of {team}'s paging happens outside business hours?",
        "Is anyone on {team} at risk of on-call burnout?",
    ],
    "slo_burn_rate": [
        "What is the current error budget burn rate for {svc}?",
        "At this rate, when does {svc} exhaust its error budget?",
        "How much of {team}'s error budget did {inc} consume?",
    ],
    "security_audit": [
        "Who accessed production credentials for {svc} in the last day?",
        "Show me the audit log for privileged actions on {svc}.",
        "Has the credential embedded in RB-031 ever been used?",
    ],
    "dependency_topology": [
        "Show me the full transitive dependency graph for {svc}.",
        "What is the current cross-region replication topology?",
        "Which services would be affected if {svc} went down completely?",
    ],
}

# Questions the agent CAN answer from its tools. These resolve to `no_match` on
# resolution_blocker, and are chosen to provoke distinct classes of remediation so
# remediation_pattern has variety to cluster.
ANSWERABLE: list[str] = [
    # Deploy-regression shape -> rollback
    "INC-4821 just paged me. What is going on and what should I do first?",
    "Is there a deploy that correlates with INC-4821? Should I roll it back?",
    "What changed on ledger-writer before INC-4821 opened?",
    "Why is index-builder failing on INC-4826, and what is the fix?",
    "Should I roll back dep-7c22e18 or ship a forward fix for INC-4826?",
    "Walk me through the timeline of INC-4821 with timestamps.",
    "Which alert fired first on INC-4821, and did it page anyone?",
    # Third-party / no-deploy shape -> external investigation, failover
    "Why is checkout-web slow on INC-4830? Nothing deployed.",
    "INC-4830 has no deploys in the window. What are the remaining candidates?",
    "Is checkout-web failing or just slow on INC-4830?",
    "How did we resolve the notification backlog in INC-4803?",
    # Config / flag shape -> disable flag
    "How was INC-4788 resolved, and how long did it take?",
    "What is the safest first mitigation for an auth-gateway token rejection spike?",
    "Summarize RB-038 for me.",
    # Capacity shape -> scale
    "Walk me through recovering session-store memory pressure.",
    "What is in RB-031, and is it safe to follow?",
    "Why did session-store evict live sessions in INC-4817?",
    "What was the fix for the search latency incident INC-4795?",
    # Network shape -> pin region
    "How did we mitigate the events-ingest replication lag in INC-4810?",
    "What is the current replication lag on events-ingest?",
    # Telemetry reads
    "Show me db_pool_saturation on ledger-writer around INC-4821.",
    "What is the 5xx rate on payments-api right now?",
    "Compare checkout-web p99 latency against its baseline for INC-4830.",
    "Is the index-builder job failure count still climbing on INC-4826?",
    "What is the memory used ratio on session-store?",
    # Ownership and routing
    "Who owns index-builder and who should I page for INC-4826?",
    "What is the escalation policy for the payments team?",
    "Who is on call for identity right now?",
    "Which team owns warehouse-sync?",
    "What does payments-api depend on?",
    "Which services depend on session-store?",
    "How much error budget does ledger-writer have left?",
    # Registry queries
    "What incidents are currently active?",
    "Show me every SEV1 we have had this quarter.",
    "Has the payments team had a database incident before?",
    "List the incidents in the third-party failure domain.",
    # Resolution-speed analysis
    "Which team is slowest to resolve incidents, and is it detection or repair?",
    "What is our median time to resolution, and which domain is worst?",
    "Which incident took longest to resolve, and why?",
    "How much of our resolution time is spent before anyone notices?",
    "Compare detection time across teams.",
    "Are SEV1s resolved faster than SEV2s?",
    # Remediation shape -> the fix and validation sub-agents. These are the most
    # expensive questions in the bank: four reasoning-model calls apiece on top of
    # the orchestrator's own turns. Kept to a handful of phrasings deliberately —
    # enough for remediation_pattern to cluster the change types the fix agent
    # actually proposes, not enough to dominate the seed's cost.
    "Draft the fix for INC-4821 and tell me whether it passes its tests.",
    "What exactly should I change to resolve INC-4826, and is it safe to ship?",
    "Write me the rollback plan for dep-9f31c2a and validate it.",
    "Is there a forward fix for INC-4826 that does not need a schema rollback?",
    "Propose a mitigation for INC-4830 and test it before I page anyone.",
    "What would break if we rolled back dep-9f31c2a?",
    # Knowledge hygiene
    "Which runbooks are past their review date?",
    "Are there overdue action items from the payments postmortems?",
    "What did we say we would fix after INC-4803, and did we?",
    "Summarize the contributing factors from PM-4779.",
]

# Research questions: these invoke the correlation sub-agent, which costs extra
# LLM calls plus Exa round-trips. Kept to a modest share of the mix.
RESEARCH: list[str] = [
    "Has the ledger-writer connection pool exhaustion in INC-4821 happened before, and how was it fixed?",
    "Correlate INC-4830 against precedent — is this us or the payment processor?",
    "What are the likely causes of INC-4826, ranked, with precedent?",
    "Is there a known issue with pgbouncer transaction pooling under high batch concurrency?",
    "Has anyone published guidance on safe batch-size changes behind a connection pooler?",
    "Are there any current outages at our upstream payment or cloud providers?",
    "What does the industry recommend for retry backoff against a provider rate limit?",
    "Find precedent for a Redis eviction policy causing session loss.",
    "Is there a known Kafka consumer stall pattern behind cross-region replication lag?",
    "What is the standard mitigation for shared-storage IOPS contention between a batch job and a query service?",
]


def build_questions(count: int, seed: int = 7) -> list[str]:
    """Assemble a question list with a realistic blocker / answerable / research mix."""
    rng = random.Random(seed)

    blocker_pool: list[str] = []
    for phrasings in BLOCKER_THEMES.values():
        for template in phrasings:
            # A template naming an incident has to be filled from an incident's own
            # context; one naming only a service can use any service in the catalog.
            contexts = INCIDENT_CONTEXTS if "{inc}" in template else SERVICE_CONTEXTS
            for context in contexts:
                filled = template
                for token, value in context.items():
                    filled = filled.replace("{" + token + "}", value)
                if "{" not in filled:
                    blocker_pool.append(filled)

    # ~40% blockers, ~40% answerable, ~20% research.
    n_blocker = int(count * 0.40)
    n_research = int(count * 0.20)
    n_answerable = count - n_blocker - n_research

    def take(pool: list[str], n: int) -> list[str]:
        """Sample without replacement, reshuffling when the pool is exhausted."""
        out: list[str] = []
        while len(out) < n:
            chunk = pool[:]
            rng.shuffle(chunk)
            out.extend(chunk[: n - len(out)])
        return out

    questions = (
        take(blocker_pool, n_blocker)
        + take(ANSWERABLE, n_answerable)
        + take(RESEARCH, n_research)
    )
    rng.shuffle(questions)
    return questions


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

_local = threading.local()
_counter_lock = threading.Lock()
_done = 0


def _session(settings) -> OncallSession:
    """One reused session per worker thread — rebuilding the graph 300x is waste."""
    if not hasattr(_local, "session"):
        _local.session = OncallSession(settings=settings, mode="batch")
    return _local.session


def run_one(question: str, settings, total: int) -> tuple[str, bool, str]:
    global _done
    session = _session(settings)
    session.reset()  # keep each seeded trace independent
    try:
        response = session.ask(question)
        ok, detail = True, f"{len(response.tool_calls)} tool call(s)"
    except Exception as exc:
        ok, detail = False, str(exc)[:80]

    with _counter_lock:
        _done += 1
        n = _done
    print(f"  [{n:>3}/{total}] {'ok ' if ok else 'ERR'} {question[:62]:<62} {detail}")
    return question, ok, detail


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the project with agent traffic.")
    parser.add_argument("--count", type=int, default=300, help="Traces to generate.")
    parser.add_argument("--workers", type=int, default=12, help="Concurrent agent runs.")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed for reproducibility.")
    parser.add_argument("--dry-run", action="store_true", help="Print questions and exit.")
    args = parser.parse_args()

    questions = build_questions(args.count, seed=args.seed)

    if args.dry_run:
        for i, question in enumerate(questions, 1):
            print(f"{i:>3}. {question}")
        blockers = sum(1 for q in questions if q not in ANSWERABLE and q not in RESEARCH)
        print(
            f"\n{len(questions)} questions ({len(set(questions))} unique): "
            f"{blockers} blocker, "
            f"{sum(1 for q in questions if q in ANSWERABLE)} answerable, "
            f"{sum(1 for q in questions if q in RESEARCH)} research"
        )
        return

    settings = load_settings()
    print(
        f"Seeding {len(questions)} traces into '{settings.braintrust_project}' "
        f"({args.workers} workers, model={settings.model}, tags mode:batch)\n"
    )

    started = time.time()
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run_one, q, settings, len(questions)) for q in questions]
        for future in as_completed(futures):
            question, ok, detail = future.result()
            if not ok:
                failures.append((question, detail))

    elapsed = time.time() - started
    print(
        f"\nDone: {len(questions) - len(failures)}/{len(questions)} succeeded "
        f"in {elapsed / 60:.1f} min"
    )
    if failures:
        reasons = Counter(detail for _, detail in failures)
        print(f"\n{len(failures)} failure(s):")
        for detail, n in reasons.most_common(5):
            print(f"  • {n}x {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
