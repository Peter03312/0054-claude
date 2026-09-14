"""SQLite persistence for edit documents and analysis snapshots."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

_SCHEMA = """
CREATE TABLE IF NOT EXISTS edits (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    doc        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    id         TEXT PRIMARY KEY,
    edit_id    TEXT NOT NULL REFERENCES edits (id),
    created_at TEXT NOT NULL,
    request    TEXT NOT NULL,
    report     TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._lock = threading.Lock()
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    # -- edits -------------------------------------------------------------
    def create_edit(self, edit_id: str, doc: dict) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO edits (id, created_at, doc) VALUES (?, ?, ?)",
                (edit_id, _now(), json.dumps(doc)),
            )

    def get_edit(self, edit_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM edits WHERE id = ?", (edit_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "edit_id": row["id"],
            "created_at": row["created_at"],
            "edit": json.loads(row["doc"]),
        }

    def list_edits(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, doc FROM edits ORDER BY created_at, id"
            ).fetchall()
        out = []
        for row in rows:
            doc = json.loads(row["doc"])
            out.append(
                {
                    "edit_id": row["id"],
                    "created_at": row["created_at"],
                    "title": doc.get("title"),
                    "node_count": len(doc.get("nodes", [])),
                    "consent_count": len(doc.get("consents", [])),
                }
            )
        return out

    # -- analysis snapshots --------------------------------------------------
    def create_analysis(
        self, analysis_id: str, edit_id: str, request: dict, report: dict
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO analyses (id, edit_id, created_at, request, report)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    analysis_id,
                    edit_id,
                    _now(),
                    json.dumps(request),
                    json.dumps(report),
                ),
            )

    def get_analysis(self, edit_id: str, analysis_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM analyses WHERE id = ? AND edit_id = ?",
                (analysis_id, edit_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "analysis_id": row["id"],
            "edit_id": row["edit_id"],
            "created_at": row["created_at"],
            "request": json.loads(row["request"]),
            "report": json.loads(row["report"]),
        }

    def list_analyses(self, edit_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at, request, report FROM analyses"
                " WHERE edit_id = ? ORDER BY created_at, id",
                (edit_id,),
            ).fetchall()
        out = []
        for row in rows:
            request = json.loads(row["request"])
            report = json.loads(row["report"])
            out.append(
                {
                    "analysis_id": row["id"],
                    "created_at": row["created_at"],
                    "output": request.get("output"),
                    "audience": request.get("audience"),
                    "decision": report.get("decision"),
                }
            )
        return out
