"""
Google OAuth token loading and refresh for google-services-agent.

Reads a token.json produced by the ONE-TIME interactive consent flow
(see README.md for the exact command — this must be run manually by
Gabriel, in a browser; it cannot be performed by an agent). Once
token.json exists, this module handles silent refresh from then on —
no further interaction needed unless scopes change.
"""

import os
import json
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

TOKEN_PATH = os.environ.get("GOOGLE_TOKEN_PATH", "/data/google-token.json")

# Phase 1 scopes per PLAN.md — gmail.modify (not full mail.google.com,
# that stays with email-triage) + calendar read/write.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
]


class GoogleAuthNotConfigured(Exception):
    """Raised when token.json is missing or invalid — the one-time
    OAuth consent flow hasn't been run yet."""
    pass


def _load_credentials() -> Credentials:
    if not os.path.exists(TOKEN_PATH):
        raise GoogleAuthNotConfigured(
            f"No token found at {TOKEN_PATH}. Run the one-time OAuth "
            "consent flow described in README.md before using any "
            "Google-backed tool."
        )
    with open(TOKEN_PATH, "r") as f:
        data = json.load(f)
    creds = Credentials.from_authorized_user_info(data, SCOPES)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        # Persist the refreshed access token so we don't re-refresh
        # unnecessarily on every call.
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return creds


def gmail_client():
    """Returns an authenticated Gmail API client (v1)."""
    creds = _load_credentials()
    return build("gmail", "v1", credentials=creds)


def calendar_client():
    """Returns an authenticated Calendar API client (v3)."""
    creds = _load_credentials()
    return build("calendar", "v3", credentials=creds)


def is_configured() -> bool:
    """Cheap check for whether OAuth has been set up at all, without
    triggering a refresh — used by the agent card to decide whether to
    advertise Google-backed tools as available on this host."""
    return os.path.exists(TOKEN_PATH)
