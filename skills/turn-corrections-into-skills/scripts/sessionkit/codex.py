"""Codex reader: <codex home>/sessions/**/rollout-*.jsonl and archived_sessions/."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .model import ActionText, Candidate, Clock, JsonLines, Session, TextTools


class CodexReader:
    """Reads interactive Codex CLI and IDE sessions from rollout files."""

    harness = "codex"
    INJECTED = ("<environment_context", "<user_instructions", "# AGENTS.md instructions", "<turn_aborted", "<user_shell_command", "<subagent_notification")
    EVENTS = {"user_message", "agent_message", "task_started", "task_complete", "turn_aborted", "error", "exec_command_end", "item_completed"}
    TOOL_ITEMS = {"function_call", "custom_tool_call", "local_shell_call", "web_search_call"}
    HEAD = re.compile(r'"type":\s*"([a-z_]+)",\s*"payload":\s*\{\s*"type":\s*"([A-Za-z_]+)"')
    SKIP_TOP = {"token_usage_record", "world_state", "inter_agent_communication_metadata", "compacted"}
    EXIT_RE = re.compile(r"(?:Exit code|exit_code\"?)\s*[:=]?\s*(-?\d+)")

    def __init__(self, codex_home: Path) -> None:
        # Remembers the Codex home folder and loads thread titles from session_index.jsonl.
        self.home = Path(codex_home).expanduser()
        self.roots = [self.home]
        self._titles = None

    def candidates(self, since) -> list:
        # Lists rollout files under sessions/ and archived_sessions/ modified at or after since.
        found = []
        floor = since.timestamp() if since else None
        for folder in (self.home / "sessions", self.home / "archived_sessions"):
            if not folder.is_dir():
                continue
            for path in folder.rglob("rollout-*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if floor is None or mtime >= floor:
                    found.append(Candidate(self.harness, path, Clock.parse_ts(mtime), self.home))
        return found

    def read(self, candidate: Candidate, since, stats: Counter):
        # Parses one rollout into a Session; meta records decide whether a person drove it.
        path = Path(candidate.ref)
        session = None
        seen_items: set = set()
        try:
            handle = open(str(path), encoding="utf-8", errors="replace")
        except OSError:
            stats["unreadable_files"] += 1
            return None
        with handle:
            for line in handle:
                if not self._wanted(line):
                    continue
                record = JsonLines.load(line, stats)
                if record is None:
                    continue
                payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
                if record.get("type") == "session_meta":
                    if session is None:
                        session = self._start(path, payload)
                        if session.skip_reason:
                            return session
                    continue
                if session is None:
                    session = Session(self.harness, path.stem, str(path))
                self._apply(session, record.get("type"), payload, Clock.parse_ts(record.get("timestamp")), seen_items)
        return session.finish() if session else None

    def _wanted(self, line: str) -> bool:
        # Cheap prefilter on the record head so large output records are never JSON-parsed.
        head = self.HEAD.search(line[:260])
        if head is None:
            return '"session_meta"' in line[:200] or '"turn_context"' in line[:200]
        top, sub = head.group(1), head.group(2)
        if top in self.SKIP_TOP:
            return False
        if top == "event_msg":
            return sub in self.EVENTS
        if top == "response_item":
            return sub in self.TOOL_ITEMS or sub == "function_call_output"
        return top in ("session_meta", "turn_context")

    def _start(self, path: Path, payload: dict) -> Session:
        # Builds the Session from session_meta and sets a skip reason for SDK, exec, subagent, or automation threads.
        session_id = str(payload.get("id") or payload.get("session_id") or path.stem)
        session = Session(self.harness, session_id, str(path))
        session.add_cwd(payload.get("cwd"))
        session.title = self.titles().get(session_id, "")
        source = payload.get("source")
        originator = str(payload.get("originator", "")).lower()
        if "sdk" in originator:
            session.skip_reason = "sdk"
        elif (isinstance(source, dict) and "subagent" in source) or payload.get("thread_source") == "subagent":
            session.skip_reason = "subagent"
        elif payload.get("thread_source") == "automation":
            session.skip_reason = "automation"
        elif originator == "codex_exec" or source == "exec":
            session.skip_reason = "non_interactive"
        return session

    def _apply(self, session: Session, kind: str, payload: dict, stamp, seen_items: set) -> None:
        # Routes one record to the session: context, tool calls, prompts, replies, and turn ends.
        if kind == "turn_context":
            session.add_cwd(payload.get("cwd"))
            session.note_model(payload.get("model"))
            return
        if kind == "response_item":
            self._tool_item(session, payload, stamp)
            return
        if kind != "event_msg":
            return
        event = payload.get("type")
        session.touch(stamp)
        if event == "user_message":
            self._prompt(session, str(payload.get("message", "")), stamp)
        elif event == "item_completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("id") in seen_items:
                return
            seen_items.add(item.get("id"))
            if item.get("type") == "UserMessage":
                self._prompt(session, TextTools.part_text(item.get("content")), stamp)
            elif item.get("type") == "AgentMessage" and item.get("phase") in (None, "final_answer"):
                session.note_agent_text(TextTools.part_text(item.get("content")), stamp)
        elif event == "agent_message" and payload.get("phase") in (None, "final_answer"):
            session.note_agent_text(payload.get("message"), stamp)
        elif event == "exec_command_end":
            if payload.get("exit_code") not in (0, "0", None):
                session.note_failure()
        elif event == "task_complete":
            last = str(payload.get("last_agent_message") or "").strip()
            if last and last != session.last_agent_text:
                session.note_agent_text(last, stamp)
            session.note_turn_end("finished")
        elif event == "turn_aborted":
            session.note_interrupt()
        elif event == "error":
            session.note_turn_end("error")

    def _tool_item(self, session: Session, payload: dict, stamp) -> None:
        # Records a tool call (and an update_plan todo snapshot), or a failed output of an earlier call.
        ptype = payload.get("type")
        if ptype == "function_call_output":
            output = payload.get("output")
            text = output if isinstance(output, str) else json.dumps(output)
            match = self.EXIT_RE.search(text[:400])
            if match and match.group(1) != "0":
                session.note_failure()
            return
        session.touch(stamp)
        name = payload.get("name") or ptype.replace("_call", "")
        arguments = payload.get("arguments") if payload.get("arguments") is not None else payload.get("input", payload.get("action"))
        if name == "update_plan":
            parsed = ActionText._parse_args(arguments) if isinstance(arguments, str) else (arguments or {})
            session.note_todos(parsed.get("plan") if isinstance(parsed, dict) else None)
        session.note_action(ActionText.describe(name, arguments))

    def _prompt(self, session: Session, text: str, stamp) -> None:
        # Adds a human prompt unless it is harness-injected context.
        if text.lstrip().startswith(self.INJECTED):
            return
        session.add_turn(text, stamp)

    def titles(self) -> dict:
        # Maps thread id to its name from session_index.jsonl (the last entry per id wins).
        if self._titles is None:
            self._titles = {}
            for record in JsonLines.iter_records(self.home / "session_index.jsonl", Counter()):
                if isinstance(record.get("id"), str) and isinstance(record.get("thread_name"), str):
                    self._titles[record["id"]] = TextTools.one_line(record["thread_name"], 200)
        return self._titles
