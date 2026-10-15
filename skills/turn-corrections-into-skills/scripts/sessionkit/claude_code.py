"""Claude Code (and OpenClaude) reader: <config dir>/projects/<project>/<session>.jsonl transcripts."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from .model import ActionText, Candidate, Clock, JsonLines, Session, TextTools


class ClaudeCodeReader:
    """Reads interactive Claude Code sessions from one or more config folders."""

    harness = "claude-code"
    INJECTED = (
        "<task-notification", "<local-command", "<system-reminder", "<bash-input", "<bash-stdout",
        "<bash-stderr", "<user-memory-input", "Stop hook feedback", "[Request interrupted",
        "This session is being continued", "Another Claude session", "[Cross-session", "Caveat:",
        "Base directory for this skill",
    )
    BUILTIN_COMMANDS = {
        "/clear", "/compact", "/model", "/effort", "/mode", "/usage", "/cost", "/status", "/config",
        "/help", "/exit", "/quit", "/login", "/logout", "/resume", "/context", "/permissions",
        "/memory", "/doctor", "/theme", "/vim", "/fast", "/plugin", "/mcp", "/agents", "/hooks",
        "/ide", "/rename", "/export", "/goal", "/output-style", "/statusline",
    }
    WANTED = ('"type":"user"', '"type":"assistant"', '"type": "user"', '"type": "assistant"', '"ai-title"', '"custom-title"', '"type":"summary"')

    def __init__(self, config_dirs: list, harness: str = "claude-code") -> None:
        # Keeps the config folders (each holding projects/) in the order given; OpenClaude reuses this format.
        self.harness = harness
        self.roots: list = []
        for folder in config_dirs:
            path = Path(folder).expanduser()
            if path not in self.roots:
                self.roots.append(path)

    def candidates(self, since) -> list:
        # Lists <dir>/projects/*/*.jsonl modified at or after since (subagent transcripts live deeper).
        found = []
        floor = since.timestamp() if since else None
        for root in self.roots:
            projects = root / "projects"
            if not projects.is_dir():
                continue
            for path in projects.glob("*/*.jsonl"):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if floor is None or mtime >= floor:
                    found.append(Candidate(self.harness, path, Clock.parse_ts(mtime), root))
        return found

    def read(self, candidate: Candidate, since, stats: Counter):
        # Parses one transcript into a Session; a skip_reason marks sessions that are not interactive.
        path = Path(candidate.ref)
        session = Session(self.harness, path.stem, str(path))
        pending: dict = {}
        for record in JsonLines.iter_records(path, stats, self.WANTED):
            kind = record.get("type")
            if kind in ("ai-title", "custom-title", "summary"):
                self._note_title(session, record)
                continue
            if str(record.get("entrypoint", "")).startswith("sdk"):
                session.skip_reason = "sdk"
                break
            if record.get("isSidechain") or kind not in ("user", "assistant"):
                continue
            stamp = Clock.parse_ts(record.get("timestamp"))
            session.add_cwd(record.get("cwd"))
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            if kind == "user":
                self._read_user(session, record, message.get("content"), stamp, pending)
            else:
                self._read_assistant(session, record, message, stamp, pending)
        return session.finish()

    def _note_title(self, session: Session, record: dict) -> None:
        # Keeps a user-set title over an AI title, and either over a compaction summary.
        title = record.get("customTitle") or record.get("aiTitle") or record.get("summary")
        if not isinstance(title, str) or not title.strip():
            return
        if record.get("type") == "custom-title" or not session.title:
            session.title = TextTools.one_line(title, 200)

    def _read_user(self, session: Session, record: dict, content: object, stamp, pending: dict) -> None:
        # Handles tool results, interrupts, and human prompts carried by a user record.
        if record.get("isCompactSummary"):
            return
        results = [p for p in content if isinstance(p, dict) and p.get("type") == "tool_result"] if isinstance(content, list) else []
        if results:
            for result in results:
                pending.pop(result.get("tool_use_id"), None)
                if result.get("is_error"):
                    session.note_failure()
            session.touch(stamp)
            return
        text = TextTools.part_text(content)
        if text.lstrip().startswith("[Request interrupted"):
            session.touch(stamp)
            session.note_interrupt()
            return
        prompt = self.prompt_text(record, text)
        if prompt is not None:
            session.add_turn(prompt, stamp)

    def _read_assistant(self, session: Session, record: dict, message: dict, stamp, pending: dict) -> None:
        # Adds agent text, tool calls, todo snapshots, the model, and how the turn ended.
        session.touch(stamp)
        if record.get("isApiErrorMessage"):
            session.note_turn_end("error")
            return
        model = message.get("model")
        if model and model != "<synthetic>":
            session.note_model(model)
        parts = message.get("content") if isinstance(message.get("content"), list) else []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
        uses = [p for p in parts if isinstance(p, dict) and p.get("type") == "tool_use"]
        if texts:
            session.note_agent_text("\n".join(texts), stamp)
        for use in uses:
            pending[use.get("id")] = use
            tool_input = use.get("input") if isinstance(use.get("input"), dict) else {}
            if use.get("name") == "TodoWrite":
                session.note_todos(tool_input.get("todos"))
            session.note_action(ActionText.describe(use.get("name"), tool_input))
        if not uses and texts and message.get("stop_reason") in ("end_turn", "stop_sequence", None):
            session.note_turn_end("finished")

    @classmethod
    def prompt_text(cls, record: dict, text: str):
        # Returns the human prompt of a user record, "/cmd args" for slash commands, or None for injected text.
        origin = record.get("origin")
        human = isinstance(origin, dict) and origin.get("kind") == "human"
        if isinstance(origin, dict) and "kind" in origin and not human:
            return None
        if not human and (record.get("isMeta") or record.get("isCompactSummary")):
            return None
        stripped = text.lstrip()
        if stripped.startswith("<command-name>") or stripped.startswith("<command-message>"):
            name = re.search(r"<command-name>\s*(/?[^<\s]+)\s*</command-name>", stripped)
            args = re.search(r"<command-args>(.*?)</command-args>", stripped, re.S)
            if not name:
                return None
            command = name.group(1) if name.group(1).startswith("/") else "/" + name.group(1)
            arg_text = args.group(1).strip() if args else ""
            if not arg_text and command in cls.BUILTIN_COMMANDS:
                return None
            return (command + " " + arg_text).strip()
        if human:
            return text
        if stripped.startswith(cls.INJECTED) or (stripped.startswith("<") and not stripped.startswith("<pasted")):
            return None
        return text
