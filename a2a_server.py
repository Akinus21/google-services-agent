"""
google-services-agent — A2A server

Matches docker-ops-agent's mesh conventions exactly:
  - Real A2A message/send (part discriminator field is "kind", not "type")
  - tools/call kept as a deprecated backward-compat alias
  - mesh_call / mesh_list_peers for direct agent-to-agent calls
  - Bearer auth: A2A_STATIC_AUTH_TOKEN (per-host, Hermes uses this) OR
    MESH_AUTH_TOKEN (shared, agent-to-agent calls use this)
  - Self-registration heartbeat against mesh-registry

Tool availability is reported dynamically based on whether the
relevant OAuth token actually exists on disk (auth.google_auth.is_configured()),
same "only advertise what's actually usable" pattern as docker-ops-agent's
binary-presence check.
"""

import os
import sys
import time
import uuid
import threading
import requests
from datetime import datetime, timezone
from flask import Flask, request, jsonify

sys.path.insert(0, os.path.dirname(__file__))

from auth.google_auth import is_configured as google_configured
from tools.gmail_tools import gmail_search, gmail_send, gmail_archive
from tools.calendar_tools import calendar_list_events, calendar_create_event

app = Flask(__name__)

BEARER_TOKEN = os.environ.get("A2A_STATIC_AUTH_TOKEN")
MESH_TOKEN = os.environ.get("MESH_AUTH_TOKEN")
HOST_LABEL = os.environ.get("A2A_HOST_LABEL", "unknown-host")
SELF_CARD_URL = os.environ.get(
    "A2A_SELF_CARD_URL", "http://google-services-agent:8200/.well-known/agent-card.json"
)
REGISTRY_URL = os.environ.get("REGISTRY_URL")
REGISTRY_TOKEN = os.environ.get("REGISTRY_TOKEN")
BIND_PORT = int(os.environ.get("BIND_PORT", "8200"))

if not BEARER_TOKEN:
    raise RuntimeError("A2A_STATIC_AUTH_TOKEN must be set")

# ---------------------------------------------------------------------
# Tool registry — each entry: (id, name, description, tags, available_fn, exec_fn)
# available_fn is checked at agent-card/tools-list time; exec_fn is
# called with the parsed arguments dict.
# ---------------------------------------------------------------------

def _tool_defs():
    return [
        {
            "id": "gmail_search",
            "name": "Gmail Search",
            "description": "Search Gmail with a standard search query string (e.g. 'from:x is:unread'). Arguments: query (string, required), max_results (integer, optional, default 10, max 50).",
            "tags": ["gmail", "read"],
            "available": google_configured(),
            "exec": lambda a: gmail_search(a.get("query", ""), a.get("max_results", 10)),
        },
        {
            "id": "gmail_send",
            "name": "Gmail Send",
            "description": "Send a plain-text email. Arguments: to (string, required), subject (string, required), body (string, required).",
            "tags": ["gmail", "write"],
            "available": google_configured(),
            "exec": lambda a: gmail_send(a.get("to", ""), a.get("subject", ""), a.get("body", "")),
        },
        {
            "id": "gmail_archive",
            "name": "Gmail Archive",
            "description": "Archive a message by id (removes from inbox, does not delete). Arguments: message_id (string, required).",
            "tags": ["gmail", "write"],
            "available": google_configured(),
            "exec": lambda a: gmail_archive(a.get("message_id", "")),
        },
        {
            "id": "calendar_list_events",
            "name": "Calendar List Events",
            "description": "List upcoming events on the primary calendar. Arguments: days_ahead (integer, optional, default 7, max 60), max_results (integer, optional, default 20, max 100).",
            "tags": ["calendar", "read"],
            "available": google_configured(),
            "exec": lambda a: calendar_list_events(a.get("days_ahead", 7), a.get("max_results", 20)),
        },
        {
            "id": "calendar_create_event",
            "name": "Calendar Create Event",
            "description": "Create an event on the primary calendar. Arguments: summary (string, required), start (RFC3339 string, required), end (RFC3339 string, required), description (string, optional).",
            "tags": ["calendar", "write"],
            "available": google_configured(),
            "exec": lambda a: calendar_create_event(
                a.get("summary", ""), a.get("start", ""), a.get("end", ""), a.get("description", "")
            ),
        },
    ]


def available_tools():
    return [t for t in _tool_defs() if t["available"]]


def find_tool(name):
    for t in _tool_defs():
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


# ---------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------

def check_auth():
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[len("Bearer "):]
    if token == BEARER_TOKEN:
        return True
    if MESH_TOKEN and token == MESH_TOKEN:
        return True
    return False


# ---------------------------------------------------------------------
# Agent card
# ---------------------------------------------------------------------

def peer_rpc_base(agent_card_url: str) -> str:
    return agent_card_url.rsplit("/.well-known/agent-card.json", 1)[0]


@app.route("/.well-known/agent-card.json", methods=["GET"])
def agent_card_route():
    skills = [
        {"id": t["id"], "name": t["name"], "description": t["description"], "tags": t["tags"]}
        for t in available_tools()
    ]
    self_rpc_base = peer_rpc_base(SELF_CARD_URL)
    return jsonify({
        "name": f"google-services-agent ({HOST_LABEL})",
        "description": (
            "Personal assistant A2A service — holds Gabriel's Google account "
            "access and acts on his behalf. Does not grant Hermes direct "
            "OAuth tokens; every operation is a named, validated function. "
            "Invoke via message/send with a single data part shaped like "
            '{"name": "<skill id>", "arguments": {...}} — see the skills '
            "array below for valid ids and their arguments. Responds with a "
            "standard Task object; the result is in "
            "artifacts[0].parts[0].data as {\"ok\": bool, \"output\": ...}."
        ),
        "version": "0.1.0",
        "hostLabel": HOST_LABEL,
        "supportedInterfaces": [
            {"url": self_rpc_base, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False},
        "securitySchemes": {
            "bearerAuth": {
                "httpAuthSecurityScheme": {
                    "description": "Bearer token authentication",
                    "scheme": "bearer",
                    "bearerFormat": "opaque",
                }
            }
        },
        "securityRequirements": [{"schemes": {"bearerAuth": {}}}],
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": skills,
    })


# ---------------------------------------------------------------------
# message/send -> Task, tools/call alias, mesh_call, mesh_list_peers, ping
# ---------------------------------------------------------------------

def tool_call_from_message(message: dict):
    parts = message.get("parts", [])
    data_part = next((p for p in parts if p.get("kind") == "data"), None)
    if data_part is not None:
        data = data_part.get("data")
        if not data or "name" not in data:
            return None, "data part must contain {\"name\": ..., \"arguments\": {...}}"
        return {"name": data["name"], "arguments": data.get("arguments", {})}, None

    text_part = next((p for p in parts if p.get("kind") == "text"), None)
    if text_part is not None:
        text = (text_part.get("text") or "").strip()
        if not text:
            return None, "text part is empty"
        if text.startswith("{"):
            import json as _json
            try:
                parsed = _json.loads(text)
                return {"name": parsed.get("name"), "arguments": parsed.get("arguments", {})}, None
            except Exception as e:
                return None, f"text part looked like JSON but failed to parse: {e}"
        return {"name": text, "arguments": {}}, None

    return None, "message must contain a \"data\" part or a \"text\" part"


def build_task_response(context_id, result):
    now = datetime.now(timezone.utc).isoformat()
    state = "completed" if result.get("ok") else "failed"
    return {
        "id": str(uuid.uuid4()),
        "contextId": context_id or str(uuid.uuid4()),
        "status": {"state": state, "timestamp": now},
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "name": "result",
                "parts": [{"kind": "data", "data": {"ok": result.get("ok"), "output": result.get("output")}}],
            }
        ],
    }


def fetch_registry_peers():
    if not REGISTRY_URL:
        return None, "REGISTRY_URL is not configured on this agent"
    try:
        resp = requests.get(f"{REGISTRY_URL}/peers", timeout=5)
        resp.raise_for_status()
        return resp.json().get("peers", []), None
    except Exception as e:
        return None, f"failed to reach registry: {e}"


def call_peer(rpc_base, mesh_token, tool_name, arguments):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "data", "data": {"name": tool_name, "arguments": arguments}}],
            }
        },
    }
    resp = requests.post(
        rpc_base,
        json=payload,
        headers={"Authorization": f"Bearer {mesh_token}", "Content-Type": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def handle_mesh_call(params):
    if not MESH_TOKEN:
        return None, "MESH_AUTH_TOKEN is not configured on this agent — cannot make outbound mesh calls"
    host_label = params.get("host_label")
    role = params.get("role")
    tool_name = params.get("tool_name")
    arguments = params.get("arguments", {})
    if not tool_name:
        return None, "mesh_call requires tool_name"

    peers, err = fetch_registry_peers()
    if err:
        return None, err

    matches = [
        p for p in peers
        if (host_label is None or p.get("host_label") == host_label)
        and (role is None or p.get("role") == role)
    ]
    if len(matches) == 0:
        return None, "no registered peer matches the given host_label/role"
    if len(matches) > 1:
        return None, f"{len(matches)} peers match — specify host_label to disambiguate"

    rpc_base = peer_rpc_base(matches[0]["agent_card_url"])
    try:
        return call_peer(rpc_base, MESH_TOKEN, tool_name, arguments), None
    except Exception as e:
        return None, f"peer call failed: {e}"


@app.route("/", methods=["POST"])
def rpc_handler():
    if not check_auth():
        return jsonify({"error": "missing or invalid bearer token"}), 401

    try:
        req = request.get_json(force=True)
    except Exception as e:
        return jsonify({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}}), 400

    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params", {})

    if method == "message/send":
        message = params.get("message", {})
        call, err = tool_call_from_message(message)
        if err:
            return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": err}})
        result = execute_tool(call["name"], call.get("arguments", {}))
        context_id = message.get("contextId")
        task = build_task_response(context_id, result)
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": task})

    if method == "tools/call":
        # DEPRECATED alias — same execution, old response shape, kept
        # only for peers mid-rollout on an older build.
        name = params.get("name")
        arguments = params.get("arguments", {})
        result = execute_tool(name, arguments)
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})

    if method == "tools/list":
        skills = [
            {"id": t["id"], "name": t["name"], "description": t["description"], "tags": t["tags"]}
            for t in available_tools()
        ]
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"tools": skills}})

    if method == "mesh_list_peers":
        peers, err = fetch_registry_peers()
        if err:
            return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": err}})
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"peers": peers}})

    if method == "mesh_call":
        result, err = handle_mesh_call(params)
        if err:
            return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": err}})
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})

    if method == "ping":
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": "Pong!"})

    return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not found: {method}"}})


# ---------------------------------------------------------------------
# Mesh registry self-registration heartbeat
# ---------------------------------------------------------------------

def registry_heartbeat_loop():
    if not REGISTRY_URL or not REGISTRY_TOKEN:
        print("REGISTRY_URL/REGISTRY_TOKEN not set — mesh self-registration disabled", flush=True)
        return
    payload = {
        "host_label": HOST_LABEL,
        "role": "google-services",
        "agent_card_url": SELF_CARD_URL,
        "ttl_seconds": 90,
    }
    while True:
        try:
            resp = requests.post(
                f"{REGISTRY_URL}/register",
                json=payload,
                headers={"X-Registry-Token": REGISTRY_TOKEN},
                timeout=5,
            )
            if resp.status_code != 200:
                print(f"registry heartbeat returned HTTP {resp.status_code}", flush=True)
        except Exception as e:
            print(f"registry heartbeat failed: {e}", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    t = threading.Thread(target=registry_heartbeat_loop, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=BIND_PORT)
