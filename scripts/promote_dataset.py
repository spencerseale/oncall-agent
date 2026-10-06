"""Promote production root spans into a Braintrust dataset for offline replay.

`make experiment` replays a dataset of real questions through the current build.
That dataset has to come from somewhere, and the honest source is production: the
questions responders actually asked, not the ones we thought to write down.

    log root spans  --(this script)-->  dataset  --(make experiment)-->  experiment

Two decisions worth knowing about:

**One row per distinct question, by default.** Seeded traffic repeats each
question several times, and a dataset that is forty copies of six questions has
the statistical power of six. ``--per-question`` raises the cap deliberately when
you want repeated trials of the same input.

**The production answer becomes ``metadata.original_output``, not ``expected``.**
Nobody hand-labeled golden answers for these, and promoting a production answer
to `expected` would quietly assert that it was correct. The offline experiment
promotes it itself, explicitly, and uses it as a *drift* reference — "does today's
build still answer this the way production did" — which is a claim the data
actually supports.

Rows are inserted with the log row's own id, so re-running updates rather than
duplicates.

Usage:
    uv run python scripts/promote_dataset.py                    # last 7 days
    uv run python scripts/promote_dataset.py --days 30 --limit 2000
    uv run python scripts/promote_dataset.py --per-question 3
    uv run python scripts/promote_dataset.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from typing import Any

import httpx
from braintrust import init_dataset
from dotenv import load_dotenv

# Standalone scripts talk to the REST API directly and never import
# oncall_agent.config, which is where load_dotenv() normally runs — so without this
# they would read only the ambient environment and ignore .env entirely. That is
# fine while a shell happens to export the right key and silently wrong the moment
# .env is the intended source, which is exactly the fresh-org bootstrap case.
load_dotenv()

API_URL = os.environ.get("BRAINTRUST_API_URL", "https://api.braintrust.dev").rstrip("/")
PROJECT_NAME = os.environ.get("BRAINTRUST_PROJECT", "oncall-agent")
DATASET_NAME = os.environ.get("ONCALL_EVAL_DATASET", "oncall-agent-root-traces")

# The root span's name. Only root spans carry the input/output/evidence contract
# the judges read; tool and LLM spans would poison the dataset with fragments.
ROOT_SPAN_NAME = "oncall_agent"

# Metadata worth carrying into the dataset. The slicing fields are the point:
# without team and domain on the row, an experiment cannot tell you whether a
# change helped payments and hurt search.
CARRY_METADATA = (
    "incident_id", "service", "team", "severity", "domain", "incident_status",
    "incident_ttr_minutes", "tool_calls", "tool_call_count", "iterations",
    "agent_version", "environment", "mode", "model", "prompt_source",
)


def resolve_project_id(name: str) -> str:
    api_key = os.environ.get("BRAINTRUST_API_KEY", "").strip()
    if not api_key:
        sys.exit("BRAINTRUST_API_KEY is not set.")
    response = httpx.get(
        f"{API_URL}/v1/project",
        headers={"Authorization": f"Bearer {api_key}"},
        params={"project_name": name, "limit": 1},
        timeout=30,
    )
    response.raise_for_status()
    objects = response.json().get("objects") or []
    if not objects:
        sys.exit(f"Project {name!r} not found.")
    return objects[0]["id"]


# Two query lint rules shape this, and both fail the query outright rather than
# degrading it:
#
#   1. A log scan needs a range filter it can use for segment elimination, so the
#      `created` bound is mandatory, not an optimization.
#   2. A query projecting large fields (input, output, metadata) is capped at
#      LIMIT 100, so a bigger pull has to be paginated.
PAGE_SIZE = 100


def _point_bt_at_credential_org() -> None:
    """Set BRAINTRUST_ORG_NAME from the API key, for the `bt` subprocesses below.

    `bt` resolves the org from its own saved config (~/.config/bt/config.json) even
    when the credential itself comes from the environment, so a .env pointing at a
    different org than the last `bt login` fails with
    "invalid configuration: Organization '<other org>' not found". Deriving the org
    from the key and exporting it (bt reads BRAINTRUST_ORG_NAME) keeps the two in
    agreement without mutating the user's saved bt config.
    """
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
        return  # let the bt call fail with its own message rather than masking it
    if orgs and orgs[0].get("name"):
        os.environ["BRAINTRUST_ORG_NAME"] = orgs[0]["name"]


def _sql(query: str) -> dict[str, Any]:
    _point_bt_at_credential_org()
    result = subprocess.run(
        ["bt", "sql", query, "--json", "--non-interactive"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        sys.exit(f"`bt sql` failed:\n{result.stderr or result.stdout}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        sys.exit(f"Could not parse `bt sql` output:\n{result.stdout[:500]}")


def fetch_root_spans(project_id: str, days: int, limit: int) -> list[dict[str, Any]]:
    """Read root spans out of the log with `bt sql`, one 100-row page at a time."""
    base = (
        "SELECT id, input, output, metadata "
        f"FROM project_logs('{project_id}') "
        f"WHERE created > CURRENT_TIMESTAMP - INTERVAL {int(days)} DAY "
        f"  AND span_attributes.name = '{ROOT_SPAN_NAME}' "
        "  AND output IS NOT NULL "
        f"LIMIT {PAGE_SIZE}"
    )

    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    while len(rows) < limit:
        # Cursor tokens are passed inline as OFFSET, not as a flag.
        payload = _sql(base if cursor is None else f"{base} OFFSET '{cursor}'")
        page = payload.get("data") or []
        rows.extend(page)
        cursor = payload.get("cursor")
        if len(page) < PAGE_SIZE or not cursor:
            break
        print(f"  … {len(rows)} rows")

    return rows[:limit]


def select_rows(
    rows: list[dict[str, Any]], per_question: int
) -> tuple[list[dict[str, Any]], Counter]:
    """Keep at most ``per_question`` rows per distinct question, longest answer first.

    Longest-first is a crude proxy for "the run that actually did the work": a
    truncated or error-shaped answer is short, and picking it would put a
    degenerate row in the reference set.
    """
    rows = sorted(rows, key=lambda r: len(str(r.get("output") or "")), reverse=True)

    seen: Counter = Counter()
    selected: list[dict[str, Any]] = []
    for row in rows:
        question = row.get("input")
        if not isinstance(question, str) or not question.strip():
            continue
        if not str(row.get("output") or "").strip():
            continue
        if seen[question] >= per_question:
            continue
        seen[question] += 1
        selected.append(row)
    return selected, seen


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote production root spans to a dataset.")
    parser.add_argument("--days", type=int, default=7, help="How far back to scan. Default 7.")
    parser.add_argument("--limit", type=int, default=1000, help="Max log rows to scan.")
    parser.add_argument(
        "--per-question",
        type=int,
        default=1,
        help="Max dataset rows per distinct question. Default 1 — repeats add no "
        "statistical power to a replay experiment.",
    )
    parser.add_argument("--dataset", default=DATASET_NAME, help="Target dataset name.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be inserted.")
    args = parser.parse_args()

    project_id = resolve_project_id(PROJECT_NAME)
    print(f"Project: {PROJECT_NAME} ({project_id})")

    rows = fetch_root_spans(project_id, args.days, args.limit)
    print(f"Scanned {len(rows)} root span(s) from the last {args.days} day(s).")
    if not rows:
        sys.exit(
            "No root spans found. Run the agent (make agent / make seed) first, and "
            "note that traces take a few seconds to become queryable."
        )

    selected, per_question_counts = select_rows(rows, args.per_question)
    print(
        f"Selected {len(selected)} row(s) across {len(per_question_counts)} distinct "
        f"question(s) (cap {args.per_question} per question)."
    )

    if args.dry_run:
        for row in selected[:20]:
            metadata = row.get("metadata") or {}
            print(
                f"  • [{metadata.get('team', '—')}/{metadata.get('domain', '—')}] "
                f"{str(row['input'])[:80]}"
            )
        if len(selected) > 20:
            print(f"  … and {len(selected) - 20} more")
        return

    dataset = init_dataset(project=PROJECT_NAME, name=args.dataset)
    for row in selected:
        source = row.get("metadata") or {}
        dataset.insert(
            # The log row's id, so re-running this script updates rows rather than
            # appending a second copy of the same trace.
            id=row["id"],
            input=row["input"],
            # Deliberately no `expected`: see the module docstring.
            expected=None,
            metadata={
                **{key: source[key] for key in CARRY_METADATA if key in source},
                "original_output": row["output"],
                "source": "production_root_span",
                "source_span_id": row["id"],
            },
        )
    dataset.flush()

    summary = dataset.summarize()
    app_url = os.environ.get("BRAINTRUST_APP_URL", "https://www.braintrust.dev").rstrip("/")
    print(f"\n✓ Inserted/updated {len(selected)} row(s) in dataset '{args.dataset}'.")
    print(f"  dataset now holds {getattr(summary, 'data_summary', None)}")
    print(f"  {app_url}/app/~/{PROJECT_NAME}/datasets")
    print("\nNext: make experiment")


if __name__ == "__main__":
    main()
