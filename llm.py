"""
Claude Agent SDK client.

Every agent goes through run_agent(): a system prompt, a user prompt, a
required JSON output shape, and an optional tool surface (built-in tools
like Read/Write/Bash, restricted via can_use_tool). The SDK's own agentic
loop drives exploration and tool calls to completion; ClaudeAgentOptions'
output_format asks the CLI for a schema-validated structured result on the
final turn, so there's no need for a hand-rolled tool-call loop or a
custom "finish" tool the way a raw-API integration would need -- one
function covers both the single-shot agents (Requirement, Release) and
the exploratory ones (Architecture, Coding, ...).

Auth: the SDK drives the bundled Claude Code CLI, which authenticates
exactly like an interactive Claude Code session (a Claude subscription
login, or ANTHROPIC_API_KEY/ANTHROPIC_AUTH_TOKEN if set) -- no separate
API key required. See README.md.
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

import config

CanUseTool = Callable[[str, Dict[str, Any]], "PermissionResultAllow | PermissionResultDeny"]

# Total attempts = MAX_RETRIES + 1. A single flaky generation (the CLI's
# own structured-output negotiation exhausting its internal repair
# attempts and exiting non-zero -- claude_agent_sdk raises this as
# ResultError, letting the whole `async for` in _run_agent_async raise
# before our own is_error/structured checks ever run) used to kill an
# entire multi-minute pipeline run outright, with no retry at any level.
# Mirrors claude_sdlc_architect/llm.py's own call_llm_json retry loop
# (max_repair_attempts=2, i.e. 3 total attempts), which already covers
# this same class of failure for that codebase.
MAX_RETRIES = 2


class LLMError(RuntimeError):
    """Raised when an agent run fails to produce a valid, schema-matching result."""


@dataclass
class AgentResult:
    result: Dict[str, Any]
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    total_cost_usd: float = 0.0


def _to_can_use_tool(scoped: Optional[CanUseTool]):
    """Adapt our simple (tool_name, tool_input) -> Allow/Deny callback into
    the SDK's async 3-arg signature (it also passes a permission context
    we have no use for)."""
    if scoped is None:
        return None

    async def _adapter(tool_name: str, tool_input: Dict[str, Any], _context: Any):
        return scoped(tool_name, tool_input)

    return _adapter


async def _run_agent_async(
    *,
    agent_name: str,
    system_prompt: str,
    user_prompt: str,
    output_schema: Dict[str, Any],
    cwd: Optional[Path] = None,
    tools: Optional[List[str]] = None,
    can_use_tool: Optional[CanUseTool] = None,
    mcp_servers: Optional[Dict[str, Any]] = None,
    max_turns: int = config.MAX_AGENT_TURNS,
    on_event: Optional[Callable[[], None]] = None,
    model: Optional[str] = None,
) -> AgentResult:
    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model or config.MODEL_ID,
        cwd=str(cwd) if cwd else None,
        tools=tools if tools is not None else [],
        can_use_tool=_to_can_use_tool(can_use_tool),
        mcp_servers=mcp_servers or {},
        permission_mode="default",
        output_format={"type": "json_schema", "schema": output_schema},
        max_turns=max_turns,
    )

    tool_calls: List[Dict[str, Any]] = []
    structured: Optional[Dict[str, Any]] = None
    cost = 0.0
    text_fallback = ""
    is_error = False
    error_detail = ""

    async for message in query(prompt=user_prompt, options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    # StructuredOutput is the SDK's own internal
                    # mechanism for delivering output_format's result,
                    # not a real tool call an agent made -- excluded
                    # from both the recorded tool_calls and the
                    # activity ping below.
                    if block.name == "StructuredOutput":
                        continue
                    tool_calls.append({"tool": block.name, "input": dict(block.input)})
                    if on_event is not None:
                        on_event()
                elif isinstance(block, TextBlock):
                    text_fallback += block.text
                    if block.text.strip() and on_event is not None:
                        on_event()
        elif isinstance(message, ResultMessage):
            cost = message.total_cost_usd or 0.0
            is_error = message.is_error
            error_detail = message.result or message.subtype
            if message.structured_output is not None:
                structured = message.structured_output

    if is_error and structured is None:
        raise LLMError(f"{agent_name} run failed ({error_detail}).")

    if structured is None:
        raise LLMError(
            f"{agent_name} did not return structured output matching its "
            f"required schema. Last text seen: {text_fallback[:500]!r}"
        )

    return AgentResult(result=structured, tool_calls=tool_calls, total_cost_usd=cost)


def run_agent(
    *,
    agent_name: str,
    system_prompt: str,
    user_prompt: str,
    output_schema: Dict[str, Any],
    required_keys: Optional[List[str]] = None,
    cwd: Optional[Path] = None,
    tools: Optional[List[str]] = None,
    can_use_tool: Optional[CanUseTool] = None,
    mcp_servers: Optional[Dict[str, Any]] = None,
    max_turns: int = config.MAX_AGENT_TURNS,
    on_event: Optional[Callable[[], None]] = None,
    model: Optional[str] = None,
) -> AgentResult:
    """Synchronous entry point -- every graph node in this codebase is a
    plain sync function (LangGraph nodes, called from Streamlit/CLI), so
    the async SDK is wrapped here once rather than at every call site.

    `on_event`, if given, is called (no arguments -- just a "something
    happened" ping, not any detail) each time Claude produces a tool
    call or a piece of narration. A UI can use this to show a plain
    "still working" indicator (e.g. cycling a status word) without
    this module needing to know anything about how it's displayed.

    `model`, if given, overrides config.MODEL_ID for this call only --
    every agents/*.py run() accepts its own `model` param so a run can
    choose a different Claude model per agent (see config.AGENT_NAMES,
    orchestrator/graph.py's `_model_for`).

    Retries up to MAX_RETRIES times on ANY failure -- a bad generation
    is generally not reproducible (the same prompt often succeeds on a
    fresh attempt), so retrying uniformly here is far cheaper than
    letting one flaky call kill an entire multi-minute pipeline run.
    For an agentic (tool-using) call, a retry re-explores from scratch
    -- there's no partial state to resume from once the CLI process
    has already exited, but that's still strictly better than crashing
    the whole run outright."""
    max_attempts = MAX_RETRIES + 1
    last_error: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        try:
            agent_result = asyncio.run(
                _run_agent_async(
                    agent_name=agent_name,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    output_schema=output_schema,
                    cwd=cwd,
                    tools=tools,
                    can_use_tool=can_use_tool,
                    mcp_servers=mcp_servers,
                    max_turns=max_turns,
                    on_event=on_event,
                    model=model,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- retried uniformly, re-raised below
            last_error = exc
            if attempt == max_attempts:
                raise LLMError(
                    f"{agent_name} failed after {max_attempts} attempt(s): {exc}"
                ) from exc
            continue

        missing = [k for k in (required_keys or []) if k not in agent_result.result]
        if missing:
            last_error = LLMError(f"{agent_name} result missing required key(s): {missing}")
            if attempt == max_attempts:
                raise last_error
            continue

        return agent_result

    raise LLMError(f"{agent_name} failed after {max_attempts} attempt(s): {last_error}")
