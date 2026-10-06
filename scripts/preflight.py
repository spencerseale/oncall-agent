"""Check that a Braintrust credential can actually run this demo, before seeding.

Bootstrapping into a fresh org fails in a small number of specific ways, and all of
them are cheap to detect and expensive to discover halfway through a 300-trace
seed. This checks each one and exits non-zero on the first that would break:

1. **Is there a credential at all**, and which org does it belong to? A brand-new
   org needs its own key; a key from another org will authenticate fine and then
   write everything into the wrong place.
2. **Can that org serve a model?** A new org has no AI providers configured until
   someone adds one. Without a provider every model call fails with
   ``no provider configured for '<model>' in org '<name>'`` — the agent, all three
   LLM judges, and all four Topics facets, i.e. the entire demo. This is the check
   that matters most, and the only way to be sure is to make a real completion.
3. **Does the project exist yet?** Informational only: on a fresh org it will not,
   and `make push-prompts` creates it.
4. **Is Exa configured?** Informational only: without it the correlation sub-agent
   degrades to internal precedent instead of failing.

Usage:
    uv run python scripts/preflight.py
    uv run python scripts/preflight.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys

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

# Every model this demo needs a provider for, across all three tiers: the
# orchestrator and the judges on gpt-4.1, the correlation sub-agent and the Topics
# facets on gpt-4o-mini, and the remediation/validation sub-agents on a reasoning
# model. A provider that serves one tier but not another produces a half-working
# demo, which is harder to diagnose than a clean failure, so each is probed.
#
# The reasoning tier is the one most likely to be missing on a fresh org: an org
# with an OpenAI provider configured still needs that provider's account to have
# access to the o-series, and the failure arrives as a 404 from the gateway rather
# than as the usual "no provider configured" message.
REQUIRED_MODELS = (
    os.environ.get("ONCALL_MODEL", "gpt-4.1"),
    os.environ.get("ONCALL_SUBAGENT_MODEL", "gpt-4o-mini"),
    os.environ.get("ONCALL_REASONING_MODEL", "o3"),
)

# Reasoning models reject `max_tokens` outright and spend their budget on
# reasoning tokens before emitting any, so the one-token probe that works for a
# sampled model has to become a slightly larger `max_completion_tokens` budget.
REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def resolve_org(key: str) -> tuple[str | None, list[str]]:
    """Return (default org name, all visible org names) for this key."""
    response = httpx.post(
        f"{API_URL}/api/apikey/login",
        headers={"Authorization": f"Bearer {key}"},
        json={},
        timeout=20,
    )
    response.raise_for_status()
    orgs = [org.get("name") for org in response.json().get("org_info", [])]
    return (orgs[0] if orgs else None), orgs


def probe_model(key: str, base_url: str, model: str) -> tuple[bool, str]:
    """Make the smallest possible real completion against the configured endpoint."""
    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "ok"}],
    }
    if model.strip().lower().startswith(REASONING_MODEL_PREFIXES):
        # A reasoning model 400s on `max_tokens`, and a budget of 1 is consumed by
        # reasoning tokens before any content is produced. Only the status code is
        # checked, so an empty completion is still a successful probe.
        payload["max_completion_tokens"] = 16
    else:
        payload["max_tokens"] = 1

    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=90,
        )
    except Exception as exc:
        return False, f"request failed: {exc}"

    if response.status_code < 400:
        return True, "ok"
    # The gateway names the org and the missing provider in the body, which is the
    # whole diagnosis — carry it rather than just the status code.
    return False, f"HTTP {response.status_code}: {response.text[:220]}"


def project_exists(key: str, name: str) -> bool:
    try:
        response = httpx.get(
            f"{API_URL}/v1/project",
            headers={"Authorization": f"Bearer {key}"},
            params={"project_name": name, "limit": 1},
            timeout=20,
        )
        response.raise_for_status()
        return bool(response.json().get("objects"))
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight a Braintrust org for this demo.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument(
        "--print-org",
        action="store_true",
        help="Print only the credential's org name and exit. The Makefile uses this "
        "to pass --org to bt, which otherwise resolves the org from its own saved "
        "config and fails with \"Organization '<other org>' not found\" whenever "
        ".env points at a different org than the last `bt login`.",
    )
    args = parser.parse_args()

    key = os.environ.get("BRAINTRUST_API_KEY", "").strip()

    if args.print_org:
        # Quiet and best-effort: this runs inside $(shell ...), so anything printed
        # here becomes part of a bt command line.
        try:
            org, _ = resolve_org(key)
        except Exception:
            return 1
        if not org:
            return 1
        print(org)
        return 0

    model_key = os.environ.get("OPENAI_API_KEY", "").strip() or key
    base_url = os.environ.get(
        "OPENAI_BASE_URL", "https://api.braintrust.dev/v1/proxy"
    ).strip()

    report: dict[str, object] = {"project": PROJECT_NAME, "base_url": base_url}

    if not key:
        print("✗ BRAINTRUST_API_KEY is not set.")
        print("  Put the target org's key in .env — see .env.example.")
        return 1

    try:
        org, all_orgs = resolve_org(key)
    except Exception as exc:
        print(f"✗ Could not authenticate BRAINTRUST_API_KEY: {exc}")
        return 1

    report["org"] = org
    report["orgs_visible"] = all_orgs
    report["key_fingerprint"] = f"sk-****{key[-5:]}"

    if not org:
        print(f"✗ Key sk-****{key[-5:]} authenticates but belongs to no org.")
        return 1

    print(f"✓ credential sk-****{key[-5:]} → org {org!r}")
    if len(all_orgs) > 1:
        # The scripts resolve the org implicitly from the key, so a multi-org key
        # silently picks its default. Say which one won.
        print(f"  note: key sees {len(all_orgs)} orgs ({', '.join(all_orgs)}); using {org!r}")

    failures = []
    model_report = {}
    for model in dict.fromkeys(REQUIRED_MODELS):
        ok, detail = probe_model(model_key, base_url, model)
        model_report[model] = detail
        if ok:
            print(f"✓ model {model!r} served by {org!r}")
        else:
            print(f"✗ model {model!r} NOT served by {org!r}")
            print(f"    {detail}")
            failures.append(model)
    report["models"] = model_report

    exists = project_exists(key, PROJECT_NAME)
    report["project_exists"] = exists
    print(
        f"· project {PROJECT_NAME!r}: "
        + ("already exists" if exists else "does not exist yet (push-prompts creates it)")
    )

    exa = bool(os.environ.get("EXA_API_KEY", "").strip())
    report["exa_configured"] = exa
    print(
        "· EXA_API_KEY: "
        + ("set" if exa else "not set — the sub-agent will skip external research")
    )

    if args.json:
        print()
        print(json.dumps(report, indent=2))

    if failures:
        print()
        print(f"Blocked: org {org!r} has no AI provider for {', '.join(failures)}.")
        print("  Add one under Settings → AI providers in that org, then re-run.")
        print("  Every model call in this demo goes through it: the agent, its three")
        print("  sub-agents, the three LLM judges, and the four Topics facets.")
        return 1

    print()
    print(f"Ready to seed org {org!r}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
