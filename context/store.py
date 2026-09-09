"""
Context store — the "learn about Gabriel over time" layer from PLAN.md
section 5. Separates distilled, queryable facts (entity_facts,
preferences) from an append-only raw feed (raw_observations), same
split as email-triage's learnings.md / corrections.log pattern.

This is written to by the agent loop as it works (e.g. noticing a
recurring correspondent, a stated preference) and read by
get_context/search_context so Hermes can ask "what do you know about
X" without re-deriving it from raw Gmail/Calendar data every time.
"""

import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.environ.get("CONTEXT_DB_PATH", "/data/context.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,           -- person/org/project/place
    name TEXT NOT NULL UNIQUE,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    fact TEXT NOT NULL,
    source TEXT NOT NULL,          -- gmail/calendar/manual
    confidence REAL NOT NULL DEFAULT 0.5,
    first_observed REAL NOT NULL,
    last_confirmed REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    last_confirmed REAL NOT NULL,
    UNIQUE(domain, key)
);

CREATE TABLE IF NOT EXISTS raw_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,          -- gmail/calendar
    ref_id TEXT,                   -- e.g. gmail message id
    observed_at REAL NOT NULL,
    summary TEXT NOT NULL
);
"""


@contextmanager
def _db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_observation(source: str, summary: str, ref_id: str = None):
    """Append-only — every raw_observations write is a new row, never
    an update. This is the slow, re-derivable layer; entity_facts /
    preferences are the fast, distilled layer built from these."""
    with _db() as conn:
        conn.execute(
            "INSERT INTO raw_observations (source, ref_id, observed_at, summary) VALUES (?, ?, ?, ?)",
            (source, ref_id, time.time(), summary),
        )


def _get_or_create_entity(conn, name: str, entity_type: str) -> int:
    row = conn.execute("SELECT id FROM entities WHERE name = ?", (name,)).fetchone()
    now = time.time()
    if row:
        conn.execute("UPDATE entities SET last_seen = ? WHERE id = ?", (now, row["id"]))
        return row["id"]
    cur = conn.execute(
        "INSERT INTO entities (type, name, first_seen, last_seen) VALUES (?, ?, ?, ?)",
        (entity_type, name, now, now),
    )
    return cur.lastrowid


def record_fact(entity_name: str, entity_type: str, fact: str, source: str, confidence: float = 0.5):
    """Write or reinforce a fact about an entity. If the same fact
    text already exists for this entity, bump last_confirmed and
    confidence rather than duplicating the row — repeated observation
    of the same thing should increase confidence, not spam the table."""
    with _db() as conn:
        entity_id = _get_or_create_entity(conn, entity_name, entity_type)
        now = time.time()
        existing = conn.execute(
            "SELECT id, confidence FROM entity_facts WHERE entity_id = ? AND fact = ?",
            (entity_id, fact),
        ).fetchone()
        if existing:
            new_confidence = min(1.0, existing["confidence"] + 0.1)
            conn.execute(
                "UPDATE entity_facts SET confidence = ?, last_confirmed = ? WHERE id = ?",
                (new_confidence, now, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO entity_facts (entity_id, fact, source, confidence, first_observed, last_confirmed) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (entity_id, fact, source, confidence, now, now),
            )


def set_preference(domain: str, key: str, value: str, source: str):
    with _db() as conn:
        conn.execute(
            "INSERT INTO preferences (domain, key, value, source, last_confirmed) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(domain, key) DO UPDATE SET value=excluded.value, source=excluded.source, last_confirmed=excluded.last_confirmed",
            (domain, key, value, source, time.time()),
        )


def get_context(topic: str = None) -> dict:
    """Return distilled context — all of it, or filtered to entities/
    preferences whose name/domain/key mentions `topic`."""
    with _db() as conn:
        if topic:
            entities = conn.execute(
                "SELECT * FROM entities WHERE name LIKE ?", (f"%{topic}%",)
            ).fetchall()
            prefs = conn.execute(
                "SELECT * FROM preferences WHERE domain LIKE ? OR key LIKE ?",
                (f"%{topic}%", f"%{topic}%"),
            ).fetchall()
        else:
            entities = conn.execute("SELECT * FROM entities").fetchall()
            prefs = conn.execute("SELECT * FROM preferences").fetchall()

        result_entities = []
        for e in entities:
            facts = conn.execute(
                "SELECT fact, source, confidence, last_confirmed FROM entity_facts WHERE entity_id = ? ORDER BY confidence DESC",
                (e["id"],),
            ).fetchall()
            result_entities.append({
                "name": e["name"],
                "type": e["type"],
                "facts": [dict(f) for f in facts],
            })

        return {
            "entities": result_entities,
            "preferences": [dict(p) for p in prefs],
        }


def search_context(query: str) -> dict:
    """Narrower lookup than get_context — searches fact text too, not
    just entity/preference names, so it can answer things like 'what
    do I know about the Fermoy Lane house' even if that phrase isn't
    an entity name itself."""
    with _db() as conn:
        facts = conn.execute(
            """
            SELECT e.name as entity_name, e.type as entity_type, f.fact, f.source, f.confidence
            FROM entity_facts f JOIN entities e ON f.entity_id = e.id
            WHERE f.fact LIKE ? OR e.name LIKE ?
            ORDER BY f.confidence DESC
            LIMIT 20
            """,
            (f"%{query}%", f"%{query}%"),
        ).fetchall()
        prefs = conn.execute(
            "SELECT * FROM preferences WHERE domain LIKE ? OR key LIKE ? OR value LIKE ?",
            (f"%{query}%", f"%{query}%", f"%{query}%"),
        ).fetchall()
        return {
            "matching_facts": [dict(f) for f in facts],
            "matching_preferences": [dict(p) for p in prefs],
        }


def prune_stale_facts(max_age_days: int = 180, min_confidence: float = 0.3):
    """Weekly validation pass per PLAN.md: drop low-confidence facts
    that haven't been reinforced in a long time. Returns how many rows
    were removed, for reporting."""
    cutoff = time.time() - (max_age_days * 86400)
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM entity_facts WHERE last_confirmed < ? AND confidence < ?",
            (cutoff, min_confidence),
        )
        return cur.rowcount
