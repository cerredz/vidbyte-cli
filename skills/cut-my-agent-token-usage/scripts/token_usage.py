#!/usr/bin/env python3
"""Find the Claude Code and Codex sessions that used the most tokens, and ask what to change.

Four subcommands, run in this order by the cut-my-agent-token-usage skill:

  gather    Free and local. Reads the Claude Code and Codex transcripts of a time window,
            totals each session's tokens (fresh input, cache reads, cache writes, output),
            ranks the sessions, and compiles the heaviest ones into digest.md (sent to the
            model), inputs.json (typed inputs pre-filled from the numbers, sent), and
            usage.json (a local index with real session ids and paths, never sent). Every
            prompt, title, path, and command is redacted before it is written.
  suggest   Checks inputs.json, then runs `vidbyte-cli --json --no-input agents suggest run ...`
            from inside the output folder. --dry-run is free; --paid is the one charged step
            and needs the user's yes first.
  report    Free and local. Ties each idea in result.json back to the sessions it came from and
            sums the tokens of the pattern it targets from those sessions' own numbers. Dollars
            appear only with prices the caller copies from the provider's own price page.
  feedback  Records the user's explicit accept or reject of one idea, copying the idea's exact
            title from result.json into `vidbyte-cli agents suggest feedback`.

Only the Python standard library is used, and Python 3.8 or newer is enough. vidbyte-cli is
started without a shell, so no argument quoting differs between bash, zsh, PowerShell, and cmd.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

SLUG = "cut-my-agent-token-usage"
PROJECT_KEY = "agent-token-usage"
CATEGORIES = ("simplification", "leverage", "quick_wins", "risk_prevention")
COUNT = 8
HORIZON = "any"
DEFAULT_DAYS = 7.0
DEFAULT_TOP = 10
MAX_TOP = 25
DEFAULT_MAX_CHARS = 60_000

CHARS_PER_TOKEN = 4  # The size of one tool result or prompt is estimated, never measured.
LARGE_RESULT_CHARS = 20_000  # About 5,000 tokens.
LONG_PROMPT_CHARS = 8_000  # About 2,000 tokens.
IMAGE_CHARS = 1_600 * CHARS_PER_TOKEN  # One image in a tool result counts as about 1,600 tokens.
REPEAT_MIN = 3  # The same tool with the same input this many times in one conversation.
CONTEXT_LINE = 100_000  # Tokens re-sent above this much context are the most a fresh start saves.
KINDS = ("fresh", "cache_read", "cache_write", "output")
KIND_WORDS = {"fresh": "fresh input", "cache_read": "cache reads", "cache_write": "cache writes", "output": "output"}

# Values in inputs.json, in the order they are passed, with the vidbyte-cli flag each one
# becomes and the most values this skill passes for it.
FLAG_KINDS = OrderedDict(
    [
        ("outcomes", ("--outcomes", 7)),
        ("mistakes", ("--mistakes", 8)),
        ("context", ("--context", 4)),
        ("capability", ("--capability", 3)),
        ("success", ("--success", 3)),
        ("constraint", ("--constraint", 3)),
        ("avoid", ("--avoid", 3)),
    ]
)
TAGGED_KINDS = ("outcomes", "mistakes", "context")  # The others are the user's own words.
MAX_VALUE_CHARS = 500
MAX_GOAL_CHARS = 700
MAX_ARGV_CHARS = 24_000  # Windows refuses command lines over 32,767 characters.
TAG_RE = re.compile(r"^\[((?:S\d+)(?:[ ,]+S\d+)*|all)\]\s+\S")
KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
LIMIT_RE = re.compile(r"hit your (?:\w+ )?limit|usage limit|rate limit|quota exceeded", re.I)
INTERACTIVE_ORIGINS = {"terminal", "IDE", "desktop app"}
ORIGIN_TEXT = {"terminal": "a person, in the terminal", "IDE": "a person, in an IDE", "desktop app": "a person, in the desktop app"}


# --------------------------------------------------------------------------------------------
# Redaction (the same rules as the pick-up-where-my-sessions-left-off skill)


class Redactor:
    """Removes secrets, emails, and home paths from any text before it is written or sent."""

    TOKEN_RULES = [
        ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED private key]"),
        ("token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
        ("token", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
        ("token", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"), "[REDACTED]"),
        ("token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
        ("token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
        ("token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
        ("token", re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
        ("token", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED]"),
        ("token", re.compile(r"\bAIza[0-9A-Za-z_-]{35}"), "[REDACTED]"),
        ("token", re.compile(r"\bvb_(?:live|test)_[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
        ("token", re.compile(r"\b0x[a-fA-F0-9]{64}\b"), "[REDACTED]"),
        ("token", re.compile(r"(?i)\b(bearer|basic)\s+(?!\[REDACTED)[A-Za-z0-9._~+/=-]{8,}"), r"\1 [REDACTED]"),
        ("token", re.compile(r"(?i)\b((?:proxy-)?authorization|x-api-key|api-key)([\"']?\s*[:=]\s*[\"']?)(?!\[REDACTED|bearer|basic)[^\s\"',;]{6,}"), r"\1\2[REDACTED]"),
        ("token", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@\"']+:[^/\s@\"']+@"), r"\1[REDACTED]@"),
        # .env-style lines: a whole line that is NAME=value, optionally quoted or exported.
        ("env_value", re.compile(r"(?m)^(\s*(?:export\s+)?[A-Z][A-Z0-9_]{2,}\s*=\s*)(?!\[REDACTED)(?:\"[^\"\n]*\"|'[^'\n]*'|\S+)[ \t]*$"), r"\1[REDACTED]"),
    ]
    # Any assignment whose name says it holds a secret: FOO_TOKEN=..., "api_key": "...", ?token=...
    SECRET_ASSIGN = re.compile(
        r"(?i)(?P<q1>[\"']?)\b(?P<name>[A-Za-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|passwd|pwd|credential|private[_-]?key|access[_-]?key|auth)[A-Za-z0-9_.-]*)(?P=q1)"
        r"(?P<sep>\s*[:=]\s*)(?P<q2>[\"']?)(?!\[REDACTED)(?P<value>[^\s\"',;{}\[\]]{4,})"
    )
    # Login names that are also ordinary words; replacing them everywhere would garble the text.
    GENERIC_LOGINS = {"user", "users", "admin", "root", "test", "dev", "home", "guest", "ubuntu", "runner", "vscode", "codespace", "pi", "build", "app", "node", "owner"}
    EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
    HOME_RULES = [
        re.compile(r"(?i)\b[A-Z]:(?:\\\\|\\|/)+Users(?:\\\\|\\|/)+[^\\/\s\"'<>|:*?`]+"),
        re.compile(r"(?i)(?<![A-Za-z0-9])/[a-z]/Users/[^/\s\"'`]+"),
        re.compile(r"(?<![A-Za-z0-9~])/Users/[^/\s\"'`]+"),
        re.compile(r"(?<![A-Za-z0-9~])/home/[^/\s\"'`]+"),
    ]

    def __init__(self, home: Path, detag: bool = False) -> None:
        # detag is for transcript text only: "S3 implementer" in a prompt or subagent name would
        # read as a session tag, so it becomes "S-3 implementer". Generated values keep their tags.
        self.detag = detag
        self.counts: Counter = Counter()
        spellings = set()
        raw = str(home)
        for value in (raw, raw.replace("\\", "/"), raw.replace("\\", "\\\\"), raw.replace("/", "\\")):
            if len(value) > 3:
                spellings.add(value)
        match = re.match(r"^([A-Za-z]):[\\/](.*)$", raw)
        if match:  # Git Bash and MSYS spell C:\Users\me as /c/Users/me.
            spellings.add("/" + match.group(1).lower() + "/" + match.group(2).replace("\\", "/"))
        if len(raw) > 3:  # Claude Code names project folders after the cwd: C--Users-me-repo.
            spellings.add(re.sub(r"[^A-Za-z0-9]", "-", raw))
        ordered = sorted(spellings, key=len, reverse=True)
        flags = re.I if os.name == "nt" else 0
        self.home_re = re.compile("|".join(re.escape(s) for s in ordered), flags) if ordered else None
        login = home.name
        self.login_re = None
        if len(login) >= 3 and login.lower() not in self.GENERIC_LOGINS:
            self.login_re = re.compile(r"(?<![A-Za-z0-9])" + re.escape(login) + r"(?![A-Za-z0-9])", re.I)

    def __call__(self, text: object) -> str:
        if text is None:
            return ""
        out = str(text)
        for kind, pattern, replacement in self.TOKEN_RULES:
            out, hits = pattern.subn(replacement, out)
            if hits:
                self.counts[kind] += hits
        out = self.SECRET_ASSIGN.sub(self._secret, out)
        out = self.EMAIL_RE.sub(self._email, out)
        if self.home_re is not None:
            out, hits = self.home_re.subn("~", out)
            self.counts["home_path"] += hits
        for pattern in self.HOME_RULES:
            out, hits = pattern.subn("~", out)
            self.counts["home_path"] += hits
        if self.login_re is not None:
            out, hits = self.login_re.subn("[user]", out)
            self.counts["login_name"] += hits
        if self.detag:
            out = re.sub(r"\bS(\d{1,3})\b", r"S-\1", out)
            # Session and thread ids in paths identify transcripts and say nothing about tokens.
            out, hits = re.subn(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "[id]", out)
            self.counts["id"] += hits
        return out

    def _secret(self, match: re.Match) -> str:
        name, value = match.group("name"), match.group("value")
        plain = not match.group("q1") and not match.group("q2") and not re.search(r"[_-]", name) and not name.isupper()
        # Prose such as "auth: flow" keeps its word; "password: hunter2" or a long value does not.
        if ":" in match.group("sep") and plain and not (re.search(r"\d", value) or len(value) >= 12):
            return match.group(0)
        self.counts["env_value"] += 1
        return "%s%s%s%s%s[REDACTED]" % (match.group("q1"), name, match.group("q1"), match.group("sep"), match.group("q2"))

    def _email(self, match: re.Match) -> str:
        if match.group(1).lower() == "git":  # git@github.com:owner/repo is an address, not a person.
            return match.group(0)
        self.counts["email"] += 1
        return "[email]"


# --------------------------------------------------------------------------------------------
# Small helpers


def parse_ts(value: object):
    """Returns an aware UTC datetime for ISO strings or epoch numbers, or None."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000.0 if value > 1e11 else float(value)
            return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc)
        text = str(value).strip()
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


def iter_jsonl(path: Path, stats: Counter):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    stats["unreadable_lines"] += 1
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        stats["unreadable_files"] += 1


def one_line(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)].rstrip() + "…"


def norm_path(value: str) -> str:
    text = str(value)
    if text.lower().startswith("file:///"):
        text = text[8:] if re.match(r"^file:///[A-Za-z]:", text) else text[7:]
    return os.path.normcase(os.path.abspath(os.path.expanduser(text)))


def inside(path: str, folder: str) -> bool:
    p, f = norm_path(path), norm_path(folder)
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def part_text(content: object) -> str:
    """Flattens message content (a string or a list of typed parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    chunks.append(part["text"])
                elif isinstance(part.get("content"), (str, list)):
                    chunks.append(part_text(part["content"]))
            elif isinstance(part, str):
                chunks.append(part)
        return "\n".join(chunks)
    return ""


def display_path(target: str, cwd: str) -> str:
    text = str(target)
    if cwd:
        try:
            rel = os.path.relpath(text, cwd)
            if not rel.startswith(".."):
                return rel.replace("\\", "/")
        except ValueError:
            pass
    return text.replace("\\", "/")


def fmt(n: float) -> str:
    """Short token counts: 3.41B, 708M, 70.8M, 7.08K, 512."""
    n = float(n)
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= size:
            return "%s%s" % (("%.3g" % (n / size)), unit)
    return "%d" % n


def pct(part: float, whole: float) -> str:
    if not whole:
        return "0%"
    value = 100.0 * part / whole
    return ("%.1f%%" % value) if value < 10 else ("%.0f%%" % value)


def as_int(value: object) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def local(stamp) -> str:
    return stamp.astimezone().strftime("%b %d %H:%M") if stamp else "?"


def duration(first, last) -> str:
    if not first or not last:
        return "?"
    hours = (last - first).total_seconds() / 3600.0
    return ("%.0f min" % (hours * 60)) if hours < 1 else ("%.1f h" % hours)


def tags_text(tags) -> str:
    return " ".join(sorted(set(tags), key=lambda t: int(t[1:])))


def many(count: int, word: str) -> str:
    """'1 request', '2 requests'."""
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def write_lf(path: Path, text: str) -> None:
    """Writes UTF-8 text with LF line endings on every system (Path.write_text has no newline= before 3.10)."""
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def usage_error(message: str) -> int:
    print(json.dumps({"error": message}), file=sys.stdout)
    print("Error: " + message, file=sys.stderr)
    return 2


# --------------------------------------------------------------------------------------------
# Session model


class Request:
    """One model request with its token usage, as the transcript recorded it."""

    __slots__ = ("ts", "model", "fresh", "cache_read", "cache_write", "output", "ctx", "thread", "segment", "pos", "turn", "calls")

    def __init__(self, ts, model, fresh, cache_read, cache_write, output, ctx, thread, segment) -> None:
        self.ts, self.model, self.thread, self.segment = ts, model or "unknown", thread, segment
        self.fresh, self.cache_read, self.cache_write, self.output, self.ctx = fresh, cache_read, cache_write, output, ctx
        self.pos, self.turn, self.calls = 0, 0, 0

    def total(self) -> int:
        return self.fresh + self.cache_read + self.cache_write + self.output


class Call:
    """One tool call: what was called, how large its result was, and which request made it."""

    __slots__ = ("ts", "name", "key", "desc", "chars", "error", "whole_file", "thread", "segment", "req")

    def __init__(self, ts, name, key, desc, whole_file, thread, segment, req) -> None:
        self.ts, self.name, self.key, self.desc, self.whole_file = ts, name, key, desc, whole_file
        self.thread, self.segment, self.req = thread, segment, req
        self.chars, self.error = 0, False


class Session:
    def __init__(self, tool: str, session_id: str, path: Path, home_dir: Path) -> None:
        self.tool = tool
        self.session_id = session_id
        self.path = path
        self.home_dir = home_dir  # The Claude config folder or CODEX_HOME the session lives in.
        self.origin = "terminal"
        self.title = ""
        self.ai_title = ""
        self.cwds: list = []
        self.prompts: list = []  # (ts, redacted text, raw length, segment)
        self.requests: list = []
        self.calls: list = []
        self.compactions: list = []  # (ts, "auto" or "manual", tokens before, thread)
        self.limit_hits = 0
        self.subagents: "OrderedDict[str, str]" = OrderedDict()  # thread -> description
        self.extra_files = 0
        self.parent_id = ""
        self.window_size = 0
        self.skip_reason = ""
        self.tag = ""
        self.stats: dict = {}

    def add_cwd(self, value: object) -> None:
        if isinstance(value, str) and value and value not in self.cwds:
            self.cwds.append(value)

    def add_prompt(self, ts, text: str, raw_len: int, segment: int) -> None:
        text = text.strip()
        if text and (not self.prompts or self.prompts[-1][1] != text):
            self.prompts.append((ts, text, raw_len, segment))

    @property
    def cwd(self) -> str:
        return self.cwds[0] if self.cwds else ""

    def display_title(self) -> str:
        if self.title or self.ai_title:
            return self.title or self.ai_title
        return one_line(self.prompts[0][1], 60) if self.prompts else "(untitled)"

    def tool_name(self) -> str:
        return "Claude Code" if self.tool == "claude-code" else "Codex"

    def first_ts(self):
        return min((r.ts for r in self.requests if r.ts), default=None)

    def last_ts(self):
        return max((r.ts for r in self.requests if r.ts), default=None)


def call_key(name: str, payload: object) -> str:
    text = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1((name + "\x00" + text).encode("utf-8", "replace")).hexdigest()[:16]


WHOLE_READ_RE = re.compile(r"^\s*(?:cat|type|Get-Content|gc|more)\s+(?!.*(?:-TotalCount|-Head|-Tail|-First|-Last|\|))", re.I)


def project_label(cwd: str, home: Path, redact: Redactor) -> str:
    if not cwd:
        return "(unknown folder)"
    if norm_path(cwd) == norm_path(str(home)):
        return "~"
    name = re.split(r"[\\/]", cwd.rstrip("\\/"))[-1] or cwd
    return redact(name) or "(unknown folder)"


# --------------------------------------------------------------------------------------------
# Claude Code transcripts

CLAUDE_INJECTED = (
    "<task-notification", "<local-command", "<system-reminder", "<bash-input", "<bash-stdout",
    "<bash-stderr", "<user-memory-input", "Stop hook feedback", "[Request interrupted",
    "This session is being continued", "Another Claude session", "[Cross-session", "Caveat:",
    "Base directory for this skill",
)
CLAUDE_BUILTIN_COMMANDS = {
    "/clear", "/compact", "/model", "/effort", "/mode", "/usage", "/cost", "/status", "/config",
    "/help", "/exit", "/quit", "/login", "/logout", "/resume", "/context", "/permissions",
    "/memory", "/doctor", "/theme", "/vim", "/fast", "/plugin", "/mcp", "/agents", "/hooks",
    "/ide", "/rename", "/export", "/goal", "/output-style", "/statusline",
}


def claude_prompt(record: dict, text: str):
    """Returns the human prompt text of a user record, or None for injected text."""
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
        if not arg_text and command in CLAUDE_BUILTIN_COMMANDS:
            return None
        return (command + " " + arg_text).strip()
    if human:
        return text
    if stripped.startswith(CLAUDE_INJECTED) or (stripped.startswith("<") and not stripped.startswith("<pasted")):
        return None
    return text


def claude_origin(entrypoint: str) -> str:
    entry = entrypoint.lower()
    if not entry or entry == "cli":
        return "terminal"
    if entry.startswith("sdk"):
        return "program (Claude Agent SDK)"
    if any(word in entry for word in ("vscode", "jetbrains", "ide", "cursor")):
        return "IDE"
    if "desktop" in entry or "web" in entry:
        return "desktop app"
    return "terminal"


def describe_claude(name: str, tool_input: dict, cwd: str) -> str:
    def path_of(*keys):
        for key in keys:
            if isinstance(tool_input.get(key), str) and tool_input[key]:
                return display_path(tool_input[key], cwd)
        return ""

    if name == "Read":
        target = path_of("file_path", "path")
        if tool_input.get("offset") or tool_input.get("limit") or tool_input.get("pages"):
            start = as_int(tool_input.get("offset")) or 1
            return "Read lines %d-%d: %s" % (start, start + as_int(tool_input.get("limit")), target)
        return "Read whole file: %s" % target
    if isinstance(tool_input.get("command"), str):
        return "%s: %s" % (name, one_line(tool_input["command"], 110))
    if name in ("Grep", "Glob"):
        where = path_of("path")
        return '%s "%s"%s' % (name, one_line(tool_input.get("pattern", ""), 60), (" in " + where) if where else "")
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "%s %s" % (name, path_of("file_path", "notebook_path"))
    if name in ("Agent", "Task"):
        return "%s: %s" % (name, one_line(tool_input.get("description") or tool_input.get("subagent_type") or "", 80))
    if name == "WebFetch":
        return "WebFetch %s" % one_line(tool_input.get("url", ""), 90)
    if name == "WebSearch":
        return 'WebSearch "%s"' % one_line(tool_input.get("query", ""), 80)
    for value in tool_input.values():
        if isinstance(value, str) and value.strip():
            return "%s %s" % (name, one_line(value, 70))
    return name


def result_chars(content: object) -> int:
    if isinstance(content, str):
        return len(content)
    total = 0
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "image":
                    total += IMAGE_CHARS
                elif isinstance(part.get("text"), str):
                    total += len(part["text"])
                elif isinstance(part.get("content"), (str, list)):
                    total += result_chars(part["content"])
            elif isinstance(part, str):
                total += len(part)
    return total


def parse_claude(path: Path, config_dir: Path, since, stats: Counter, redact: Redactor, session=None, thread: str = "main") -> Session:
    """Reads one Claude Code transcript. A subagent transcript is read into its parent session."""
    is_sub = session is not None
    if session is None:
        session = Session("claude-code", path.stem, path, config_dir)
        session.stats["entrypoint"] = ""
    seen: dict = {}  # message id -> Request; Claude Code writes one record per content block.
    pending: dict = {}
    segments: Counter = Counter()
    last_ts = None
    for record in iter_jsonl(path, stats):
        kind = record.get("type")
        if not is_sub and kind == "custom-title" and record.get("customTitle"):
            session.title = redact(record["customTitle"])
            continue
        if not is_sub and kind == "ai-title" and record.get("aiTitle"):
            session.ai_title = redact(record["aiTitle"])
            continue
        if kind not in ("user", "assistant", "system"):
            continue
        stamp = parse_ts(record.get("timestamp")) or last_ts
        last_ts = stamp
        if not is_sub and record.get("entrypoint") and not session.stats["entrypoint"]:
            session.stats["entrypoint"] = str(record["entrypoint"])
        rec_thread = "sidechain" if (not is_sub and record.get("isSidechain")) else thread
        if rec_thread == "sidechain":
            session.subagents.setdefault("sidechain", "subagent records kept in the main transcript")
        if kind == "system":
            if record.get("subtype") == "compact_boundary":
                segments[rec_thread] += 1
                meta = record.get("compactMetadata") if isinstance(record.get("compactMetadata"), dict) else {}
                if stamp is None or stamp >= since:
                    trigger = "manual" if str(meta.get("trigger", "auto")).lower() == "manual" else "auto"
                    session.compactions.append((stamp, trigger, as_int(meta.get("preTokens")), rec_thread))
            continue
        if not is_sub:
            session.add_cwd(record.get("cwd"))
        if stamp is not None and stamp < since:
            continue
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        if kind == "assistant":
            if record.get("isApiErrorMessage"):
                if LIMIT_RE.search(part_text(content)):
                    session.limit_hits += 1
                continue
            usage = message.get("usage")
            model = str(message.get("model") or "")
            if not isinstance(usage, dict) or model == "<synthetic>":
                continue
            key = message.get("id") or record.get("requestId") or record.get("uuid")
            request = seen.get(key)
            fresh = as_int(usage.get("input_tokens"))
            cache_read = as_int(usage.get("cache_read_input_tokens"))
            cache_write = as_int(usage.get("cache_creation_input_tokens"))
            output = as_int(usage.get("output_tokens"))
            if request is None:
                request = Request(stamp, model, fresh, cache_read, cache_write, output, fresh + cache_read + cache_write, rec_thread, segments[rec_thread])
                session.requests.append(request)
                seen[key] = request
            else:  # Later blocks of the same response repeat its usage; keep the largest.
                request.fresh, request.cache_read = max(request.fresh, fresh), max(request.cache_read, cache_read)
                request.cache_write, request.output = max(request.cache_write, cache_write), max(request.output, output)
                request.ctx = request.fresh + request.cache_read + request.cache_write
            for part in content if isinstance(content, list) else []:
                if isinstance(part, dict) and part.get("type") == "tool_use":
                    name = str(part.get("name") or "tool")
                    tool_input = part.get("input") if isinstance(part.get("input"), dict) else {}
                    whole = name == "Read" and not (tool_input.get("offset") or tool_input.get("limit") or tool_input.get("pages"))
                    call = Call(stamp, name, call_key(name, tool_input), redact(describe_claude(name, tool_input, session.cwd)), whole, rec_thread, segments[rec_thread], request)
                    session.calls.append(call)
                    pending[part.get("id")] = call
            continue
        if kind != "user":
            continue
        if isinstance(content, list):
            results = [p for p in content if isinstance(p, dict) and p.get("type") == "tool_result"]
            for result in results:
                call = pending.pop(result.get("tool_use_id"), None)
                if call is not None:
                    call.chars = result_chars(result.get("content"))
                    call.error = bool(result.get("is_error"))
            if results:
                continue
        if is_sub or rec_thread != "main" or record.get("isCompactSummary"):
            continue
        text = part_text(content)
        prompt = claude_prompt(record, text)
        if prompt is None or prompt.lstrip().startswith("[Request interrupted"):
            continue
        session.add_prompt(stamp, redact(prompt), len(prompt), segments["main"])
    if not is_sub:
        session.origin = claude_origin(session.stats.get("entrypoint", ""))
    return session


def read_claude_subagents(session: Session, since_epoch: float, since, stats: Counter, redact: Redactor) -> None:
    folder = session.path.with_suffix("") / "subagents"
    if not folder.is_dir():
        return
    for sub in sorted(folder.glob("*.jsonl")):
        try:
            if sub.stat().st_mtime < since_epoch:
                continue
        except OSError:
            continue
        description = ""
        meta_path = sub.with_name(sub.stem + ".meta.json")
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8", errors="replace"))
                description = str(meta.get("description") or meta.get("agentType") or "")
            except (OSError, ValueError):
                pass
        session.subagents[sub.stem] = redact(one_line(description, 80)) or "a subagent"
        session.extra_files += 1
        parse_claude(sub, session.home_dir, since, stats, redact, session=session, thread=sub.stem)


# --------------------------------------------------------------------------------------------
# Codex rollouts


def codex_titles(codex_home: Path, stats: Counter) -> dict:
    titles: dict = {}
    index = codex_home / "session_index.jsonl"
    if index.is_file():
        for record in iter_jsonl(index, stats):
            if record.get("id") and record.get("thread_name"):
                titles[str(record["id"])] = str(record["thread_name"])
    return titles


def codex_origin(payload: dict) -> str:
    originator = str(payload.get("originator", "")).lower()
    source = payload.get("source")
    if (isinstance(source, dict) and "subagent" in source) or payload.get("thread_source") == "subagent":
        return "subagent"
    if payload.get("thread_source") == "automation":
        return "program (Codex automation)"
    if "sdk" in originator:
        return "program (Codex SDK)"
    if "exec" in originator:
        return "program (codex exec)"
    if "desktop" in originator or "vscode" in originator:
        return "desktop app"
    return "terminal"


CMD_RE = re.compile(r"""\bcmd\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|`([^`]*)`)""")


def codex_commands(name: str, raw: str) -> list:
    """The shell commands inside one Codex tool call, for display and whole-file detection."""
    found = []
    for match in CMD_RE.finditer(raw):
        text = next(g for g in match.groups() if g is not None)
        try:
            text = json.loads('"%s"' % text.replace('"', '\\"').replace('\\\\"', '\\"'))
        except ValueError:
            pass
        found.append(text)
    if found:
        return found
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if isinstance(data, dict):
        command = data.get("command") or data.get("cmd")
        if isinstance(command, list) and command:
            parts = [str(c) for c in command]
            if len(parts) >= 3 and parts[-2] in ("-Command", "-c", "-lc", "/c"):
                return [parts[-1]]
            return [" ".join(parts)]
        if isinstance(command, str):
            return [command]
    return []


def codex_output(output: object):
    """Returns (characters the model saw, whether the call failed) for one Codex tool output."""
    text, failed = "", False
    if isinstance(output, list):
        text = part_text(output)
    elif isinstance(output, dict):
        text = str(output.get("output") or output.get("content") or "")
        meta = output.get("metadata") if isinstance(output.get("metadata"), dict) else {}
        failed = as_int(meta.get("exit_code")) != 0
    elif isinstance(output, str):
        text = output
        try:
            data = json.loads(output)
        except ValueError:
            data = None
        if isinstance(data, dict) and "output" in data:
            text = str(data.get("output") or "")
            meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            failed = as_int(meta.get("exit_code")) != 0
    if re.search(r'"exit_code"\s*:\s*"?[1-9]|Exit code:? [1-9]|Process exited with code [1-9]', text):
        failed = True
    return len(text), failed


def parse_codex(path: Path, codex_home: Path, since, titles: dict, stats: Counter, redact: Redactor):
    session = None
    model = ""
    window = 0
    segment = 0
    last_ts = None
    seen_responses: set = set()
    seen_items: set = set()
    record_requests: list = []  # From token_usage_record (Codex 0.160+), one per response_id.
    count_requests: list = []  # From token_count last_token_usage (every version).
    previous_total = None
    pending: dict = {}
    for record in iter_jsonl(path, stats):
        kind = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if kind == "session_meta" and session is None:
            session_id = str(payload.get("id") or payload.get("session_id") or path.stem)
            session = Session("codex", session_id, path, codex_home)
            session.origin = codex_origin(payload)
            source = payload.get("source")
            if isinstance(source, dict) and isinstance(source.get("subagent"), dict):
                spawn = source["subagent"].get("thread_spawn") if isinstance(source["subagent"].get("thread_spawn"), dict) else {}
                session.parent_id = str(spawn.get("parent_thread_id") or "")
                session.stats["agent"] = redact(one_line("%s %s" % (spawn.get("agent_nickname") or "", spawn.get("agent_path") or ""), 80))
            window = as_int(payload.get("context_window"))
            session.add_cwd(payload.get("cwd"))
            if session_id in titles:
                session.title = redact(titles[session_id])
            continue
        if session is None:
            return None
        stamp = parse_ts(record.get("timestamp")) or last_ts
        last_ts = stamp
        in_window = stamp is None or stamp >= since
        if kind == "turn_context":
            model = str(payload.get("model") or model)
            session.add_cwd(payload.get("cwd"))
            continue
        if kind == "compacted":
            segment += 1
            if in_window:  # Codex does not record whether a compaction was automatic or /compact.
                before = max((r.ctx for r in (record_requests or count_requests)[-1:]), default=0)
                session.compactions.append((stamp, "unmarked", before, "main"))
            continue
        if kind == "token_usage_record":
            response = payload.get("response_id") or "%s:%s" % (payload.get("turn_id"), record.get("ordinal"))
            if response in seen_responses:
                continue
            seen_responses.add(response)
            request = codex_request(stamp, model, payload.get("usage"), segment)
            if request is not None:
                record_requests.append(request)
            continue
        if kind == "response_item":
            ptype = payload.get("type")
            if ptype in ("function_call", "custom_tool_call", "local_shell_call"):
                if not in_window:
                    continue
                name = str(payload.get("name") or ptype)
                raw = payload.get("input") if "input" in payload else payload.get("arguments", payload.get("action", ""))
                raw = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True, default=str)
                commands = codex_commands(name, raw)
                reads = [c for c in commands if WHOLE_READ_RE.match(c)]
                if reads:  # Label a whole-file read by the read, not by the call's first command.
                    commands = reads + [c for c in commands if c not in reads]
                if commands:
                    desc = "%s: %s" % (name, one_line(commands[0], 110)) + (" (+%d more commands)" % (len(commands) - 1) if len(commands) > 1 else "")
                else:
                    desc = "%s %s" % (name, one_line(raw, 70)) if raw and raw not in ("{}", '""') else name
                whole = bool(reads)
                call = Call(stamp, name, call_key(name, raw), redact(desc), whole, "main", segment, None)
                session.calls.append(call)
                pending[payload.get("call_id") or payload.get("id")] = call
            elif ptype in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
                call = pending.pop(payload.get("call_id") or payload.get("id"), None)
                if call is not None:
                    call.chars, call.error = codex_output(payload.get("output"))
            continue
        if kind != "event_msg":
            continue
        event = payload.get("type")
        if event == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else None
            if info is None:
                continue
            window = as_int(info.get("model_context_window")) or window
            total = json.dumps(info.get("total_token_usage"), sort_keys=True)
            if total == previous_total:  # Codex re-emits the last count when a thread resumes.
                continue
            previous_total = total
            request = codex_request(stamp, model, info.get("last_token_usage"), segment)
            if request is not None:
                count_requests.append(request)
        elif event == "user_message" and in_window:
            message = str(payload.get("message", ""))
            session.add_prompt(stamp, redact(message), len(message), segment)
        elif event == "item_completed" and in_window:
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("type") == "UserMessage" and item.get("id") not in seen_items:
                seen_items.add(item.get("id"))
                message = part_text(item.get("content"))
                session.add_prompt(stamp, redact(message), len(message), segment)
        elif event == "error" and in_window:
            if LIMIT_RE.search(str(payload.get("message", ""))):
                session.limit_hits += 1
        elif event == "thread_name_updated" and payload.get("thread_name"):
            session.title = redact(payload["thread_name"])
    if session is None:
        return None
    # token_usage_record counts each response once and includes compaction requests, which
    # token_count misses. Older parts of a file, written before Codex had it, use token_count.
    if record_requests:
        first = min((r.ts for r in record_requests if r.ts), default=None)
        older = [r for r in count_requests if first is not None and r.ts is not None and (first - r.ts).total_seconds() > 2]
        requests = older + record_requests
    else:
        requests = count_requests
    session.requests = [r for r in requests if r.ts is None or r.ts >= since]
    session.requests.sort(key=lambda r: r.ts or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    session.window_size = window
    stamps = [r.ts for r in session.requests]
    for call in session.calls:  # A call belongs to the response that emitted it: the next request record.
        if call.ts is None or not stamps:
            continue
        index = bisect.bisect_left(stamps, call.ts)
        call.req = session.requests[min(index, len(stamps) - 1)]
    return session


def codex_request(stamp, model: str, usage: object, segment: int):
    if not isinstance(usage, dict):
        return None
    total_input = as_int(usage.get("input_tokens"))
    cached = as_int(usage.get("cached_input_tokens"))
    written = as_int(usage.get("cache_write_input_tokens"))
    output = as_int(usage.get("output_tokens"))  # Includes reasoning tokens.
    if not (total_input or output):
        return None
    fresh = max(total_input - cached - written, 0)  # Codex input_tokens already holds the cached part.
    return Request(stamp, model, fresh, cached, written, output, total_input, "main", segment)


def attach_codex_child(parent: Session, child: Session) -> None:
    thread = child.session_id
    parent.subagents[thread] = child.stats.get("agent") or "a subagent thread"
    parent.extra_files += 1
    for request in child.requests:
        request.thread = thread
        parent.requests.append(request)
    for call in child.calls:
        call.thread = thread
        parent.calls.append(call)
    for stamp, trigger, before, _thread in child.compactions:
        parent.compactions.append((stamp, trigger, before, thread))
    parent.limit_hits += child.limit_hits


# --------------------------------------------------------------------------------------------
# Analysis


def analyze(session: Session) -> dict:
    """Totals, context, turns, and the measured patterns of one session."""
    requests = sorted(session.requests, key=lambda r: r.ts or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    groups: dict = defaultdict(list)
    for request in requests:
        groups[(request.thread, request.segment)].append(request)
    sizes = {key: len(members) for key, members in groups.items()}
    stamps_by_group = {key: [r.ts for r in members] for key, members in groups.items()}
    for members in groups.values():
        for index, request in enumerate(members):
            request.pos = index

    def later(request) -> int:  # Requests that re-send what this request added.
        return sizes[(request.thread, request.segment)] - request.pos - 1 if request is not None else 0

    tokens = {kind: sum(getattr(r, kind) for r in requests) for kind in KINDS}
    tokens["total"] = sum(tokens[k] for k in KINDS)
    models: dict = defaultdict(lambda: {k: 0 for k in KINDS + ("total",)})
    for request in requests:
        entry = models[request.model]
        for kind in KINDS:
            entry[kind] += getattr(request, kind)
        entry["total"] += request.total()
    main = [r for r in requests if r.thread == "main"]
    subs = [r for r in requests if r.thread != "main"]
    sub_kinds = {kind: sum(getattr(r, kind) for r in subs) for kind in KINDS}
    sub_tokens = sum(sub_kinds.values())
    per_sub: Counter = Counter()
    for request in subs:
        per_sub[request.thread] += request.total()

    # Turns: one human prompt and everything until the next one, subagent requests included.
    prompts = sorted([p for p in session.prompts if p[0] is not None], key=lambda p: p[0])
    prompt_stamps = [p[0] for p in prompts]
    turns = [{"index": i, "prompt": (prompts[i - 1] if i else None), "tokens": 0, "requests": 0, "sub_tokens": 0, "ctx_sum": 0, "ctx_n": 0, "calls": []} for i in range(len(prompts) + 1)]
    for request in requests:
        request.turn = bisect.bisect_right(prompt_stamps, request.ts) if request.ts else 0
        turn = turns[request.turn]
        turn["tokens"] += request.total()
        turn["requests"] += 1
        if request.thread != "main":
            turn["sub_tokens"] += request.total()
        else:
            turn["ctx_sum"] += request.ctx
            turn["ctx_n"] += 1

    calls_per_request: Counter = Counter(id(c.req) for c in session.calls if c.req is not None)
    for call in session.calls:
        if call.req is not None:
            turns[call.req.turn]["calls"].append(call)

    large = []
    for call in session.calls:
        if call.chars >= LARGE_RESULT_CHARS:
            approx = call.chars // CHARS_PER_TOKEN
            large.append({"call": call, "approx": approx, "later": later(call.req), "carried": approx * later(call.req)})
    large.sort(key=lambda e: e["carried"], reverse=True)

    by_key: dict = defaultdict(list)
    for call in session.calls:
        by_key[(call.thread, call.key)].append(call)
    repeats = []
    for members in by_key.values():
        if len(members) < REPEAT_MIN:
            continue
        cost = sum(c.req.total() / max(calls_per_request[id(c.req)], 1) for c in members[1:] if c.req is not None)
        repeats.append({"desc": members[0].desc, "count": len(members), "failed": sum(1 for c in members if c.error), "tokens": int(cost), "calls": members})
    repeats.sort(key=lambda e: e["tokens"], reverse=True)

    long_prompts = []
    for stamp, text, raw_len, segment in prompts:
        if raw_len >= LONG_PROMPT_CHARS:
            key = ("main", segment)
            after = len(stamps_by_group.get(key, [])) - bisect.bisect_right(stamps_by_group.get(key, []), stamp)
            approx = raw_len // CHARS_PER_TOKEN
            long_prompts.append({"approx": approx, "later": after, "carried": approx * after, "text": text})

    compaction_turns: Counter = Counter()
    for stamp, trigger, _before, thread in session.compactions:
        if thread == "main" and stamp is not None:
            compaction_turns[bisect.bisect_right(prompt_stamps, stamp)] += 1

    for turn in turns:
        turn["avg_ctx"] = turn["ctx_sum"] // turn["ctx_n"] if turn["ctx_n"] else 0
        turn["large"] = [e for e in large if e["call"].req is not None and e["call"].req.turn == turn["index"]]
        turn["repeat_extra"] = sum(sum(1 for c in r["calls"][1:] if c.req is not None and c.req.turn == turn["index"]) for r in repeats)
        turn["compactions"] = compaction_turns.get(turn["index"], 0)
        turn["long_prompt"] = turn["prompt"][2] // CHARS_PER_TOKEN if turn["prompt"] and turn["prompt"][2] >= LONG_PROMPT_CHARS else 0
    costly = sorted([t for t in turns if t["tokens"]], key=lambda t: t["tokens"], reverse=True)

    main_compactions = [c for c in session.compactions if c[3] == "main"]
    auto = sum(1 for c in main_compactions if c[1] == "auto")
    manual = sum(1 for c in main_compactions if c[1] == "manual")
    unmarked = sum(1 for c in main_compactions if c[1] == "unmarked")
    sub_compactions = len(session.compactions) - len(main_compactions)
    sub_auto = sum(1 for c in session.compactions if c[3] != "main" and c[1] in ("auto", "unmarked"))
    whole = [e for e in large if e["call"].whole_file]
    other = [e for e in large if not e["call"].whole_file]
    over_line = sum(max(r.ctx - CONTEXT_LINE, 0) for r in requests)
    top_model = max(models.items(), key=lambda item: item[1]["total"])[0] if models else ""
    return {
        "tokens": tokens,
        "models": {m: dict(v) for m, v in sorted(models.items(), key=lambda item: item[1]["total"], reverse=True)},
        "top_model": top_model,
        "requests_main": len(main),
        "requests_sub": len(subs),
        "avg_ctx": (sum(r.ctx for r in main) // len(main)) if main else 0,
        "peak_ctx": max((r.ctx for r in main), default=0),
        "auto_compactions": auto,
        "manual_compactions": manual,
        "unmarked_compactions": unmarked,
        "sub_compactions": sub_compactions,
        "limit_compactions": auto + unmarked + sub_auto,
        "sub_tokens": sub_tokens,
        "sub_kinds": sub_kinds,
        "per_sub": per_sub,
        "turns": costly,
        "prompt_count": len(prompts),
        "large": large,
        "whole": whole,
        "other_large": other,
        "repeats": repeats,
        "long_prompts": long_prompts,
        "patterns": {
            "whole_file_reads": {"count": len(whole), "tokens": sum(e["carried"] for e in whole), "largest": max((e["approx"] for e in whole), default=0)},
            "long_tool_output": {"count": len(other), "tokens": sum(e["carried"] for e in other), "largest": max((e["approx"] for e in other), default=0)},
            "repeated_calls": {"groups": len(repeats), "extra_calls": sum(r["count"] - 1 for r in repeats), "failed": sum(r["failed"] for r in repeats), "tokens": sum(r["tokens"] for r in repeats)},
            "context_over_100k": {"tokens": over_line, "compactions": auto + unmarked + sub_auto},
            "long_prompts": {"count": len(long_prompts), "tokens": sum(e["carried"] for e in long_prompts), "largest": max((e["approx"] for e in long_prompts), default=0)},
            "subagents": {"count": len(per_sub), "tokens": sub_tokens},
        },
    }


# --------------------------------------------------------------------------------------------
# Gather


def claude_default_dir(home: Path) -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else home / ".claude"


def codex_default_home(home: Path) -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else home / ".codex"


def recent_files(root: Path, since_epoch: float, pattern: str):
    if not root.is_dir():
        return []
    found = []
    for candidate in root.glob(pattern):
        try:
            if candidate.is_file() and candidate.stat().st_mtime >= since_epoch:
                found.append(candidate)
        except OSError:
            continue
    return sorted(found)


def window_start(args, now):
    if args.since:
        stamp = None
        for form in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                stamp = dt.datetime.strptime(args.since, form)
                break
            except ValueError:
                continue
        if stamp is None:
            raise SystemExit(usage_error("--since must look like 2026-10-04 or 2026-10-04T09:00 (local time)."))
        return stamp.astimezone(dt.timezone.utc)  # A naive value is read as local time.
    if args.days <= 0:
        raise SystemExit(usage_error("--days must be greater than 0."))
    return now - dt.timedelta(days=args.days)


def window_words(args, since) -> str:
    if args.since:
        return "since %s" % local(since)
    if args.days == 1:
        return "in the last 24 hours"
    return "in the last %g days" % args.days


def cmd_gather(args) -> int:
    home = Path.home()
    now = dt.datetime.now(dt.timezone.utc)
    since = window_start(args, now)
    since_epoch = since.timestamp()
    if not 1 <= args.top <= MAX_TOP:
        return usage_error("--top must be between 1 and %d." % MAX_TOP)
    redact = Redactor(home)  # For values this script writes; they carry session tags.
    clean = Redactor(home, detag=True)  # For transcript text: prompts, titles, paths, commands.
    stats: Counter = Counter()

    claude_dirs = [] if args.no_claude else [claude_default_dir(home)] + [Path(d).expanduser() for d in args.claude_dir]
    claude_dirs = list(OrderedDict((norm_path(str(d)), d) for d in claude_dirs).values())
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else codex_default_home(home)
    exclude = set(args.exclude_session)
    for variable in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
        if os.environ.get(variable) and not args.include_current:
            exclude.add(os.environ[variable])

    sessions = []
    for config_dir in claude_dirs:
        for path in recent_files(config_dir / "projects", since_epoch, "*/*.jsonl"):
            session = parse_claude(path, config_dir, since, stats, clean)
            read_claude_subagents(session, since_epoch, since, stats, clean)
            sessions.append(session)
    codex_sessions = []
    if not args.no_codex:
        titles = codex_titles(codex_home, stats)
        paths = recent_files(codex_home / "sessions", since_epoch, "*/*/*/rollout-*.jsonl")
        paths += recent_files(codex_home / "archived_sessions", since_epoch, "rollout-*.jsonl")
        for path in paths:
            session = parse_codex(path, codex_home, since, titles, stats, clean)
            if session is not None:
                codex_sessions.append(session)
    by_id = {s.session_id: s for s in codex_sessions}
    for session in codex_sessions:  # A subagent thread counts inside the thread that spawned it.
        parent = by_id.get(session.parent_id)
        hops = 0
        while parent is not None and parent.parent_id and parent.parent_id in by_id and hops < 8:
            parent, hops = by_id[parent.parent_id], hops + 1
        if parent is not None and parent is not session:
            attach_codex_child(parent, session)
            session.skip_reason = "counted_in_parent"
        sessions.append(session)

    every = []
    for session in sessions:
        if session.skip_reason:
            stats["skipped_" + session.skip_reason] += 1
        elif session.session_id in exclude:
            stats["skipped_current_or_excluded"] += 1
        elif not session.requests:
            stats["skipped_no_usage_in_window"] += 1
        elif args.project_dir and not any(inside(c, d) for c in session.cwds for d in args.project_dir):
            stats["skipped_outside_project_dir"] += 1
        else:
            every.append(session)
    analyses = {id(s): analyze(s) for s in every}

    other_dirs = []
    if not args.no_claude:
        read = {norm_path(str(d)) for d in claude_dirs}
        for candidate in sorted(home.glob(".claude*")):
            if candidate.is_dir() and (candidate / "projects").is_dir() and norm_path(str(candidate)) not in read:
                other_dirs.append(str(candidate))

    summary = {
        "window": {"since": since.isoformat(), "until": now.isoformat(), "since_local": local(since), "until_local": local(now)},
        "read": {"claude_dirs": [str(d) for d in claude_dirs], "codex_home": None if args.no_codex else str(codex_home)},
        "other_claude_dirs_not_read": other_dirs,
        "skipped": {k[len("skipped_"):]: v for k, v in sorted(stats.items()) if k.startswith("skipped_")},
        "unreadable": {k: v for k, v in stats.items() if k.startswith("unreadable")},
    }
    if not every:
        summary["error"] = "no token usage found"
        summary["hint"] = "No Claude Code or Codex request with token usage in this window matched. Widen --days, add --claude-dir, or drop --project-dir."
        print(json.dumps(summary, indent=2))
        return 3

    totals = window_totals(every, analyses)
    candidates = [s for s in every if not args.interactive_only or s.origin in INTERACTIVE_ORIGINS]
    if args.interactive_only:
        summary["skipped"]["started_by_a_program_not_ranked"] = len(every) - len(candidates)
    candidates.sort(key=lambda s: (analyses[id(s)]["tokens"]["total"], s.last_ts() or now), reverse=True)
    top = candidates[: args.top]
    if not top:
        summary["error"] = "no interactive session found"
        summary["hint"] = "Every session in this window was started by a program. Run gather again without --interactive-only."
        print(json.dumps(summary, indent=2))
        return 3
    for number, session in enumerate(top, 1):
        session.tag = "S%d" % number
    top_total = sum(analyses[id(s)]["tokens"]["total"] for s in top)
    labels = {id(s): project_label(s.cwd, home, clean) for s in top}

    out_dir = Path(args.out).expanduser() if args.out else Path(tempfile.gettempdir()) / "cut-token-usage" / now.astimezone().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = render_digest(top, analyses, labels, totals, top_total, since, now, args.max_chars)
    write_lf(out_dir / "digest.md", digest)
    index = build_index(top, analyses, labels, totals, top_total, since, now, home, summary)
    write_lf(out_dir / "usage.json", json.dumps(index, indent=2))
    inputs_path = out_dir / "inputs.json"
    warnings = []
    if inputs_path.exists():
        warnings.append("inputs.json already existed and was left as it is; check its session tags against the new digest.")
    else:
        template = inputs_template(top, analyses, labels, totals, top_total, args, since, now, redact)
        write_lf(inputs_path, json.dumps(template, indent=2))
    if summary["unreadable"]:
        warnings.append("Some transcript lines or files could not be read; their tokens are not counted (see unreadable).")

    summary.update(
        {
            "out_dir": str(out_dir),
            "digest": str(out_dir / "digest.md"),
            "usage_index": str(out_dir / "usage.json"),
            "inputs": str(inputs_path),
            "digest_chars": len(digest),
            "totals": {
                "sessions": totals["sessions"],
                "claude_code": totals["claude_code"],
                "codex": totals["codex"],
                "tokens": totals["tokens"]["total"],
                "tokens_text": fmt(totals["tokens"]["total"]),
                "top_tokens": top_total,
                "top_share": pct(top_total, totals["tokens"]["total"]),
            },
            "redactions_in_digest": digest_markers(digest),
            "table": [
                {
                    "tag": s.tag,
                    "tool": s.tool,
                    "origin": s.origin,
                    "project": labels[id(s)],
                    "title": one_line(s.display_title(), 80),
                    "tokens": analyses[id(s)]["tokens"]["total"],
                    "tokens_text": fmt(analyses[id(s)]["tokens"]["total"]),
                    "share": pct(analyses[id(s)]["tokens"]["total"], totals["tokens"]["total"]),
                }
                for s in top
            ],
            "warnings": warnings,
        }
    )
    print(json.dumps(summary, indent=2))
    return 0


def window_totals(sessions: list, analyses: dict) -> dict:
    tokens = {k: 0 for k in KINDS + ("total",)}
    by_model: dict = defaultdict(lambda: {k: 0 for k in KINDS + ("total",)})
    by_tool: Counter = Counter()
    for session in sessions:
        result = analyses[id(session)]
        for kind in KINDS + ("total",):
            tokens[kind] += result["tokens"][kind]
        for model, values in result["models"].items():
            for kind, value in values.items():
                by_model[model][kind] += value
        by_tool[session.tool] += result["tokens"]["total"]
    return {
        "sessions": len(sessions),
        "claude_code": sum(1 for s in sessions if s.tool == "claude-code"),
        "codex": sum(1 for s in sessions if s.tool == "codex"),
        "tokens": tokens,
        "by_model": OrderedDict(sorted(((m, dict(v)) for m, v in by_model.items()), key=lambda item: item[1]["total"], reverse=True)),
        "by_tool": dict(by_tool),
    }


LEVELS = [
    {"prompts": 2, "prompt": 300, "turns": 3, "turn_prompt": 140, "large": 3, "repeats": 3, "subs": 3},
    {"prompts": 2, "prompt": 200, "turns": 2, "turn_prompt": 100, "large": 2, "repeats": 2, "subs": 2},
    {"prompts": 1, "prompt": 150, "turns": 1, "turn_prompt": 80, "large": 1, "repeats": 1, "subs": 1},
]


def turn_causes(turn: dict) -> list:
    causes = []
    if turn["sub_tokens"] and turn["sub_tokens"] >= 0.3 * turn["tokens"]:
        causes.append("subagents used %s (%s)" % (fmt(turn["sub_tokens"]), pct(turn["sub_tokens"], turn["tokens"])))
    if turn["requests"] >= 50:
        causes.append("%d requests" % turn["requests"])
    if turn["avg_ctx"] >= CONTEXT_LINE:
        causes.append("about %s tokens of conversation re-sent per request" % fmt(turn["avg_ctx"]))
    if turn["large"]:
        causes.append("%d large tool result%s re-sent later (about %s tokens)" % (len(turn["large"]), "" if len(turn["large"]) == 1 else "s", fmt(sum(e["carried"] for e in turn["large"]))))
    if turn["repeat_extra"]:
        causes.append("%d repeated identical tool call%s" % (turn["repeat_extra"], "" if turn["repeat_extra"] == 1 else "s"))
    if turn["compactions"]:
        causes.append("%d compaction%s" % (turn["compactions"], "" if turn["compactions"] == 1 else "s"))
    if turn["long_prompt"]:
        causes.append("a long pasted prompt (about %s tokens)" % fmt(turn["long_prompt"]))
    return causes or ["no single cause stands out"]


def render_session(session: Session, result: dict, label: str, totals: dict, level: dict) -> str:
    t = result["tokens"]
    window_total = totals["tokens"]["total"]
    lines = ['## %s · %s · %s · "%s" · %s tokens (%s of the window)' % (session.tag, session.tool_name(), label, one_line(session.display_title(), 70), fmt(t["total"]), pct(t["total"], window_total))]
    requests = result["requests_main"] + result["requests_sub"]
    first, last = session.first_ts(), session.last_ts()
    req_text = many(requests, "request")
    if result["requests_sub"]:
        req_text += " (%d in the main conversation, %d in %d subagent%s)" % (result["requests_main"], result["requests_sub"], len(result["per_sub"]), "" if len(result["per_sub"]) == 1 else "s")
    lines.append("- Started by: %s. Active %s to %s (%s), %d prompt%s, %s." % (ORIGIN_TEXT.get(session.origin, session.origin), local(first), local(last), duration(first, last), result["prompt_count"], "" if result["prompt_count"] == 1 else "s", req_text))
    lines.append("- Models: " + ", ".join("%s %s" % (m, fmt(v["total"])) for m, v in list(result["models"].items())[:4]) + ".")
    lines.append("- Tokens: " + ", ".join("%s %s (%s)" % (KIND_WORDS[k], fmt(t[k]), pct(t[k], t["total"])) for k in ("cache_read", "cache_write", "fresh", "output")) + ".")
    context = "- Context: about %s tokens re-sent per request on average, peak %s" % (fmt(result["avg_ctx"]), fmt(result["peak_ctx"]))
    if session.window_size:
        context += " of a %s window" % fmt(session.window_size)
    if session.tool == "codex":
        context += ". Compactions in the main conversation: %d (Codex does not record whether they were automatic)" % result["unmarked_compactions"]
    else:
        context += ". Compactions in the main conversation: %d automatic (the context limit), %d manual" % (result["auto_compactions"], result["manual_compactions"])
    context += "; in subagents: %d." % result["sub_compactions"] if result["sub_compactions"] else "."
    if session.limit_hits:
        context += " Usage-limit errors: %d." % session.limit_hits
    lines.append(context)
    if result["sub_tokens"]:
        biggest = result["per_sub"].most_common(level["subs"])
        lines.append("- Subagents: %d used %s tokens (%s of this session); largest: %s." % (len(result["per_sub"]), fmt(result["sub_tokens"]), pct(result["sub_tokens"], t["total"]), "; ".join('"%s" %s' % (one_line(session.subagents.get(thread, "a subagent"), 60), fmt(n)) for thread, n in biggest)))
    prompts = sorted([p for p in session.prompts if p[0] is not None], key=lambda p: p[0])
    if prompts:
        lines.append("- First prompts:" if len(prompts) > 1 and level["prompts"] > 1 else "- First prompt:")
        for number, prompt in enumerate(prompts[: level["prompts"]], 1):
            lines.append('  %d. "%s"' % (number, one_line(prompt[1], level["prompt"])))
    else:
        lines.append("- No prompt was typed in the window; the work continued from an earlier prompt.")
    turns = result["turns"][: level["turns"]]
    if turns:
        lines.append("- Costliest turns:")
        for number, turn in enumerate(turns, 1):
            if turn["prompt"]:
                head = 'Prompt %d, "%s"' % (turn["index"], one_line(turn["prompt"][1], level["turn_prompt"]))
            else:
                head = "Work continued from before the window"
            lines.append("  %d. %s: %s tokens (%s), %s. Causes: %s." % (number, head, fmt(turn["tokens"]), pct(turn["tokens"], t["total"]), many(turn["requests"], "request"), "; ".join(turn_causes(turn))))
    if result["large"]:
        lines.append("- Large tool results (about 5,000 tokens or more): %d; the costliest:" % len(result["large"]))
        for entry in result["large"][: level["large"]]:
            lines.append("  - `%s`: about %s tokens, re-sent by %s, about %s tokens." % (one_line(entry["call"].desc, 100), fmt(entry["approx"]), many(entry["later"], "later request"), fmt(entry["carried"])))
    if result["repeats"]:
        p = result["patterns"]["repeated_calls"]
        lines.append("- Repeated identical tool calls (%d or more times): %s, %s (%d failed), about %s tokens in the requests that repeated them; the costliest:" % (REPEAT_MIN, many(p["groups"], "call"), many(p["extra_calls"], "extra call"), p["failed"], fmt(p["tokens"])))
        for entry in result["repeats"][: level["repeats"]]:
            failed = ", %d failed" % entry["failed"] if entry["failed"] else ""
            lines.append("  - `%s` %d times%s, about %s tokens." % (one_line(entry["desc"], 90), entry["count"], failed, fmt(entry["tokens"])))
    if result["long_prompts"]:
        p = result["patterns"]["long_prompts"]
        lines.append("- Long pasted prompts (over %d characters): %d, the longest about %s tokens, re-sent by later requests: about %s tokens." % (LONG_PROMPT_CHARS, p["count"], fmt(p["largest"]), fmt(p["tokens"])))
    p = result["patterns"]
    lines.append(
        "- Tokens tied to each pattern (they overlap; never add them): whole-file reads %s · long tool output %s · repeated calls %s · context above 100K %s · long prompts %s · subagents %s."
        % (fmt(p["whole_file_reads"]["tokens"]), fmt(p["long_tool_output"]["tokens"]), fmt(p["repeated_calls"]["tokens"]), fmt(p["context_over_100k"]["tokens"]), fmt(p["long_prompts"]["tokens"]), fmt(p["subagents"]["tokens"]))
    )
    return "\n".join(lines) + "\n"


def render_digest(top: list, analyses: dict, labels: dict, totals: dict, top_total: int, since, now, max_chars: int) -> str:
    tz = now.astimezone().strftime("%Z") or now.astimezone().strftime("UTC%z")
    t = totals["tokens"]
    header = [
        "# Token usage digest: the %d heaviest coding-agent sessions, %s to %s (%s)" % (len(top), local(since), local(now), tz),
        "",
        "How to read this: every request an agent sends re-sends the whole conversation so far, so a session's tokens grow with its number of requests times the size of its conversation. "
        "Claude Code and Codex save each request's usage in their transcripts: fresh input, cache reads (conversation re-sent from the prompt cache), cache writes, and output. "
        "\"Tokens\" below is the sum of all four, subagents included. Small background requests the tools do not save, such as title generation, are not counted. "
        "The size of one tool result or prompt is an estimate (characters / %d); \"re-sent by N later requests\" multiplies it by the requests that followed in the same conversation before a compaction. "
        "Secrets, email addresses, and home-folder paths were removed; home folders appear as ~. Sessions are tagged S1 (most tokens) to S%d." % (CHARS_PER_TOKEN, len(top)),
        "",
        "## Window totals",
        "- %s with token usage (%d Claude Code, %d Codex): %s tokens." % (many(totals["sessions"], "session"), totals["claude_code"], totals["codex"], fmt(t["total"])),
        "- By kind: " + ", ".join("%s %s (%s)" % (KIND_WORDS[k], fmt(t[k]), pct(t[k], t["total"])) for k in ("cache_read", "cache_write", "fresh", "output")) + ".",
        "- By model: " + ", ".join("%s %s" % (m, fmt(v["total"])) for m, v in list(totals["by_model"].items())[:6]) + ".",
        "- By tool: " + ", ".join("%s %s (%s)" % ("Claude Code" if k == "claude-code" else "Codex", fmt(v), pct(v, t["total"])) for k, v in sorted(totals["by_tool"].items(), key=lambda kv: kv[1], reverse=True)) + ".",
        "- The %d sessions below used %s tokens, %s of the window." % (len(top), fmt(top_total), pct(top_total, t["total"])),
        "",
    ]
    budget = max(1500, (max_chars - 2500) // max(len(top), 1))
    body = []
    for session in top:
        text = ""
        for level in LEVELS:
            text = render_session(session, analyses[id(session)], labels[id(session)], totals, level)
            if len(text) <= budget:
                break
        if len(text) > budget:
            text = text[: budget - 40].rstrip() + "\n- [... cut to fit the digest size limit]\n"
        body.append(text)
    text = "\n".join(body)
    counts = digest_markers(text)
    if any(counts.values()):
        words = {"redacted": "[REDACTED]", "email": "[email]", "user": "[user]"}
        header.insert(4, "Redaction markers in this file: " + ", ".join("%s %d" % (words[k], v) for k, v in counts.items() if v) + ".")
        header.insert(5, "")
    return "\n".join(header) + "\n" + text


def digest_markers(text: str) -> dict:
    # The digest's own line that reports these counts is not counted.
    text = "\n".join(line for line in text.splitlines() if not line.startswith("Redaction markers in this file:"))
    return {"redacted": text.count("[REDACTED"), "email": text.count("[email]"), "user": text.count("[user]")}


def build_index(top: list, analyses: dict, labels: dict, totals: dict, top_total: int, since, now, home: Path, summary: dict) -> dict:
    entries = []
    for session in top:
        r = analyses[id(session)]
        entries.append(
            {
                "tag": session.tag,
                "tool": session.tool,
                "origin": session.origin,
                "session_id": session.session_id,
                "title": session.display_title(),
                "project": labels[id(session)],
                "cwd": session.cwd,
                "config_dir": str(session.home_dir),
                "transcript": str(session.path),
                "subagent_transcripts": session.extra_files,
                "first_activity": session.first_ts().isoformat() if session.first_ts() else None,
                "last_activity": session.last_ts().isoformat() if session.last_ts() else None,
                "prompts": r["prompt_count"],
                "requests": {"main": r["requests_main"], "subagents": r["requests_sub"]},
                "tokens": r["tokens"],
                "share_of_window": round(100.0 * r["tokens"]["total"] / max(totals["tokens"]["total"], 1), 2),
                "models": r["models"],
                "top_model": r["top_model"],
                "context": {"average": r["avg_ctx"], "peak": r["peak_ctx"], "window": session.window_size or None},
                "compactions": {"automatic": r["auto_compactions"], "manual": r["manual_compactions"], "unmarked": r["unmarked_compactions"], "in_subagents": r["sub_compactions"]},
                "usage_limit_errors": session.limit_hits,
                "subagents": {"count": len(r["per_sub"]), "tokens": r["sub_tokens"], "by_kind": r["sub_kinds"]},
                "patterns": r["patterns"],
            }
        )
    return {
        "schema": 1,
        "window": {"since": since.isoformat(), "until": now.isoformat()},
        "read": summary["read"],
        "totals": totals,
        "top_tokens": top_total,
        "sessions": entries,
    }


def session_line(session: Session, result: dict, label: str) -> str:
    extra = []
    if result["sub_tokens"] >= 0.3 * result["tokens"]["total"]:
        extra.append("%s in %s" % (pct(result["sub_tokens"], result["tokens"]["total"]), many(len(result["per_sub"]), "subagent")))
    compactions = result["auto_compactions"] + result["unmarked_compactions"] + result["sub_compactions"]
    if compactions:
        extra.append("%s, subagents included" % many(compactions, "compaction"))
    if result["avg_ctx"] >= CONTEXT_LINE:
        extra.append("about %s tokens re-sent per request" % fmt(result["avg_ctx"]))
    first, last = session.first_ts(), session.last_ts()
    text = '[%s] %s session in %s, "%s": %s tokens over %s in %s' % (
        session.tag, session.tool_name(), label, one_line(session.display_title(), 60), fmt(result["tokens"]["total"]),
        many(result["requests_main"] + result["requests_sub"], "request"), duration(first, last))
    return text + ("; " + "; ".join(extra) if extra else "") + "."


def inputs_template(top: list, analyses: dict, labels: dict, totals: dict, top_total: int, args, since, now, redact: Redactor) -> dict:
    t = totals["tokens"]
    n = len(top)
    words = window_words(args, since)
    goal = (
        "These are the %d Claude Code and Codex sessions that used the most tokens on this machine %s (%s of all %s tokens). "
        "Given that these use the most usage, what should I change in how I work with coding agents so the same work costs fewer tokens? "
        "Base each change on the sessions in the token-usage digest and name the tags (S1 to S%d) of the sessions it would have saved tokens in."
        % (n, words, pct(top_total, t["total"]), fmt(t["total"]), n)
    )
    outcomes = [
        "[all] From %s to %s, %s with token usage (%d Claude Code, %d Codex) used %s tokens: %s cache reads, %s cache writes, %s fresh input, %s output."
        % (local(since), local(now), many(totals["sessions"], "session"), totals["claude_code"], totals["codex"], fmt(t["total"]), pct(t["cache_read"], t["total"]), pct(t["cache_write"], t["total"]), pct(t["fresh"], t["total"]), pct(t["output"], t["total"])),
        "[all] The %d heaviest sessions, S1 to S%d, used %s tokens, %s of the window; S1 alone used %s."
        % (n, n, fmt(top_total), pct(top_total, t["total"]), pct(analyses[id(top[0])]["tokens"]["total"], t["total"])),
    ]
    for session in top[:3]:
        outcomes.append(one_line(session_line(session, analyses[id(session)], labels[id(session)]), MAX_VALUE_CHARS))
    limited = [s for s in top if s.limit_hits]
    if limited:
        outcomes.append("[%s] Work hit the usage limit: %s in these sessions." % (tags_text(s.tag for s in limited), many(sum(s.limit_hits for s in limited), "usage-limit error")))

    mistakes = []

    def pattern_value(key, build):
        # Tag the sessions that carry most of the pattern, so the tags tell sessions apart.
        most = max(analyses[id(s)]["patterns"][key].get("tokens", 0) for s in top)
        hits = [s for s in top if most and analyses[id(s)]["patterns"][key].get("tokens", 0) >= 0.1 * most]
        if hits:
            total = sum(analyses[id(s)]["patterns"][key]["tokens"] for s in hits)
            mistakes.append((total, one_line(build(hits), MAX_VALUE_CHARS)))

    def biggest_desc(entries_key, hits):
        entries = [e for s in hits for e in analyses[id(s)][entries_key]]
        best = max(entries, key=lambda e: e.get("carried", e.get("tokens", 0)), default=None)
        if best is None:
            return ""
        return one_line(best["call"].desc if "call" in best else best["desc"], 70)

    pattern_value("whole_file_reads", lambda hits: (
        "[%s] Whole-file reads: %s of whole files of about 5,000 tokens or more (the largest about %s tokens) stayed in the conversation and were re-sent by later requests, about %s tokens in all; the costliest was `%s`. Cause: whole files were read where a search or a line range would have found the same code."
        % (tags_text(s.tag for s in hits), many(sum(analyses[id(s)]["patterns"]["whole_file_reads"]["count"] for s in hits), "read"), fmt(max(analyses[id(s)]["patterns"]["whole_file_reads"]["largest"] for s in hits)),
           fmt(sum(analyses[id(s)]["patterns"]["whole_file_reads"]["tokens"] for s in hits)), biggest_desc("whole", hits))))
    pattern_value("long_tool_output", lambda hits: (
        "[%s] Long tool output: %s of about 5,000 tokens or more (the largest about %s tokens) were re-sent by later requests, about %s tokens in all; the costliest was `%s`. Cause: full output was returned where a filtered or shortened command would have been enough."
        % (tags_text(s.tag for s in hits), many(sum(analyses[id(s)]["patterns"]["long_tool_output"]["count"] for s in hits), "command or tool result"), fmt(max(analyses[id(s)]["patterns"]["long_tool_output"]["largest"] for s in hits)),
           fmt(sum(analyses[id(s)]["patterns"]["long_tool_output"]["tokens"] for s in hits)), biggest_desc("other_large", hits))))
    pattern_value("repeated_calls", lambda hits: (
        "[%s] Repeated identical tool calls: %s repeated %d or more times with the same input in one conversation (%s, %d failed), about %s tokens in the requests that repeated them; the costliest was `%s`. Cause: the agent polled or retried the same call unchanged instead of waiting longer or changing its approach."
        % (tags_text(s.tag for s in hits), many(sum(analyses[id(s)]["patterns"]["repeated_calls"]["groups"] for s in hits), "call"), REPEAT_MIN, many(sum(analyses[id(s)]["patterns"]["repeated_calls"]["extra_calls"] for s in hits), "extra call"),
           sum(analyses[id(s)]["patterns"]["repeated_calls"]["failed"] for s in hits), fmt(sum(analyses[id(s)]["patterns"]["repeated_calls"]["tokens"] for s in hits)), biggest_desc("repeats", hits))))

    def context_text(hits):
        auto = sum(analyses[id(s)]["auto_compactions"] for s in hits)
        compactions = sum(analyses[id(s)]["auto_compactions"] + analyses[id(s)]["unmarked_compactions"] + analyses[id(s)]["sub_compactions"] for s in hits)
        over = sum(analyses[id(s)]["patterns"]["context_over_100k"]["tokens"] for s in hits)
        peak = max(analyses[id(s)]["peak_ctx"] for s in hits)
        start = "[%s] " % tags_text(s.tag for s in hits)
        if compactions:
            start += "Context limit: these sessions were compacted %s, subagents included (%d marked automatic in the main conversation, meaning the context window was full), with a peak of %s tokens of context; " % (many(compactions, "time"), auto, fmt(peak))
        else:
            start += "Large conversations: these sessions reached up to %s tokens of context; " % fmt(peak)
        return start + "%s tokens were re-sent above the first 100K tokens of context. Cause: long sessions kept going across several tasks instead of compacting at a milestone or starting a new session per task." % fmt(over)

    pattern_value("context_over_100k", context_text)
    pattern_value("long_prompts", lambda hits: (
        "[%s] Long pasted prompts: %s over %d characters (the longest about %s tokens), re-sent by every later request, about %s tokens in all. Cause: large text was pasted into the chat instead of saved in a file the agent can search."
        % (tags_text(s.tag for s in hits), many(sum(analyses[id(s)]["patterns"]["long_prompts"]["count"] for s in hits), "prompt"), LONG_PROMPT_CHARS,
           fmt(max(analyses[id(s)]["patterns"]["long_prompts"]["largest"] for s in hits)), fmt(sum(analyses[id(s)]["patterns"]["long_prompts"]["tokens"] for s in hits)))))

    def subagent_text(hits):
        count = sum(analyses[id(s)]["patterns"]["subagents"]["count"] for s in hits)
        tokens = sum(analyses[id(s)]["patterns"]["subagents"]["tokens"] for s in hits)
        return ("[%s] Subagent fan-out: %s used %s tokens, %s of the top sessions' tokens. Each subagent starts its own conversation and builds its context again from the start. Cause: work was split across many subagents, and each one rebuilt its context."
                % (tags_text(s.tag for s in hits), many(count, "subagent"), fmt(tokens), pct(tokens, top_total)))

    pattern_value("subagents", subagent_text)
    mistakes = [text for _total, text in sorted(mistakes, key=lambda item: item[0], reverse=True)][: FLAG_KINDS["mistakes"][1]]

    context = [
        "[all] How tokens add up: every request re-sends the whole conversation so far, mostly as cache reads, so a session's total grows with its number of requests times the size of its conversation. A large result read early is re-sent by every later request until the conversation is compacted.",
    ]
    model_tags: dict = defaultdict(list)
    model_tokens: Counter = Counter()
    for session in top:
        for model, values in analyses[id(session)]["models"].items():
            model_tokens[model] += values["total"]
            if values["total"] >= 0.1 * analyses[id(session)]["tokens"]["total"]:
                model_tags[model].append(session.tag)
    parts = ["%s %s%s" % (m, fmt(v), (" (%s)" % tags_text(model_tags[m])) if model_tags[m] else "") for m, v in model_tokens.most_common(5)]
    context.append(one_line("[all] Models in the top sessions: " + "; ".join(parts) + ".", MAX_VALUE_CHARS))
    programs = [s for s in top if s.origin not in INTERACTIVE_ORIGINS]
    if programs:
        context.append(one_line("[%s] Started by a program or as a subagent thread, not typed by a person: %s." % (tags_text(s.tag for s in programs), ", ".join("%s (%s)" % (s.tag, s.origin) for s in programs)), MAX_VALUE_CHARS))
    project_tags: dict = defaultdict(list)
    for session in top:
        project_tags[labels[id(session)]].append(session.tag)
    context.append(one_line("[all] Projects of the top sessions: " + "; ".join("%s (%s)" % (p, tags_text(tags)) for p, tags in project_tags.items()) + ".", MAX_VALUE_CHARS))

    return {
        "goal": redact(goal),
        "project": None,
        "outcomes": [redact(v) for v in outcomes][: FLAG_KINDS["outcomes"][1]],
        "mistakes": [redact(v) for v in mistakes],
        "context": [redact(v) for v in context][: FLAG_KINDS["context"][1]],
        "capability": ["The user can edit the instruction files their agents read (CLAUDE.md for Claude Code, AGENTS.md for Codex), choose the model and reasoning effort for each session, and compact, clear, or start new sessions."],
        "success": [],
        "constraint": [],
        "avoid": [],
    }


# --------------------------------------------------------------------------------------------
# Suggest


def load_json(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(usage_error("%s not found: %s. Run `gather` first, or pass the folder it printed as --out." % (label, path)))
    except ValueError as error:
        raise SystemExit(usage_error("%s is not valid JSON (%s): %s" % (label, error, path)))


def validate_inputs(data, known_tags: set, redact: Redactor):
    if not isinstance(data, dict):
        return None, ["inputs.json must hold one JSON object."]
    problems = []
    allowed = {"goal", "project"} | set(FLAG_KINDS)
    for key in data:
        if key not in allowed:
            problems.append("Unknown key %r. Allowed keys: goal, project, %s." % (key, ", ".join(FLAG_KINDS)))
    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        problems.append("goal must be a non-empty string.")
        goal = ""
    goal = redact(" ".join(goal.split()))
    if len(goal) > MAX_GOAL_CHARS:
        problems.append("goal has %d characters; keep it under %d." % (len(goal), MAX_GOAL_CHARS))
    project = data.get("project")
    if project is not None and (not isinstance(project, str) or not PROJECT_RE.match(project)):
        problems.append("project must be null or a key of 1-64 lowercase letters, digits, hyphens, or underscores.")
    values: "OrderedDict[str, list]" = OrderedDict()
    for kind, (flag, most) in FLAG_KINDS.items():
        items = data.get(kind, [])
        if items is None:
            items = []
        if not isinstance(items, list) or any(not isinstance(i, str) for i in items):
            problems.append("%s must be a list of strings." % kind)
            continue
        cleaned = [redact(" ".join(i.split())) for i in items if i.strip()]
        if len(cleaned) > most:
            problems.append("%s has %d values; this skill passes at most %d. Merge or drop the weakest." % (kind, len(cleaned), most))
        for value in cleaned:
            if len(value) > MAX_VALUE_CHARS:
                problems.append("A %s value has %d characters; keep each value under %d: %s" % (kind, len(value), MAX_VALUE_CHARS, one_line(value, 80)))
            if kind not in TAGGED_KINDS:
                continue
            match = TAG_RE.match(value)
            if not match:
                problems.append("Every %s value must start with session tags such as [S2] or [S1 S4], or [all]: %s" % (kind, one_line(value, 80)))
                continue
            for tag in re.findall(r"S\d+", match.group(1)):
                if tag not in known_tags:
                    problems.append("%s names %s, which is not in usage.json: %s" % (kind, tag, one_line(value, 80)))
        values[kind] = cleaned
    if not values.get("outcomes"):
        problems.append("Keep at least one outcomes value (the window totals from gather).")
    return {"goal": goal, "project": project, "values": values}, problems


def build_argv(exe: str, inputs: dict, dry_run: bool, key: str) -> list:
    argv = [exe, "--json", "--no-input", "agents", "suggest", "run", "--goal", inputs["goal"]]
    for category in CATEGORIES:
        argv += ["--category", category]
    argv += ["--count", str(COUNT), "--horizon", HORIZON]
    for kind, (flag, _most) in FLAG_KINDS.items():
        for value in inputs["values"].get(kind, []):
            argv += [flag, value]
    argv += ["--files", "digest.md"]
    if inputs["project"]:
        argv += ["--project", inputs["project"]]
    argv += ["--dry-run"] if dry_run else ["--idempotency-key", key]
    return argv


def run_cli(argv: list, out_dir: Path, stdout_name: str):
    """Runs vidbyte-cli in out_dir; saves stdout to a file and shows and saves stderr."""
    stdout_path = out_dir / stdout_name
    stderr_path = out_dir / (Path(stdout_name).stem + ".stderr.txt")
    captured = []
    with open(stdout_path, "wb") as sink:
        process = subprocess.Popen(argv, cwd=str(out_dir), stdout=sink, stderr=subprocess.PIPE)
        assert process.stderr is not None
        for line in iter(process.stderr.readline, b""):
            captured.append(line)
            sys.stderr.buffer.write(line)
            sys.stderr.flush()
        code = process.wait()
    stderr_path.write_bytes(b"".join(captured))
    error = None
    for raw in reversed(captured):
        try:
            document = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(document, dict) and document.get("kind") == "error":
            error = document.get("data")
            break
    if code != 0 and stdout_path.exists() and stdout_path.stat().st_size == 0:
        stdout_path.unlink()
    return code, stdout_path, stderr_path, error


def resolve_cli(value):
    """The vidbyte-cli executable: --cli when given, else vidbyte-cli on PATH. None when missing."""
    if value:
        found = shutil.which(value)
        if found:
            return found
        return value if Path(value).expanduser().is_file() else None
    return shutil.which("vidbyte-cli")


def display_command(argv: list) -> str:
    shown = ["vidbyte-cli"] + argv[1:]
    return " ".join(shlex.quote(a) for a in shown)


def cmd_suggest(args) -> int:
    out_dir = Path(args.out).expanduser()
    if args.dry_run == args.paid:
        return usage_error("Pass exactly one of --dry-run (free check) or --paid (the charged run, only after the user said yes).")
    if not (out_dir / "digest.md").is_file():
        return usage_error("digest.md not found in %s. Run `gather` first and pass the out_dir it printed." % out_dir)
    index = load_json(out_dir / "usage.json", "usage.json")
    known = {e.get("tag") for e in index.get("sessions", []) if isinstance(e, dict)}
    redact = Redactor(Path.home())
    inputs, problems = validate_inputs(load_json(out_dir / "inputs.json", "inputs.json"), known, redact)
    if problems:
        print(json.dumps({"error": "inputs.json needs fixing", "problems": problems}, indent=2))
        for problem in problems:
            print("inputs.json: " + problem, file=sys.stderr)
        return 2
    exe = resolve_cli(args.cli)
    if not exe:
        print(json.dumps({"error": ("--cli %s was not found." % args.cli) if args.cli else "vidbyte-cli is not on PATH. Follow references/setup.md step 1."}))
        return 127
    print("Using %s" % exe, file=sys.stderr)
    key = ""
    if args.paid:
        result_path = out_dir / "result.json"
        key_path = out_dir / "idempotency-key.txt"
        finished = result_path.is_file() and result_path.stat().st_size > 0
        if args.idempotency_key:
            if not KEY_RE.match(args.idempotency_key):
                return usage_error("--idempotency-key must be 8-128 letters, digits, dots, underscores, colons, or hyphens.")
            if finished:
                return usage_error("result.json already holds the output of a paid run, so there is nothing to recover. Read it instead.")
            key = args.idempotency_key
        elif finished or result_path.exists() or key_path.exists():
            # Any earlier paid attempt here may already have been charged. A fresh key would buy
            # a second admission, so only an explicit recovery key or a new folder may continue.
            return usage_error(
                "A paid run was already started in this folder (see idempotency-key.txt and result.json). "
                "To recover an attempt that stopped before Vidbyte verified its admission, rerun with --idempotency-key and the saved key. "
                "Any other new run is a new charge: ask the user first, then gather into a new folder."
            )
        else:
            key = "%s:%s:%s" % (SLUG, dt.datetime.now().strftime("%Y%m%d"), secrets.token_hex(3))
            write_lf(key_path, key + "\n")
        print("Idempotency key for this run: %s (saved in idempotency-key.txt)" % key, file=sys.stderr)
    argv = build_argv(exe, inputs, args.dry_run, key)
    length = sum(len(a) + 3 for a in argv)
    if length > MAX_ARGV_CHARS:
        return usage_error("The command would be %d characters; shorten the inputs.json values to stay under %d." % (length, MAX_ARGV_CHARS))
    print("Running in %s:\n%s" % (out_dir, display_command(argv)), file=sys.stderr)
    code, stdout_path, stderr_path, error = run_cli(argv, out_dir, "dry-run.json" if args.dry_run else "result.json")
    report: dict = {"exit_code": code, "stderr_file": str(stderr_path)}
    if args.paid:
        report["idempotency_key"] = key
    if code == 0:
        try:
            document = json.loads(stdout_path.read_text(encoding="utf-8"))
            data = document.get("data", {})
        except ValueError:
            document, data = {}, {}
        report.update(
            {
                "result_file": str(stdout_path),
                "kind": document.get("kind"),
                "status": data.get("status"),
                "stop_reason": data.get("stop_reason"),
                "ideas": len(data.get("ideas") or []),
                "charged_cents": data.get("charged_cents"),
                "context_manifest": [{"ref": m.get("ref"), "kind": m.get("kind"), "status": m.get("status")} for m in data.get("context_manifest") or []],
                "warnings": data.get("warnings") or [],
            }
        )
    elif error:
        report["error"] = {k: error.get(k) for k in ("code", "message", "hint", "retryable") if k in error}
    print(json.dumps(report, indent=2))
    return code


# --------------------------------------------------------------------------------------------
# Report

# Which measured pattern an idea targets, read from its own words. The agent checks each match.
PATTERN_WORDS = OrderedDict(
    [
        ("whole_file_reads", re.compile(r"\b(?:whole[- ]files?|entire files?|full files?|line ranges?|offset and limit|read(?:s|ing)? only|search(?:es|ing)? (?:first|before)|grep|ripgrep|rg|code (?:map|index)|repo(?:sitory)? map)\b", re.I)),
        ("long_tool_output", re.compile(r"\b(?:long|large|full|verbose) (?:tool |command )?output|truncat\w*|filter(?:ed|ing)? (?:the )?output|\| ?head|\| ?tail|--quiet|-q\b|summari[sz]e (?:the )?(?:output|logs?)|logs?\b", re.I)),
        ("repeated_calls", re.compile(r"\b(?:repeat\w*|identical|duplicate\w*|loop\w*|retr(?:y|ies|ied)|poll\w*|wait(?:ing)? longer|back ?off)\b", re.I)),
        ("context_over_100k", re.compile(r"(?:\bcompact\w*|/clear\b|\bnew session|\bfresh session|\bsession per task|\bone task per session|\bcontext (?:window|limit|size|budget)|\bhandoff|\bhand off|\blong[- ]running sessions?)", re.I)),
        ("long_prompts", re.compile(r"\b(?:paste\w*|long prompts?|prompt size|large prompts?)\b", re.I)),
        ("subagents", re.compile(r"\b(?:sub-?agents?|fan[- ]out|parallel agents|spawn\w*|delegat\w*)\b", re.I)),
        ("model", re.compile(r"\b(?:smaller|cheaper|lighter|faster) model|model (?:choice|selection|per task|by task)|reasoning effort|\b(?:haiku|sonnet|mini)\b", re.I)),
    ]
)
PATTERN_NAMES = {
    "whole_file_reads": "whole-file reads",
    "long_tool_output": "long tool output",
    "repeated_calls": "repeated identical tool calls",
    "context_over_100k": "context re-sent above 100K tokens",
    "long_prompts": "long pasted prompts",
    "subagents": "subagent tokens",
    "model": "tokens on each session's largest model (moved, not removed)",
}
SESSION_TAG_RE = re.compile(r"\bS(\d{1,3})\b")


def parse_prices(values: list):
    prices = {}
    for value in values:
        match = re.match(r"^\s*([^=\s]+)\s*=\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)\s*$", value)
        if not match:
            raise SystemExit(usage_error("--price must look like MODEL=INPUT,OUTPUT,CACHE_READ,CACHE_WRITE in US dollars per million tokens: %s" % value))
        prices[match.group(1)] = dict(zip(("fresh", "output", "cache_read", "cache_write"), (float(match.group(i)) for i in range(2, 6))))
    return prices


def price_for(model: str, prices: dict):
    if model in prices:
        return prices[model]
    keys = [k for k in prices if model.startswith(k)]
    return prices[max(keys, key=len)] if keys else None


def dollars(kinds: dict, price) -> float:
    return sum(kinds.get(k, 0) * price[k] for k in KINDS) / 1e6


def idea_text(idea: dict) -> str:
    parts = [idea.get(k) or "" for k in ("title", "summary", "first_action", "why_now", "expected_benefit", "completion_criteria")]
    parts += list(idea.get("suggested_actions") or [])
    return "\n".join(str(p) for p in parts)


def cmd_report(args) -> int:
    out_dir = Path(args.out).expanduser()
    index = load_json(out_dir / "usage.json", "usage.json")
    result = load_json(out_dir / "result.json", "result.json")
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return usage_error("result.json does not hold a suggestions result.")
    prices = parse_prices(args.price)
    if prices and not args.price_source:
        return usage_error("--price needs --price-source with the URL of the provider's own price page each price was copied from.")
    sessions = {e["tag"]: e for e in index.get("sessions", []) if isinstance(e, dict) and e.get("tag")}
    window_total = index["totals"]["tokens"]["total"]

    def session_dollars(entry: dict):
        if not prices:
            return None
        total, missing = 0.0, []
        for model, kinds in entry["models"].items():
            price = price_for(model, prices)
            if price is None:
                missing.append(model)
            else:
                total += dollars(kinds, price)
        return total, missing

    ideas_out = []
    for idea in data.get("ideas") or []:
        if not isinstance(idea, dict):
            continue
        text = idea_text(idea)
        cited = set("S" + m for m in SESSION_TAG_RE.findall(text))
        for evidence in (idea.get("handoff") or {}).get("evidence") or []:
            # Typed inputs start with session tags. A digest.md excerpt is only the file's start,
            # so its tags say nothing about this idea and are ignored.
            if str(evidence.get("source", "")).startswith("flag:"):
                content = str(evidence.get("content", ""))
                match = TAG_RE.match(content)
                if match and match.group(1) == "all":
                    cited.add("all")
                elif match:
                    cited.update(re.findall(r"S\d+", match.group(1)))
        tags = sorted((t for t in cited if t in sessions), key=lambda t: int(t[1:]))
        scope = "cited"
        if not tags:
            tags = sorted(sessions, key=lambda t: int(t[1:]))
            scope = "all top sessions (the idea names no session)" if "all" not in cited else "all top sessions"
        matched = OrderedDict()
        for key, pattern in PATTERN_WORDS.items():
            if pattern.search(text):
                if key == "model":
                    matched[key] = sum(sessions[t]["models"].get(sessions[t]["top_model"], {}).get("total", 0) for t in tags)
                else:
                    matched[key] = sum(sessions[t]["patterns"][key]["tokens"] for t in tags)
        removable = {k: v for k, v in matched.items() if k != "model" and v}
        best = max(removable.items(), key=lambda kv: kv[1]) if removable else None
        entry = {
            "id": idea.get("id"),
            "rank": idea.get("rank"),
            "title": idea.get("title"),
            "horizon": idea.get("horizon"),
            "readiness": idea.get("readiness"),
            "category": idea.get("primary_category"),
            "sessions": tags,
            "session_scope": scope,
            "matched_patterns": matched,
            "estimate_tokens": best[1] if best else None,
            "estimate_pattern": best[0] if best else None,
            "first_action": idea.get("first_action"),
            "effort": idea.get("effort_estimate"),
        }
        if prices and best:
            cost, missing = 0.0, []
            for tag in tags:
                info = sessions[tag]
                price = price_for(info["top_model"], prices)
                if price is None:
                    missing.append(info["top_model"])
                elif best[0] == "subagents":
                    cost += dollars(info["subagents"].get("by_kind", {}), price)
                else:  # Re-sent conversation is billed mostly as cache reads.
                    cost += info["patterns"][best[0]]["tokens"] * price["cache_read"] / 1e6
            entry["estimate_dollars"] = round(cost, 2)
            entry["estimate_dollars_basis"] = "subagent tokens at the prices of each session's largest model" if best[0] == "subagents" else "re-sent tokens at the cache-read price of each session's largest model"
            entry["models_without_price"] = sorted(set(missing))
        ideas_out.append(entry)

    report_path = out_dir / "report.md"
    write_lf(report_path, render_report(ideas_out, sessions, index, data, prices, args.price_source, session_dollars))
    window_dollars = None
    if prices:
        window_dollars = 0.0
        for model, kinds in index["totals"]["by_model"].items():
            price = price_for(model, prices)
            if price is not None:
                window_dollars += dollars(kinds, price)
    print(json.dumps(
        {
            "report": str(report_path),
            "status": data.get("status"),
            "stop_reason": data.get("stop_reason"),
            "charged_cents": data.get("charged_cents"),
            "window_tokens": window_total,
            "window_dollars_at_api_prices": round(window_dollars, 2) if window_dollars is not None else None,
            "price_sources": args.price_source or [],
            "ideas": ideas_out,
        },
        indent=2,
    ))
    return 0


def render_report(ideas: list, sessions: dict, index: dict, data: dict, prices: dict, sources: list, session_dollars) -> str:
    totals = index["totals"]
    lines = [
        "# What to change to cut token usage",
        "",
        "Window: %s to %s. %s used %s tokens; the %d heaviest used %s (%s)."
        % (index["window"]["since"][:16].replace("T", " "), index["window"]["until"][:16].replace("T", " "), many(totals["sessions"], "session"), fmt(totals["tokens"]["total"]), len(sessions), fmt(index["top_tokens"]), pct(index["top_tokens"], totals["tokens"]["total"])),
        "",
        "Token estimates are upper bounds from each session's own numbers for one window like this one. Patterns overlap, so never add two estimates.",
    ]
    if prices:
        lines.append("Dollar figures use API prices copied from %s. On a subscription plan there is no per-token bill; read them as what the same tokens cost at API prices." % ", ".join(sources))
    lines.append("")
    for horizon in ("now", "next", "later"):
        group = [i for i in ideas if i.get("horizon") == horizon]
        if not group:
            continue
        lines += ["## %s" % horizon.capitalize(), ""]
        for idea in sorted(group, key=lambda i: i.get("rank") or 0):
            lines.append("### %s. %s (%s, %s)" % (idea.get("rank"), idea.get("title"), idea.get("id"), idea.get("category")))
            names = []
            for tag in idea["sessions"]:
                info = sessions[tag]
                names.append('%s (%s, %s, "%s", %s tokens)' % (tag, "Claude Code" if info["tool"] == "claude-code" else "Codex", info["project"], one_line(info["title"], 50), fmt(info["tokens"]["total"])))
            lines.append("- Sessions (%s): %s" % (idea["session_scope"], "; ".join(names)))
            if idea["estimate_tokens"]:
                estimate = "- Tokens tied to what it targets: up to %s tokens (%s)" % (fmt(idea["estimate_tokens"]), PATTERN_NAMES[idea["estimate_pattern"]])
                if idea.get("estimate_dollars") is not None:
                    estimate += ", about $%.2f (%s)" % (idea["estimate_dollars"], idea["estimate_dollars_basis"])
                lines.append(estimate + ".")
            moved = idea["matched_patterns"].get("model")
            if moved and not idea["estimate_tokens"]:
                lines.append("- Tokens it would move to another model, not remove: up to %s tokens (each session's largest model)." % fmt(moved))
            others = ["%s %s" % (PATTERN_NAMES[k], fmt(v)) for k, v in idea["matched_patterns"].items() if k != idea["estimate_pattern"] and v and not (k == "model" and not idea["estimate_tokens"])]
            if others:
                lines.append("- Also matched (check each fits): " + "; ".join(others) + ".")
            if not idea["matched_patterns"]:
                lines.append("- No measured pattern matched this idea's words, so there is no token estimate.")
            lines.append("- First action: %s" % one_line(idea.get("first_action") or "", 300))
            if idea.get("readiness") and idea["readiness"] != "ready":
                lines.append("- Readiness: %s" % idea["readiness"])
            lines.append("")
    if prices:
        lines += ["## Sessions at API prices", ""]
        for tag, info in sessions.items():
            cost, missing = session_dollars(info)
            note = " (no price given for %s)" % ", ".join(missing) if missing else ""
            lines.append("- %s: %s tokens, about $%.2f%s." % (tag, fmt(info["tokens"]["total"]), cost, note))
        lines.append("")
    if data.get("charged_cents"):
        lines.append("This run cost %s cents from the Vidbyte balance." % data["charged_cents"])
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------------
# Feedback


def cmd_feedback(args) -> int:
    out_dir = Path(args.out).expanduser()
    result = load_json(out_dir / "result.json", "result.json")
    ideas = (result.get("data") or {}).get("ideas") or [] if isinstance(result, dict) else []
    match = [i for i in ideas if isinstance(i, dict) and i.get("id") == args.idea]
    if not match:
        return usage_error("No idea with id %s in result.json. Ids look like idea-001." % args.idea)
    project = args.project
    if not project:
        inputs = load_json(out_dir / "inputs.json", "inputs.json")
        project = inputs.get("project") if isinstance(inputs, dict) else None
    if not project or not PROJECT_RE.match(project):
        return usage_error("No suggestion project. Create it first (see the skill's Deliver section), then pass --project or set project in inputs.json.")
    exe = resolve_cli(args.cli)
    if not exe:
        print(json.dumps({"error": ("--cli %s was not found." % args.cli) if args.cli else "vidbyte-cli is not on PATH. Follow references/setup.md step 1."}))
        return 127
    print("Using %s" % exe, file=sys.stderr)
    verb = "accept" if args.accept else "reject"
    argv = [exe, "--json", "agents", "suggest", "feedback", verb, "--project", project, "--suggestion", match[0]["title"]]
    if args.reason and args.reason.strip():
        argv += ["--reason", Redactor(Path.home())(" ".join(args.reason.split()))]
    print(display_command(argv), file=sys.stderr)
    return subprocess.call(argv)


# --------------------------------------------------------------------------------------------
# Command line


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="token_usage.py",
        description="Find the Claude Code and Codex sessions that used the most tokens, and ask the Vidbyte Suggestion Agent what to change.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 token_usage.py gather                       # last 7 days, top 10 sessions\n"
            "  python3 token_usage.py gather --days 30 --top 15 --project-dir .\n"
            "  python3 token_usage.py gather --claude-dir ~/.claude-work\n"
            "  python3 token_usage.py suggest --out <out_dir> --dry-run   # free\n"
            "  python3 token_usage.py suggest --out <out_dir> --paid      # charged; ask the user first\n"
            "  python3 token_usage.py report --out <out_dir>\n"
            "  python3 token_usage.py feedback --out <out_dir> --idea idea-002 --reject --reason \"I need subagents for reviews\"\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    gather = sub.add_parser("gather", help="Total tokens per session, rank, and compile the heaviest into digest.md (free, local).", description="Read the transcripts of a time window, total each session's tokens, rank the sessions, compile the heaviest into digest.md, and write usage.json and a pre-filled inputs.json into a new folder. Prints a JSON summary. Exit 3 means no token usage matched.")
    gather.add_argument("--days", type=float, default=DEFAULT_DAYS, help="Window length in days, ending now (default: %g)." % DEFAULT_DAYS)
    gather.add_argument("--since", help="Window start in local time, such as 2026-10-04 or 2026-10-04T09:00. Overrides --days.")
    gather.add_argument("--top", type=int, default=DEFAULT_TOP, help="How many of the heaviest sessions to compile (default: %d, at most %d)." % (DEFAULT_TOP, MAX_TOP))
    gather.add_argument("--project-dir", action="append", default=[], help="Keep only sessions that ran in this folder or below it. Repeat for more folders.")
    gather.add_argument("--claude-dir", action="append", default=[], help="An extra Claude Code config folder to read, such as ~/.claude-work. Repeatable. $CLAUDE_CONFIG_DIR, else ~/.claude, is always read.")
    gather.add_argument("--codex-home", help="The Codex folder to read (default: $CODEX_HOME, else ~/.codex).")
    gather.add_argument("--no-claude", action="store_true", help="Skip Claude Code sessions.")
    gather.add_argument("--no-codex", action="store_true", help="Skip Codex sessions.")
    gather.add_argument("--interactive-only", action="store_true", help="Rank only sessions a person typed into; sessions started by programs still count in the window totals.")
    gather.add_argument("--exclude-session", action="append", default=[], help="A session id to leave out. Repeatable.")
    gather.add_argument("--include-current", action="store_true", help="Keep the session this command runs in (left out by default when the host exposes its id).")
    gather.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Size limit for digest.md in characters (default: %d)." % DEFAULT_MAX_CHARS)
    gather.add_argument("--out", help="Output folder (default: a new folder under the system temp folder).")

    suggest = sub.add_parser("suggest", help="Check inputs.json and run vidbyte-cli agents suggest run (--dry-run is free; --paid is charged).", description="Validate <out>/inputs.json against usage.json, then run `vidbyte-cli --json --no-input agents suggest run` from inside <out>. Saves stdout to dry-run.json or result.json and stderr to a .stderr.txt file, prints a JSON summary, and exits with vidbyte-cli's exit code.")
    suggest.add_argument("--out", required=True, help="The out_dir that gather printed.")
    suggest.add_argument("--dry-run", action="store_true", help="Validate the full request for free: no model, no admission, no charge.")
    suggest.add_argument("--paid", action="store_true", help="The charged run. Use only after the user agreed to the cost.")
    suggest.add_argument("--idempotency-key", help="Reuse the key of an earlier attempt to recover its admission without paying twice.")
    suggest.add_argument("--cli", help="The vidbyte-cli executable to run (default: vidbyte-cli on PATH).")

    report = sub.add_parser("report", help="Tie each idea to its sessions and estimate the tokens it targets (free, local).", description="Read <out>/result.json and <out>/usage.json, write <out>/report.md, and print a JSON summary. Dollars appear only with --price and --price-source.")
    report.add_argument("--out", required=True, help="The out_dir that holds result.json.")
    report.add_argument("--price", action="append", default=[], help="MODEL=INPUT,OUTPUT,CACHE_READ,CACHE_WRITE in US dollars per million tokens, copied from the provider's own price page. Repeatable.")
    report.add_argument("--price-source", action="append", default=[], help="The URL of the provider's price page the --price values come from. Required with --price. Repeatable.")

    feedback = sub.add_parser("feedback", help="Record the user's explicit accept or reject of one idea (free, local).", description="Copy the exact title of one idea from <out>/result.json into `vidbyte-cli --json agents suggest feedback accept|reject`.")
    feedback.add_argument("--out", required=True, help="The out_dir that holds result.json.")
    feedback.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
    verdict = feedback.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accept", action="store_true", help="The user said they want this idea.")
    verdict.add_argument("--reject", action="store_true", help="The user said no to this idea.")
    feedback.add_argument("--reason", help="The user's own reason, if they gave one.")
    feedback.add_argument("--project", help="The suggestion project key (default: project in inputs.json).")
    feedback.add_argument("--cli", help="The vidbyte-cli executable to run (default: vidbyte-cli on PATH).")

    args = parser.parse_args(argv)
    if args.command == "gather":
        return cmd_gather(args)
    if args.command == "suggest":
        return cmd_suggest(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "feedback":
        return cmd_feedback(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:  # A redirected Windows console may not be UTF-8; never fail on one character.
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
