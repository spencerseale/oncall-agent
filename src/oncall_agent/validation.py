"""Validation sub-agent: design a test suite for a proposed fix, then run it.

The orchestrator delegates here after :mod:`.remediation` has drafted a change.
Three steps, as its own pipeline:

    plan_test_suite -> execute_test_suite -> summarize_test_results

Steps one and three run the reasoning tier. Step two runs no model at all: given a
suite it is a **deterministic function**, so every number in the report is
derivable from the evidence in the same trace and a grounding judge has something
to check against — the same reason ``data.py`` has a frozen clock and generated
metric series rather than stored ones.

The suite is model-generated, so a *rerun* can produce different test names and a
different verdict. What is fixed is the mapping from a suite to its results: the
same suite always yields the same outcomes, durations included. That is the
property the report rests on, and it is why nothing here samples.

So test outcomes are derived, not sampled:

* **telemetry** tests are answered from ``data.telemetry`` — the real pre-change
  anomaly peak against the metric's real baseline. The post-change value is
  simulated as a return to baseline plus a hash-derived residual, and the test
  passes on the same 25% deviation threshold ``data.telemetry`` itself uses to
  call a window anomalous.
* a **load test against a rollback** is answered mechanically: reverting a change
  that shipped to improve something restores the condition it was shipped to
  improve. Rolling ``dep-9f31c2a`` back to a flush size of 200 is the correct
  mitigation for INC-4821 *and* it reinstates the write amplification the deploy
  was addressing. That is a real tradeoff a responder needs stated, not a coin
  flip, and it is why the flagship incident lands on "ship behind a canary"
  rather than a clean pass.
* **every other kind** is answered by hashing the test's identity together with
  the incident and the change, and comparing against odds set by the change's own
  risk. A high-risk change fails tests more often than a low-risk one; a change
  with precedent — a prior incident this same fix already resolved — fails less;
  load tests fail more, because "works in staging, not at production throughput"
  is the failure this suite exists to catch.

Nothing is applied. There is no production surface here to apply it to, and the
result says so in three places for the same reason :mod:`.remediation` does.

The OpenAI client is passed in by the caller; this module never constructs one.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from openai import OpenAI

from . import data
from .config import Settings
from .llm import complete, complete_json
from .tracing import span_metadata, traced_step

logger = logging.getLogger(__name__)

MAX_TESTS = 8

# Deviation from baseline above which a metric is not considered recovered. Same
# threshold data.telemetry uses to flag a window as anomalous, so "the test says
# recovered" and "the telemetry tool says normal" cannot disagree.
RECOVERY_TOLERANCE = 0.25

# One failure in N tests, by the change's own risk. A plan that reverts a
# high-risk deploy is touching the thing that broke, so its suite finds more.
_ODDS_BY_RISK = {"high": 4, "medium": 8, "low": 16}
_ODDS_DEFAULT = 6
# A fix with precedent — a prior incident resolved by this same change — is a
# known quantity, and its suite should read like one.
_PRECEDENT_MULTIPLIER = 3
# "Passes in staging, falls over at incident throughput" is the specific failure a
# load test is there to catch, so it is the kind most likely to find something.
_LOAD_DIVISOR = 2

# Words in a deploy summary that mark it as having shipped to improve something.
# Reverting such a deploy is often the right mitigation and always reinstates the
# condition it was shipped to improve, which is the tradeoff a load test surfaces.
# A declared vocabulary over nine synthetic deploy summaries, in the same spirit
# as the cue lists inlined in the handoff code scorer — it discriminates the perf
# changes (batch flush size, caches, consumer parallelism, retry intervals) from
# the schema migration, the copy change, and the library bump.
_OPTIMIZATION_CUES = (
    "cut",
    "reduce",
    "improve",
    "optimiz",
    "speed",
    "throughput",
    "latency",
    "amplification",
    "parallelism",
    "cache",
    "tighten",
)


def _digest(*parts: Any) -> int:
    """A stable integer from the test's identity. Deterministic across runs."""
    payload = "|".join(str(part) for part in parts)
    return int.from_bytes(hashlib.sha256(payload.encode()).digest()[:8], "big")


def _failure_odds(kind: str, change_risk: str | None, has_precedent: bool) -> int:
    """One-in-N odds this test finds something, from the change's own properties."""
    odds = _ODDS_BY_RISK.get((change_risk or "").lower(), _ODDS_DEFAULT)
    if has_precedent:
        odds *= _PRECEDENT_MULTIPLIER
    if kind == "load":
        odds = max(2, odds // _LOAD_DIVISOR)
    return odds


def _target_deploy(plan: dict[str, Any]) -> dict[str, Any] | None:
    """The deploy this plan reverts, when it reverts one."""
    target = plan.get("target_deploy_id")
    if not target:
        return None
    return next((d for d in data.DEPLOYS if d["deploy_id"] == target), None)


def _reinstated_condition(plan: dict[str, Any], deploy: dict[str, Any] | None) -> str | None:
    """What a rollback of ``deploy`` puts back, if the deploy shipped to improve it."""
    if not deploy or (plan.get("change_type") or "").lower() != "rollback":
        return None
    summary = deploy.get("summary") or ""
    if not any(cue in summary.lower() for cue in _OPTIMIZATION_CUES):
        return None
    return summary


def _has_precedent(incident: dict[str, Any]) -> bool:
    """Whether a linked prior incident was actually resolved, fix and all."""
    return any(
        (related := data.get_incident(related_id)) and related.get("resolution")
        for related_id in incident.get("related_incident_ids", [])
    )


def _telemetry_outcome(
    test: dict[str, Any], incident: dict[str, Any], seed: int
) -> dict[str, Any]:
    """Answer a telemetry test from the real series, with a simulated recovery."""
    service = test.get("target_service") or incident["service"]
    metric = test.get("metric") or ""
    series = data.telemetry(
        service=service,
        metric=metric,
        center=data._parse(incident["opened_at"]),
        window_minutes=60,
    )
    if "error" in series:
        # A test naming a metric that does not exist is a real finding about the
        # suite, not a failure of the change. Reported as such rather than hidden.
        return {
            "status": "error",
            "detail": series["error"],
            "blocking": False,
        }

    baseline = series["baseline"]
    observed = series["max"]
    # Simulated recovery: back to baseline, plus a residual that keeps the number
    # from looking suspiciously exact. Deterministic, so it is the same every run.
    residual = ((seed % 41) - 20) / 100.0 * RECOVERY_TOLERANCE
    simulated = round(baseline * (1 + residual), 4)
    deviation = abs(simulated - baseline) / max(abs(baseline), 1e-9)
    recovered = deviation <= RECOVERY_TOLERANCE

    return {
        "status": "pass" if recovered else "fail",
        "unit": series["unit"],
        "baseline": baseline,
        "observed_pre_change": observed,
        "simulated_post_change": simulated,
        "detail": (
            f"{metric} on {service}: {observed} {series['unit']} during the incident "
            f"window, {simulated} simulated post-change against a {baseline} baseline"
        ),
        "blocking": bool(test.get("blocking", True)),
    }


@traced_step(name="plan_test_suite")
def _plan_suite(
    client: OpenAI,
    settings: Settings,
    incident: dict[str, Any],
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Design the suite. Reasoning tier."""
    catalog = data.get_service(incident["service"])
    dependents = catalog.get("dependents", []) if catalog else []
    metrics = {
        name: data.available_metrics(name)
        for name in {
            incident["service"],
            plan.get("target_service") or incident["service"],
            *(catalog.get("depends_on", []) if catalog else []),
        }
        if name in data.SERVICES
    }

    suite = complete_json(
        client,
        settings,
        settings.reasoning_model,
        "fix-test-plan",
        f"Incident: {incident['incident_id']} — {incident['title']} "
        f"({incident['severity']}, service {incident['service']}, team {incident['team']}, "
        f"domain {incident['domain']})\n"
        f"Customer impact: {incident.get('customer_impact')}\n"
        f"Services that depend on {incident['service']}: {', '.join(dependents) or 'none'}\n\n"
        f"Proposed change plan:\n{json.dumps(plan, default=str, indent=2)}\n\n"
        f"Metrics available per service (a telemetry test may name only these):\n"
        f"{json.dumps(metrics, default=str)}\n\n"
        f"Prior incidents on this failure shape:\n"
        f"{json.dumps(incident.get('related_incident_ids', []))}",
    )

    tests = [t for t in (suite.get("tests") or []) if isinstance(t, dict)][:MAX_TESTS]
    span_metadata(
        test_count=len(tests),
        kinds=sorted({str(t.get("kind")) for t in tests}),
        blocking_count=sum(1 for t in tests if t.get("blocking")),
        model=settings.reasoning_model,
    )
    return {"tests": tests, "not_covered": suite.get("not_covered") or []}


@traced_step(name="execute_test_suite")
def _execute(
    incident: dict[str, Any], plan: dict[str, Any], suite: dict[str, Any]
) -> dict[str, Any]:
    """Simulate the run. No model call, and the same result on every run."""
    deploy = _target_deploy(plan)
    change_risk = deploy.get("change_risk") if deploy else None
    reinstated = _reinstated_condition(plan, deploy)
    has_precedent = _has_precedent(incident)
    change_type = plan.get("change_type") or "unknown"

    results: list[dict[str, Any]] = []
    for index, test in enumerate(suite["tests"], start=1):
        test_id = str(test.get("id") or f"T{index}")
        kind = str(test.get("kind") or "unit").lower()
        seed = _digest(incident["incident_id"], change_type, test_id, test.get("name"), kind)

        if kind == "telemetry":
            outcome = _telemetry_outcome(test, incident, seed)
        elif reinstated:
            # Non-blocking on purpose: the rollback is still the right call, and a
            # verdict of "hold" here would argue for leaving a SEV1 burning to
            # avoid reintroducing a write-amplification regression. The responder
            # needs the tradeoff named, not the mitigation blocked.
            outcome = {
                "status": "fail" if kind == "load" else "pass",
                "detail": (
                    f"Reverting {deploy['deploy_id']} reinstates the condition it "
                    f"shipped to address ({reinstated}). The rollback is still the "
                    "faster mitigation; the original problem returns with it."
                    if kind == "load"
                    else test.get("asserts") or "assertion held"
                ),
                "blocking": False if kind == "load" else bool(test.get("blocking", True)),
            }
        else:
            odds = _failure_odds(kind, change_risk, has_precedent)
            passed = seed % odds != 0
            outcome = {
                "status": "pass" if passed else "fail",
                "detail": (
                    test.get("asserts") or "assertion held"
                    if passed
                    else test.get("fails_when") or "assertion did not hold"
                ),
                "blocking": bool(test.get("blocking", True)),
            }

        results.append(
            {
                "id": test_id,
                "name": test.get("name"),
                "kind": kind,
                "target_service": test.get("target_service"),
                # Seconds, derived from the same seed so a rerun reports the same
                # wall clock. A suite whose duration moved between runs would make
                # the trace look non-deterministic when it is not.
                "duration_seconds": 5 + (seed >> 8) % 175,
                **outcome,
            }
        )

    failed = [r for r in results if r["status"] == "fail"]
    blocking_failures = [r for r in failed if r["blocking"]]
    errored = [r for r in results if r["status"] == "error"]

    if blocking_failures:
        verdict = "hold"
    elif failed or errored:
        verdict = "canary"
    else:
        verdict = "ship"

    span_metadata(
        verdict=verdict,
        total=len(results),
        passed=sum(1 for r in results if r["status"] == "pass"),
        failed=len(failed),
        blocking_failures=[r["id"] for r in blocking_failures],
        errored=[r["id"] for r in errored],
        change_risk=change_risk,
        has_precedent=has_precedent,
        reinstates_condition=bool(reinstated),
        deterministic=True,
    )
    return {
        "verdict": verdict,
        "results": results,
        "totals": {
            "total": len(results),
            "passed": sum(1 for r in results if r["status"] == "pass"),
            "failed": len(failed),
            "errored": len(errored),
            "blocking_failures": len(blocking_failures),
            "duration_seconds": sum(r["duration_seconds"] for r in results),
        },
    }


@traced_step(name="summarize_test_results")
def _summarize(
    client: OpenAI,
    settings: Settings,
    incident: dict[str, Any],
    plan: dict[str, Any],
    suite: dict[str, Any],
    run: dict[str, Any],
) -> str:
    """Turn results into a ship / canary / hold report. Reasoning tier."""
    return complete(
        client,
        settings,
        settings.reasoning_model,
        "fix-test-report",
        f"Incident: {incident['incident_id']} — {incident['title']} "
        f"({incident['severity']}, {incident['service']}, team {incident['team']})\n\n"
        f"Change plan under test:\n{json.dumps(plan, default=str, indent=2)}\n\n"
        f"Suite the plan was tested against:\n{json.dumps(suite['tests'], default=str, indent=2)}\n\n"
        f"What the suite states it does not cover:\n{json.dumps(suite['not_covered'])}\n\n"
        f"Simulated results:\n{json.dumps(run['results'], default=str, indent=2)}\n\n"
        f"Totals: {json.dumps(run['totals'])}\n"
        f"Computed verdict (deterministic, not yours to change): {run['verdict']}",
    )


@traced_step(name="test_subagent")
def run_fix_validation(
    client: OpenAI,
    settings: Settings,
    incident_id: str,
    change_plan: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Design and simulate a test suite for a proposed fix.

    ``change_plan`` is the ``change_plan`` object ``propose_fix`` returned. A string
    is accepted because the orchestrator sometimes passes the plan back as JSON
    text; anything unparseable becomes a thin plan rather than an error, so the
    suite still runs against the incident.
    """
    incident = data.get_incident(incident_id)
    if incident is None:
        known = ", ".join(i["incident_id"] for i in data.find_incidents(status="ACTIVE", limit=6))
        return {
            "error": f"No incident {incident_id!r} in the registry. Active: {known}",
            "verdict": None,
        }

    if isinstance(change_plan, str):
        try:
            change_plan = json.loads(change_plan)
        except json.JSONDecodeError:
            change_plan = {"summary": change_plan}
    plan = change_plan if isinstance(change_plan, dict) else {}
    if not plan:
        # Validating nothing in particular is worse than saying so: a suite designed
        # against an absent plan tests the incident, not the change.
        plan = {
            "summary": "(no change plan supplied — call propose_fix first)",
            "change_type": "unknown",
        }

    suite = _plan_suite(client, settings, incident, plan)
    if not suite["tests"]:
        return {
            "error": "The validation sub-agent could not produce a test suite.",
            "verdict": None,
            "incident_id": incident["incident_id"],
        }

    run = _execute(incident, plan, suite)
    report = _summarize(client, settings, incident, plan, suite, run)

    span_metadata(
        subagent="validation",
        incident_id=incident["incident_id"],
        model=settings.reasoning_model,
        reasoning_effort=settings.reasoning_effort,
        verdict=run["verdict"],
        change_type=plan.get("change_type"),
    )

    return {
        "simulated": True,
        "applied": False,
        "environment": "staging-replica (dry-run)",
        "incident_id": incident["incident_id"],
        "service": incident["service"],
        "team": incident["team"],
        "domain": incident["domain"],
        "severity": incident["severity"],
        "verdict": run["verdict"],
        "report": report,
        "totals": run["totals"],
        "tests": run["results"],
        "not_covered": suite["not_covered"],
        "plan_under_test": plan.get("summary"),
    }
