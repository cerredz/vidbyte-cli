"""OpenCode reader: opencode.db (v2 then v1 tables) plus the legacy storage/ JSON tree."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

from .model import ActionText, Candidate, Clock, Session, TextTools

try:
    import sqlite3
except ImportError:  # Some minimal Python builds ship without sqlite3.
    sqlite3 = None


class OpenCodeDatabase:
    """A read-only connection to opencode.db, falling back to a private copy when the file is locked."""

    def __init__(self, path: Path) -> None:
        # Remembers the database path; nothing is opened until connect().
        self.path = Path(path)
        self.connection = None
        self.copy_dir = None
        self.error = ""
        self.tables: set = set()

    def connect(self) -> bool:
        # Opens the database read-only, else a copy of it with its WAL files; records the error on failure.
        if sqlite3 is None:
            self.error = "sqlite3 unavailable"
            return False
        try:
            self.connection = self._open(self.path)
        except sqlite3.Error as first:
            try:
                self.connection = self._open(self._copy())
            except (sqlite3.Error, OSError) as second:
                self.error = "%s; copy failed: %s" % (first, second)
                self.close()
                return False
        try:
            self.tables = {row[0] for row in self.connection.execute("select name from sqlite_master where type='table'")}
        except sqlite3.Error as exc:
            self.error = str(exc)
            self.close()
            return False
        return True

    def rows(self, sql: str, params: tuple = ()) -> list:
        # Runs a query and returns all rows, or [] when the schema differs from what this reader expects.
        if self.connection is None:
            return []
        try:
            return self.connection.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []

    def close(self) -> None:
        # Closes the connection and deletes the private copy, if one was made.
        if self.connection is not None:
            try:
                self.connection.close()
            except sqlite3.Error:
                pass
            self.connection = None
        if self.copy_dir:
            shutil.rmtree(self.copy_dir, ignore_errors=True)
            self.copy_dir = None

    def _open(self, path: Path):
        # Opens path in SQLite read-only URI mode and probes it so a lock surfaces here.
        uri = "file:%s?mode=ro" % str(path).replace("\\", "/").replace("?", "%3F").replace("#", "%23")
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.execute("select 1 from sqlite_master limit 1").fetchall()
        return connection

    def _copy(self) -> Path:
        # Copies the database and its -wal/-shm files into a fresh temp folder and returns the copy's path.
        self.copy_dir = tempfile.mkdtemp(prefix="sessionkit-opencode-")
        target = Path(self.copy_dir) / self.path.name
        shutil.copy2(str(self.path), str(target))
        for suffix in ("-wal", "-shm"):
            extra = Path(str(self.path) + suffix)
            if extra.exists():
                shutil.copy2(str(extra), str(target) + suffix)
        return target


class OpenCodeReader:
    """Reads interactive OpenCode sessions; each session id is read from one backend only."""

    harness = "opencode"
    ABORT_NAMES = ("MessageAbortedError", "aborted", "AbortError")

    def __init__(self, data_dir: Path) -> None:
        # Remembers the OpenCode data folder (holding opencode.db and storage/).
        self.data_dir = Path(data_dir).expanduser()
        self.roots = [self.data_dir]
        self.database = OpenCodeDatabase(self.data_dir / "opencode.db")
        self.notes: list = []
        self.root_errors: dict = {}
        self._connected = None
        self._row_model = None

    def candidates(self, since) -> list:
        # Lists top-level sessions from v2, then v1 ids absent from v2, then legacy files absent from both.
        found: dict = {}
        floor = int(since.timestamp() * 1000) if since else 0
        if self._connect():
            for table, backend in (("session_v2", "v2"), ("session", "v1")):
                if table not in self.database.tables:
                    continue
                for session_id, updated in self.database.rows("select id, time_updated from %s where parent_id is null and time_updated >= ?" % table, (floor,)):
                    if session_id not in found:
                        found[session_id] = Candidate(self.harness, session_id, Clock.parse_ts(updated), self.data_dir, backend)
        for path in self._legacy_session_files():
            data = TextTools.read_json_file(path) or {}
            session_id = data.get("id") or path.stem
            updated = Clock.parse_ts((data.get("time") or {}).get("updated")) if isinstance(data.get("time"), dict) else None
            if session_id in found or data.get("parentID") or (updated and since and updated < since):
                continue
            found[session_id] = Candidate(self.harness, session_id, updated or Clock.parse_ts(path.stat().st_mtime), self.data_dir, ("legacy", path))
        self._drop_v2_without_messages(found)
        return list(found.values())

    def read(self, candidate: Candidate, since, stats: Counter):
        # Reads one session from the backend its candidate names.
        backend = candidate.extra
        if backend == "v2":
            return self._read_v2(candidate.ref, stats)
        if backend == "v1":
            return self._read_v1(candidate.ref, stats)
        return self._read_legacy(candidate.ref, backend[1], stats)

    def close(self) -> None:
        # Releases the database (and its copy, if one was made).
        self.database.close()

    def _connect(self) -> bool:
        # Connects once; a missing database is silent, an unreadable one is noted.
        if self._connected is None:
            if not self.database.path.exists():
                self._connected = False
            else:
                self._connected = self.database.connect()
                if not self._connected:
                    self.notes.append("opencode.db not read: %s" % self.database.error)
                    self.root_errors[str(self.data_dir)] = "opencode.db not read: %s" % self.database.error
        return self._connected

    def _drop_v2_without_messages(self, found: dict) -> None:
        # Moves v2 sessions that have no session_message rows to v1, where their messages live.
        v2_ids = [sid for sid, c in found.items() if c.extra == "v2"]
        if not v2_ids or "session" not in self.database.tables:
            return
        with_messages = {row[0] for row in self.database.rows("select distinct session_id from session_message")}
        for session_id in v2_ids:
            if session_id not in with_messages:
                found[session_id].extra = "v1"

    def _session_row(self, table: str, session_id: str) -> Session:
        # Builds a Session from a session_v2 or session row: folder, title, todos; keeps the row model as a fallback.
        self._row_model = None
        session = Session(self.harness, session_id, "%s#%s" % (self.database.path, session_id))
        for directory, title, model in self.database.rows("select directory, title, model from %s where id = ?" % table, (session_id,)):
            session.add_cwd(directory)
            session.title = TextTools.one_line(title or "", 200)
            parsed = self._json(model)
            if isinstance(parsed, dict):
                self._row_model = parsed.get("id") or parsed.get("modelID")
        todos = self.database.rows("select content, status from todo where session_id = ? order by position", (session_id,)) if "todo" in self.database.tables else []
        if todos:
            session.todos = [(TextTools.one_line(content, 300), str(status)) for content, status in todos if content]
        return session

    def _read_v2(self, session_id: str, stats: Counter) -> Session:
        # Replays session_message rows in seq order.
        session = self._session_row("session_v2", session_id)
        for kind, created, data in self.database.rows("select type, time_created, data from session_message where session_id = ? order by seq", (session_id,)):
            payload = self._json(data)
            if not isinstance(payload, dict):
                stats["unreadable_lines"] += 1
                continue
            stamp = Clock.parse_ts(created)
            if kind == "user":
                session.add_turn(payload.get("text"), stamp)
            elif kind == "assistant":
                self._v2_assistant(session, payload, stamp)
            elif kind == "location-switched":
                location = payload.get("location") if isinstance(payload.get("location"), dict) else {}
                session.add_cwd(location.get("directory"))
        return self._done(session)

    def _v2_assistant(self, session: Session, payload: dict, stamp) -> None:
        # Applies one v2 assistant message: text, tool calls, the model, and how it finished.
        session.touch(stamp)
        model = payload.get("model") if isinstance(payload.get("model"), dict) else {}
        session.note_model(model.get("id") or model.get("modelID"))
        texts = []
        for item in payload.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text" and item.get("text"):
                texts.append(item["text"])
            elif item.get("type") == "tool":
                self._tool(session, item.get("name") or item.get("tool"), item.get("state"))
        if texts:
            session.note_agent_text("\n".join(texts), stamp)
        self._finish(session, payload.get("finish"), payload.get("error"))

    def _read_v1(self, session_id: str, stats: Counter) -> Session:
        # Replays v1 message rows with their parts, in creation order.
        session = self._session_row("session", session_id)
        parts: dict = {}
        for message_id, data in self.database.rows("select message_id, data from part where session_id = ? order by time_created, id", (session_id,)):
            parts.setdefault(message_id, []).append(self._json(data))
        for message_id, created, data in self.database.rows("select id, time_created, data from message where session_id = ? order by time_created, id", (session_id,)):
            info = self._json(data)
            if not isinstance(info, dict):
                stats["unreadable_lines"] += 1
                continue
            self._v1_message(session, info, parts.get(message_id, []), Clock.parse_ts(created))
        return self._done(session)

    def _done(self, session: Session) -> Session:
        # Closes the session, using the session row's model only when no message named one.
        if not session.models and self._row_model:
            session.note_model(self._row_model)
        return session.finish()

    def _read_legacy(self, session_id: str, path: Path, stats: Counter) -> Session:
        # Reads storage/session, storage/message/<ses>, and storage/part/<msg> JSON files.
        storage = self.data_dir / "storage"
        session = Session(self.harness, session_id, str(path))
        data = TextTools.read_json_file(path) or {}
        session.add_cwd(data.get("directory"))
        session.title = TextTools.one_line(data.get("title") or "", 200)
        messages = []
        for message_path in sorted((storage / "message" / session_id).glob("*.json")):
            info = TextTools.read_json_file(message_path)
            if not isinstance(info, dict):
                stats["unreadable_files"] += 1
                continue
            created = (info.get("time") or {}).get("created") if isinstance(info.get("time"), dict) else None
            messages.append((created or 0, message_path.stem, info))
        for created, message_id, info in sorted(messages, key=lambda m: (m[0], m[1])):
            part_files = sorted((storage / "part" / (info.get("id") or message_id)).glob("*.json"))
            parts = [TextTools.read_json_file(p) for p in part_files]
            self._v1_message(session, info, parts, Clock.parse_ts(created))
        return session.finish()

    def _v1_message(self, session: Session, info: dict, parts: list, stamp) -> None:
        # Applies one v1 or legacy message (role, model, error) with its parts.
        role = info.get("role")
        session.add_cwd((info.get("path") or {}).get("cwd") if isinstance(info.get("path"), dict) else None)
        if role == "user":
            texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text" and not p.get("synthetic") and p.get("text")]
            if texts:
                session.add_turn("\n".join(texts), stamp)
            return
        if role != "assistant":
            return
        session.touch(stamp)
        model = info.get("model") if isinstance(info.get("model"), dict) else {}
        session.note_model(info.get("modelID") or model.get("modelID"))
        texts = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and part.get("text") and not part.get("synthetic"):
                texts.append(part["text"])
            elif part.get("type") == "tool":
                self._tool(session, part.get("tool"), part.get("state"))
        if texts:
            session.note_agent_text("\n".join(texts), stamp)
        finish = info.get("finish")
        if finish is None:
            reasons = [p.get("reason") for p in parts if isinstance(p, dict) and p.get("type") == "step-finish"]
            finish = reasons[-1] if reasons else None
        self._finish(session, finish, info.get("error"))

    def _tool(self, session: Session, name: object, state: object) -> None:
        # Records a tool call and its todo list (todowrite), counting errored states as failures.
        state = state if isinstance(state, dict) else {}
        tool_input = state.get("input") if isinstance(state.get("input"), dict) else {}
        if str(name or "").lower() == "todowrite":
            session.note_todos(tool_input.get("todos"))
        session.note_action(ActionText.describe(name, tool_input), failed=state.get("status") == "error")

    def _finish(self, session: Session, finish: object, error: object) -> None:
        # Maps OpenCode's finish reason and error to interrupted, error, or finished.
        name = ""
        if isinstance(error, dict):
            name = str(error.get("name") or error.get("type") or "")
        aborted = bool(name) and any(n.lower() in name.lower() for n in self.ABORT_NAMES)
        if finish == "aborted" or aborted:
            session.note_interrupt()
        elif error:
            session.note_turn_end("error")
        elif finish in ("stop", "end_turn", "length"):
            session.note_turn_end("finished")

    def _legacy_session_files(self) -> list:
        # Lists storage/session/<project>/<ses>.json files.
        folder = self.data_dir / "storage" / "session"
        return [p for p in folder.glob("*/*.json")] if folder.is_dir() else []

    @staticmethod
    def _json(value: object):
        # Parses a JSON column, returning None for invalid text.
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value) if value else None
        except (TypeError, ValueError):
            return None
