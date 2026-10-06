"""Prove online scoring is live before spending a seed run on it.

An online scoring rule does not take effect the instant it is created, and it does
**not** backfill. Creating the rules and immediately seeding — which is the obvious
order, and what `make bootstrap` did first — produces a few hundred traces that are
permanently unscored, with no error anywhere to explain it. The rules are correct,
the traces are correct, and the `scores` column is simply null forever.

So the bootstrap sends one canary turn and waits until it comes back scored. One
wasted agent run is a much better trade than three hundred.

The canary is tagged ``mode:canary`` so it stays out of the ``mode:batch``
dashboards the seeded traffic populates.

Usage:
    uv run python scripts/verify_scoring.py
    uv run python scripts/verify_scoring.py --timeout 240 --expect 4
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

API_URL = os.environ.get("BRAINTRUST_API_URL", "https://api.braintrust.dev").rstrip("/")
PROJECT_NAME = os.environ.get("BRAINTRUST_PROJECT", "oncall-agent")

# A question that exercises the real tools, so the canary is also a smoke test of
# the agent itself rather than just of the scoring pipeline.
CANARY_QUESTION = "Is there a deploy that correlates with INC-4821? Should I roll it back?"


def _point_bt_at_credential_org() -> None:
    """Set BRAINTRUST_ORG_NAME from the API key, for the `bt` subprocess below."""
    if os.environ.get("BRAINTRUST_ORG_NAME"):
        return
    key = os.environ.get("BRAINTRUST_API_KEY", "").strip()
    if not key:
        return
    try:
        response = httpx.post(
            f"{API_URL}/api/apikey/login",
            headers={"Authorization": f"Bearer {key}"},
            json={},
            timeout=20,
        )
        response.raise_for_status()
        orgs = response.json().get("org_info") or []
    except Exception:
        return
    if orgs and orgs[0].get("name"):
        os.environ["BRAINTRUST_ORG_NAME"] = orgs[0]["name"]


def resolve_project_id(key: str, name: str) -> str:
    response = httpx.get(
        f"{API_URL}/v1/project",
        headers={"Authorization": f"Bearer {key}"},
        params={"project_name": name, "limit": 1},
        timeout=30,
    )
    response.raise_for_status()
    objects = response.json().get("objects") or []
    if not objects:
        sys.exit(f"Project {name!r} not found. Run `make push-prompts` first.")
    return objects[0]["id"]


def poll_scores(project_id: str, since: datetime) -> dict[str, float] | None:
    """Return the canary trace's scores once they appear."""
    _point_bt_at_credential_org()
    query = (
        "SELECT created, scores FROM project_logs('%s') "
        "WHERE created > CURRENT_TIMESTAMP - INTERVAL 1 DAY "
        "  AND span_attributes.name = 'oncall_agent' "
        "  AND metadata.mode = 'canary' "
        "LIMIT 20" % project_id
    )
    result = subprocess.run(
        ["bt", "sql", query, "--json", "--non-interactive"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        rows = json.loads(result.stdout).get("data") or []
    except json.JSONDecodeError:
        return None

    cutoff = since.strftime("%Y-%m-%dT%H:%M:%S")
    for row in sorted(rows, key=lambda r: r.get("created") or "", reverse=True):
        created = str(row.get("created") or "")
        if created[:19] >= cutoff and row.get("scores"):
            return row["scores"]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify online scoring is live.")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds to wait. Default 300.")
    parser.add_argument("--interval", type=int, default=20, help="Poll interval. Default 20.")
    parser.add_argument(
        "--expect",
        type=int,
        default=4,
        help="How many scorers should report. Default 4. Fewer is a warning, not a failure — "
        "a sampled-down rule legitimately skips a trace.",
    )
    args = parser.parse_args()

    key = os.environ.get("BRAINTRUST_API_KEY", "").strip()
    if not key:
        sys.exit("BRAINTRUST_API_KEY is not set.")
    project_id = resolve_project_id(key, PROJECT_NAME)

    # Import late: this pulls in the agent, and a missing credential should fail
    # above with a clear message rather than inside a graph build.
    from oncall_agent.agent import OncallSession

    print(f"Sending one canary turn to {PROJECT_NAME!r} (tagged mode:canary)…")
    # A second of slack: the row's `created` is server-side, and a cutoff taken
    # after the call can land just ahead of it.
    since = datetime.now(timezone.utc) - timedelta(seconds=1)
    response = OncallSession(mode="canary").ask(CANARY_QUESTION)
    print(f"  agent answered with {len(response.tool_calls)} tool call(s)")

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        scores = poll_scores(project_id, since)
        if scores:
            print(f"✓ online scoring is live: {json.dumps(scores)}")
            if len(scores) < args.expect:
                print(
                    f"  note: {len(scores)} of {args.expect} scorers reported. Check "
                    f"sampling rates, or query score spans for errors:\n"
                    f"    bt sql \"SELECT error FROM project_logs('{project_id}') WHERE "
                    f"created > CURRENT_TIMESTAMP - INTERVAL 1 DAY AND "
                    f"span_attributes.type = 'score' LIMIT 10\""
                )
            return 0
        remaining = int(deadline - time.time())
        print(f"  no scores yet, {remaining}s left…")
        time.sleep(args.interval)

    print(f"✗ no scores on the canary trace after {args.timeout}s.")
    print("  Online scoring rules apply only to NEW traffic and never backfill, so")
    print("  seeding now would produce traffic that stays permanently unscored.")
    print("  Check `make eval-status`, then re-run this before seeding.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
