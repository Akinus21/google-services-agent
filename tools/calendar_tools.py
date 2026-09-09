"""
Calendar action-layer tools — thin wrappers, no inference.
"""

from datetime import datetime, timedelta, timezone
from auth.google_auth import calendar_client


def calendar_list_events(days_ahead: int = 7, max_results: int = 20) -> dict:
    """List upcoming events on the primary calendar over the next
    N days (default 7, capped at 60)."""
    try:
        service = calendar_client()
        now = datetime.now(timezone.utc)
        time_max = now + timedelta(days=min(days_ahead, 60))
        events_result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=time_max.isoformat(),
                maxResults=min(max_results, 100),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        events = events_result.get("items", [])
        out = [
            {
                "id": e.get("id"),
                "summary": e.get("summary"),
                "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date")),
                "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date")),
                "location": e.get("location"),
            }
            for e in events
        ]
        return {"ok": True, "output": out}
    except Exception as e:
        return {"ok": False, "output": f"calendar_list_events failed: {e}"}


def calendar_create_event(summary: str, start: str, end: str, description: str = "") -> dict:
    """Create an event on the primary calendar. start/end must be
    RFC3339 timestamps (e.g. '2026-09-10T14:00:00-05:00')."""
    try:
        service = calendar_client()
        event = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        created = service.events().insert(calendarId="primary", body=event).execute()
        return {"ok": True, "output": {"id": created.get("id"), "htmlLink": created.get("htmlLink")}}
    except Exception as e:
        return {"ok": False, "output": f"calendar_create_event failed: {e}"}
