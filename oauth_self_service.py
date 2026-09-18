"""
oauth_self_service.py

Drop-in Flask blueprint giving google-services-agent the ability to recover
its own Google OAuth token without a human running Python/scp by hand.

Flow:
  1. Something (a scheduled refresh check, or a failed API call) detects
     invalid_grant and calls notify_reauth_needed(), which pushes an ntfy
     link to /oauth/authorize.
  2. Gabriel taps the link on his phone/laptop (must be reachable — see
     PUBLIC_BASE_URL note below).
  3. /oauth/authorize redirects to Google's consent screen.
  4. Google redirects back to /oauth/callback with an auth code.
  5. /oauth/callback exchanges the code for tokens and writes them straight
     to GOOGLE_TOKEN_PATH — no scp, no local machine involved.

Uses google_auth_oauthlib.flow.Flow (not InstalledAppFlow) because this is
a fixed-redirect server flow, not a loopback/local-server flow.
"""
import os
import json
import secrets
import logging

from flask import Blueprint, redirect, request, abort
from google_auth_oauthlib.flow import Flow

log = logging.getLogger("oauth_self_service")

oauth_bp = Blueprint("oauth_self_service", __name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]

CLIENT_SECRET_PATH = os.environ.get(
    "GOOGLE_CLIENT_SECRET_PATH", "/data/client_secret.json"
)
GOOGLE_TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "/data/google-token.json")

# Must exactly match an "Authorized redirect URI" on the OAuth client in
# Cloud Console. Reachable over the WireGuard mesh is enough if you're
# always consenting from a device already on akai-net; otherwise route it
# through whatever Caddy/public hostname fronts this agent.
PUBLIC_BASE_URL = os.environ["OAUTH_PUBLIC_BASE_URL"]  # e.g. http://10.200.200.5:8200
REDIRECT_URI = f"{PUBLIC_BASE_URL}/oauth/callback"

# In-memory CSRF state store, now also holding the PKCE code_verifier
# generated for each authorize request — Google now requires PKCE, and
# since authorize/callback build separate Flow objects, the verifier has
# to be threaded through manually rather than relying on Flow to remember
# it. Fine for single-operator use; if this needs to survive a restart
# mid-flow, swap for a one-row sqlite/file store instead.
_pending_states: dict[str, str] = {}  # state -> code_verifier


def _build_flow(code_verifier: str | None = None) -> Flow:
    flow = Flow.from_client_secrets_file(
        CLIENT_SECRET_PATH,
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=(code_verifier is None),
    )
    if code_verifier is not None:
        flow.code_verifier = code_verifier
    return flow


@oauth_bp.route("/oauth/authorize", methods=["GET"])
def authorize():
    flow = _build_flow()
    state = secrets.token_urlsafe(24)
    _pending_states[state] = flow.code_verifier

    auth_url, _ = flow.authorization_url(
        access_type="offline",       # required to get a refresh token
        prompt="consent",            # force re-consent so a refresh token is
                                      # actually issued even on repeat grants
        include_granted_scopes="true",
        state=state,
    )
    log.info("OAuth authorize requested, redirecting to Google consent screen")
    return redirect(auth_url)


@oauth_bp.route("/oauth/callback", methods=["GET"])
def callback():
    state = request.args.get("state")
    if not state or state not in _pending_states:
        log.warning("OAuth callback with unknown/missing state — rejecting")
        abort(400, "invalid or expired state")
    code_verifier = _pending_states.pop(state)

    error = request.args.get("error")
    if error:
        log.error("OAuth consent denied or errored: %s", error)
        return f"Consent failed: {error}", 400

    flow = _build_flow(code_verifier=code_verifier)
    try:
        # authorization_response must be the full callback URL Google hit,
        # including query string — request.url gives that.
        flow.fetch_token(authorization_response=request.url)
    except Exception:
        log.exception("Token exchange failed")
        return "Token exchange failed — check agent logs", 500

    creds = flow.credentials
    os.makedirs(os.path.dirname(GOOGLE_TOKEN_PATH), exist_ok=True)
    with open(GOOGLE_TOKEN_PATH, "w") as f:
        f.write(creds.to_json())

    log.info("New Google OAuth token written to %s", GOOGLE_TOKEN_PATH)
    return (
        "<html><body style='font-family: sans-serif; padding: 2rem;'>"
        "<h2>Reconnected</h2>"
        "<p>google-services-agent has a fresh token. You can close this tab.</p>"
        "</body></html>"
    )
