"""
Gmail action-layer tools — thin wrappers, no inference. Uses
gmail.modify scope (read/label/archive), NOT full mail.google.com
(permanent delete stays with the separate email-triage service —
see PLAN.md section 1 for why the scope split is deliberate).
"""

from auth.google_auth import gmail_client


def gmail_search(query: str, max_results: int = 10) -> dict:
    """Search Gmail with a standard Gmail search query string
    (e.g. 'from:someone@example.com is:unread'). Returns message
    ids + snippets, not full bodies — keep it cheap by default."""
    try:
        service = gmail_client()
        results = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=min(max_results, 50))
            .execute()
        )
        messages = results.get("messages", [])
        out = []
        for m in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=m["id"], format="metadata",
                     metadataHeaders=["From", "Subject", "Date"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            out.append({
                "id": m["id"],
                "threadId": msg.get("threadId"),
                "from": headers.get("From"),
                "subject": headers.get("Subject"),
                "date": headers.get("Date"),
                "snippet": msg.get("snippet"),
            })
        return {"ok": True, "output": out}
    except Exception as e:
        return {"ok": False, "output": f"gmail_search failed: {e}"}


def gmail_send(to: str, subject: str, body: str) -> dict:
    """Send a plain-text email."""
    import base64
    from email.mime.text import MIMEText

    try:
        service = gmail_client()
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        sent = service.users().messages().send(userId="me", body={"raw": raw}).execute()
        return {"ok": True, "output": {"id": sent.get("id"), "threadId": sent.get("threadId")}}
    except Exception as e:
        return {"ok": False, "output": f"gmail_send failed: {e}"}


def gmail_archive(message_id: str) -> dict:
    """Archive a message by removing it from INBOX (does not delete)."""
    try:
        service = gmail_client()
        service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}
        ).execute()
        return {"ok": True, "output": f"archived {message_id}"}
    except Exception as e:
        return {"ok": False, "output": f"gmail_archive failed: {e}"}


_label_cache = {"data": None}


def _fetch_labels(service) -> dict:
    result = service.users().labels().list(userId="me").execute()
    return {l["name"].lower(): l["id"] for l in result.get("labels", [])}


def _resolve_label_ids(service, names: list) -> tuple:
    """Resolve human label names (e.g. 'Important', 'Work') to Gmail's
    internal label IDs. System labels (INBOX, STARRED, TRASH, UNREAD,
    etc.) are matched case-insensitively against their own name; custom
    labels are matched by exact display name. Returns (ids, unresolved).

    Caches the label list so repeated calls don't refetch on every
    request, but transparently refreshes once if a name isn't found —
    so a label created after the container started is still picked up,
    without needing a restart."""
    if _label_cache["data"] is None:
        _label_cache["data"] = _fetch_labels(service)

    by_name = _label_cache["data"]
    ids, unresolved = [], []
    for name in names:
        label_id = by_name.get(name.lower())
        if label_id:
            ids.append(label_id)
        else:
            unresolved.append(name)

    if unresolved:
        # Refresh once — the label(s) may have been created recently.
        _label_cache["data"] = _fetch_labels(service)
        by_name = _label_cache["data"]
        still_unresolved = []
        for name in unresolved:
            label_id = by_name.get(name.lower())
            if label_id:
                ids.append(label_id)
            else:
                still_unresolved.append(name)
        unresolved = still_unresolved

    return ids, unresolved


def gmail_label(message_id: str, add: list = None, remove: list = None) -> dict:
    """Add and/or remove labels on a message by human-readable label
    name (e.g. 'Important', 'Work', or system labels like 'UNREAD').
    Arguments: message_id (string, required), add (array of label
    names, optional), remove (array of label names, optional)."""
    add = add or []
    remove = remove or []
    if not add and not remove:
        return {"ok": False, "output": "must provide at least one label in `add` or `remove`"}
    try:
        service = gmail_client()
        add_ids, add_unresolved = _resolve_label_ids(service, add)
        remove_ids, remove_unresolved = _resolve_label_ids(service, remove)
        unresolved = add_unresolved + remove_unresolved
        if unresolved:
            return {"ok": False, "output": f"unknown label name(s): {', '.join(unresolved)}"}
        body = {}
        if add_ids:
            body["addLabelIds"] = add_ids
        if remove_ids:
            body["removeLabelIds"] = remove_ids
        service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        return {"ok": True, "output": f"updated labels on {message_id}: +{add} -{remove}"}
    except Exception as e:
        return {"ok": False, "output": f"gmail_label failed: {e}"}


def gmail_star(message_id: str, starred: bool = True) -> dict:
    """Star or unstar a message. Arguments: message_id (string,
    required), starred (boolean, optional, default true — pass false
    to unstar)."""
    try:
        service = gmail_client()
        body = {"addLabelIds": ["STARRED"]} if starred else {"removeLabelIds": ["STARRED"]}
        service.users().messages().modify(userId="me", id=message_id, body=body).execute()
        return {"ok": True, "output": f"{'starred' if starred else 'unstarred'} {message_id}"}
    except Exception as e:
        return {"ok": False, "output": f"gmail_star failed: {e}"}


def gmail_trash(message_id: str) -> dict:
    """Move a message to Trash (reversible for ~30 days, then Gmail
    auto-empties it — this is NOT permanent delete, which stays out of
    scope for this agent by design). Arguments: message_id (string, required)."""
    try:
        service = gmail_client()
        service.users().messages().trash(userId="me", id=message_id).execute()
        return {"ok": True, "output": f"moved {message_id} to trash"}
    except Exception as e:
        return {"ok": False, "output": f"gmail_trash failed: {e}"}


def gmail_untrash(message_id: str) -> dict:
    """Restore a message out of Trash. Arguments: message_id (string, required)."""
    try:
        service = gmail_client()
        service.users().messages().untrash(userId="me", id=message_id).execute()
        return {"ok": True, "output": f"restored {message_id} from trash"}
    except Exception as e:
        return {"ok": False, "output": f"gmail_untrash failed: {e}"}
