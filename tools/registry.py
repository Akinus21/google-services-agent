"""
Shared tool registry for google-services-agent.

Both the A2A server (direct tool calls from Hermes) and the agent loop
(autonomous task execution) call into this same registry, so there is
exactly one place that defines what this agent can actually do and
whether each capability is currently available.
"""

from auth.google_auth import is_configured as google_configured
from tools.gmail_tools import (
    gmail_search,
    gmail_read,
    gmail_send,
    gmail_archive,
    gmail_label,
    gmail_star,
    gmail_trash,
    gmail_untrash,
)
from tools.calendar_tools import calendar_list_events, calendar_create_event
import mesh


def _get_message_id(a: dict) -> str:
    """Accept both 'message_id' (our documented schema) and bare 'id'
    (what Gmail's own API calls it, and a very natural mistake for a
    caller to make) — a mismatch here used to silently fall through to
    an empty string via a.get('message_id', ''), which then got sent
    straight to Google's API as an empty id, producing a confusing
    "'id' required" error instead of a clear one at our own layer."""
    return a.get("message_id") or a.get("id") or ""


def tool_defs():
    return [
        {
            "id": "gmail_search",
            "name": "Gmail Search",
            "description": "Search Gmail with a standard search query string (e.g. 'from:x is:unread'). Arguments: query (string, required), max_results (integer, optional, default 10, max 50).",
            "tags": ["gmail", "read"],
            "available": google_configured(),
            "exec": lambda a: gmail_search(a.get("query", ""), a.get("max_results", 10)),
            "params_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search query, e.g. 'in:inbox' or 'from:x is:unread'"},
                    "max_results": {"type": "integer", "description": "Max results, up to 50", "default": 10},
                },
                "required": ["query"],
            },
        },
        {
            "id": "gmail_read",
            "name": "Gmail Read",
            "description": "Fetch the full body of a message (not just the search snippet) — use before making any judgment call on an ambiguous email.",
            "tags": ["gmail", "read"],
            "available": google_configured(),
            "exec": lambda a: gmail_read(_get_message_id(a)),
            "params_schema": {
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
            },
        },
        {
            "id": "gmail_send",
            "name": "Gmail Send",
            "description": "Send a plain-text email.",
            "tags": ["gmail", "write"],
            "available": google_configured(),
            "exec": lambda a: gmail_send(a.get("to", ""), a.get("subject", ""), a.get("body", "")),
            "params_schema": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["to", "subject", "body"],
            },
        },
        {
            "id": "gmail_archive",
            "name": "Gmail Archive",
            "description": "Archive a message by id (removes from inbox, does not delete).",
            "tags": ["gmail", "write"],
            "available": google_configured(),
            "exec": lambda a: gmail_archive(_get_message_id(a)),
            "params_schema": {
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
            },
        },
        {
            "id": "gmail_label",
            "name": "Gmail Label",
            "description": "Add and/or remove labels on a message by human-readable label name (e.g. 'Important', 'Work', or system labels like 'UNREAD'). At least one of add/remove is required.",
            "tags": ["gmail", "write"],
            "available": google_configured(),
            "exec": lambda a: gmail_label(_get_message_id(a), a.get("add", []), a.get("remove", [])),
            "params_schema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "add": {"type": "array", "items": {"type": "string"}},
                    "remove": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["message_id"],
            },
        },
        {
            "id": "gmail_star",
            "name": "Gmail Star",
            "description": "Star or unstar a message.",
            "tags": ["gmail", "write"],
            "available": google_configured(),
            "exec": lambda a: gmail_star(_get_message_id(a), a.get("starred", True)),
            "params_schema": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "starred": {"type": "boolean", "default": True},
                },
                "required": ["message_id"],
            },
        },
        {
            "id": "gmail_trash",
            "name": "Gmail Trash",
            "description": "Move a message to Trash (reversible for ~30 days — NOT permanent delete).",
            "tags": ["gmail", "write"],
            "available": google_configured(),
            "exec": lambda a: gmail_trash(_get_message_id(a)),
            "params_schema": {
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
            },
        },
        {
            "id": "gmail_untrash",
            "name": "Gmail Untrash",
            "description": "Restore a message out of Trash.",
            "tags": ["gmail", "write"],
            "available": google_configured(),
            "exec": lambda a: gmail_untrash(_get_message_id(a)),
            "params_schema": {
                "type": "object",
                "properties": {"message_id": {"type": "string"}},
                "required": ["message_id"],
            },
        },
        {
            "id": "calendar_list_events",
            "name": "Calendar List Events",
            "description": "List upcoming events on the primary calendar.",
            "tags": ["calendar", "read"],
            "available": google_configured(),
            "exec": lambda a: calendar_list_events(a.get("days_ahead", 7), a.get("max_results", 20)),
            "params_schema": {
                "type": "object",
                "properties": {
                    "days_ahead": {"type": "integer", "default": 7},
                    "max_results": {"type": "integer", "default": 20},
                },
                "required": [],
            },
        },
        {
            "id": "calendar_create_event",
            "name": "Calendar Create Event",
            "description": "Create an event on the primary calendar. start/end are RFC3339 timestamps.",
            "tags": ["calendar", "write"],
            "available": google_configured(),
            "exec": lambda a: calendar_create_event(
                a.get("summary", ""), a.get("start", ""), a.get("end", ""), a.get("description", "")
            ),
            "params_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["summary", "start", "end"],
            },
        },
        {
            "id": "mesh_call",
            "name": "Mesh Call",
            "description": (
                "Call a tool on another agent in the mesh (e.g. docker-ops-agent "
                "on a different host) when this agent's own tools can't do "
                "something a task requires. Specify host_label (preferred) or "
                "role to pick the target peer, plus the tool_name and arguments "
                "to call on it."
            ),
            "tags": ["mesh"],
            "available": bool(mesh.MESH_TOKEN),
            "exec": lambda a: mesh.mesh_call(
                a.get("host_label"), a.get("role"), a.get("tool_name"), a.get("arguments", {})
            ),
            "params_schema": {
                "type": "object",
                "properties": {
                    "host_label": {"type": "string", "description": "e.g. 'services', 'ai', 'security', 'devops'"},
                    "role": {"type": "string", "description": "e.g. 'docker-ops', 'google-services'"},
                    "tool_name": {"type": "string", "description": "the tool to call on the target peer"},
                    "arguments": {"type": "object", "description": "arguments for that tool"},
                },
                "required": ["tool_name"],
            },
        },
        {
            "id": "mesh_list_peers",
            "name": "Mesh List Peers",
            "description": "List every agent currently registered in the mesh, with their host_label and role — use this to discover what's available before calling mesh_call.",
            "tags": ["mesh"],
            "available": bool(mesh.MESH_TOKEN) or bool(mesh.REGISTRY_URL),
            "exec": lambda a: mesh.mesh_list_peers(),
            "params_schema": {"type": "object", "properties": {}, "required": []},
        },
        {
            "id": "ask_hermes",
            "name": "Ask Hermes",
            "description": (
                "Ask Hermes a question directly — e.g. 'what corrections or "
                "guidance have I given about email triage?' Use this to check "
                "for updated instructions before an autonomous run, since "
                "Hermes accumulates guidance from conversations this agent "
                "never sees on its own."
            ),
            "tags": ["hermes"],
            "available": bool(mesh.HERMES_AGENT_URL) and bool(mesh.HERMES_AGENT_TOKEN),
            "exec": lambda a: mesh.ask_hermes(a.get("question", "")),
            "params_schema": {
                "type": "object",
                "properties": {"question": {"type": "string"}},
                "required": ["question"],
            },
        },
    ]


def available_tools():
    return [t for t in tool_defs() if t["available"]]


def find_tool(name):
    for t in tool_defs():
        if t["id"] == name:
            return t
    return None


def execute_tool(name, arguments):
    tool = find_tool(name)
    if tool is None:
        return {"ok": False, "output": f"unknown tool `{name}` — see agent card `skills` for the allowlist"}
    if not tool["available"]:
        return {"ok": False, "output": f"tool `{name}` is not available — Google OAuth has not been configured on this host yet"}
    return tool["exec"](arguments)


def openai_tool_schemas():
    """Convert the tool registry into OpenAI/Ollama function-calling
    schema format, for use by the agent loop."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["id"],
                "description": t["description"],
                "parameters": t["params_schema"],
            },
        }
        for t in available_tools()
    ]
