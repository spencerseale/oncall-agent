"""One place where a sub-agent step turns a prompt into a model call.

Three sub-agents make seven model calls between them across two model families,
and the families do not take the same arguments: a reasoning model rejects
``temperature`` and takes ``reasoning_effort`` instead. Resolving that per call
site meant every new step had to remember which tier it was on, and getting it
wrong is a 400 at runtime rather than a type error at import.

The client is always passed in. Nothing here constructs an ``OpenAI``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from .config import Settings
from .prompts import system_prompt

logger = logging.getLogger(__name__)


def complete(
    client: OpenAI,
    settings: Settings,
    model: str,
    slug: str,
    user: str,
    *,
    temperature: float = 0.1,
    json_object: bool = False,
) -> str:
    """Run one prompt slug against one model and return the raw text.

    ``temperature`` applies only to the sampled tier — :meth:`Settings.call_params`
    drops it for a reasoning model, which would otherwise reject the call.
    """
    kwargs: dict[str, Any] = {}
    if json_object:
        kwargs["response_format"] = {"type": "json_object"}

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt(settings, slug)},
            {"role": "user", "content": user},
        ],
        **settings.call_params(model, temperature),
        **kwargs,
    )
    return response.choices[0].message.content or ""


def complete_json(
    client: OpenAI,
    settings: Settings,
    model: str,
    slug: str,
    user: str,
    *,
    temperature: float = 0.0,
) -> dict[str, Any]:
    """Like :func:`complete`, but parse the response as a JSON object.

    A model that returns unparseable JSON degrades to ``{}`` rather than raising:
    the caller can then report a thin result, which is a recoverable turn, where a
    raised exception would take down the whole agent run.
    """
    raw = complete(
        client, settings, model, slug, user, temperature=temperature, json_object=True
    )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Prompt %r returned non-JSON (%d chars); degrading to {}.", slug, len(raw))
        return {}
    return parsed if isinstance(parsed, dict) else {}
