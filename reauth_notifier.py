"""
reauth_notifier.py

Wraps Google API calls so an invalid_grant (dead refresh token) triggers an
ntfy push with a tappable link to /oauth/authorize, instead of just failing
silently or logging an error nobody sees until asked.
"""
import os
import time
import logging
import requests
from google.auth.exceptions import RefreshError

log = logging.getLogger("reauth_notifier")

NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")  # override for self-hosted ntfy
NTFY_TOPIC = os.environ["NTFY_TOPIC"]  # e.g. the same topic google-services-agent already uses
OAUTH_PUBLIC_BASE_URL = os.environ["OAUTH_PUBLIC_BASE_URL"]

# Debounce: don't spam a notification on every single failed call while
# waiting for Gabriel to tap the link — one push per cooldown window.
_COOLDOWN_SECONDS = 30 * 60
_last_notified_at = 0.0


def notify_reauth_needed(detail: str = "") -> None:
    global _last_notified_at
    now = time.time()
    if now - _last_notified_at < _COOLDOWN_SECONDS:
        log.info("Reauth notification suppressed (cooldown active)")
        return
    _last_notified_at = now

    link = f"{OAUTH_PUBLIC_BASE_URL}/oauth/authorize"
    try:
        requests.post(
            f"{NTFY_URL}/{NTFY_TOPIC}",
            data=f"Google token expired/revoked. Tap to reconnect: {link}\n{detail}".encode(),
            headers={
                "Title": "google-services-agent needs reauth",
                "Priority": "high",
                "Tags": "warning,key",
                "Click": link,
            },
            timeout=10,
        )
        log.info("Sent reauth-needed ntfy notification")
    except Exception:
        log.exception("Failed to send reauth ntfy notification")


def call_with_reauth_guard(fn, *args, **kwargs):
    """
    Wrap any function that hits the Google API (or refreshes creds) so a
    dead refresh token triggers a notification instead of just bubbling up.
    Usage: call_with_reauth_guard(gmail_search, query="in:inbox")
    """
    try:
        return fn(*args, **kwargs)
    except RefreshError as e:
        # google-auth raises RefreshError wrapping the underlying
        # invalid_grant response on a dead/revoked refresh token.
        if "invalid_grant" in str(e):
            log.error("invalid_grant on Google API call: %s", e)
            notify_reauth_needed(detail=str(e))
        raise
