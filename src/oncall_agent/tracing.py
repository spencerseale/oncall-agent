"""Braintrust instrumentation setup.

One call to :func:`setup_tracing` at process start wires up:

* ``auto_instrument()`` — patches the OpenAI SDK and LangChain/LangGraph so every
  model call and graph node becomes a span with no per-call-site code.
* ``init_logger()`` — routes those spans to the configured Braintrust project.

The rest of the package imports ``traced`` / ``start_span`` from here so there is
a single place to make tracing a no-op if Braintrust is not configured.
"""

from __future__ import annotations

import dataclasses
import functools
import inspect
import logging
from typing import Any, Callable, TypeVar

import braintrust
from braintrust import current_span, start_span, traced  # re-exported

from .config import Settings

__all__ = [
    "setup_tracing",
    "traced",
    "traced_step",
    "start_span",
    "current_span",
    "span_metadata",
    "log_span",
]

F = TypeVar("F", bound=Callable[..., Any])

# Infrastructure objects that get threaded through every call but carry no
# debugging value in a trace — and are not JSON-serializable, so Braintrust
# would log them as useless "<openai.OpenAI object at 0x...>" strings.
_UNLOGGED_ARGS = frozenset({"client", "settings", "exa", "self"})

logger = logging.getLogger(__name__)

_INITIALIZED = False


def setup_tracing(settings: Settings) -> bool:
    """Initialize Braintrust tracing. Returns True if tracing is live.

    Safe to call more than once; only the first call does work.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return True
    if not settings.tracing_enabled:
        logger.debug("BRAINTRUST_API_KEY not set — running without tracing.")
        return False

    # Patch OpenAI + LangChain/LangGraph. Disable the integrations we do not use
    # so we are not paying import cost for them.
    braintrust.auto_instrument(
        openai=True,
        langchain=True,
        anthropic=False,
        litellm=False,
        google_genai=False,
        crewai=False,
        llamaindex=False,
        dspy=False,
    )
    braintrust.init_logger(
        api_key=settings.braintrust_api_key,
        project=settings.braintrust_project,
    )
    _INITIALIZED = True
    return True


def _serializable(value: Any) -> Any:
    """Best-effort conversion so span payloads survive JSON encoding."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    if isinstance(value, (list, tuple, set)):
        return [_serializable(v) for v in value]
    if isinstance(value, dict):
        return {k: _serializable(v) for k, v in value.items()}
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    return repr(value)


def traced_step(name: str, type: str = "task") -> Callable[[F], F]:
    """Like ``@traced``, but omits infrastructure args from the span input.

    ``@traced`` logs every argument. Our tool and sub-agent functions take the
    OpenAI client and Settings on every call, which would bloat each span with
    an unserializable object repr. This logs only the meaningful arguments.

    ``type`` sets the Braintrust span type. Use ``"tool"`` for anything the model
    can invoke and ``"task"`` for internal pipeline steps — the type drives how
    Braintrust groups and filters spans, and online scoring rules can target
    span types directly.
    """

    def decorator(fn: F) -> F:
        signature = inspect.signature(fn)

        @traced(name=name, type=type, notrace_io=True)
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                loggable = {
                    k: v for k, v in bound.arguments.items() if k not in _UNLOGGED_ARGS
                }
                current_span().log(input=_serializable(loggable))
            except Exception:  # pragma: no cover
                logger.debug("Could not log span input for %s", name, exc_info=True)

            result = fn(*args, **kwargs)

            try:
                current_span().log(output=_serializable(result))
            except Exception:  # pragma: no cover
                logger.debug("Could not log span output for %s", name, exc_info=True)
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def span_metadata(**fields: Any) -> None:
    """Attach metadata to the current span, ignoring the no-logger case."""
    try:
        current_span().log(metadata=_serializable(fields))
    except Exception:  # pragma: no cover - tracing must never break the agent
        logger.debug("Could not attach span metadata", exc_info=True)


def log_span(**fields: Any) -> None:
    """Log fields (input/output/expected) on the current span, never raising."""
    try:
        current_span().log(**{k: _serializable(v) for k, v in fields.items()})
    except Exception:  # pragma: no cover
        logger.debug("Could not log span fields", exc_info=True)
