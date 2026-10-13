"""Gemini CLI and Qwen Code reader: <root>/tmp/<project>/chats/session-*.json files."""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

from .model import ActionText, Candidate, Clock, Session, TextTools


class GeminiReader:
    """Reads Gemini CLI (harness "gemini-cli") or Qwen Code (harness "qwen-code") chat files."""

    BUILTIN_COMMANDS = {
        "/about", "/auth", "/bug", "/chat", "/clear", "/compress", "/copy", "/corgi", "/docs",
        "/editor", "/extensions", "/help", "/ide", "/init", "/mcp", "/memory", "/model", "/privacy",
        "/quit", "/exit", "/restore", "/settings", "/stats", "/theme", "/tools", "/vim", "/permissions",
        "/directory", "/approval-mode", "/agents", "/summary",
    }
    CANCELLED = ("Request cancelled.", "Request cancelled")

    def __init__(self, root: Path, harness: str = "gemini-cli") -> None:
        # Remembers the root (~/.gemini or ~/.qwen) and which harness name its sessions carry.
        self.root = Path(root).expanduser()
        self.harness = harness
        self.roots = [self.root]
        self._projects = None

    def candidates(self, since) -> list:
        # Lists tmp/*/chats/session-*.json files modified at or after since.
        found = []
        floor = since.timestamp() if since else None
        tmp = self.root / "tmp"
        if not tmp.is_dir():
            return found
        for path in tmp.glob("*/chats/session-*.json"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if floor is None or mtime >= floor:
                found.append(Candidate(self.harness, path, Clock.parse_ts(mtime), self.root))
        return found

    def read(self, candidate: Candidate, since, stats: Counter):
        # Parses one chat file into a Session.
        path = Path(candidate.ref)
        data = TextTools.read_json_file(path)
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            stats["unreadable_files"] += 1
            return None
        session = Session(self.harness, str(data.get("sessionId") or path.stem), str(path))
        session.add_cwd(self.project_folder(path.parent.parent, str(data.get("projectHash") or "")))
        session.title = TextTools.one_line(data.get("summary") or "", 200) if isinstance(data.get("summary"), str) else ""
        session.touch(Clock.parse_ts(data.get("startTime")))
        for message in data["messages"]:
            if isinstance(message, dict):
                self._apply(session, message)
        session.touch(Clock.parse_ts(data.get("lastUpdated")))
        return session.finish()

    def _apply(self, session: Session, message: dict) -> None:
        # Routes one message: user prompt, agent reply with tool calls, cancellation, or error.
        kind = message.get("type")
        stamp = Clock.parse_ts(message.get("timestamp"))
        if kind == "user":
            text = TextTools.part_text(message.get("content")).strip()
            command = text.split()[0] if text.startswith("/") and text.split() else ""
            if command and command in self.BUILTIN_COMMANDS and len(text.split()) == 1:
                return
            session.add_turn(text, stamp)
        elif kind in ("gemini", "qwen", "model", "assistant"):
            session.touch(stamp)
            session.note_model(message.get("model"))
            calls = message.get("toolCalls") if isinstance(message.get("toolCalls"), list) else []
            for call in calls:
                if isinstance(call, dict):
                    failed = str(call.get("status", "")).lower() in ("error", "failed", "cancelled")
                    if str(call.get("name", "")).lower() in ("write_todos", "todo_write", "todowrite"):
                        args = call.get("args") if isinstance(call.get("args"), dict) else {}
                        session.note_todos(args.get("todos"))
                    session.note_action(ActionText.describe(call.get("displayName") or call.get("name"), call.get("args")), failed=failed)
            text = TextTools.part_text(message.get("content"))
            if text.strip():
                session.note_agent_text(text, stamp)
            if not calls and text.strip():
                session.note_turn_end("finished")
        elif kind == "info" and str(message.get("content", "")).strip().startswith(self.CANCELLED):
            session.note_interrupt()
        elif kind == "error":
            session.note_turn_end("error")

    def project_folder(self, project_dir: Path, project_hash: str) -> str:
        # Resolves the project folder from .project_root, projects.json, or the sha256 hash; else a placeholder.
        root_file = project_dir / ".project_root"
        try:
            if root_file.is_file():
                value = root_file.read_text(encoding="utf-8", errors="replace").strip()
                if value:
                    return value
        except OSError:
            pass
        projects = self.projects()
        name = project_dir.name
        for folder, short in projects.items():
            if short == name or self._hash(folder) in (name, project_hash):
                return folder
        tag = (project_hash or name)[:8]
        return "%s project %s" % (self.harness.split("-")[0], tag) if tag else ""

    def projects(self) -> dict:
        # Loads projects.json as {folder: short name}.
        if self._projects is None:
            data = TextTools.read_json_file(self.root / "projects.json")
            mapping = data.get("projects") if isinstance(data, dict) else None
            self._projects = {k: str(v) for k, v in mapping.items()} if isinstance(mapping, dict) else {}
        return self._projects

    @staticmethod
    def _hash(folder: str) -> str:
        # The sha256 hex digest Gemini CLI uses to name a project's tmp folder.
        return hashlib.sha256(folder.encode("utf-8")).hexdigest()
