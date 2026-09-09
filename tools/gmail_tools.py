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
