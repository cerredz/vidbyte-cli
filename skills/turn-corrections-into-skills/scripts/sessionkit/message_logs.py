"""Factory Droid and Pi readers: <root>/sessions/<project>/<session>.jsonl message logs."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .model import ActionText, Candidate, Clock, JsonLines, Session, TextTools


class MessageLogReader:
    """Shared reading for harnesses that log one header record, then {"type": "message", "message": {...}} records."""

    harness = ""
    HEADER_TYPE = ""
    INJECTED = ("<system-reminder", "<system-notification")
    TOOL_PARTS = ("toolCall", "tool_use", "tool-call")
    TODO_TOOLS = ("todowrite", "todo_write", "write_todos", "update_plan")
    INTERRUPTED = ("aborted", "cancelled", "canceled", "interrupted")
    FINISHED = ("stop", "end_turn", "length", "max_tokens", "stop_sequence")

    def __init__(self, root: Path) -> None:
        # Remembers the harness folder that holds sessions/.
        self.root = Path(root).expanduser()
        self.roots = [self.root]

    def candidates(self, since) -> list:
        # Lists sessions/*/*.jsonl modified at or after since.
        found = []
        floor = since.timestamp() if since else None
        sessions = self.root / "sessions"
        if not sessions.is_dir():
            return found
        for path in sessions.glob("*/*.jsonl"):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if floor is None or mtime >= floor:
                found.append(Candidate(self.harness, path, Clock.parse_ts(mtime), self.root))
        return found

    def read(self, candidate: Candidate, since, stats: Counter):
        # Parses one message log into a Session.
        path = Path(candidate.ref)
        session = Session(self.harness, path.stem, str(path))
        for record in JsonLines.iter_records(path, stats):
            kind = record.get("type")
            if kind == self.HEADER_TYPE:
                self.apply_header(session, record)
            elif kind == "message" and isinstance(record.get("message"), dict):
                self.apply_message(session, record, record["message"])
            else:
                self.apply_other(session, record)
        return session.finish()

    def apply_header(self, session: Session, record: dict) -> None:
        # Takes the session id and working folder from the header record.
        if record.get("id"):
            session.session_id = str(record["id"])
        session.add_cwd(record.get("cwd"))
        session.touch(Clock.parse_ts(record.get("timestamp")))

    def apply_other(self, session: Session, record: dict) -> None:
        # Records the time of any other record; subclasses read their own extra record types.
        session.touch(Clock.parse_ts(record.get("timestamp")))

    def apply_message(self, session: Session, record: dict, message: dict) -> None:
        # Routes one message by role: a typed prompt, an agent reply, or a tool result.
        role = message.get("role")
        stamp = Clock.parse_ts(record.get("timestamp")) or Clock.parse_ts(message.get("timestamp"))
        if role == "user":
            self.apply_user(session, message, stamp)
        elif role == "assistant":
            self.apply_assistant(session, message, stamp)
        elif role in ("toolResult", "tool"):
            session.touch(stamp)
            if message.get("isError") or message.get("is_error"):
                session.note_failure()

    def is_typed(self, message: dict) -> bool:
        # True when a user message is something the person typed; subclasses narrow this.
        return True

    def apply_user(self, session: Session, message: dict, stamp) -> None:
        # Adds the typed text of a user message as a prompt, leaving out injected reminder parts.
        if not self.is_typed(message):
            session.touch(stamp)
            return
        content = message.get("content")
        parts = content if isinstance(content, list) else [content]
        texts = [TextTools.part_text(part) for part in parts]
        text = self.clean_prompt("\n".join(t for t in texts if t.strip() and not t.lstrip().startswith(self.INJECTED)).strip())
        if text:
            session.add_turn(text, stamp)
        else:
            session.touch(stamp)

    def clean_prompt(self, text: str) -> str:
        # Returns the prompt as the person typed it; subclasses undo their harness's expansions.
        return text

    def apply_assistant(self, session: Session, message: dict, stamp) -> None:
        # Records the reply's model, text, and tool calls, then how the turn ended.
        session.touch(stamp)
        session.note_model(message.get("model"))
        content = message.get("content") if isinstance(message.get("content"), list) else [message.get("content")]
        calls = 0
        for part in content:
            if isinstance(part, dict) and part.get("type") in self.TOOL_PARTS:
                self.apply_tool_call(session, part)
                calls += 1
            elif isinstance(part, str) or (isinstance(part, dict) and part.get("type") == "text"):
                session.note_agent_text(TextTools.part_text(part), stamp)
        self.apply_stop(session, str(message.get("stopReason") or message.get("stop_reason") or "").lower(), calls)

    def apply_tool_call(self, session: Session, part: dict) -> None:
        # Records one tool call and, for a todo tool, its todo list.
        name = part.get("name") or part.get("toolName")
        args = part.get("arguments") if "arguments" in part else part.get("input")
        if str(name or "").lower() in self.TODO_TOOLS and isinstance(args, dict):
            session.note_todos(args.get("todos") or args.get("plan"))
        session.note_action(ActionText.describe(name, args))

    def apply_stop(self, session: Session, reason: str, calls: int) -> None:
        # Maps the stop reason to interrupted, error, or finished; a tool-use stop keeps the turn working.
        if reason in self.INTERRUPTED:
            session.note_interrupt()
        elif reason == "error":
            session.note_turn_end("error")
        elif reason in self.FINISHED or (not reason and not calls):
            session.note_turn_end("finished")


class FactoryReader(MessageLogReader):
    """Reads Factory Droid sessions (~/.factory/sessions); messages shown only to the model or only in the UI are not prompts."""

    harness = "factory-droid"
    HEADER_TYPE = "session_start"
    NOT_TYPED = ("llm_only", "user_only")
    DEFAULT_TITLES = ("New Session", "")

    def apply_header(self, session: Session, record: dict) -> None:
        # Adds the session title, unless it is the default "New Session".
        super().apply_header(session, record)
        title = record.get("sessionTitle") or record.get("title") or ""
        if isinstance(title, str) and title.strip() not in self.DEFAULT_TITLES:
            session.title = TextTools.one_line(title, 200)

    def is_typed(self, message: dict) -> bool:
        # Factory marks injected reminders "llm_only" and UI notices "user_only".
        return message.get("visibility") not in self.NOT_TYPED


class PiReader(MessageLogReader):
    """Reads Pi coding agent sessions (~/.pi/agent/sessions); each reply names its own model."""

    harness = "pi"
    HEADER_TYPE = "session"
    SKILL_BLOCK = re.compile(r'^\s*<skill name="([^"]+)"[^>]*>.*?</skill>\s*', re.S)

    def clean_prompt(self, text: str) -> str:
        # Pi inlines an invoked skill as <skill name=...>body</skill> before the typed text; keeps "/name typed text".
        match = self.SKILL_BLOCK.match(text)
        return ("/%s %s" % (match.group(1), text[match.end():])).strip() if match else text
