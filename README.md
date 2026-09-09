# google-services-agent

Personal assistant A2A service — holds Google OAuth access (Gmail,
Calendar; Drive/Docs/Spotify/Vikunja are later phases per PLAN.md) and
acts on your behalf. Peer to `docker-ops-agent` in the mesh, same
conventions: real A2A `message/send`, bearer + shared mesh token auth,
self-registration against `mesh-registry`, `mesh_call` for direct
agent-to-agent calls.

Hermes never sees your Google token directly — only this agent's A2A
interface.

## One-time setup (you have to do this part yourself — it needs your browser)

### 1. Google Cloud Console

1. https://console.cloud.google.com → New Project (or reuse one) → name it e.g. `google-services-agent`.
2. **APIs & Services → Library** → enable:
   - Gmail API
   - Google Calendar API
3. **APIs & Services → OAuth consent screen**:
   - User type: External (fine even for personal use)
   - Publishing status: leave in **Testing** — no Google review needed
   - Add your own Google account under **Test users**
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID**:
   - Application type: **Desktop app**
   - Name it whatever you like
5. Download the resulting JSON — save it as `client_secret.json`.

### 2. Get the refresh token (run this once, on a machine with a browser)

```bash
pip install google-auth-oauthlib google-api-python-client
python3 -c "
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    'https://www.googleapis.com/auth/gmail.modify',
    'https://www.googleapis.com/auth/calendar',
]
flow = InstalledAppFlow.from_client_secrets_file('client_secret.json', SCOPES)
creds = flow.run_local_server(port=0)
open('google-token.json', 'w').write(creds.to_json())
"
```

This opens a browser, you log in and approve, and it writes
`google-token.json` — the only file google-services-agent actually needs going
forward. `client_secret.json` isn't needed again unless you regenerate
the token or add new scopes later.

### 3. Move the token onto the host

```bash
scp google-token.json <host>:/path/to/google-services-agent-data/google-token.json
```

That directory gets mounted into the container at `/data` (see compose
block below) — `GOOGLE_TOKEN_PATH` defaults to `/data/google-token.json`.

**That's the entire manual part.** Once `google-token.json` exists,
google-services-agent refreshes it automatically forever (silent refresh, no
further browser interaction) unless you add new scopes later, at which
point you re-run step 2 with the expanded `SCOPES` list.

## Tools (Phase 1 — Gmail + Calendar)

| Tool | Arguments | Does |
|---|---|---|
| `gmail_search` | `query` (string), `max_results` (int, ≤50) | Standard Gmail search query, returns metadata + snippets, not full bodies |
| `gmail_send` | `to`, `subject`, `body` (strings) | Send a plain-text email |
| `gmail_archive` | `message_id` (string) | Remove from inbox (not delete) |
| `calendar_list_events` | `days_ahead` (int, ≤60), `max_results` (int, ≤100) | Upcoming events on primary calendar |
| `calendar_create_event` | `summary`, `start`, `end` (RFC3339), `description` (optional) | Create an event |

Tools only appear in the agent card / `tools/list` once `google-token.json`
actually exists — same "only advertise what's usable" pattern as
`docker-ops-agent`'s binary detection.

Scope is deliberately narrow: `gmail.modify` (not full `mail.google.com`)
— permanent delete is intentionally left to a separate email-triage
service, so a bug here can't nuke your mailbox. See PLAN.md for the full
reasoning and what's planned next (Drive/Docs read access, Spotify,
Vikunja, and the context-inference layer).

## Compose service

```yaml
  google-services-agent:
    image: ghcr.io/akinus21/google-services-agent:latest
    container_name: google-services-agent
    restart: unless-stopped
    volumes:
      - ./google-services-agent-data:/data
    environment:
      - A2A_STATIC_AUTH_TOKEN=${GOOGLE_SERVICES_AGENT_TOKEN}
      - MESH_AUTH_TOKEN=${MESH_PEER_TOKEN}
      - A2A_HOST_LABEL=<this-host-name>
      - A2A_SELF_CARD_URL=http://10.200.200.X:8200/.well-known/agent-card.json
      - REGISTRY_URL=http://10.200.200.5:9000
      - REGISTRY_TOKEN=${MESH_REGISTRY_TOKEN}
    ports:
      - "10.200.200.X:8200:8200"
    networks:
      - ai-net
```

Generate `GOOGLE_SERVICES_AGENT_TOKEN` fresh (unique to this agent, same
per-host-token principle as `docker-ops-agent`). `MESH_PEER_TOKEN` and
`MESH_REGISTRY_TOKEN` are the same shared values already used by every
`docker-ops-agent` instance in the mesh — google-services-agent joins the same
mesh, it doesn't need its own separate registry.

## What's NOT built yet (see PLAN.md)

- Drive/Docs read access (Phase 2 scopes)
- Spotify integration
- Vikunja integration (wrap vs. absorb `vikunja-mcp` — still an open decision)
- The context-inference layer (`entity_facts`/`preferences`/`raw_observations`
  store, daily refresh + weekly validation passes) — this is genuinely the
  hard, interesting part of the original ask and comes after the action-layer
  skeleton here is proven live, per the build order in PLAN.md
