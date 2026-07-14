from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from miniagent.event_log import EventLog, StorageEvent
from miniagent.messages import Message


SESSION_STARTED = "session_started"
MESSAGE_APPENDED = "message_appended"
TOOL_CALL_APPENDED = "tool_call_appended"
TOOL_RESULT_APPENDED = "tool_result_appended"
PERMISSION_DECISION_APPENDED = "permission_decision_appended"
STATE_SNAPSHOT = "state_snapshot"
SESSION_SAVED_EVENT = "session_saved"


class SessionRecord(BaseModel):
    id: str
    cwd: str
    messages: list[Message] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    permission_decisions: list[dict[str, Any]] = Field(default_factory=list)
    todos: list[dict[str, Any]] = Field(default_factory=list)
    file_reads: dict[str, dict[str, Any]] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class SessionSummary(BaseModel):
    id: str
    cwd: str
    created_at: float
    updated_at: float
    message_count: int = 0
    tool_call_count: int = 0
    tool_result_count: int = 0
    permission_count: int = 0
    json_path: str
    event_path: str


class SessionStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.events_dir = self.root / "events"
        self.latest_path = self.root / "latest"
        self.index_path = self.root / "sessions.db"
        self.event_log = EventLog(self.events_dir)

    def save(self, record: SessionRecord) -> Path:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        record.updated_at = time.time()
        path = self.session_path(record.id)
        path.write_text(
            json.dumps(record.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.latest_path.parent.mkdir(parents=True, exist_ok=True)
        self.latest_path.write_text(record.id, encoding="utf-8")
        self._append_events(record, path)
        self._upsert_index(record, path)
        return path

    def load(self, session_id: str) -> SessionRecord:
        path = self.session_path(session_id)
        return SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def load_latest(self) -> SessionRecord | None:
        if not self.latest_path.exists():
            return None
        session_id = self.latest_path.read_text(encoding="utf-8").strip()
        if not session_id:
            return None
        return self.load(session_id)

    def session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.json"

    def event_path(self, session_id: str) -> Path:
        return self.event_log.path_for(session_id)

    def read_events(self, session_id: str) -> list[StorageEvent]:
        return self.event_log.read(session_id)

    def load_from_events(self, session_id: str) -> SessionRecord:
        events = self.read_events(session_id)
        if not events:
            return self.load(session_id)

        cwd = ""
        messages: list[Message] = []
        tool_calls: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        permission_decisions: list[dict[str, Any]] = []
        todos: list[dict[str, Any]] = []
        file_reads: dict[str, dict[str, Any]] = {}
        state: dict[str, Any] = {}
        created_at = events[0].created_at
        updated_at = events[-1].created_at

        for event in events:
            updated_at = event.created_at
            if event.type == SESSION_STARTED:
                cwd = str(event.data.get("cwd", cwd))
                created_at = float(event.data.get("created_at", created_at))
            elif event.type == MESSAGE_APPENDED:
                messages.append(Message.model_validate(event.data["message"]))
            elif event.type == TOOL_CALL_APPENDED:
                tool_calls.append(dict(event.data["call"]))
            elif event.type == TOOL_RESULT_APPENDED:
                tool_results.append(dict(event.data["result"]))
            elif event.type == PERMISSION_DECISION_APPENDED:
                permission_decisions.append(dict(event.data["decision"]))
            elif event.type == STATE_SNAPSHOT:
                state = dict(event.data.get("state", {}))
                todos = list(event.data.get("todos", []))
                file_reads = dict(event.data.get("file_reads", {}))

        if not cwd:
            summary = self.get_session_summary(session_id)
            cwd = summary.cwd if summary else ""
        return SessionRecord(
            id=session_id,
            cwd=cwd,
            messages=messages,
            tool_calls=tool_calls,
            tool_results=tool_results,
            permission_decisions=permission_decisions,
            todos=todos,
            file_reads=file_reads,
            state=state,
            created_at=created_at,
            updated_at=updated_at,
        )

    def list_sessions(self) -> list[SessionSummary]:
        self._ensure_index()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, cwd, created_at, updated_at, message_count, tool_call_count,
                       tool_result_count, permission_count, json_path, event_path
                FROM sessions
                ORDER BY updated_at DESC
                """
            ).fetchall()
        return [self._summary_from_row(row) for row in rows]

    def get_session_summary(self, session_id: str) -> SessionSummary | None:
        self._ensure_index()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, cwd, created_at, updated_at, message_count, tool_call_count,
                       tool_result_count, permission_count, json_path, event_path
                FROM sessions
                WHERE id = ?
                """,
                (session_id,),
            ).fetchone()
        return self._summary_from_row(row) if row else None

    def export(self, session_id: str) -> dict[str, Any]:
        snapshot = self.load(session_id)
        events = [event.model_dump(mode="json") for event in self.read_events(session_id)]
        rebuilt = self.load_from_events(session_id)
        return {
            "snapshot": snapshot.model_dump(mode="json"),
            "events": events,
            "rebuilt": rebuilt.model_dump(mode="json"),
        }

    def _append_events(self, record: SessionRecord, path: Path) -> None:
        summary = self.get_session_summary(record.id)
        if summary is None:
            self.event_log.append(
                session_id=record.id,
                event_type=SESSION_STARTED,
                data={
                    "cwd": record.cwd,
                    "created_at": record.created_at,
                    "json_path": str(path),
                },
            )
            message_start = 0
            tool_call_start = 0
            tool_result_start = 0
            permission_start = 0
        else:
            message_start = summary.message_count
            tool_call_start = summary.tool_call_count
            tool_result_start = summary.tool_result_count
            permission_start = summary.permission_count

        for index, message in enumerate(record.messages[message_start:], start=message_start):
            self.event_log.append(
                session_id=record.id,
                event_type=MESSAGE_APPENDED,
                data={"index": index, "message": message.model_dump(mode="json")},
            )
        for index, call in enumerate(record.tool_calls[tool_call_start:], start=tool_call_start):
            self.event_log.append(
                session_id=record.id,
                event_type=TOOL_CALL_APPENDED,
                data={"index": index, "call": call},
            )
        for index, result in enumerate(record.tool_results[tool_result_start:], start=tool_result_start):
            self.event_log.append(
                session_id=record.id,
                event_type=TOOL_RESULT_APPENDED,
                data={"index": index, "result": result},
            )
        for index, decision in enumerate(
            record.permission_decisions[permission_start:],
            start=permission_start,
        ):
            self.event_log.append(
                session_id=record.id,
                event_type=PERMISSION_DECISION_APPENDED,
                data={"index": index, "decision": decision},
            )

        self.event_log.append(
            session_id=record.id,
            event_type=STATE_SNAPSHOT,
            data={
                "todos": record.todos,
                "file_reads": record.file_reads,
                "state": record.state,
            },
        )
        self.event_log.append(
            session_id=record.id,
            event_type=SESSION_SAVED_EVENT,
            data={"json_path": str(path), "updated_at": record.updated_at},
        )

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.index_path)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_index(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    cwd TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    message_count INTEGER NOT NULL,
                    tool_call_count INTEGER NOT NULL,
                    tool_result_count INTEGER NOT NULL,
                    permission_count INTEGER NOT NULL,
                    json_path TEXT NOT NULL,
                    event_path TEXT NOT NULL
                )
                """
            )
            connection.commit()

    def _upsert_index(self, record: SessionRecord, path: Path) -> None:
        self._ensure_index()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    id, cwd, created_at, updated_at, message_count, tool_call_count,
                    tool_result_count, permission_count, json_path, event_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    cwd = excluded.cwd,
                    updated_at = excluded.updated_at,
                    message_count = excluded.message_count,
                    tool_call_count = excluded.tool_call_count,
                    tool_result_count = excluded.tool_result_count,
                    permission_count = excluded.permission_count,
                    json_path = excluded.json_path,
                    event_path = excluded.event_path
                """,
                (
                    record.id,
                    record.cwd,
                    record.created_at,
                    record.updated_at,
                    len(record.messages),
                    len(record.tool_calls),
                    len(record.tool_results),
                    len(record.permission_decisions),
                    str(path),
                    str(self.event_path(record.id)),
                ),
            )
            connection.commit()

    @staticmethod
    def _summary_from_row(row: sqlite3.Row) -> SessionSummary:
        return SessionSummary(
            id=row["id"],
            cwd=row["cwd"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=row["message_count"],
            tool_call_count=row["tool_call_count"],
            tool_result_count=row["tool_result_count"],
            permission_count=row["permission_count"],
            json_path=row["json_path"],
            event_path=row["event_path"],
        )
