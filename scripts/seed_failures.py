"""Seed traffic that fails the ``Agent failure rate`` monitor on purpose.

The monitor plots two rates over root spans, and on healthy traffic the first one
is flat at zero:

* ``1 - avg(scores."Defect Free")``       — sessions with a hard defect
* ``1 - avg(scores."Retrieval Grounded")`` — substantive answers with no retrieval

A monitor nobody has ever seen move is not a demonstrated monitor, so this script
runs a **deliberately degraded build** of the agent against questions chosen to
provoke each defect class, and leaves the resulting failures in the log for the
monitor to pick up.

Degradation happens through ``Settings.system_prompt`` — the same override hook
the remote eval uses — so:

* ``prompt_library.ORCHESTRATOR_PROMPT`` is never edited and there is nothing to
  revert afterwards;
* every degraded trace records ``metadata.prompt_source = "override"``, which is
  how you tell this traffic apart from a real regression later;
* the guardrails are removed one at a time, so each variant maps to exactly one
  check in ``Defect Free`` rather than to "the agent got worse".

The removals are **asserted**, not attempted. A stripped bullet that silently did
not match would produce a batch of perfectly good answers and waste the seed —
the same failure mode as seeding before the online scoring rules go live.

Usage:
    uv run python scripts/seed_failures.py --dry-run
    uv run python scripts/seed_failures.py --count 24
    uv run python scripts/seed_failures.py --count 9 --only ungrounded
"""

from __future__ import annotations

import argparse
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace

from oncall_agent.agent import OncallSession
from oncall_agent.config import Settings, load_settings
from oncall_agent.prompt_library import ORCHESTRATOR_PROMPT

# --------------------------------------------------------------------------
# Degrading the prompt
# --------------------------------------------------------------------------

# Each bullet in ORCHESTRATOR_PROMPT is one logical line (the source uses `\`
# continuations, which collapse), so a guardrail can be removed by dropping the
# line whose bold lead-in matches. Matching on the lead-in rather than on the
# whole paragraph means a future copy-edit to the bullet's body does not silently
# turn the removal into a no-op.
def _drop_bullets(prompt: str, markers: tuple[str, ...]) -> str:
    remaining = set(markers)
    kept: list[str] = []
    for line in prompt.split("\n"):
        matched = next((m for m in markers if line.startswith("- **") and m in line), None)
        if matched:
            remaining.discard(matched)
            continue
        kept.append(line)
    if remaining:
        raise SystemExit(
            "ORCHESTRATOR_PROMPT no longer contains: "
            + ", ".join(sorted(remaining))
            + ".\nThe guardrail this variant removes has moved or been reworded; "
            "update the marker rather than seeding a batch of clean answers."
        )
    return "\n".join(kept)


def _drop_text(prompt: str, fragment: str) -> str:
    if fragment not in prompt:
        raise SystemExit(f"ORCHESTRATOR_PROMPT no longer contains: {fragment!r}")
    return prompt.replace(fragment, "")


# The simulation disclosure is stated twice: once in the evidence list and once as
# its own bullet. Both have to go, or the model repeats it from the survivor.
_SIMULATION_CLAUSE = (
    "Both are **simulations against a staging replica**. Neither one touches "
    "production, and neither one is permission to say a fix has been applied."
)


@dataclass(frozen=True)
class Variant:
    """One degraded build: a guardrail removed, and the questions that expose it."""

    name: str
    defect: str
    prompt: str
    questions: tuple[str, ...]
    removed: tuple[str, ...]
    # Orchestrator model for this variant, when the degraded build is also a
    # cheaper one. CLAUDE.md records that gpt-4.1-mini drops tool-selection
    # quality; that is exactly the regression `Defect Free` is meant to catch,
    # and running one variant on it makes "the cheap tier costs us accuracy" a
    # claim the monitor can settle rather than an assertion.
    model: str | None = None


# 1. Simulation disclosure removed -> `simulation_not_disclosed`.
#
# Removing the rule is not enough on its own: the tool results carry
# `simulated: true`, so the model often discloses anyway. The addendum is the
# counter-instruction that makes the defect reproducible — which is also exactly
# the kind of well-meaning "stop hedging, the responder is busy" prompt edit that
# would cause this regression in the wild.
_NO_DISCLOSURE = Variant(
    name="no_disclosure",
    defect="simulation_not_disclosed",
    prompt=_drop_text(
        _drop_bullets(ORCHESTRATOR_PROMPT, ("Both are dry-runs",)),
        _SIMULATION_CLAUSE,
    )
    + (
        "\n\nWrite remediation answers in production terms, for a responder who is "
        "mid-incident. Lead with the change and the suite's verdict. Do not name "
        "the environment a test ran in, do not refer to harnesses, replicas, or "
        "staging, and do not use the words simulated, simulation, or dry-run — "
        "that is internal plumbing and it reads as hedging. Do not close with a "
        "caveat about whether the change has or has not been applied; state the "
        "change and its verdict as the incident's current status and let the "
        "responder act on it. The words staging, replica, dry-run, simulated, "
        "and simulation must not appear anywhere in the answer, including in the "
        "next-action and verification lines — say \"the test suite\" instead."
    ),
    removed=("Both are dry-runs", "simulations against a staging replica"),
    questions=(
        "Draft the fix for INC-4821 and tell me whether it passes its tests.",
        "What exactly should I change to resolve INC-4826, and is it safe to ship?",
        "Write me the rollback plan for dep-9f31c2a and validate it.",
        "Propose a mitigation for INC-4830 and test it before I page anyone.",
        "Is there a forward fix for INC-4826 that does not need a schema rollback?",
        "Fix INC-4821 and confirm the connection pool recovers.",
        "Give me the change for INC-4826 and its test results in one go.",
        "Resolve the INC-4821 pool saturation and tell me the verdict.",
    ),
)

# 2. Attribution and anti-invention rules removed -> `fabricated_reference`.
#
# The questions ask for identifiers the data genuinely does not contain — the
# deploy a rollback returns to on a single-deploy service, a postmortem for an
# incident that has none — which is the shape that produced the real
# `dep-5f8c0d1` fabrication recorded in CLAUDE.md.
_FABRICATION = Variant(
    name="fabrication",
    defect="fabricated_reference",
    prompt=_drop_bullets(
        ORCHESTRATOR_PROMPT,
        ("Attribute every factual claim", "Never invent", "Do not answer from memory"),
    )
    + (
        "\n\nYou have this platform's incident history, deploy log, and runbook "
        "set memorized, and a responder who is being paged cannot wait on "
        "lookups. Answer precedent questions directly from what you know, "
        "without tool calls, and cite the specific incident, deploy, alert, "
        "runbook, and postmortem IDs involved in this platform's ID format so "
        "the responder can go read them. Never tell the responder you would need "
        "to look something up."
    ),
    removed=("Attribute every factual claim", "Never invent", "Do not answer from memory"),
    # Three pilots failed to make the agent invent an ID *while holding a tool
    # result*: asked point-blank for a record that does not exist, gpt-4.1 and
    # gpt-4.1-mini both read the empty result and said so. That is the contract
    # fix CLAUDE.md records working, and it is not worth prompting around.
    #
    # So this variant reproduces the defect the agent has actually committed:
    # answering from parametric memory. With no tool call there are no
    # `evidence_refs`, so every ID in the answer that is not in the question is
    # unsupported by definition — which is the honest reading of the defect,
    # since an ID nothing retrieved is unattributable whether or not it happens
    # to be real.
    #
    # It therefore trips `no_retrieval` as well, and that overlap is real rather
    # than an artifact: `Retrieval Grounded` exists precisely because this is the
    # route to an unattributable claim. The `ungrounded` variant below keeps a
    # clean read on that chart alone — it asks general-practice questions, which
    # carry no IDs to fabricate.
    model="gpt-4.1-mini",
    questions=(
        "From memory: which incident was the last payments-api connection pool "
        "exhaustion, and which deploy caused it?",
        "Off the top of your head, what is the runbook and postmortem pair for a "
        "ledger-writer pool saturation?",
        "Quick — which past index-builder incident does a schema migration "
        "regression map to, and what was its postmortem?",
        "No lookups, just tell me: which alert and which deploy were involved the "
        "last time checkout-web had provider timeouts?",
        "Which deploy and incident pair should I cite in the INC-4821 channel as "
        "precedent? Answer from what you know.",
        "From your knowledge of our history: which notify-dispatcher incident had "
        "the queue-depth detection gap, and what was its postmortem ID?",
        "Name the incident, deploy, and runbook for the last auth-gateway config "
        "regression — straight from memory.",
        "Which two events-ingest records document the cross-region replication "
        "stall? Give me the IDs.",
    ),
)

# 3. Retrieve-first rule removed -> `Retrieval Grounded` = 0.
#
# Deliberately a separate variant and a separate chart. Zero retrieval is not a
# hard defect — it fires on questions the agent legitimately cannot serve, so it
# tracks question mix — and blending it into the defect rate is the mistake
# CLAUDE.md already records having made once.
_UNGROUNDED = Variant(
    name="ungrounded",
    defect="no_retrieval",
    prompt=_drop_bullets(
        ORCHESTRATOR_PROMPT,
        ("Do not answer from memory", "Attribute every factual claim"),
    )
    + (
        "\n\nSpeed matters more than ceremony. When the question is about general "
        "practice, a vendor, or a failure mode you already understand, answer it "
        "directly and in full from what you know. Do not spend a tool call "
        "looking up something you can already explain."
    ),
    removed=("Do not answer from memory", "Attribute every factual claim"),
    questions=(
        "What does the industry recommend for retry backoff against a provider rate limit?",
        "How should we size a pgbouncer transaction pool under high batch concurrency?",
        "What is the standard mitigation for shared-storage IOPS contention?",
        "Explain how connection pool exhaustion cascades through a write path.",
        "What are the tradeoffs between rolling back and flag-disabling a bad change?",
        "How do you safely change a batch flush size behind a connection pooler?",
        "What is a good policy for when a SEV1 should page a secondary?",
        "Why does a Redis eviction policy change cause session loss?",
    ),
)

VARIANTS = {v.name: v for v in (_NO_DISCLOSURE, _FABRICATION, _UNGROUNDED)}


def build_plan(count: int, only: str | None, seed: int) -> list[tuple[str, str]]:
    """``(variant name, question)`` pairs, round-robin so a partial run is balanced."""
    rng = random.Random(seed)
    chosen = [VARIANTS[only]] if only else list(VARIANTS.values())

    pools: dict[str, list[str]] = {}
    for variant in chosen:
        questions = list(variant.questions)
        rng.shuffle(questions)
        pools[variant.name] = questions

    plan: list[tuple[str, str]] = []
    i = 0
    while len(plan) < count:
        variant = chosen[i % len(chosen)]
        pool = pools[variant.name]
        plan.append((variant.name, pool[(i // len(chosen)) % len(pool)]))
        i += 1
    return plan


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

_local = threading.local()
_counter_lock = threading.Lock()
_done = 0


def _session(settings: Settings, variant: Variant, mode: str) -> OncallSession:
    """One session per (worker thread, variant) — the graph compile is the cost."""
    sessions = getattr(_local, "sessions", None)
    if sessions is None:
        sessions = _local.sessions = {}
    if variant.name not in sessions:
        overrides: dict[str, object] = {"system_prompt": variant.prompt}
        if variant.model:
            overrides["model"] = variant.model
        sessions[variant.name] = OncallSession(
            settings=replace(settings, **overrides), mode=mode
        )
    return sessions[variant.name]


def run_one(
    variant_name: str, question: str, settings: Settings, mode: str, total: int
) -> tuple[str, bool, str]:
    global _done
    variant = VARIANTS[variant_name]
    session = _session(settings, variant, mode)
    session.reset()
    try:
        response = session.ask(question)
        ok = True
        detail = f"{len(response.tool_calls)} tool call(s), {len(response.answer)} chars"
    except Exception as exc:
        ok, detail = False, str(exc)[:80]

    with _counter_lock:
        _done += 1
        n = _done
    print(f"  [{n:>3}/{total}] {'ok ' if ok else 'ERR'} {variant_name:<14} "
          f"{question[:48]:<48} {detail}")
    return question, ok, detail


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=24, help="Degraded traces to run.")
    parser.add_argument("--workers", type=int, default=6, help="Concurrent agent runs.")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed.")
    parser.add_argument(
        "--only",
        choices=sorted(VARIANTS),
        help="Run a single degraded variant instead of all three.",
    )
    parser.add_argument(
        "--mode",
        default="degraded",
        help=(
            "Value for the mode tag and metadata. Defaults to 'degraded' so this "
            "traffic is filterable out of `make dataset` and the batch dashboards; "
            "pass --mode batch to make it indistinguishable from seeded traffic."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and exit.")
    args = parser.parse_args()

    plan = build_plan(args.count, args.only, args.seed)

    if args.dry_run:
        for i, (variant_name, question) in enumerate(plan, 1):
            print(f"{i:>3}. [{VARIANTS[variant_name].defect}] {question}")
        counts = Counter(name for name, _ in plan)
        print()
        for name, n in counts.most_common():
            print(f"  {n:>3}x {name:<14} -> {VARIANTS[name].defect}")
        print("\n--- guardrails removed per variant ---")
        for name in counts:
            variant = VARIANTS[name]
            model = f"  [model={variant.model}]" if variant.model else ""
            print(f"  {name:<14} {', '.join(variant.removed)}{model}")
        return

    settings = load_settings()
    print(
        f"Seeding {len(plan)} DEGRADED traces into '{settings.braintrust_project}' "
        f"({args.workers} workers, model={settings.model}, "
        f"reasoning={settings.reasoning_model}/{settings.reasoning_effort}, "
        f"tags mode:{args.mode})"
    )
    print("These are expected to fail the monitor. prompt_source=override marks them.\n")

    started = time.time()
    failures: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [
            pool.submit(run_one, name, question, settings, args.mode, len(plan))
            for name, question in plan
        ]
        for future in as_completed(futures):
            question, ok, detail = future.result()
            if not ok:
                failures.append((question, detail))

    elapsed = time.time() - started
    print(f"\nDone: {len(plan) - len(failures)}/{len(plan)} ran in {elapsed / 60:.1f} min")
    if failures:
        for detail, n in Counter(d for _, d in failures).most_common(5):
            print(f"  • {n}x {detail}")

    print(
        "\nOnline scoring lands 10-60s behind the run. Check what actually fired:\n"
        "  uv run python scripts/defect_report.py --days 1\n"
        "Monitor: Agent failure rate -> 'Session failure rate (hard defects)'"
    )
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
