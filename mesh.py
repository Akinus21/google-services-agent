"""
Shared mesh-calling logic — extracted so both the A2A server (when
Hermes calls mesh_call directly) and the agent loop (when the LLM
autonomously decides it needs another peer's help mid-task) use the
exact same code path.
"""

import os
import requests

REGISTRY_URL = os.environ.get("REGISTRY_URL")
MESH_TOKEN = os.environ.get("MESH_AUTH_TOKEN")


def peer_rpc_base(agent_card_url: str) -> str:
    return agent_card_url.rsplit("/.well-known/agent-card.json", 1)[0]


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
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def mesh_call(host_label=None, role=None, tool_name=None, arguments=None):
    arguments = arguments or {}
    if not MESH_TOKEN:
        return {"ok": False, "output": "MESH_AUTH_TOKEN is not configured on this agent — cannot make outbound mesh calls"}
    if not tool_name:
        return {"ok": False, "output": "mesh_call requires tool_name"}

    peers, err = fetch_registry_peers()
    if err:
        return {"ok": False, "output": err}

    matches = [
        p for p in peers
        if (host_label is None or p.get("host_label") == host_label)
        and (role is None or p.get("role") == role)
    ]
    if len(matches) == 0:
        return {"ok": False, "output": "no registered peer matches the given host_label/role"}
    if len(matches) > 1:
        return {"ok": False, "output": f"{len(matches)} peers match — specify host_label to disambiguate"}

    rpc_base = peer_rpc_base(matches[0]["agent_card_url"])
    try:
        raw = call_peer(rpc_base, MESH_TOKEN, tool_name, arguments)
        # Unwrap the peer's Task response down to the actual result,
        # so the calling LLM sees a plain {ok, output} like every other
        # tool result, not a nested A2A envelope it has to parse itself.
        result = raw.get("result", {})
        artifacts = result.get("artifacts", [])
        if artifacts:
            data = artifacts[0].get("parts", [{}])[0].get("data", {})
            return data if isinstance(data, dict) and "ok" in data else {"ok": True, "output": data}
        return {"ok": True, "output": raw}
    except Exception as e:
        return {"ok": False, "output": f"peer call failed: {e}"}


def mesh_list_peers():
    peers, err = fetch_registry_peers()
    if err:
        return {"ok": False, "output": err}
    return {"ok": True, "output": peers}


# ---------------------------------------------------------------------
# Asking Hermes directly — separate from mesh_call/the mesh-registry,
# because Hermes is not one of our self-registering agents; it's its
# own product with its own inbound A2A endpoint (the a2a-platform
# plugin), reached at a fixed configured URL instead of registry
# lookup. Used so this agent can ask Hermes things like "what triage
# guidance have I been given" and adjust its own behavior — closing
# the loop between corrections you give Hermes conversationally and
# what this agent actually does autonomously.
# ---------------------------------------------------------------------

HERMES_AGENT_URL = os.environ.get("HERMES_AGENT_URL")
HERMES_AGENT_TOKEN = os.environ.get("HERMES_AGENT_TOKEN")


def ask_hermes(question: str) -> dict:
    if not HERMES_AGENT_URL or not HERMES_AGENT_TOKEN:
        return {
            "ok": False,
            "output": "HERMES_AGENT_URL/HERMES_AGENT_TOKEN are not configured on this agent — cannot ask Hermes anything",
        }
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": question}],
            }
        },
    }
    try:
        resp = requests.post(
            HERMES_AGENT_URL,
            json=payload,
            headers={"Authorization": f"Bearer {HERMES_AGENT_TOKEN}", "Content-Type": "application/json"},
            timeout=60,
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as e:
        return {"ok": False, "output": f"failed to reach Hermes: {e}"}

    result = raw.get("result", {})
    # Hermes' own A2A response shape may differ from our internal
    # agents' — try the same artifact-unwrapping first, then fall back
    # to whatever text-ish field is present rather than failing outright.
    artifacts = result.get("artifacts", [])
    if artifacts:
        parts = artifacts[0].get("parts", [])
        for p in parts:
            if p.get("kind") == "text" and p.get("text"):
                return {"ok": True, "output": p["text"]}
            if p.get("kind") == "data" and p.get("data"):
                return {"ok": True, "output": p["data"]}
    if isinstance(result, str):
        return {"ok": True, "output": result}
    return {"ok": True, "output": raw}
