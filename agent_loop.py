"""
Agent loop — this is what makes google-services-agent an actual agent
rather than a plain tool server. Given a natural-language task
("triage my inbox", "find anything urgent from the last day"), this
calls a local LLM (Ollama, OpenAI-compatible API — same model Hermes
itself uses) with the tool registry exposed as function-calling
tools, executes whatever the model decides to call, feeds results
back, and iterates until the model returns a final answer or a step
cap is hit.

This is intentionally a separate, bounded reasoning loop — it doesn't
have Hermes' full context or memory, only what's in the task
instructions plus its own context store. That's deliberate: it's
meant to be handed a scoped job, not asked open-ended questions about
Gabriel's whole life.
"""

import os
import json
import requests

from tools.registry import execute_tool, openai_tool_schemas
import context.store as context_store

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "minimax-m2.7:cloud")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "8"))


def _chat_completion(messages, tools):
    resp = requests.post(
        f"{OLLAMA_BASE_URL}/chat/completions",
        json={
            "model": OLLAMA_MODEL,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def run_task(instructions: str, system_prompt: str = None) -> dict:
    """Run an autonomous multi-step task. Returns {"ok": bool,
    "output": <final answer or error>, "steps": <list of tool calls made>}."""
    tools = openai_tool_schemas()
    if not tools:
        return {
            "ok": False,
            "output": "no tools are available — Google OAuth has not been configured on this host yet",
            "steps": [],
        }

    default_system = (
        "You are google-services-agent, an autonomous assistant that manages "
        "Gabriel's Gmail and Calendar. You have a fixed set of tools — call "
        "them as needed to complete the task. When you are done, respond with "
        "a final plain-text summary of what you did, with no further tool calls."
    )
    messages = [
        {"role": "system", "content": system_prompt or default_system},
        {"role": "user", "content": instructions},
    ]

    steps = []
    for step_num in range(MAX_STEPS):
        try:
            completion = _chat_completion(messages, tools)
        except Exception as e:
            return {"ok": False, "output": f"LLM call failed: {e}", "steps": steps}

        choice = completion.get("choices", [{}])[0]
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            # Model returned a final answer — done.
            final_text = message.get("content", "")
            return {"ok": True, "output": final_text, "steps": steps}

        messages.append(message)
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name")
            try:
                arguments = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError as e:
                arguments = {}
                result = {"ok": False, "output": f"model produced invalid arguments JSON: {e}"}
            else:
                result = execute_tool(name, arguments)

            steps.append({"tool": name, "arguments": arguments, "result": result})
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id"),
                "content": json.dumps(result),
            })

    return {
        "ok": False,
        "output": f"task did not complete within {MAX_STEPS} steps — may need a narrower instruction",
        "steps": steps,
    }
