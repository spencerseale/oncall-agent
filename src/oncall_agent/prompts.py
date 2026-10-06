"""Resolve the prompts the agent runs with.

Three sources, in precedence order:

1. **An explicit override** on ``Settings.system_prompt`` — how a playground run
   drives the agent through the remote eval.
2. **Braintrust**, when ``ONCALL_PROMPT_SOURCE=braintrust`` — the version pushed
   by ``make push-prompts``, so the prompt can be edited in the UI and rolled out
   without redeploying the agent.
3. **The literal** in :mod:`.prompt_library`, which is the default and the
   fallback whenever Braintrust cannot be reached.

The resolved source is recorded on the span (``metadata.prompt_source``) so a
trace answers "which prompt actually ran" without guesswork. A prompt that
silently fell back to local because a network call failed is a real failure mode,
and it should be visible in the trace rather than inferred.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Settings
from .prompt_library import ORCHESTRATOR_SLUG, PROMPT_SPECS

logger = logging.getLogger(__name__)

# (slug, project) -> resolved system prompt. load_prompt caches its own HTTP
# call, but this also caches the message flattening and the failure case, so a
# Braintrust outage costs one attempt per process rather than one per turn.
_CACHE: dict[tuple[str, str], tuple[str, str]] = {}


def message_text(content: Any) -> str:
    """Flatten a chat message's content, which may be text parts, not a string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def system_prompt_from_messages(messages: list[dict[str, Any]]) -> str:
    """Pull a system prompt out of a chat message list.

    System messages are the intended place for it. Falling back to every message
    means someone who typed a prompt into the user slot still gets what they
    expected instead of a silently unchanged agent.
    """
    system = [m for m in messages if m.get("role") == "system"]
    return "\n\n".join(message_text(m.get("content")) for m in (system or messages)).strip()


def _from_braintrust(project: str, slug: str) -> str | None:
    """Fetch a deployed prompt's system message, or None if unavailable."""
    try:
        from braintrust import load_prompt

        prompt = load_prompt(project, slug)
        built = prompt.build(input="")
        return system_prompt_from_messages(built.get("messages") or []) or None
    except Exception as exc:
        # A missing or unreachable prompt must not take the agent down; falling
        # back to the checked-in literal is always correct behavior.
        logger.warning("Could not load prompt %r from Braintrust (%s); using local.", slug, exc)
        return None


def resolve(settings: Settings, slug: str = ORCHESTRATOR_SLUG) -> tuple[str, str]:
    """Return ``(system_prompt, source)`` for one prompt slug."""
    spec = PROMPT_SPECS[slug]

    if slug == ORCHESTRATOR_SLUG and settings.system_prompt:
        return settings.system_prompt, "override"

    if settings.prompt_source != "braintrust":
        return spec.system, "local"

    key = (slug, settings.braintrust_project)
    if key not in _CACHE:
        deployed = _from_braintrust(settings.braintrust_project, slug)
        _CACHE[key] = (deployed, "braintrust") if deployed else (spec.system, "local_fallback")
    return _CACHE[key]


def system_prompt(settings: Settings, slug: str = ORCHESTRATOR_SLUG) -> str:
    return resolve(settings, slug)[0]
