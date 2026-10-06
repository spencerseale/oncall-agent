"""Push the agent's prompts to Braintrust.

    make push-prompts      # bt functions push oncall_prompts.py --if-exists replace

Three prompts go up: the orchestrator's system prompt and the two sub-agent
prompts. Once pushed they are editable in the UI, versioned, diffable across
versions, and usable directly as a playground task.

The text is **not** duplicated here. Every prompt is defined once in
``oncall_agent.prompt_library`` and pushed from that definition, so the deployed
prompt and the checked-in prompt cannot drift. The runtime reads the same source:
with ``ONCALL_PROMPT_SOURCE=braintrust`` the agent loads the deployed version at
startup and records which one it got in ``metadata.prompt_source``, so a UI edit
takes effect on the next run without a redeploy.

Each prompt is deployed as a chat prompt whose user turn is ``{{{input}}}``, in
triple braces: mustache HTML-escapes double braces, which mangles the ``<``,
``>``, and quoting that appear throughout commands and JSON payloads.
"""

import os

import braintrust

from oncall_agent.prompt_library import PROMPT_SPECS

PROJECT_NAME = os.environ.get("BRAINTRUST_PROJECT", "oncall-agent")

project = braintrust.projects.create(name=PROJECT_NAME)

for spec in PROMPT_SPECS.values():
    project.prompts.create(
        name=spec.name,
        slug=spec.slug,
        description=spec.description,
        tags=list(spec.tags),
        model=spec.model,
        params=dict(spec.params),
        messages=spec.messages(),
        metadata={"source": "oncall_agent.prompt_library", "slug": spec.slug},
    )
