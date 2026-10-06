"""The on-call agent graph.

A three-node LangGraph ReAct loop:

    agent --(tool calls?)--> tools --(budget left?)--> agent
      |                        |
      |                        +--(budget spent)--> finalize --> END
      |
      +--(no tool calls)--> END

The ``finalize`` node exists because the two-node version could end a turn with
**no answer at all**. When the loop hit ``max_tool_iterations`` the router sent it
straight to END while the last assistant message still carried pending tool calls,
so no assistant turn ever produced prose and ``output`` was logged as an empty
string. That is a broken handoff during an incident, and it is a violation of the
root-span contract, which promises ``output`` is the answer.

Observed on "What would break if we rolled back dep-9f31c2a?": the model looped on
``get_service_catalog`` five times, spent all eight iterations, and returned "".
Caught by the ``empty_answer`` check in the ``Defect Free`` scorer.

The cap is now enforced **after** the tools node rather than before it, so the tool
batch the model already asked for is executed rather than discarded — which also
keeps the transcript valid, since an assistant message with ``tool_calls`` must be
followed by a tool message per call. ``finalize`` then makes one more model call
with no tools available and an instruction to answer from what it has.

The OpenAI client is **not** constructed here. It is built once at process start
and handed to the graph through the invocation config
(``config["configurable"]["openai_client"]``), so the same compiled graph can be
driven by a wrapped/traced client, a mock in tests, or a different endpoint
without touching graph code.
"""

from __future__ import annotations

import json
import logging
import operator
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph
from openai import OpenAI

from .config import Settings
from .prompt_library import FINALIZE_INSTRUCTION
from .prompts import resolve as resolve_prompt
from .tools import TOOL_SCHEMAS, dispatch_tool
from .tracing import span_metadata

logger = logging.getLogger(__name__)


def _merge_entities(
    left: dict[str, list[str]], right: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Union two entity maps. Sorted so a trace's metadata is stable run to run."""
    merged = {key: list(values) for key, values in (left or {}).items()}
    for key, values in (right or {}).items():
        merged[key] = sorted(set(merged.get(key, [])) | set(values))
    return merged


class AgentState(TypedDict):
    """Graph state. ``messages`` is the raw OpenAI chat transcript."""

    messages: Annotated[list[dict[str, Any]], operator.add]
    iterations: int
    sources: Annotated[list[dict[str, Any]], operator.add]
    # Which incidents, services, teams, severities, and failure domains this turn
    # touched. Promoted to root-span metadata so production traffic is sliceable
    # by team and domain without parsing the answer text.
    entities: Annotated[dict[str, list[str]], _merge_entities]


def _resolve(config: RunnableConfig) -> tuple[OpenAI, Settings]:
    """Pull the injected client and settings out of the invocation config."""
    configurable = (config or {}).get("configurable", {})
    client = configurable.get("openai_client")
    settings = configurable.get("settings")
    if client is None or settings is None:
        raise ValueError(
            "The graph requires 'openai_client' and 'settings' in "
            "config['configurable']. Use oncall_agent.agent.OncallSession."
        )
    return client, settings


def agent_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Call the model with the full transcript and the tool schemas."""
    client, settings = _resolve(config)

    # The prompt may be the checked-in literal, a version deployed in Braintrust,
    # or a playground override. Which one ran is recorded on the span, because a
    # prompt that silently fell back is indistinguishable from one that loaded.
    prompt, source = resolve_prompt(settings)
    span_metadata(prompt_source=source)

    response = client.chat.completions.create(
        model=settings.model,
        messages=[{"role": "system", "content": prompt}, *state["messages"]],
        tools=TOOL_SCHEMAS,
        tool_choice="auto",
        **{"temperature": 0.1, **settings.model_params},
    )

    message = response.choices[0].message
    assistant: dict[str, Any] = {"role": "assistant", "content": message.content}
    if message.tool_calls:
        assistant["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in message.tool_calls
        ]
        span_metadata(requested_tools=[tc.function.name for tc in message.tool_calls])

    return {"messages": [assistant], "iterations": state["iterations"] + 1}


def tools_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Execute every tool call the model requested in the last turn."""
    client, settings = _resolve(config)

    last = state["messages"][-1]
    tool_calls = last.get("tool_calls") or []

    outputs: list[dict[str, Any]] = []
    collected_sources: list[dict[str, Any]] = []
    entities: dict[str, list[str]] = {}

    for call in tool_calls:
        name = call["function"]["name"]
        result, touched = dispatch_tool(
            name=name,
            arguments=call["function"]["arguments"],
            settings=settings,
            client=client,
        )

        entities = _merge_entities(entities, {k: sorted(v) for k, v in touched.items()})

        # Surface sub-agent citations at the top level so the CLI can print them.
        if name == "correlate_root_cause" and isinstance(result, dict):
            collected_sources.extend(result.get("sources", []))

        outputs.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(result, default=str),
            }
        )

    return {"messages": outputs, "sources": collected_sources, "entities": entities}


def finalize_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    """Force a final answer once the tool budget is spent.

    Called with **no tools** in the request, so the model cannot ask for more and
    has to synthesize. The instruction is appended as a user turn for this call
    only and is deliberately not added to the returned state: the REPL carries the
    transcript into the next turn, and a synthetic instruction left in it would
    accumulate.
    """
    client, settings = _resolve(config)
    prompt, source = resolve_prompt(settings)

    logger.warning(
        "Tool budget exhausted after %s iterations; forcing a final answer.",
        state["iterations"],
    )
    span_metadata(prompt_source=source, reason="tool_budget_exhausted")

    response = client.chat.completions.create(
        model=settings.model,
        messages=[
            {"role": "system", "content": prompt},
            *state["messages"],
            {"role": "user", "content": FINALIZE_INSTRUCTION},
        ],
        **{"temperature": 0.1, **settings.model_params},
    )

    # No `iterations` in the return: the field is a plain overwrite, and the count
    # is what agent.ask reads to decide whether the budget was exhausted.
    return {"messages": [{"role": "assistant", "content": response.choices[0].message.content}]}


def should_continue(state: AgentState, config: RunnableConfig) -> str:
    """After the model turn: run the tools it asked for, or end if it asked for none."""
    last = state["messages"][-1]
    return "tools" if last.get("tool_calls") else END


def after_tools(state: AgentState, config: RunnableConfig) -> str:
    """After the tool node: loop back for another turn, or force an answer.

    The cap is checked here rather than in ``should_continue`` so the tool batch the
    model already requested is executed first. Ending the turn with unanswered
    ``tool_calls`` in the transcript is what produced empty answers, and it also
    leaves a message list the API rejects on the next turn.
    """
    _, settings = _resolve(config)
    if state["iterations"] >= settings.max_tool_iterations:
        return "finalize"
    return "agent"


def build_graph():
    """Compile the agent graph. Safe to build once and reuse."""
    builder = StateGraph(AgentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("finalize", finalize_node)
    builder.set_entry_point("agent")
    builder.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    builder.add_conditional_edges(
        "tools", after_tools, {"agent": "agent", "finalize": "finalize"}
    )
    builder.add_edge("finalize", END)
    return builder.compile()
