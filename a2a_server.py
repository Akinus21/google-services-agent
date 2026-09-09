"""
google-services-agent — A2A server

Matches docker-ops-agent's mesh conventions:
  - Real A2A message/send (part discriminator field is "kind", not "type")
  - tools/call kept as a deprecated backward-compat alias
  - mesh_call / mesh_list_peers for direct agent-to-agent calls
  - Bearer auth: A2A_STATIC_AUTH_TOKEN (per-host, Hermes uses this) OR
    MESH_AUTH_TOKEN (shared, agent-to-agent calls use this)
  - Self-registration heartbeat against mesh-registry

Beyond that, this agent is a real agent, not just a tool server:
  - run_task: hand it a natural-language goal, it decides which tools
    to call itself via agent_loop.py (LLM-driven, Ollama-backed)
  - An autonomous background scheduler (ENABLE_AUTONOMOUS_TRIAGE) that
    runs email triage on its own schedule without Hermes prompting it
  - A context store (context/store.py) it can read/write to as it
    works, so repeated observations accumulate into durable facts
    Hermes can later query via get_context/search_context

Tool availability is reported dynamically based on whether the
relevant OAuth token actually exists on disk, same "only advertise
what's actually usable" pattern as docker-ops-agent's binary check.
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

from tools.registry import available_tools, execute_tool, find_tool
import agent_loop
import context.store as context_store
import mesh

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
ENABLE_AUTONOMOUS_TRIAGE = os.environ.get("ENABLE_AUTONOMOUS_TRIAGE", "false").lower() == "true"
TRIAGE_INTERVAL_SECONDS = int(os.environ.get("TRIAGE_INTERVAL_SECONDS", "900"))

if not BEARER_TOKEN:
    raise RuntimeError("A2A_STATIC_AUTH_TOKEN must be set")

TRIAGE_INSTRUCTIONS = """
Triage the inbox. For each email in the inbox:
1. Read it fully before deciding anything.
2. If it's clearly spam, marketing, or noise the user has no interest
   in, trash it.
3. If it's from a known-important sender (family, financial
   institutions, government/military correspondence, direct work
   contacts) or is otherwise clearly important, add the "IMPORTANT"
   label and remove it from the inbox.
4. If it's something worth keeping but not urgent (receipts,
   confirmations, reference material), add the "Save" label and
   remove it from the inbox.
5. If you are not confident which of the above applies, add the
   "Review" label and leave it in the inbox — do not guess on
   anything ambiguous.
Do not trash anything you are not certain is unwanted. When done,
summarize how many emails fell into each category.
"""

# How often (in triage cycles, not seconds) to check with Hermes for
# updated guidance — every cycle would be wasteful (an extra LLM call
# for something that changes rarely); this defaults to roughly once a
# day at the default 15-minute interval.
GUIDANCE_REFRESH_EVERY_N_CYCLES = int(os.environ.get("GUIDANCE_REFRESH_EVERY_N_CYCLES", "96"))


def refresh_triage_guidance_from_hermes():
    """Ask Hermes what triage corrections/preferences the user has
    given it, and persist the answer so future autonomous runs use it
    — this is what makes "adjust its own processes" actually durable
    rather than a one-off nudge that's forgotten next cycle."""
    result = mesh.ask_hermes(
        "I'm the autonomous email triage process. Have you received any "
        "corrections, preferences, or new rules from the user about how "
        "email should be triaged (what counts as important, what to "
        "trash, any senders or patterns to add or remove) since the last "
        "time I asked? If nothing has changed, say so plainly. Keep the "
        "answer to concrete, actionable rules only — no commentary."
    )
    if result["ok"]:
        context_store.set_preference("email", "triage_guidance", str(result["output"]), "hermes")
    return result


def current_triage_instructions() -> str:
    ctx = context_store.get_context("email")
    guidance = next(
        (p["value"] for p in ctx.get("preferences", []) if p["domain"] == "email" and p["key"] == "triage_guidance"),
        None,
    )
    if guidance:
        return TRIAGE_INSTRUCTIONS + f"\n\nAdditional guidance from Hermes (may reflect recent corrections the user gave): {guidance}"
    return TRIAGE_INSTRUCTIONS


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


def peer_rpc_base(agent_card_url: str) -> str:
    return mesh.peer_rpc_base(agent_card_url)


@app.route("/.well-known/agent-card.json", methods=["GET"])
def agent_card_route():
    skills = [
        {"id": t["id"], "name": t["name"], "description": t["description"], "tags": t["tags"]}
        for t in available_tools()
    ]
    if available_tools():
        skills.append({
            "id": "run_task",
            "name": "Run Task",
            "description": (
                "Hand this agent a natural-language goal (e.g. 'triage my inbox', "
                "'find anything urgent from today') and it will autonomously decide "
                "which of its own tools to call to accomplish it, using a local LLM. "
                "Arguments: instructions (string, required)."
            ),
            "tags": ["autonomous", "agent"],
        })
    skills.append({
        "id": "get_context",
        "name": "Get Context",
        "description": "Return accumulated distilled facts/preferences this agent has learned, optionally filtered by topic. Arguments: topic (string, optional).",
        "tags": ["context", "read"],
    })
    skills.append({
        "id": "search_context",
        "name": "Search Context",
        "description": "Search accumulated facts/preferences by content, not just entity name. Arguments: query (string, required).",
        "tags": ["context", "read"],
    })

    self_rpc_base = peer_rpc_base(SELF_CARD_URL)
    return jsonify({
        "name": f"google-services-agent ({HOST_LABEL})",
        "description": (
            "Autonomous personal assistant A2A service — holds Gabriel's Google "
            "account access and can either execute specific named tools directly, "
            "or be given a natural-language task via run_task and decide the "
            "steps itself. Accumulates a context store of learned facts/"
            "preferences as it operates. Does not grant Hermes direct OAuth "
            "tokens; every operation is a named, validated function underneath. "
            "Invoke via message/send with a single data part shaped like "
            '{"name": "<skill id>", "arguments": {...}}. Responds with a '
            "standard Task object; the result is in "
            "artifacts[0].parts[0].data."
        ),
        "version": "0.2.0",
        "hostLabel": HOST_LABEL,
        "autonomousTriageEnabled": ENABLE_AUTONOMOUS_TRIAGE,
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


def dispatch(name, arguments):
    if name == "run_task":
        instructions = arguments.get("instructions", "")
        if not instructions:
            return {"ok": False, "output": "run_task requires `instructions`"}
        result = agent_loop.run_task(instructions)
        return {"ok": result["ok"], "output": result["output"], "steps": result.get("steps", [])}

    if name == "get_context":
        return {"ok": True, "output": context_store.get_context(arguments.get("topic"))}

    if name == "search_context":
        query = arguments.get("query", "")
        if not query:
            return {"ok": False, "output": "search_context requires `query`"}
        return {"ok": True, "output": context_store.search_context(query)}

    if find_tool(name) is not None:
        return execute_tool(name, arguments)

    return {"ok": False, "output": f"unknown tool `{name}` — see agent card `skills` for the allowlist"}


META_TOOL_NAMES = {"run_task", "get_context", "search_context"}


def is_known_tool_name(name: str) -> bool:
    return name in META_TOOL_NAMES or find_tool(name) is not None


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
        if is_known_tool_name(text):
            return {"name": text, "arguments": {}}, None
        return {"name": "run_task", "arguments": {"instructions": text}}, None

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
                "parts": [{"kind": "data", "data": result}],
            }
        ],
    }



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
        result = dispatch(call["name"], call.get("arguments", {}))
        context_id = message.get("contextId")
        task = build_task_response(context_id, result)
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": task})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        result = dispatch(name, arguments)
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})

    if method == "tools/list":
        skills = [
            {"id": t["id"], "name": t["name"], "description": t["description"], "tags": t["tags"]}
            for t in available_tools()
        ]
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"tools": skills}})

    if method == "mesh_list_peers":
        result = mesh.mesh_list_peers()
        if not result["ok"]:
            return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": result["output"]}})
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"peers": result["output"]}})

    if method == "mesh_call":
        result = mesh.mesh_call(
            params.get("host_label"), params.get("role"), params.get("tool_name"), params.get("arguments", {})
        )
        if not result["ok"]:
            return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": result["output"]}})
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": result["output"]})

    if method == "ping":
        return jsonify({"jsonrpc": "2.0", "id": req_id, "result": "Pong!"})

    return jsonify({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method not found: {method}"}})


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


def autonomous_triage_loop():
    if not ENABLE_AUTONOMOUS_TRIAGE:
        print("ENABLE_AUTONOMOUS_TRIAGE not set — autonomous triage disabled", flush=True)
        return
    print(f"autonomous triage enabled, running every {TRIAGE_INTERVAL_SECONDS}s", flush=True)
    cycle = 0
    while True:
        time.sleep(TRIAGE_INTERVAL_SECONDS)
        cycle += 1
        if not available_tools():
            print("skipping triage run — Google OAuth not configured", flush=True)
            continue

        if cycle % GUIDANCE_REFRESH_EVERY_N_CYCLES == 1:
            guidance_result = refresh_triage_guidance_from_hermes()
            if guidance_result["ok"]:
                print(f"refreshed triage guidance from Hermes: {guidance_result['output']}", flush=True)
            else:
                print(f"could not refresh guidance from Hermes (continuing with existing rules): {guidance_result['output']}", flush=True)

        try:
            instructions = current_triage_instructions()
            result = agent_loop.run_task(instructions)
            context_store.record_observation(
                "gmail", None, f"autonomous triage run: {result.get('output', '')[:500]}"
            )
            print(f"autonomous triage run completed: ok={result['ok']}", flush=True)
        except Exception as e:
            print(f"autonomous triage run failed: {e}", flush=True)


if __name__ == "__main__":
    threading.Thread(target=registry_heartbeat_loop, daemon=True).start()
    threading.Thread(target=autonomous_triage_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=BIND_PORT)
