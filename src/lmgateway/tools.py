"""
Tool-call emulation layer.

The upstream LLM does not support native function/tool calling.
This module bridges the gap by:

  1. Converting OpenAI `tools` definitions into plain-text instructions
     that are prepended to the user prompt.
  2. Parsing `<tool_call>{...}</tool_call>` blocks that the model emits
     in its response and converting them into the OpenAI `tool_calls` format.
  3. Re-serialising past tool calls into the conversation transcript so the
     model can follow multi-step agentic loops.

Reliability note: emulation quality depends on the upstream model's ability
to follow prompt instructions. For best results keep tool names and
descriptions concise and unambiguous. Set LMG_TOOL_EMULATION=false to
disable emulation entirely and receive plain text responses.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

# Matches <tool_call>{...}</tool_call> with optional stray leading angle brackets
_TOOL_CALL_RE = re.compile(r"<+tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


# ── Introspection helpers ─────────────────────────────────────────────────────

def tool_names(tools: list[dict[str, Any]]) -> set[str]:
    """Return the set of function names declared in a tools list."""
    return {str(_fn(t).get("name", "")) for t in tools}


# ── Prompt injection ──────────────────────────────────────────────────────────

def tool_instructions(tools: list[dict[str, Any]]) -> str:
    """
    Build a plain-text instruction block that teaches the model how to request
    tool calls using the <tool_call> syntax.
    """
    signatures: list[str] = []
    for tool in tools:
        fn = _fn(tool)
        name = fn.get("name", "")
        description = fn.get("description", "")
        props = (fn.get("parameters") or {}).get("properties") or {}
        params = ", ".join(
            f"{k}: {v.get('type', 'any')}" for k, v in props.items()
        )
        signatures.append(f"{name}({params}): {description}")

    return (
        "You are the reasoning engine of an agentic coding assistant. "
        "The client application that called you provides these functions: "
        + "; ".join(signatures)
        + ". The client executes them on your behalf and sends the results back. "
        "You cannot read files, run commands, or access the network directly. "
        "To request a function call, reply ONLY with one or more tool_call blocks "
        "in this exact format (one block per call): "
        '<tool_call>{"name": "<function name>", "arguments": {...}}</tool_call>. '
        "Never claim you executed a command or read a file yourself, and never "
        "invent results. If no function is needed, answer normally without any "
        "tool_call block."
    )


# ── Response parsing ──────────────────────────────────────────────────────────

def parse_tool_calls(
    text: str,
    known_names: set[str],
) -> tuple[str, list[dict[str, Any]]]:
    """
    Extract <tool_call> blocks from the model response.

    Returns
    -------
    remaining : str
        The response text with all tool_call blocks stripped out.
    calls : list[dict]
        Tool calls in the OpenAI tool_calls format, filtered to known names only.
    """
    calls: list[dict[str, Any]] = []

    for match in _TOOL_CALL_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue

        if not isinstance(data, dict):
            continue

        name = data.get("name")
        if not isinstance(name, str):
            continue
        if known_names and name not in known_names:
            continue

        arguments = data.get("arguments", {})
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)

        calls.append({
            "id": f"call_{uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })

    remaining = _TOOL_CALL_RE.sub("", text).strip()
    return remaining, calls


# ── Transcript serialisation ──────────────────────────────────────────────────

def render_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    """
    Re-serialise past tool calls into the conversation transcript so the model
    can follow multi-step agentic loops.
    """
    blocks: list[str] = []
    for call in tool_calls:
        fn = call.get("function", {})
        raw_args = fn.get("arguments", "{}")
        try:
            args_value: Any = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            args_value = {}
        payload = {"name": fn.get("name", ""), "arguments": args_value}
        blocks.append(f"<tool_call>{json.dumps(payload, ensure_ascii=False)}</tool_call>")
    return "\n".join(blocks)


# ── Internal ──────────────────────────────────────────────────────────────────

def _fn(tool: dict[str, Any]) -> dict[str, Any]:
    """Normalise both {type, function:{...}} and flat {name,...} tool shapes."""
    return tool.get("function") or tool