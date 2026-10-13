"""The harness-agnostic session model and the small helpers every reader shares."""

from __future__ import annotations

import datetime as dt
import json
import re
from collections import Counter
from pathlib import Path

MAX_ACTIONS_PER_TURN = 25
MAX_ACTION_CHARS = 160
BEFORE_TEXT_CHARS = 400
REPLY_HEAD_CHARS = 400
REPLY_TAIL_CHARS = 400
FINAL_TEXT_CHARS = 1200
DUPLICATE_SECONDS = 10.0
END_STATES = ("finished", "question", "interrupted", "working", "error", "unknown")


class Candidate:
    """A session that may be read: which harness, where it is, and when it was last active."""

    __slots__ = ("harness", "ref", "last_activity", "root", "extra")

    def __init__(self, harness: str, ref: object, last_activity, root: Path, extra: object = None) -> None:
        # Stores the reader-specific reference (a file path or a database id) and its activity time.
        self.harness = harness
        self.ref = ref
        self.last_activity = last_activity
        self.root = root
        self.extra = extra


class JsonLines:
    """Line-by-line JSONL reading that counts unreadable files and lines instead of failing."""

    @staticmethod
    def iter_records(path: Path, stats: Counter, wanted=None):
        # Yields each JSON object line; wanted, if given, is a cheap substring prefilter on the raw line.
        try:
            with open(str(path), encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if wanted is not None and not any(w in line for w in wanted):
                        continue
                    record = JsonLines.load(line, stats)
                    if record is not None:
                        yield record
        except OSError:
            stats["unreadable_files"] += 1

    @staticmethod
    def load(line: str, stats: Counter):
        # Parses one line, or returns None (and counts it) when it is not a JSON object.
        line = line.strip()
        if not line:
            return None
        try:
            record = json.loads(line)
        except ValueError:
            stats["unreadable_lines"] += 1
            return None
        return record if isinstance(record, dict) else None


class Clock:
    """Timestamp parsing and display, always in aware UTC internally."""

    @staticmethod
    def parse_ts(value: object):
        # Returns an aware UTC datetime for an ISO string or epoch seconds/milliseconds, else None.
        if value is None or value == "" or isinstance(value, bool):
            return None
        try:
            if isinstance(value, (int, float)):
                seconds = value / 1000.0 if value > 1e11 else float(value)
                return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
            text = str(value).strip()
            if re.match(r"^\d{10,13}$", text):
                return Clock.parse_ts(int(text))
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # Python 3.8-3.10 accept 6 digits at most.
            text = re.sub(r"\.(\d{1,5})(?=[+-]|$)", lambda m: "." + m.group(1).ljust(6, "0"), text)
            stamp = dt.datetime.fromisoformat(text)
        except (ValueError, OverflowError, OSError):
            return None
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=dt.timezone.utc)
        return stamp.astimezone(dt.timezone.utc)

    @staticmethod
    def iso(stamp) -> str:
        # ISO 8601 in UTC, or "" for no time.
        return stamp.astimezone(dt.timezone.utc).isoformat(timespec="seconds") if stamp else ""

    @staticmethod
    def local_day(stamp) -> str:
        # A short local date such as "Oct 13", or "?" for no time.
        return stamp.astimezone().strftime("%b %d") if stamp else "?"

    @staticmethod
    def local_stamp(stamp) -> str:
        # A local date and minute such as "2026-10-13 14:05", or "?" for no time.
        return stamp.astimezone().strftime("%Y-%m-%d %H:%M") if stamp else "?"

    @staticmethod
    def now():
        # The current time as an aware UTC datetime.
        return dt.datetime.now(tz=dt.timezone.utc)


class TextTools:
    """Text flattening, trimming, and file helpers that behave the same on Python 3.8 and later."""

    @staticmethod
    def part_text(content: object) -> str:
        # Flattens message content (a string or a list of typed parts) to plain text.
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            if isinstance(content.get("text"), str):
                return content["text"]
            return TextTools.part_text(content.get("content"))
        if isinstance(content, list):
            chunks = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") in ("image", "input_image", "image_url", "file", "tool_use", "tool_result", "function_call"):
                        continue
                    if isinstance(part.get("text"), str):
                        chunks.append(part["text"])
                    elif isinstance(part.get("content"), (str, list)):
                        chunks.append(TextTools.part_text(part["content"]))
                elif isinstance(part, str):
                    chunks.append(part)
            return "\n".join(c for c in chunks if c)
        return ""

    @staticmethod
    def one_line(text: object, limit: int) -> str:
        # Collapses whitespace and cuts to limit characters with an ellipsis.
        flat = " ".join(str(text or "").split())
        if len(flat) <= limit:
            return flat
        return flat[: max(limit - 1, 1)].rstrip() + "…"

    @staticmethod
    def word_count(text: object) -> int:
        # Counts whitespace-separated words.
        return len(str(text or "").split())

    @staticmethod
    def head_tail(text: object, head: int, tail: int) -> str:
        # Keeps the first head and last tail characters, joined by " … " when text is longer.
        value = str(text or "").strip()
        if len(value) <= head + tail + 3:
            return value
        return value[:head].rstrip() + " … " + value[-tail:].lstrip()

    @staticmethod
    def tail(text: object, limit: int) -> str:
        # Keeps the last limit characters of text.
        value = str(text or "").strip()
        return value if len(value) <= limit else "…" + value[-(limit - 1):].lstrip()

    @staticmethod
    def write_lf(path: Path, text: str) -> None:
        # Writes UTF-8 text with LF line endings (Path.write_text has no newline= before 3.10).
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    @staticmethod
    def read_json_file(path: Path, limit_bytes: int = 50_000_000):
        # Reads one JSON document, or returns None when it is missing, too large, or invalid.
        try:
            path = Path(path)
            if path.stat().st_size > limit_bytes:
                return None
            with open(str(path), encoding="utf-8-sig", errors="replace") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None


class ActionText:
    """Short, harness-neutral descriptions of tool calls, such as "Edit src/app.py" or "Bash: npm test"."""

    EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "patch", "str_replace_based_edit_tool", "replace", "write_file", "create_file"}
    READ_TOOLS = {"read", "view", "read_file", "read_many_files", "cat"}
    SHELL_TOOLS = {"bash", "powershell", "shell", "shell_command", "exec_command", "local_shell", "container.exec", "run_shell_command", "execute_command", "terminal"}
    SEARCH_TOOLS = {"grep", "glob", "search", "search_file_content", "find", "list", "ls", "list_directory", "codebase_search"}
    PATH_KEYS = ("file_path", "filePath", "path", "notebook_path", "absolute_path", "target_file", "file")
    PATCH_FILE_RE = re.compile(r"\*\*\* (?:Update|Add|Delete) File: (\S+)")

    @staticmethod
    def describe(name: object, tool_input: object) -> str:
        # Returns a one-line description of a tool call from its name and input arguments.
        label = str(name or "tool")
        args = tool_input if isinstance(tool_input, dict) else {}
        if isinstance(tool_input, str):
            args = ActionText._parse_args(tool_input)
        key = label.lower().rsplit(".", 1)[-1] if not label.lower().startswith("container.") else label.lower()
        if key in ActionText.SHELL_TOOLS:
            return "%s: %s" % (label, ActionText._command(args) or "(command)")
        path = ActionText._path(args, tool_input)
        if key in ActionText.EDIT_TOOLS:
            return "Edit %s" % (path or "(file)")
        if key in ActionText.READ_TOOLS:
            return "Read %s" % (path or "(file)")
        if key in ActionText.SEARCH_TOOLS:
            target = args.get("pattern") or args.get("query") or args.get("glob") or path or ""
            return ("%s %s" % (label, target)).strip()
        if key in ("webfetch", "web_fetch", "fetch"):
            return "WebFetch %s" % (args.get("url") or "")
        if key in ("websearch", "web_search", "google_web_search"):
            return "WebSearch %s" % (args.get("query") or "")
        if key in ("task", "agent"):
            return "Agent: %s" % (args.get("description") or args.get("prompt") or "")
        first = next((v for v in args.values() if isinstance(v, str) and v.strip()), "")
        return ("%s %s" % (label, first)).strip()

    @staticmethod
    def _parse_args(text: str) -> dict:
        # Parses JSON-encoded tool arguments (Codex) into a dict, or wraps raw text as {"input": text}.
        try:
            value = json.loads(text)
        except ValueError:
            return {"input": text}
        return value if isinstance(value, dict) else {"input": text}

    @staticmethod
    def _command(args: dict) -> str:
        # Extracts the shell command, unwrapping ["bash", "-lc", "cmd"] style argument lists.
        command = args.get("command") or args.get("cmd") or args.get("script") or args.get("input")
        if isinstance(command, list) and command:
            parts = [str(c) for c in command]
            if len(parts) >= 3 and parts[-2] in ("-Command", "-c", "-lc", "/c"):
                return parts[-1]
            return " ".join(parts)
        return str(command or "")

    @staticmethod
    def _path(args: dict, raw: object) -> str:
        # Finds the file path argument, or the first file named in an apply_patch body.
        for key in ActionText.PATH_KEYS:
            if isinstance(args.get(key), str) and args[key].strip():
                return args[key]
        body = args.get("input") if isinstance(args.get("input"), str) else (raw if isinstance(raw, str) else "")
        match = ActionText.PATCH_FILE_RE.search(body or "")
        return match.group(1) if match else ""


class Turn:
    """One human prompt and what the agent did after it, until the next prompt."""

    __slots__ = (
        "index", "text", "ts", "before_agent_text", "after_finished", "after_question",
        "reply_text", "tool_calls", "actions", "failed_actions", "interrupted",
    )

    def __init__(self, index: int, text: str, ts, before_agent_text: str, after_finished, after_question: bool) -> None:
        # Starts a turn with the agent state that came just before the prompt.
        self.index = index
        self.text = text
        self.ts = ts
        self.before_agent_text = before_agent_text
        self.after_finished = after_finished
        self.after_question = after_question
        self.reply_text = ""
        self.tool_calls = 0
        self.actions: list = []
        self.failed_actions = 0
        self.interrupted = False


class Session:
    """One interactive session from any harness, as an ordered list of turns."""

    def __init__(self, harness: str, session_id: str, source: str) -> None:
        # Creates an empty session; readers fill it in record by record.
        self.harness = harness
        self.session_id = session_id
        self.source = source
        self.title = ""
        self.cwds: list = []
        self.first_ts = None
        self.last_ts = None
        self.models: Counter = Counter()
        self.turns: list = []
        self.todos: list = []
        self.end_state = "unknown"
        self.final_agent_text = ""
        self.skip_reason = ""
        self._last_agent_text = ""
        self._reply_parts: list = []
        self._last_prompt = ("", None)

    @property
    def cwd(self) -> str:
        # The first working folder the session reported, or "".
        return self.cwds[0] if self.cwds else ""

    @property
    def prompt_count(self) -> int:
        # The number of human prompts kept for this session.
        return len(self.turns)

    @property
    def last_agent_text(self) -> str:
        # The most recent agent text seen so far, untrimmed.
        return self._last_agent_text

    @property
    def current(self):
        # The turn the agent is currently answering, or None before the first prompt.
        return self.turns[-1] if self.turns else None

    def add_cwd(self, value: object) -> None:
        # Records a working folder once, in the order first seen.
        if isinstance(value, str) and value.strip() and value not in self.cwds:
            self.cwds.append(value)

    def touch(self, stamp) -> None:
        # Widens the session's first and last activity times to include stamp.
        if stamp is None:
            return
        if self.first_ts is None or stamp < self.first_ts:
            self.first_ts = stamp
        if self.last_ts is None or stamp > self.last_ts:
            self.last_ts = stamp

    def note_model(self, model: object) -> None:
        # Counts one agent message produced by model.
        if isinstance(model, str) and model.strip():
            self.models[model.strip()] += 1

    def add_turn(self, text: str, stamp):
        # Appends a human prompt as a new turn; returns None for empty or duplicate prompts.
        text = str(text or "").strip()
        if not text:
            return None
        previous_text, previous_ts = self._last_prompt
        if previous_text == text and (previous_ts is None or stamp is None or abs((stamp - previous_ts).total_seconds()) < DUPLICATE_SECONDS):
            return None  # Codex can record one prompt twice, as an event and as an item.
        self._last_prompt = (text, stamp)
        self._flush_reply()
        finished = None if self.end_state == "unknown" and not self.turns else self.end_state in ("finished", "question")
        before = TextTools.tail(self._last_agent_text, BEFORE_TEXT_CHARS)
        turn = Turn(len(self.turns), text, stamp, before, finished, self.end_state == "question")
        self.turns.append(turn)
        self.touch(stamp)
        self.end_state = "working"
        return turn

    def note_agent_text(self, text: object, stamp=None) -> None:
        # Adds agent-visible text to the current reply and to the running last agent text.
        value = str(text or "").strip()
        if not value:
            return
        self.touch(stamp)
        self._last_agent_text = value
        self.final_agent_text = TextTools.tail(value, FINAL_TEXT_CHARS)
        if self.turns:
            self._reply_parts.append(value)

    def note_action(self, description: object, failed: bool = False) -> None:
        # Records one tool call of the current turn with a short description.
        turn = self.current
        if turn is None:
            return
        turn.tool_calls += 1
        if failed:
            turn.failed_actions += 1
        if len(turn.actions) < MAX_ACTIONS_PER_TURN:
            turn.actions.append(TextTools.one_line(description, MAX_ACTION_CHARS))
        self.end_state = "working"

    def note_failure(self) -> None:
        # Marks the latest tool call of the current turn as failed after its result arrives.
        if self.current is not None:
            self.current.failed_actions += 1

    def note_interrupt(self) -> None:
        # Marks the current turn as interrupted by the user.
        if self.current is not None:
            self.current.interrupted = True
        self.end_state = "interrupted"

    def note_turn_end(self, state: str) -> None:
        # Sets how the agent's latest turn ended; "finished" becomes "question" when it asked one.
        if state not in END_STATES:
            state = "unknown"
        if state == "finished" and self._last_agent_text.rstrip().endswith("?"):
            state = "question"
        self.end_state = state

    def note_todos(self, todos: object) -> None:
        # Replaces the todo snapshot with a list of (content, status) pairs.
        snapshot = []
        if isinstance(todos, list):
            for item in todos:
                if not isinstance(item, dict):
                    continue
                content = item.get("content") or item.get("step") or item.get("text") or item.get("title")
                status = item.get("status") or ("completed" if item.get("completed") else "pending")
                if isinstance(content, str) and content.strip():
                    snapshot.append((TextTools.one_line(content, 300), str(status)))
        if snapshot or isinstance(todos, list):
            self.todos = snapshot

    def finish(self) -> "Session":
        # Closes the last reply; call once after the reader saw every record.
        self._flush_reply()
        return self

    def project_name(self) -> str:
        # The last folder name of the working folder, or "" when unknown.
        cwd = self.cwd.replace("\\", "/").rstrip("/")
        return cwd.rsplit("/", 1)[-1] if cwd else ""

    def _flush_reply(self) -> None:
        # Stores the collected reply text on the turn it answered.
        if self.turns and self._reply_parts:
            joined = "\n".join(self._reply_parts)
            self.turns[-1].reply_text = TextTools.head_tail(joined, REPLY_HEAD_CHARS, REPLY_TAIL_CHARS)
        self._reply_parts = []
