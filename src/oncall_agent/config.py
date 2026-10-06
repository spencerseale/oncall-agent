"""Runtime configuration for the on-call agent.

Everything the agent needs to talk to a model, Braintrust, and Exa is resolved
here so the rest of the package never reads ``os.environ`` directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

from . import __version__

load_dotenv()

# The Braintrust AI proxy gives us a single OpenAI-compatible endpoint plus
# automatic cost/usage attribution. Point OPENAI_BASE_URL at OpenAI directly if
# you would rather bypass it.
DEFAULT_BASE_URL = "https://api.braintrust.dev/v1/proxy"
DEFAULT_PROJECT = "oncall-agent"

# Three model tiers, cheapest at the top. The orchestrator's job is routing and
# attribution over tool results — a lot of tokens, not much reasoning — so it runs
# the cheap tier and the expensive tier is spent only where a wrong answer is
# expensive. Splitting them this way is also what makes the cost column in a
# Braintrust trace legible: the tier is visible per span, so "what did the deep
# work cost, and how often did we need it" is a question the traces answer.
DEFAULT_MODEL = "gpt-4.1"
DEFAULT_SUBAGENT_MODEL = "gpt-4o-mini"
# Remediation and validation are the two steps where being confidently wrong costs
# a responder the most: a plausible-looking patch, or a test suite that passes for
# the wrong reason. Both run a reasoning model.
DEFAULT_REASONING_MODEL = "o3"
DEFAULT_REASONING_EFFORT = "medium"
VALID_REASONING_EFFORTS = ("low", "medium", "high")

# Models that reject `temperature` and take `reasoning_effort` instead. Matched by
# prefix rather than by an exact list so setting ONCALL_REASONING_MODEL to a newer
# o-series or GPT-5 model does not need a code change.
REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")

# Deployment identity. These become trace tags, which is how you slice logs by
# "what was actually running" — compare v1.0.0 against v1.1.0, or exclude staging
# traffic from a production quality dashboard.
DEFAULT_ENVIRONMENT = "prod"
VALID_ENVIRONMENTS = ("prod", "staging", "dev")

# Where the orchestrator's system prompt comes from. "braintrust" pulls the
# version pushed by `make push-prompts`, so the prompt can be edited in the UI
# and rolled out without redeploying the agent.
VALID_PROMPT_SOURCES = ("local", "braintrust")


class ConfigError(RuntimeError):
    """Raised when required credentials or settings are missing or invalid."""


def is_reasoning_model(model: str) -> bool:
    """Whether a model takes ``reasoning_effort`` rather than ``temperature``."""
    return model.strip().lower().startswith(REASONING_MODEL_PREFIXES)


@dataclass(frozen=True)
class Settings:
    """Resolved settings for one agent process."""

    openai_api_key: str
    openai_base_url: str
    braintrust_api_key: str | None
    braintrust_project: str
    exa_api_key: str | None
    model: str
    subagent_model: str
    reasoning_model: str
    reasoning_effort: str
    max_tool_iterations: int
    agent_version: str
    environment: str
    prompt_source: str

    # Set by the remote eval so a playground run can swap the orchestrator's
    # system prompt and model params without redeploying the agent. Left unset
    # everywhere else, where the configured prompt and temperature apply.
    system_prompt: str | None = None
    model_params: dict[str, Any] = field(default_factory=dict)

    def call_params(self, model: str, temperature: float = 0.1) -> dict[str, Any]:
        """Sampling parameters for one model call.

        Reasoning models reject ``temperature`` outright (the o-series accepts only
        the default) and take ``reasoning_effort`` instead, so the two families
        cannot share a call site. Resolved from the model name rather than from
        which sub-agent is calling, so pointing ONCALL_REASONING_MODEL at a
        non-reasoning model degrades to a normal sampled call instead of erroring.
        """
        if is_reasoning_model(model):
            return {"reasoning_effort": self.reasoning_effort}
        return {"temperature": temperature}

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.braintrust_api_key)

    @property
    def web_search_enabled(self) -> bool:
        return bool(self.exa_api_key)

    def trace_tags(self, mode: str) -> list[str]:
        """Tags applied to every root span.

        Namespaced ``key:value`` strings rather than bare words so they stay
        readable in the UI tag filter once there are several of them, and so
        ``tags INCLUDES 'env:prod'`` is unambiguous in a query.
        """
        return [
            f"env:{self.environment}",
            f"agent:v{self.agent_version}",
            f"mode:{mode}",
        ]


def load_settings() -> Settings:
    """Read settings from the environment, failing fast on bad values."""
    openai_api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    braintrust_api_key = os.environ.get("BRAINTRUST_API_KEY", "").strip() or None

    # When running through the proxy the Braintrust key doubles as the model key.
    if not openai_api_key:
        openai_api_key = braintrust_api_key or ""

    if not openai_api_key:
        raise ConfigError(
            "Set OPENAI_API_KEY (or BRAINTRUST_API_KEY when using the Braintrust AI "
            "proxy). See .env.example."
        )

    environment = os.environ.get("ONCALL_ENV", DEFAULT_ENVIRONMENT).strip().lower()
    if environment not in VALID_ENVIRONMENTS:
        raise ConfigError(
            f"ONCALL_ENV must be one of {', '.join(VALID_ENVIRONMENTS)}; got {environment!r}. "
            "A typo here silently splits your traces across two tag values."
        )

    prompt_source = os.environ.get("ONCALL_PROMPT_SOURCE", "local").strip().lower()
    if prompt_source not in VALID_PROMPT_SOURCES:
        raise ConfigError(
            f"ONCALL_PROMPT_SOURCE must be one of {', '.join(VALID_PROMPT_SOURCES)}; "
            f"got {prompt_source!r}."
        )

    reasoning_effort = os.environ.get(
        "ONCALL_REASONING_EFFORT", DEFAULT_REASONING_EFFORT
    ).strip().lower()
    if reasoning_effort not in VALID_REASONING_EFFORTS:
        raise ConfigError(
            f"ONCALL_REASONING_EFFORT must be one of {', '.join(VALID_REASONING_EFFORTS)}; "
            f"got {reasoning_effort!r}."
        )

    return Settings(
        openai_api_key=openai_api_key,
        openai_base_url=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL).strip(),
        braintrust_api_key=braintrust_api_key,
        braintrust_project=os.environ.get("BRAINTRUST_PROJECT", DEFAULT_PROJECT).strip(),
        exa_api_key=os.environ.get("EXA_API_KEY", "").strip() or None,
        model=os.environ.get("ONCALL_MODEL", DEFAULT_MODEL).strip(),
        subagent_model=os.environ.get("ONCALL_SUBAGENT_MODEL", DEFAULT_SUBAGENT_MODEL).strip(),
        reasoning_model=os.environ.get(
            "ONCALL_REASONING_MODEL", DEFAULT_REASONING_MODEL
        ).strip(),
        reasoning_effort=reasoning_effort,
        max_tool_iterations=int(os.environ.get("ONCALL_MAX_TOOL_ITERATIONS", "8")),
        agent_version=os.environ.get("ONCALL_AGENT_VERSION", __version__).strip(),
        environment=environment,
        prompt_source=prompt_source,
    )
