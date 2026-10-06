"""Preflight: does this process's API key belong to the org holding the project?

A remote eval spans two orgs, and they have to agree. The playground runs in the
org you are browsing; the dev server authenticates with `BRAINTRUST_API_KEY` from
whatever shell it was launched in. When they disagree, the run itself proceeds and
only the scorers fail — `bt`'s eval runner invokes playground-selected scorers
with `braintrust.functions.invoke()`, which sends no `x-bt-org-name` header, so
the gateway resolves the org from the process's key and looks the scorer's model
up in *that* org's AI providers:

    no provider configured for 'gpt-4o-mini' in org '<other org>'

That message names the org the key belongs to, not the org you are looking at.
The usual cause is a stale `BRAINTRUST_API_KEY` exported from another demo's
`.env` earlier in the same terminal.

Checking key-can-see-project is a sharper test than printing the org name,
because it catches the mismatch whatever its cause. This exits non-zero on
failure so `make remote-eval` stops before serving a session whose scorers cannot
work.

Usage:
    uv run python scripts/check_org.py
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import httpx
from dotenv import load_dotenv

# Standalone scripts talk to the REST API directly and never import
# oncall_agent.config, which is where load_dotenv() normally runs — so without this
# they would read only the ambient environment and ignore .env entirely. That is
# fine while a shell happens to export the right key and silently wrong the moment
# .env is the intended source, which is exactly the fresh-org bootstrap case.
load_dotenv()

API_URL = "https://api.braintrust.dev"
LOGIN_URL = f"{API_URL}/api/apikey/login"
PROJECT_URL = f"{API_URL}/v1/project"


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


def bt_identity() -> tuple[str | None, str | None]:
    """What `bt` itself resolves — which is what the eval runner logs in with.

    `bt` has its own credential order (profile, then BRAINTRUST_API_KEY, then
    saved config), so it can end up on a different key than this shell's
    environment. Asking it directly closes that gap.
    """
    try:
        _point_bt_at_credential_org()
        out = subprocess.run(["bt", "status"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return None, None
    org = re.search(r"^org:\s*(.+)$", out, re.M)
    user = re.search(r"^user:\s*(.+)$", out, re.M)
    return (org.group(1).strip() if org else None, user.group(1).strip() if user else None)


def main() -> int:
    key = os.environ.get("BRAINTRUST_API_KEY", "").strip()
    project = os.environ.get("BRAINTRUST_PROJECT", "oncall-agent")

    if not key:
        print("BRAINTRUST_API_KEY is not set — traces and scorers will not work.")
        return 1

    headers = {"Authorization": f"Bearer {key}"}
    try:
        login = httpx.post(LOGIN_URL, headers=headers, json={}, timeout=10)
        login.raise_for_status()
        orgs = [org.get("name") for org in login.json().get("org_info", [])]

        found = httpx.get(
            PROJECT_URL, headers=headers, params={"project_name": project, "limit": 1}, timeout=10
        )
        found.raise_for_status()
        visible = bool(found.json().get("objects"))
    except Exception as exc:  # a network hiccup should not block the demo
        print(f"could not verify the API key's org: {exc}")
        return 0

    bt_org, bt_user = bt_identity()

    if visible:
        print(f"key org: {orgs[0]} — sees project '{project}' ✓")
        if bt_org and bt_org != orgs[0]:
            print(f"  but `bt status` resolves org {bt_org} ({bt_user}) — the eval runner")
            print(f"  logs in with that one, and its scorers will invoke in {bt_org}.")
            return 1
        return 0

    print(f"key org: {', '.join(orgs) or '<none>'} — cannot see project '{project}'.")
    print(f"  BRAINTRUST_API_KEY in this shell does not belong to the org that holds")
    print(f"  '{project}', so playground scorers will fail with")
    print(f"  \"no provider configured for '<model>' in org '{orgs[0] if orgs else '?'}'\".")
    print("  Start a shell without the other demo's .env, or export the right key.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
