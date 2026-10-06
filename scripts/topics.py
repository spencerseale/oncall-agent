"""Enable Braintrust Topics and push the custom facets for the on-call agent.

Topics clusters production traffic so you can see its *shape* — what responders
actually bring to this agent — rather than reading traces one at a time.

Four custom facets go up. The first three answer the questions this demo exists
to answer about the **platform**; the fourth asks the same kind of question about
the **agent**. They are listed here in the order they appear on the Topics
page, below the one built-in facet worth keeping (see ``KEEP_BUILTIN_FACETS`` — the
page pins built-ins above custom facets and that is not configurable). The order of
``FACETS`` controls the relative order of these three:

| Facet | Output shape | Answers |
| --- | --- | --- |
| ``failure_domain`` | ``<team> / <failure class>`` | How does incident load distribute across teams and domains? |
| ``resolution_blocker`` | lowercase noun phrase, 3-8 words | What is stopping incidents from resolving faster? |
| ``remediation_pattern`` | lowercase verb phrase, 2-6 words | Which fixes recur, and where? |
| ``agent_failure_mode`` | lowercase noun phrase, 3-8 words | Where is the agent itself falling short, and how often? |

``failure_domain`` leads the custom facets because it is the cross-team measurement
axis, and measuring effectiveness across teams and domains is the question this
demo exists to answer. It turns "is response getting faster" into "for whom, and on
what kind of failure" — which a platform team has to settle before it can prioritize.

``resolution_blocker`` is the actionable one: cluster it and you get a ranked
reliability backlog mined from real traffic rather than from opinion. Every
cluster is a missing data source, a missing alert, or a stale runbook, sized by
how often responders hit it.

``remediation_pattern`` is the one that surprises people: cluster it and the
recurring mitigations become visible as a set, which is an argument about tooling
rather than about any one incident.

``agent_failure_mode`` is last because it has a different subject and a different
audience. The first three describe the **platform** and are read by whoever owns
reliability; this one describes the **agent** and is read by whoever owns the
agent. Clustering it turns "is the agent any good" into a ranked list of the ways
it actually falls short, sized by how often each one happens.

It is scoped tightly to *defects*, and the line against ``resolution_blocker`` is
load-bearing: an assistant that correctly reports it lacks feature-flag history is
a **blocker** (a thing to go build), not a **failure** (a thing the agent did
wrong). Blurring the two produces a facet that fires on every hard question and
measures difficulty rather than quality.

Note what this facet can and cannot see. It runs on the ``thread`` transcript, so
it reads the question and the answer but not ``metadata.evidence`` — it reliably
catches *shape* defects (no owner, no verification, simulation described as
applied, an answer that argues against itself) and catches *grounding* defects
only when they are self-evident from the text. Frequency of grounding failures
belongs to the ``Defect Free`` code scorer and to ``diagnostic-accuracy``, which
both see the evidence. Point the facet at the raw span instead of ``thread`` if you
want it checking claims against retrieved records, and expect a longer, costlier
extraction call.

Each custom facet needs TWO Braintrust functions, and mixing them up breaks the UI:

  1. A **facet function** (``function_type="facet"``) — runs the LLM extraction
     prompt against each trace and emits a label.
  2. A **topic map function** (``function_type="classifier"``,
     ``function_data.type="topic_map"``) — clusters the extracted labels into
     named topics, the same way the built-in Task/Sentiment/Issues facets work.

The automation's ``facet_functions`` lists the extractors; ``topic_map_functions``
lists the classifiers. Putting a facet function ID into ``topic_map_functions``
renders as broken null entries in the UI.

Usage:
    uv run python scripts/topics.py           # enable Topics + push the facets
    uv run python scripts/topics.py --status  # show current Topics config
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
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
FACET_MODEL = os.environ.get("ONCALL_FACET_MODEL", "gpt-4o-mini")

# agent_failure_mode asks for a negative judgment against several criteria with a
# NONE default, which is harder than the descriptive extraction the other three do.
# Measured over 6 known-good and 4 known-defective transcripts, gpt-4o-mini
# false-positived on a good remediation answer ("next action buried in long
# answer") while gpt-4.1-mini and gpt-4o returned NONE on all six and still fired
# on all four defects. A facet that finds fault in good answers measures nothing,
# so this one pays for the better model — and only this one, since the other three
# are validated on gpt-4o-mini.
FAILURE_FACET_MODEL = os.environ.get("ONCALL_FAILURE_FACET_MODEL", "gpt-4.1-mini")

# How far back extraction walks when the automation is (re-)enabled.
#
# This was "1d", and on a project with three weeks of history that is the
# difference between clustering and not. Measured after a re-run: 57 traces got
# facets out of 445 root spans on record, and because `no_match` is excluded from
# topic generation the clusterable pool per facet was 54 / 45 / 35 / 1 — all under
# the ~100 that HDBSCAN needs to find density. The custom topic maps had labeled
# 7, 6, 1 and 0 traces respectively, which reads as "only the built-in Task facet
# clusters" when the real cause is that the window never reached the traffic.
#
# The built-in `Task` facet is unaffected by this: it is a *global* classifier that
# already carries clusters from earlier corpora, so it assigns rather than
# discovers and looks instant by comparison.
#
# Cost scales directly with the window — one extraction call per facet per trace,
# so 30 days over this project is ~445 x 4 calls. Narrow it for a smoke test.
BACKFILL_TIME_RANGE = os.environ.get("ONCALL_TOPICS_BACKFILL", "30d")

# How much recent facet history a topic-map generation pass looks at.
#
# This is NOT the same knob as BACKFILL_TIME_RANGE. Backfill controls how far back
# facets get *extracted*; the topic window controls how much of that extracted
# history *clustering* considers. Both have to cover the corpus or the Topics page
# stays empty, and they fail in different ways: too narrow a backfill leaves facets
# unextracted, too narrow a window leaves extracted facets unclustered.
#
# It defaulted to 3600 (1h) because the patch never sent it, which is invisible
# until you notice `bt topics status --full` saying "0/1 traces have facet labels
# in the 1h topic window" against a project with five hundred labeled traces.
TOPIC_WINDOW_SECONDS = int(os.environ.get("ONCALL_TOPICS_WINDOW_SECONDS", 7 * 24 * 3600))

# Facets left over from earlier iterations. Removed on each run so the Topics
# page doesn't accumulate dead extractors.
RETIRED_FACETS = ["incident_intent", "_probe_facet"]

# Which built-in facets to keep on the automation.
#
# The Topics page pins built-in facets **above** custom ones, in a fixed platform
# order, and nothing in the automation controls that: there is no position field on
# a facet ref and no ordering flag on `bt topics config`. Reordering the
# `facet_functions` array has no effect on the page. Dropping a built-in is the
# only way to move a custom facet up.
#
# Sentiment and Issues earn nothing on an incident corpus — measured over 954
# traces they matched 24 and 28 respectively and generated **zero** topics, while
# occupying the second and third slots on the page. Task is kept: 946/954 matched
# and 434 topics labeled, so it is genuinely productive even though it is generic.
KEEP_BUILTIN_FACETS = ("Task",)

# All four facets are **open-ended**: the prompt fixes the theme, the
# granularity, and the output shape, but the model writes the value. That is what
# makes the topic map useful — it clusters emergent free-text summaries into
# topics you did not have to predict. A fixed enum would only ever reproduce the
# categories we guessed at authoring time, which is strictly less informative
# than the traffic.
#
# Each returns the literal NONE when it does not apply, and `no_match_pattern`
# ("^NONE") tells Braintrust to store those as 'no_match' and keep them out of
# topic generation. Without it, "NONE" becomes the single largest cluster and
# drowns the signal.
FACETS = [
    {
        "name": "failure_domain",
        "description": (
            "Which team and which class of failure each interaction concerns — how "
            "incident load and response quality distribute across the organization"
        ),
        "prompt": """You are analyzing a trace from an incident-response assistant used \
by on-call responders at a commerce platform.

Identify the operational context of the interaction: which team's territory it falls in, \
and what class of failure is being investigated.

Teams include payments, checkout, identity, search, notifications, and data-platform. \
Other teams may appear — do not force an interaction into one of these six.

Common failure classes include database and connection pooling, capacity and saturation, \
dependency latency, deploy regression, configuration and feature flags, network and \
replication, and third-party provider degradation. Describe what you actually see rather \
than picking the nearest label from this list.

Output format: the team name in lowercase, then a forward slash, then a short lowercase \
phrase naming the failure class. Keep the phrase to 2 to 6 words and describe the class \
of failure rather than the individual symptom.

Good outputs:
payments / connection pool exhaustion
identity / feature flag misconfiguration
search / shared storage iops contention
notifications / third party rate limiting
data-platform / silent job failure

If the interaction concerns response process, on-call logistics, or cross-team metrics \
rather than a specific team's failure, output:
platform / followed by a short lowercase phrase naming the process focus

If no team or failure context can be determined, output exactly:
NONE""",
    },
    {
        "name": "resolution_blocker",
        "description": (
            "What stood between the responder and a resolved incident — a ranked "
            "reliability backlog of missing data, missing alerting, and stale "
            "knowledge, mined from real traffic"
        ),
        "prompt": """You are analyzing a trace from an incident-response assistant used \
by on-call responders at a commerce platform.

The assistant can read the incident registry, the service catalog and on-call rotation, \
the deploy log, alert history and metric series, runbooks, and postmortems. It can search \
the public web. It can also draft a proposed fix and dry-run a simulated test suite \
against it. It cannot do anything else, and in particular it cannot apply a change to \
production. It never claims to have done so, so "the fix was not applied" is normal \
behavior and is not a blocker.

Identify the single most significant thing that stood between this responder and a \
resolved incident. Look for:
- data the assistant said it did not have, could not find, or does not track
- signals that existed but did not reach a human — an alert routed to a channel, a \
  detection gap, a silent failure
- knowledge that was stale, missing, wrong, or unsafe — an out-of-date runbook, a dead \
  dashboard link, an overdue action item
- analysis the assistant could not perform with the tools available
- a question it answered only partially, or deflected

Name the BLOCKER in general terms, not the specific incident. Write it so that two \
responders hitting the same underlying blocker from different incidents produce the \
same phrase.

Output format: a lowercase noun phrase of 3 to 8 words. No punctuation at the end, no \
explanation, no restating the question.

Good outputs:
no trace level latency attribution
alert routed to channel without paging
runbook stale past review date
no feature flag change history
no customer impact quantification
missing queue depth alerting
no staging replica of production traffic shape

If the assistant fully resolved the question with the data available and nothing stood \
in the way, output exactly:
NONE""",
    },
    {
        "name": "remediation_pattern",
        "description": (
            "The class of fix the assistant steered toward — a library of the "
            "mitigations that actually recur, and which failures they attach to"
        ),
        "prompt": """You are analyzing a trace from an incident-response assistant used \
by on-call responders at a commerce platform.

Identify the class of remediation the assistant steered the responder toward. Describe \
the KIND of fix, not the specific target — two incidents fixed by reverting different \
deploys are the same remediation pattern.

Prefer what the assistant actually recommended as the next action. If it recommended \
several, choose the first or most emphasized one.

Output format: a lowercase verb phrase of 2 to 6 words. No punctuation at the end, no \
explanation.

Good outputs:
roll back correlated deploy
disable feature flag
scale instance class up
fail over to secondary provider
pin consumers to healthy region
enable retry backoff
defer batch job off peak
escalate to secondary oncall

If the assistant recommended no remediation — because the question was informational, \
or because it could not determine a next step — output exactly:
NONE""",
    },
    {
        "name": "agent_failure_mode",
        "model": FAILURE_FACET_MODEL,
        # Created as a function but NOT attached to the automation. It only fires
        # on defects, and at this agent's ~1% hard-defect rate it accumulated 4
        # labeled values across 540 traces — far under the 100-summary minimum, so
        # its card sat on the Topics page reading "topic report is not available"
        # indefinitely. Keeping the function means it stays testable and is one
        # config write away from being re-attached.
        #
        # Flip this to True once there is degraded traffic to cluster: weaken the
        # safety paragraph in ORCHESTRATOR_PROMPT, seed, then re-run `make topics`.
        "attach": False,
        "description": (
            "How the assistant itself fell short on this interaction — a discovered "
            "taxonomy of agent defects, distinct from the platform gaps that "
            "resolution_blocker collects"
        ),
        "prompt": """You are auditing a trace from an incident-response assistant used \
by on-call responders at a commerce platform. You are looking for ways the ASSISTANT \
fell short — not ways the platform did.

This distinction is the whole point, so hold it firmly:

- A **capability limit, handled well, is NOT a failure.** If the assistant did not have \
some data and said so plainly, named what it would need, and gave the responder \
something else to act on, it behaved correctly. Output NONE.
- A **defect** is the assistant doing its own job badly: asserting something it did not \
retrieve, contradicting the records it was given, leaving the responder with nothing to \
do, burying the answer, or being unsafe.

Look for, in rough order of severity:
- a claim, number, identifier, or timestamp asserted with no visible basis in what was \
retrieved
- an answer that reads as general knowledge or best practice rather than as this \
platform's records
- a conclusion that contradicts the evidence in the same answer, or event ordering \
stated backwards
- a recommended action with no named owner, or no way to tell whether it worked
- a destructive or irreversible step relayed as a thing to run, or a credential \
reproduced
- hedging where the records supported a definite answer, or an answer so long the next \
action is lost in it

**Default to NONE.** Most interactions have no defect, and a facet that finds fault \
everywhere measures nothing. Fire only when the defect is unambiguous from the text in \
front of you — something you could point at and show a colleague. Do not infer a defect \
from an answer merely being long, from a topic being difficult, or from your own opinion \
about how you would have phrased it. If you find yourself choosing between several \
mild possibilities, the answer is NONE. If exactly one defect is unambiguous, name it. \
If several are, name the most severe.

Name the DEFECT in general terms, not the specific incident, and write it so two \
different traces exhibiting the same defect produce the same phrase.

Output format: a lowercase noun phrase of 3 to 8 words. No punctuation at the end, no \
explanation, no restating the question.

Good outputs:
answered from general knowledge without retrieval
unattributed claim with no supporting record
recommended action without named owner
no verification signal for recommended action
conclusion contradicts retrieved evidence
event ordering stated backwards
next action buried in long answer

Two cases that are explicitly NOT defects, however much of the answer they take up:
- The assistant correctly reported that it lacked some data and said what it would need.
- The assistant drafted or tested a fix and reported simulated results. Proposing a \
change, reporting that a dry-run passed or failed, and stating that nothing has been \
applied is exactly the intended behavior.

If the assistant did its job well, output exactly:
NONE""",
    },
]

NO_MATCH_PATTERN = "^NONE"


def _client() -> httpx.Client:
    api_key = os.environ.get("BRAINTRUST_API_KEY", "").strip()
    if not api_key:
        sys.exit("BRAINTRUST_API_KEY is not set.")
    return httpx.Client(
        base_url=API_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=90.0,
    )


def _objects(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        return payload.get("objects", [])
    return payload if isinstance(payload, list) else []


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


def _bt(args: list[str], check: bool = True) -> str:
    """Run a `bt` command, returning stdout."""
    _point_bt_at_credential_org()
    result = subprocess.run(["bt", *args], capture_output=True, text=True, timeout=180)
    if check and result.returncode != 0:
        sys.exit(f"`bt {' '.join(args)}` failed:\n{result.stderr or result.stdout}")
    return result.stdout


def resolve_project_id(client: httpx.Client, name: str) -> str:
    response = client.get("/v1/project", params={"project_name": name})
    response.raise_for_status()
    projects = _objects(response.json())
    if not projects:
        sys.exit(f"Project {name!r} not found. Run the agent once so it gets created.")
    return projects[0]["id"]


def topics_status() -> dict[str, Any]:
    """Read the current Topics automation config via the CLI."""
    raw = _bt(["topics", "config", "--json", "--project", PROJECT_NAME, "--no-input"])
    return json.loads(raw)


def enable_topics() -> None:
    """Enable Topics with the built-in facets.

    Custom facets are attached by the API patch below, not here. Passing them to
    the enable command inserts them into topic_map_functions as raw facet refs,
    which the platform renders as broken null entries.
    """
    # Delete any prior config so re-runs start from a known state.
    _bt(
        ["topics", "config", "delete", "--force", "--project", PROJECT_NAME, "--no-input"],
        check=False,
    )
    _bt(
        [
            "topics", "config", "enable",
            "--name", "On-call Response Topics",
            "--topic-window", "1d",
            "--generation-cadence", "1h",
            "--project", PROJECT_NAME,
            "--no-input",
        ]
    )
    print(
        "  ✓ Topics enabled (built-ins: Task, Sentiment, Issues; "
        f"keeping {', '.join(KEEP_BUILTIN_FACETS)})"
    )


def delete_retired_facets(client: httpx.Client, project_id: str) -> None:
    """Remove facets from earlier iterations, plus their topic maps."""
    response = client.get("/v1/function", params={"project_id": project_id, "limit": 200})
    response.raise_for_status()
    by_slug = {f["slug"]: f["id"] for f in _objects(response.json()) if f.get("slug")}

    for name in RETIRED_FACETS:
        for slug in (name, f"{name}_topic_map"):
            fn_id = by_slug.get(slug)
            if not fn_id:
                continue
            deleted = client.delete(f"/v1/function/{fn_id}")
            if deleted.status_code < 400:
                print(f"  ✓ removed retired function {slug}")


def create_facet_functions(
    client: httpx.Client, project_id: str, facet: dict[str, str]
) -> tuple[str, str]:
    """Create one facet extractor and its topic map classifier."""
    name = facet["name"]
    result = client.post(
        "/v1/function",
        json={
            "project_id": project_id,
            "name": name,
            "slug": name,
            "description": facet["description"],
            "function_type": "facet",
            "if_exists": "replace",
            "function_data": {
                "type": "facet",
                # The global "thread" preprocessor renders the trace as a
                # conversation transcript before the extraction call, so the
                # facet sees the question and answer rather than raw span JSON.
                "preprocessor": {
                    "type": "global",
                    "name": "thread",
                    "function_type": "preprocessor",
                },
                "prompt": facet["prompt"],
                # Per-facet override, defaulting to the shared FACET_MODEL. Only
                # agent_failure_mode sets one — see its "model" key for why.
                "model": facet.get("model") or FACET_MODEL,
                # Outputs matching this are stored as 'no_match' and excluded
                # from topic generation.
                "no_match_pattern": NO_MATCH_PATTERN,
            },
        },
    )
    if result.status_code >= 400:
        sys.exit(f"Facet creation failed for {name}: {result.status_code} {result.text}")
    facet_id = result.json()["id"]

    # /insert-functions is the endpoint the platform UI uses for topic maps.
    topic_map = client.post(
        "/insert-functions",
        json={
            "functions": [
                {
                    "project_id": project_id,
                    "name": f"{name}_topic_map",
                    "slug": f"{name}_topic_map",
                    "function_type": "classifier",
                    "if_exists": "replace",
                    "function_data": {
                        "type": "topic_map",
                        "source_facet": name,
                        "embedding_model": "brain-embedding-1",
                    },
                }
            ]
        },
    )
    if topic_map.status_code >= 400:
        sys.exit(f"Topic map creation failed for {name}: {topic_map.status_code} {topic_map.text}")
    functions = topic_map.json().get("functions", [])
    if not functions:
        sys.exit(f"Topic map creation returned no function ID for {name}.")
    topic_map_id = functions[0]["id"]

    print(f"  ✓ {name}: facet ({facet_id[:8]}…) + topic map ({topic_map_id[:8]}…)")
    return facet_id, topic_map_id


def patch_automation(
    client: httpx.Client, facet_ids: list[str], topic_map_ids: list[str]
) -> None:
    """Attach the custom facets to the Topics automation.

    patch_id replaces the whole config rather than merging, so the built-in
    facets and classifiers have to be read back and re-sent alongside ours.
    """
    status = topics_status()
    automations = status.get("automations") or []
    if not automations:
        sys.exit("Topics automation not found — enable step may have failed.")
    auto = automations[0]

    # `bt topics config enable` turns on all three built-ins, so the ones we do not
    # want are dropped here by simply not re-sending them. function_type must be
    # included on the ones we keep or they render as broken in the UI, even though
    # the platform normalizes it.
    global_refs = [
        {"type": "global", "name": ff["name"], "function_type": "facet"}
        for ff in auto.get("facet_functions", [])
        if ff.get("ref_type") == "global" and ff["name"] in KEEP_BUILTIN_FACETS
    ]

    # Same keep-list applied to the built-in classifiers. A positive filter also
    # makes duplication impossible: our own topic maps are named
    # "<facet>_topic_map" and can never be in KEEP_BUILTIN_FACETS, so reading the
    # config back and appending cannot re-add them even if this is called against
    # an automation that was already patched.
    existing_classifiers = [
        {"function": {"type": tmf["ref_type"], "id": tmf["id"]}}
        for tmf in auto.get("topic_map_functions", [])
        if tmf.get("function_type") == "classifier" and tmf.get("name") in KEEP_BUILTIN_FACETS
    ]

    # Array order is documentary, not functional: the page pins built-ins first
    # regardless (see KEEP_BUILTIN_FACETS). These lists are written in the order
    # the page actually renders — kept built-ins, then the custom facets — so the
    # code reads the same as the screen.
    #
    # The relative order of the custom facets *is* controlled here, by the order of
    # FACETS: they are created in that order and sent in that order, so both
    # possible orderings the platform could apply agree. failure_domain leads
    # because "effectiveness across teams and domains" is the question this demo
    # exists to answer.
    response = client.post(
        "/api/project_automation/patch_id",
        json={
            "id": auto["id"],
            "config": {
                "event_type": "topic",
                "sampling_rate": 1.0,
                "facet_functions": [
                    *global_refs,
                    *({"type": "function", "id": fid} for fid in facet_ids),
                ],
                "topic_map_functions": [
                    *existing_classifiers,
                    *({"function": {"type": "function", "id": tid}} for tid in topic_map_ids),
                ],
                "scope": {"type": auto.get("scope_type", "logs"), "idle_seconds": 600},
                "rerun_seconds": auto.get("rerun_seconds"),
                "relabel_overlap_seconds": auto.get("relabel_overlap_seconds"),
                "backfill_time_range": BACKFILL_TIME_RANGE,
            },
        },
    )
    if response.status_code >= 400:
        sys.exit(f"Automation patch failed: {response.status_code} {response.text}")
    print("  ✓ Topics automation patched (facet extractors + topic map classifiers)")


def print_status() -> None:
    status = topics_status()
    automations = status.get("automations") or []
    if not automations:
        print("Topics is not configured for this project.")
        return
    for auto in automations:
        print(f"Topics automation: {auto.get('name', auto.get('id'))}")
        print(f"  scope={auto.get('scope_type')} rerun_seconds={auto.get('rerun_seconds')}")
        print("  facet functions:")
        for ff in auto.get("facet_functions", []):
            print(f"    • {ff.get('name') or ff.get('id')}  ({ff.get('ref_type')})")
        print("  topic map functions:")
        for tmf in auto.get("topic_map_functions", []):
            print(f"    • {tmf.get('name') or tmf.get('id')}  ({tmf.get('function_type')})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Enable Topics and push the custom facets.")
    parser.add_argument("--status", action="store_true", help="Show Topics config and exit.")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    with _client() as client:
        project_id = resolve_project_id(client, PROJECT_NAME)
        print(f"Project: {PROJECT_NAME} ({project_id})\n")

        enable_topics()
        delete_retired_facets(client, project_id)

        facet_ids: list[str] = []
        topic_map_ids: list[str] = []
        for facet in FACETS:
            facet_id, topic_map_id = create_facet_functions(client, project_id, facet)
            # A facet with attach=False is still created, so it stays testable and
            # can be re-enabled with one config write, but it is kept off the
            # automation. A facet that cannot reach the 100-summary minimum renders
            # a permanent "topic report is not available" card on the Topics page.
            if facet.get("attach", True):
                facet_ids.append(facet_id)
                topic_map_ids.append(topic_map_id)
            else:
                print(f"    (not attached to the automation: {facet['name']})")

        patch_automation(client, facet_ids, topic_map_ids)

    app_url = os.environ.get("BRAINTRUST_APP_URL", "https://www.braintrust.dev").rstrip("/")
    names = ", ".join(f["name"] for f in FACETS)
    print(
        f"\nTopics is live with custom facets: {names}\n"
        f"Clustering needs ~100 traces and runs asynchronously (usually within the hour).\n"
        f"Review at {app_url}/app/~/{PROJECT_NAME}/topics"
    )


if __name__ == "__main__":
    main()
