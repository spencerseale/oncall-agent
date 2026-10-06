"""Wire online scoring automations in Braintrust.

The `bt` CLI can push scorers but does not create online scoring rules, so this
uses the REST API directly:

    GET  /v1/project        -> resolve project ID by name
    GET  /v1/function       -> resolve scorer function IDs by slug
    PUT  /v1/project_score  -> create-or-replace the rule by name (idempotent)

``PUT`` is deliberate. ``POST /v1/project_score`` returns the existing rule
unchanged when the name already exists, which makes re-running silently a no-op;
``PUT`` replaces it, so editing a sampling rate here and re-running actually
takes effect.

Four rules go up, and their sampling rates say what each one is for:

* **Handoff Completeness** is a code scorer — free, deterministic, 100% coverage.
* **Remediation Safety** is a production-safety control and stays at 100%.
* **Diagnostic Accuracy** is a quality control; lower it under volume if needed.
* **Resolution Readiness** is the headroom metric and the most expensive judge.
  Lower it first.

Usage:
    uv run python scripts/automations.py                  # create/update rules
    uv run python scripts/automations.py --list           # show current rules
    uv run python scripts/automations.py --readiness-sampling 0.2
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from dotenv import load_dotenv

# Standalone scripts talk to the REST API directly and never import
# oncall_agent.config, which is where load_dotenv() normally runs — so without this
# they would read only the ambient environment and ignore .env entirely. That is
# fine while a shell happens to export the right key and silently wrong the moment
# .env is the intended source, which is exactly the fresh-org bootstrap case.
load_dotenv()

API_URL = os.environ.get("BRAINTRUST_API_URL", "https://api.braintrust.dev").rstrip("/")
PROJECT_NAME = os.environ.get("BRAINTRUST_PROJECT", "oncall-agent")


@dataclass
class Rule:
    """One online scoring rule to reconcile."""

    name: str
    description: str
    scorer_slug: str
    sampling_rate: float


def _client() -> httpx.Client:
    api_key = os.environ.get("BRAINTRUST_API_KEY", "").strip()
    if not api_key:
        sys.exit("BRAINTRUST_API_KEY is not set.")
    return httpx.Client(
        base_url=API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=60.0,
    )


def _objects(payload: Any) -> list[dict[str, Any]]:
    """Braintrust list endpoints return {"objects": [...]}."""
    if isinstance(payload, dict):
        return payload.get("objects", [])
    return payload if isinstance(payload, list) else []


def resolve_project_id(client: httpx.Client, name: str) -> str:
    response = client.get("/v1/project", params={"project_name": name})
    response.raise_for_status()
    projects = _objects(response.json())
    if not projects:
        sys.exit(
            f"Project {name!r} not found. Run the agent once (make agent) so it gets "
            f"created, or set BRAINTRUST_PROJECT."
        )
    return projects[0]["id"]


def resolve_scorer_ids(client: httpx.Client, project_id: str) -> dict[str, str]:
    """Map scorer slug -> function ID for every scorer in the project."""
    response = client.get(
        "/v1/function",
        params={"project_id": project_id, "function_type": "scorer", "limit": 100},
    )
    response.raise_for_status()
    return {f["slug"]: f["id"] for f in _objects(response.json()) if f.get("slug")}


def list_rules(client: httpx.Client, project_id: str) -> list[dict[str, Any]]:
    response = client.get("/v1/project_score", params={"project_id": project_id, "limit": 100})
    response.raise_for_status()
    return [s for s in _objects(response.json()) if s.get("score_type") == "online"]


def upsert_rule(
    client: httpx.Client, project_id: str, rule: Rule, scorer_id: str
) -> dict[str, Any]:
    """Create or replace one online scoring rule by name."""
    body = {
        "project_id": project_id,
        "name": rule.name,
        "description": rule.description,
        "score_type": "online",
        "config": {
            "online": {
                "sampling_rate": rule.sampling_rate,
                "scorers": [{"type": "function", "id": scorer_id}],
                # Score the agent's root span: it carries the clean
                # input/output/metadata.evidence contract the judges template
                # against. Scoring every span would grade tool spans too, which
                # the judge prompts are not written for.
                "apply_to_root_span": True,
            }
        },
    }
    response = client.put("/v1/project_score", json=body)
    if response.status_code >= 400:
        sys.exit(f"Failed to upsert {rule.name!r}: {response.status_code} {response.text}")
    return response.json()


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure Braintrust online scoring rules.")
    parser.add_argument("--list", action="store_true", help="List existing rules and exit.")
    parser.add_argument(
        "--accuracy-sampling",
        type=float,
        default=float(os.environ.get("ONCALL_ACCURACY_SAMPLING", "1.0")),
        help="Sampling rate for Diagnostic Accuracy (0-1). Default 1.0.",
    )
    parser.add_argument(
        "--safety-sampling",
        type=float,
        default=float(os.environ.get("ONCALL_SAFETY_SAMPLING", "1.0")),
        help="Sampling rate for Remediation Safety (0-1). Default 1.0 — this is a "
        "production-safety control and should stay at full coverage.",
    )
    parser.add_argument(
        "--readiness-sampling",
        type=float,
        default=float(os.environ.get("ONCALL_READINESS_SAMPLING", "1.0")),
        help="Sampling rate for Resolution Readiness (0-1). Default 1.0. This is the "
        "most expensive judge; lower it first under volume.",
    )
    parser.add_argument(
        "--handoff-sampling",
        type=float,
        default=float(os.environ.get("ONCALL_HANDOFF_SAMPLING", "1.0")),
        help="Sampling rate for Handoff Completeness (0-1). Default 1.0 — it is a code "
        "scorer with no model cost, so there is no reason to sample it down.",
    )
    parser.add_argument(
        "--defect-sampling",
        type=float,
        default=float(os.environ.get("ONCALL_DEFECT_SAMPLING", "1.0")),
        help="Sampling rate for Defect Free (0-1). Default 1.0, and leave it there: "
        "it is the failure-rate signal, and a sampled rate is a worse estimate for no "
        "saving — the scorer costs nothing to run.",
    )
    parser.add_argument(
        "--retrieval-sampling",
        type=float,
        default=float(os.environ.get("ONCALL_RETRIEVAL_SAMPLING", "1.0")),
        help="Sampling rate for Retrieval Grounded (0-1). Default 1.0 — free code "
        "scorer. It is a trend read per slice rather than an alarm, so it tolerates "
        "sampling, but there is no cost reason to.",
    )
    args = parser.parse_args()

    with _client() as client:
        project_id = resolve_project_id(client, PROJECT_NAME)
        print(f"Project: {PROJECT_NAME} ({project_id})")

        if args.list:
            rules = list_rules(client, project_id)
            if not rules:
                print("No online scoring rules configured.")
                return
            print(f"\n{len(rules)} online scoring rule(s):")
            for rule in rules:
                online = (rule.get("config") or {}).get("online") or {}
                scorers = ", ".join(s.get("id", "?") for s in online.get("scorers", []))
                print(
                    f"  • {rule['name']}\n"
                    f"      sampling={online.get('sampling_rate')} "
                    f"root_span={online.get('apply_to_root_span')}\n"
                    f"      scorers=[{scorers}]"
                )
            return

        rules = [
            Rule(
                name="Diagnostic accuracy (production)",
                description=(
                    "Grades every incident answer for grounding in retrieved evidence, "
                    "numeric fidelity, correct event ordering, and correct dependency "
                    "direction."
                ),
                scorer_slug="diagnostic-accuracy",
                sampling_rate=args.accuracy_sampling,
            ),
            Rule(
                name="Remediation safety (production)",
                description=(
                    "Production-safety control: flags any answer that leaks a credential "
                    "or recommends an irreversible action without blast radius and a "
                    "reversible alternative. Kept at full sampling."
                ),
                scorer_slug="remediation-safety",
                sampling_rate=args.safety_sampling,
            ),
            Rule(
                name="Resolution readiness (production)",
                description=(
                    "Strict incident-commander standard for the answer as a handoff. A "
                    "headroom metric, not a pass/fail control — production answers land "
                    "around 0.4-0.75, and the score moves when prompts or tooling "
                    "improve. This is the axis tied to time to resolution."
                ),
                scorer_slug="resolution-readiness",
                sampling_rate=args.readiness_sampling,
            ),
            Rule(
                name="Handoff completeness (production)",
                description=(
                    "Deterministic coverage floor with no model cost: does the answer "
                    "cite a record, name a pageable owner, give an action, and state a "
                    "verification signal. Catches shape regressions the judges would "
                    "only see on a sampled trace."
                ),
                scorer_slug="handoff-completeness",
                sampling_rate=args.handoff_sampling,
            ),
            Rule(
                name="Defect free (production)",
                description=(
                    "The failure-rate signal, deterministic and free: 1.0 when no "
                    "mechanical defect fired, 0.0 when one did, so the production "
                    "failure rate is 1 - mean. Catches a record ID the evidence does "
                    "not support, a substantive answer with no retrieval behind it, a "
                    "simulated fix not disclosed as one, and an empty answer. Must run "
                    "at full sampling: a rate estimated from a sample is a worse number "
                    "for no saving."
                ),
                scorer_slug="defect-free",
                sampling_rate=args.defect_sampling,
            ),
            Rule(
                name="Retrieval grounded (production)",
                description=(
                    "Did a substantive answer have any retrieval behind it. Split "
                    "out of Defect Free because it fires on questions the agent "
                    "legitimately cannot serve — a class that is ~40% of seeded "
                    "traffic — so the mean tracks question mix as much as quality. "
                    "Read per slice as a trend; do not alert on it."
                ),
                scorer_slug="retrieval-grounded",
                sampling_rate=args.retrieval_sampling,
            ),
        ]

        available = resolve_scorer_ids(client, project_id)
        if not available:
            sys.exit("No scorers found in the project. Run `make push-scorers` first.")

        missing = [r.scorer_slug for r in rules if r.scorer_slug not in available]
        if missing:
            sys.exit(
                f"Scorer(s) not found: {', '.join(missing)}. "
                f"Available: {', '.join(sorted(available))}. Run `make push-scorers` first."
            )

        print()
        for rule in rules:
            result = upsert_rule(client, project_id, rule, available[rule.scorer_slug])
            print(
                f"✓ {rule.name}\n"
                f"    scorer={rule.scorer_slug} sampling={rule.sampling_rate:.0%} "
                f"rule_id={result.get('id')}"
            )

        app_url = os.environ.get("BRAINTRUST_APP_URL", "https://www.braintrust.dev").rstrip("/")
        print(
            f"\nOnline scoring is live. New traces will be scored automatically.\n"
            f"Review at {app_url}/app/~/configuration/automations"
        )


if __name__ == "__main__":
    main()
