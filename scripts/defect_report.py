"""Per-defect failure frequency, derived from root spans.

    uv run python scripts/defect_report.py --days 7

`Defect Free` gives the production failure *rate* (1 - mean) and nothing more: a
code scorer may only return a bare number, and the per-defect breakdown its
handler logs with ``current_span().log(metadata=...)`` lands offline but is
silently dropped online — the platform's JS runner owns the score span there, so
the Python handler has no span to write to. See CLAUDE.md, "Score-span metadata
does not survive online scoring".

That leaves the breakdown to be re-derived, which is possible only because every
check in the scorer is a pure function of root-span fields: ``output``,
``metadata.evidence_refs``, and ``metadata.tool_calls``. This script applies the
same checks to root spans pulled with ``bt sql`` and reports how often each fires,
grouped by the scorer that owns it — the "in what ways, and how often" half of the
question that ``agent_failure_mode`` answers qualitatively.

**The checks are duplicated from ``scorers/code_scorers.py`` on purpose.** A
pushed scorer runs in an isolated sandbox with no access to this repo, so it
cannot import a shared module and neither can this script share one with it
without breaking the push. Keep the two in step by hand; the drift risk is the
price of the sandbox. ``--verify`` cross-checks this script's verdicts against the
scores the deployed scorer actually wrote, which is how you find out they have
drifted.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter

from dotenv import load_dotenv

# Standalone scripts never import oncall_agent.config, which is the only place
# load_dotenv() runs — without this they read the ambient environment and ignore
# .env entirely.
load_dotenv()

# --- kept in step with scorers/code_scorers.py ------------------------------
REFERENCE_PATTERN = re.compile(
    r"\b(?:INC-\d{3,}|dep-[0-9a-f]{6,}|ALERT-\d{3,}|RB-\d{2,}|PM-\d{3,})\b"
)
TRUNCATION_MARKER = "[truncated]"
SIMULATION_TOOLS = ("propose_fix", "validate_fix")
SIMULATION_DISCLAIMER = re.compile(
    r"\b(?:simulat\w*|dry ?run|staging|not (?:been )?applied|has not shipped|"
    r"not yet applied|before (?:you |they )?appl\w+|would be applied)\b",
    re.IGNORECASE,
)
UNGROUNDED_ANSWER_CHARS = 300

# Which scorer owns which check. They were one scorer; folding the zero-retrieval
# check in with the hard failures made the blended rate track question mix rather
# than agent quality — see scorers/code_scorers.py for the measurement.
HARD_DEFECTS = (
    "fabricated_reference",
    "simulation_not_disclosed",
    "empty_answer",
)
RETRIEVAL_DEFECTS = ("no_retrieval",)
DEFECTS = HARD_DEFECTS + RETRIEVAL_DEFECTS

SCORERS = (
    ("Defect Free", HARD_DEFECTS, "alarmable — every firing is unambiguous"),
    ("Retrieval Grounded", RETRIEVAL_DEFECTS, "composition-sensitive — read per slice"),
)


def defects_for(row: dict) -> tuple[list[str], dict]:
    """The defects one root span exhibits, plus the details behind them."""
    text = row.get("output") or ""
    meta = row.get("metadata") or {}
    evidence = str(meta.get("evidence") or "")
    refs = meta.get("evidence_refs")
    tool_calls = meta.get("tool_calls") or []
    if not isinstance(tool_calls, list):
        tool_calls = []

    found: list[str] = []
    details: dict = {}

    if isinstance(refs, list):
        haystack = "\n".join([*(str(r) for r in refs), row.get("input") or ""])
    elif evidence and TRUNCATION_MARKER not in evidence:
        haystack = f"{evidence}\n{row.get('input') or ''}"
    else:
        haystack = None
        details["reference_check"] = "skipped"

    if haystack is not None:
        unsupported = sorted(
            {ref for ref in REFERENCE_PATTERN.findall(text) if ref not in haystack}
        )
        if unsupported:
            found.append("fabricated_reference")
            details["unsupported_references"] = unsupported

    if not tool_calls and len(text) > UNGROUNDED_ANSWER_CHARS:
        found.append("no_retrieval")

    if any(tool in tool_calls for tool in SIMULATION_TOOLS):
        if not SIMULATION_DISCLAIMER.search(text):
            found.append("simulation_not_disclosed")

    if not text.strip():
        found.append("empty_answer")

    return found, details


def project_id(name: str) -> str:
    result = subprocess.run(
        ["bt", "projects", "list", "--json"], capture_output=True, text=True
    )
    try:
        for project in json.loads(result.stdout):
            if project.get("name") == name:
                return project["id"]
    except Exception:
        pass
    sys.exit(f"Could not resolve project {name!r}: {(result.stderr or '')[:200]}")


def root_spans(pid: str, days: int, limit: int) -> list[dict]:
    """Root spans with the fields the checks need.

    The `created` range filter is mandatory — a log scan without one fails the
    query lint outright rather than running slowly. Large-field projections are
    capped at LIMIT 100, so this pages by nothing and simply takes the most recent
    window; widen `--days` rather than raising `--limit` past 100.
    """
    query = (
        f"SELECT created, input, output, scores, metadata, tags "
        f"FROM project_logs('{pid}') "
        f"WHERE created > CURRENT_TIMESTAMP - INTERVAL {days} DAY "
        f"  AND span_attributes.name = 'oncall_agent' AND is_root "
        f"ORDER BY created DESC LIMIT {min(limit, 100)}"
    )
    result = subprocess.run(["bt", "sql", query, "--json"], capture_output=True, text=True)
    try:
        payload = json.loads(result.stdout)
    except Exception:
        sys.exit(f"Query failed: {(result.stderr or result.stdout)[:300]}")
    # `bt sql --json` wraps rows in {"data": [...]}. Older builds used "rows", and a
    # bare list is also accepted — checking only one of the three returns silently
    # empty results, which reads as "no traffic" rather than as a parsing bug.
    if isinstance(payload, list):
        return payload
    return payload.get("data") or payload.get("rows") or []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--project", default="oncall-agent")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=100, help="Max root spans (cap 100).")
    parser.add_argument(
        "--mode",
        default=None,
        help="Only traces tagged mode:<value> (oneshot, batch, interactive, eval).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Cross-check against the deployed scorer's own Defect Free score and "
        "report any trace where the two disagree — how you find out this script "
        "and the pushed handler have drifted apart.",
    )
    args = parser.parse_args()

    pid = project_id(args.project)
    rows = root_spans(pid, args.days, args.limit)
    if args.mode:
        rows = [r for r in rows if f"mode:{args.mode}" in (r.get("tags") or [])]

    if not rows:
        print(f"No root spans in the last {args.days} day(s).")
        return 0

    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = {name: [] for name in DEFECTS}
    per_row: list[list[str]] = []

    for row in rows:
        found, details = defects_for(row)
        per_row.append(found)
        counts.update(found)
        for name in found:
            if len(examples[name]) < 3:
                note = details.get("unsupported_references") or ""
                examples[name].append(f"{(row.get('input') or '')[:60]} {note}".strip())

    total = len(rows)
    print(f"Project {args.project} — {total} trace(s) over {args.days} day(s)"
          + (f", mode:{args.mode}" if args.mode else ""))

    # Reported per scorer, not as one blended number. A single rate over both
    # groups moves when the question mix changes, which is what the split fixed.
    for scorer, group, note in SCORERS:
        failing = sum(1 for found in per_row if any(name in group for name in found))
        print(f"\n  {scorer}  —  {note}")
        print(f"    failure rate : {failing / total:.1%}  ({failing} of {total})")
        for name in group:
            n = counts.get(name, 0)
            print(f"      {name:26} {n / total:6.1%}  ({n})")
            for example in examples[name]:
                print(f"          · {example}")

    if args.verify:
        print()
        exit_code = 0
        for scorer, group, _ in SCORERS:
            disagreements = []
            with_score = 0
            for row, found in zip(rows, per_row):
                scored = (row.get("scores") or {}).get(scorer)
                if scored is None:
                    continue
                with_score += 1
                local_fail = any(name in group for name in found)
                if (scored == 0.0) != local_fail:
                    disagreements.append(
                        (row.get("created"), (row.get("input") or "")[:60], scored, found)
                    )
            status = "no disagreements" if not disagreements else f"{len(disagreements)} DISAGREEMENT(S)"
            print(f"  {scorer}: verified on {with_score} scored trace(s) — {status}")
            for created, question, scored, found in disagreements:
                print(f"    ✗ {created} score={scored} local={found or ['clean']}  {question}")
            if disagreements:
                exit_code = 1
        if exit_code:
            print("\n  This script and scorers/code_scorers.py have drifted. The scorer is")
            print("  the authority for the score; reconcile the check lists by hand.")
        return exit_code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
