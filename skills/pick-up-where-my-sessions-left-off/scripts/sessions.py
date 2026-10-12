#!/usr/bin/env python3
"""Turn recent Claude Code and Codex sessions into input for the Vidbyte Suggestion Agent.

Three subcommands, run in this order by the pick-up-where-my-sessions-left-off skill:

  gather    Free and local. Reads the Claude Code and Codex session transcripts of a time
            window, distills each session, redacts secrets, emails, and home paths, and writes
            digest.md (the only file that is ever sent) and sessions.json (a local index with
            the commands that resume each session) into a new output folder.
  suggest   Checks inputs.json (the values the coding agent wrote from the digest), then runs
            `vidbyte-cli --json --no-input agents suggest run ...` from inside the output folder.
            --dry-run is free; --paid is the one billed step and needs the user's yes first.
  feedback  Records the user's explicit accept or reject of one idea, copying the idea's exact
            title from result.json into `vidbyte-cli agents suggest feedback`.

Only the Python standard library is used, and Python 3.8 or newer is enough. vidbyte-cli is
started without a shell, so no argument quoting differs between bash, zsh, PowerShell, and cmd.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, OrderedDict
from pathlib import Path

CATEGORIES = ("completion", "continuation", "immediate_next_steps", "verification")
COUNT = 8
# The run starts no new model call once it has used this many tokens, which bounds its cost.
MAX_TOTAL_TOKENS = 400_000

# vidbyte-cli's default model for both roles, and its regular rates in US dollars per million
# tokens (https://developers.openai.com/api/docs/models/gpt-6-sol), which Vidbyte's model gateway
# bills plus a 2% markup. They only estimate a finished run's cost; vidbyte.pro/usage is exact.
MODEL = "gpt-6-sol"
RATES_PER_MILLION = {"input": 2.0, "cached_input": 0.2, "output": 10.0}
MARKUP = 1.02
USAGE_KEYS = ("model_calls", "input_tokens", "cached_input_tokens", "output_tokens")
DEFAULT_MAX_CHARS = 60_000
MAX_SESSIONS = 40
RECENT_MINUTES = 15

# Values the coding agent may put in inputs.json, in the order they are passed, with the
# vidbyte-cli flag each one becomes and the most values this skill passes for it.
FLAG_KINDS = OrderedDict(
    [
        ("trajectory", ("--trajectory", 2)),
        ("completed", ("--completed", 8)),
        ("in_progress", ("--in-progress", 8)),
        ("decision", ("--decision", 5)),
        ("blockers", ("--blockers", 5)),
        ("mistakes", ("--mistakes", 6)),
        ("outcomes", ("--outcomes", 6)),
        ("context", ("--context", 4)),
        ("constraint", ("--constraint", 3)),
        ("avoid", ("--avoid", 3)),
    ]
)
UNTAGGED_KINDS = ("constraint", "avoid")  # The user's own words; --avoid must match verbatim.
MAX_VALUE_CHARS = 400
MAX_GOAL_CHARS = 600
MAX_ARGV_CHARS = 24_000  # Windows refuses command lines over 32,767 characters.
TAG_RE = re.compile(r"^\[((?:S\d+)(?:[ ,]+S\d+)*|all)\]\s+\S")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def write_text_lf(path, text):
    """Write UTF-8 text with LF line endings (Path.write_text has no newline= before Python 3.10)."""
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# --------------------------------------------------------------------------------------------
# Redaction


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

    def __init__(self, home: Path) -> None:
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


def tail_lines(text: str, count: int = 2, limit: int = 220) -> str:
    lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    lines = [ln for ln in lines if not re.match(r"^Exit code \d+$", ln)]
    return one_line(" | ".join(lines[-count:]), limit) if lines else ""


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


CHECK_WORDS = ("test", "pytest", "jest", "vitest", "lint", "ruff", "mypy", "pyright", "tsc", "eslint", "flake8", "clippy", "build", "check", "typecheck")
SKIP_PREFIX_WORDS = {"cd", "pushd", "set-location", "export", "source", ".", "set", "sl"}


def command_key(command: str) -> str:
    first = str(command).strip().splitlines()[0] if str(command).strip() else ""
    for segment in re.split(r"&&|\|\||;|\|", first):
        tokens = [t.strip("\"'") for t in segment.split()]
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if not tokens or tokens[0].lower() in SKIP_PREFIX_WORDS:
            continue
        width = 3 if len(tokens) > 2 and tokens[1] in ("-m", "run", "exec", "x") else 2
        return " ".join(tokens[:width]).lower()
    return one_line(first, 40).lower()


# --------------------------------------------------------------------------------------------
# Session model


class Session:
    def __init__(self, tool: str, session_id: str, path: Path, home_dir: Path) -> None:
        self.tool = tool
        self.session_id = session_id
        self.path = path
        self.home_dir = home_dir  # The Claude config folder or CODEX_HOME the session lives in.
        self.title = ""
        self.ai_title = ""
        self.cwds: list = []
        self.branch = ""
        self.first_ts = None
        self.last_ts = None
        self.prompts: list = []
        self.last_agent_text = ""
        self.recap = ""
        self.compact_tail = ""
        self.commands: list = []
        self.rejected: list = []
        self.denied: list = []
        self.files: "OrderedDict[str, None]" = OrderedDict()
        self.prs: "OrderedDict[str, str]" = OrderedDict()
        self.commits: list = []
        self.pushes = 0
        self.last_event = ""
        self.error_text = ""
        self.skip_reason = ""
        self.prior_prompt = ""
        self.prompt_before_window = False
        self.tag = ""

    def finish(self, redact: "Redactor") -> "Session":
        # A session that was active in the window without a new prompt keeps its last earlier one.
        if not self.prompts and self.prior_prompt.strip() and self.last_ts is not None:
            self.prompts.append(redact(self.prior_prompt.strip()))
            self.prompt_before_window = True
        return self

    def touch(self, stamp) -> None:
        if stamp is None:
            return
        if self.first_ts is None or stamp < self.first_ts:
            self.first_ts = stamp
        if self.last_ts is None or stamp > self.last_ts:
            self.last_ts = stamp

    def add_cwd(self, value: object) -> None:
        if isinstance(value, str) and value and value not in self.cwds:
            self.cwds.append(value)

    def add_prompt(self, text: str) -> None:
        text = text.strip()
        if text and (not self.prompts or self.prompts[-1] != text):
            self.prompts.append(text)

    def add_command(self, command: str, ok: bool, exit_code, output: str) -> None:
        if not command:
            return
        entry = {"cmd": command, "key": command_key(command), "ok": ok, "exit": exit_code, "err": "" if ok else tail_lines(output)}
        self.commands.append(entry)
        if not ok:
            return
        low = command.lower()
        if re.search(r"\bgit\b[^\n]*\bcommit\b", low):
            self.commits.append(commit_message(command))
        if re.search(r"\bgit\b[^\n]*\bpush\b", low):
            self.pushes += 1
        if re.search(r"\bgh\s+pr\s+create\b", low):
            for url in re.findall(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", output or ""):
                self.prs.setdefault(url, "opened")
        if re.search(r"\bgh\s+pr\s+merge\b", low):
            number = re.search(r"\bmerge\s+(\d+)", low)
            self.prs.setdefault("#" + number.group(1) if number else "a pull request", "merged")

    @property
    def cwd(self) -> str:
        return self.cwds[0] if self.cwds else ""

    def display_title(self) -> str:
        if self.title or self.ai_title:
            return self.title or self.ai_title
        return one_line(self.prompts[0], 60) if self.prompts else "(untitled)"

    def ended(self, now) -> str:
        if self.last_event == "finished":
            return "asked_user" if self.last_agent_text.rstrip().endswith("?") else "finished"
        if self.last_event in ("interrupted", "api_error"):
            return self.last_event
        return "stopped_mid_task"

    def recent(self, now) -> bool:
        return self.last_ts is not None and (now - self.last_ts).total_seconds() < RECENT_MINUTES * 60


ENDED_TEXT = {
    "finished": "finished its last turn",
    "asked_user": "finished its last turn with a question to the user (an open decision)",
    "stopped_mid_task": "stopped mid-task: the transcript ends during a turn, with no final answer",
    "interrupted": "interrupted by the user",
    "api_error": "stopped by an error from the model provider",
}


def commit_message(command: str) -> str:
    match = re.search(r"-m\s+(\"([^\"]*)\"|'([^']*)')", command)
    if match:
        text = match.group(2) if match.group(2) is not None else match.group(3)
        return one_line(text.splitlines()[0] if text.strip() else text, 90)
    match = re.search(r"<<\s*'?\"?(\w+)'?\"?\s*\n(.*?)\n", command, re.S)
    if match and match.group(2).strip():
        return one_line(match.group(2), 90)
    return "(message not parsed)"


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
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
COMMAND_TOOLS = {"Bash", "PowerShell", "BashOutput"}


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


def compact_tail(text: str) -> str:
    match = re.search(r"\n\s*\d+\.\s*Pending Tasks\b", text)
    return text[match.start():].strip() if match else ""


def parse_claude(path: Path, config_dir: Path, since, stats: Counter, redact: Redactor) -> Session:
    session = Session("claude-code", path.stem, path, config_dir)
    pending: dict = {}
    last_message_id = None
    for record in iter_jsonl(path, stats):
        kind = record.get("type")
        if kind == "custom-title" and record.get("customTitle"):
            session.title = redact(record["customTitle"])
            continue
        if kind == "ai-title" and record.get("aiTitle"):
            session.ai_title = redact(record["aiTitle"])
            continue
        stamp = parse_ts(record.get("timestamp"))
        if kind == "pr-link":
            if stamp is None or stamp >= since:
                label = record.get("prUrl") or ("#%s" % record.get("prNumber"))
                session.prs.setdefault(redact(label), "opened")
            continue
        if str(record.get("entrypoint", "")).startswith("sdk"):
            session.skip_reason = "sdk"
        if record.get("isSidechain") or kind not in ("user", "assistant", "system"):
            continue
        if stamp is not None and stamp < since:
            if kind == "user" and not record.get("isCompactSummary") and isinstance(record.get("message"), dict):
                earlier = record["message"].get("content")
                if not (isinstance(earlier, list) and any(isinstance(p, dict) and p.get("type") == "tool_result" for p in earlier)):
                    prompt = claude_prompt(record, part_text(earlier))
                    if prompt is not None and not prompt.lstrip().startswith("[Request interrupted"):
                        session.prior_prompt = prompt
            continue
        session.add_cwd(record.get("cwd"))
        if record.get("gitBranch"):
            session.branch = str(record["gitBranch"])
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        if kind == "system":
            if record.get("subtype") == "away_summary" and record.get("content"):
                session.recap = redact(record["content"])
            continue
        session.touch(stamp)
        content = message.get("content")
        if kind == "user":
            if record.get("isCompactSummary"):
                session.compact_tail = redact(compact_tail(part_text(content)))
                continue
            results = [p for p in content if isinstance(p, dict) and p.get("type") == "tool_result"] if isinstance(content, list) else []
            if results:
                for result in results:
                    claude_result(session, pending.pop(result.get("tool_use_id"), None), result, record, redact)
                session.last_event = "working"
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"] if isinstance(content, list) else []
            if any(t.lstrip().startswith("[Request interrupted") for t in texts) or (isinstance(content, str) and content.lstrip().startswith("[Request interrupted")):
                session.last_event = "interrupted"
                continue
            if results:
                continue
            text = part_text(content)
            prompt = claude_prompt(record, text)
            if prompt is not None:
                session.add_prompt(redact(prompt))
                session.last_event = "working"
            continue
        # assistant
        if record.get("isApiErrorMessage"):
            session.last_event = "api_error"
            session.error_text = redact(one_line(part_text(content), 200))
            continue
        parts = content if isinstance(content, list) else []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
        uses = [p for p in parts if isinstance(p, dict) and p.get("type") == "tool_use"]
        for use in uses:
            pending[use.get("id")] = use
        if texts:
            joined = redact("\n".join(texts))
            if message.get("id") and message.get("id") == last_message_id and session.last_agent_text:
                session.last_agent_text += "\n" + joined
            else:
                session.last_agent_text = joined
            last_message_id = message.get("id")
        if uses:
            session.last_event = "working"
        elif texts and message.get("stop_reason") in ("end_turn", "stop_sequence", None):
            session.last_event = "finished"
    return session.finish(redact)


def claude_result(session: Session, use, result: dict, record: dict, redact: Redactor) -> None:
    text = part_text(result.get("content"))
    is_error = bool(result.get("is_error"))
    extra = record.get("toolUseResult") if isinstance(record.get("toolUseResult"), dict) else {}
    name = use.get("name", "") if isinstance(use, dict) else ""
    tool_input = use.get("input", {}) if isinstance(use, dict) and isinstance(use.get("input"), dict) else {}
    if is_error and re.search(r"doesn't want to (?:proceed|take this action)|user rejected|was rejected by the user", text, re.I):
        session.rejected.append(redact(one_line("%s: %s" % (name or "tool", tool_input.get("command") or tool_input.get("file_path") or ""), 140)))
        return
    if is_error and re.search(r"permission .{0,40}denied|denied permission", text, re.I):
        session.denied.append(redact(one_line("%s: %s" % (name or "tool", tool_input.get("command") or tool_input.get("file_path") or ""), 140)))
        return
    if name in EDIT_TOOLS or (not name and extra.get("filePath")):
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or extra.get("filePath")
        if target and not is_error:
            session.files.setdefault(redact(display_path(target, session.cwd)), None)
        return
    command = tool_input.get("command")
    if (name in COMMAND_TOOLS or isinstance(command, str)) and isinstance(command, str):
        if extra.get("interrupted"):
            return
        match = re.match(r"\s*Exit code (\d+)", text)
        exit_code = int(match.group(1)) if match else (1 if is_error else 0)
        output = text if is_error else (extra.get("stdout") or text)
        session.add_command(redact(command), not is_error, exit_code, redact(output))


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


# --------------------------------------------------------------------------------------------
# Codex rollouts


CODEX_ACTIVITY = {
    "user_message", "item_completed", "agent_message", "exec_command_end", "patch_apply_end",
    "task_started", "task_complete", "turn_aborted", "error",
}


def codex_titles(codex_home: Path, stats: Counter) -> dict:
    titles: dict = {}
    index = codex_home / "session_index.jsonl"
    if index.is_file():
        for record in iter_jsonl(index, stats):
            if record.get("id") and record.get("thread_name"):
                titles[str(record["id"])] = str(record["thread_name"])
    return titles


def codex_command_text(item: dict) -> str:
    parsed = item.get("parsed_cmd")
    command = item.get("command")
    if isinstance(command, list) and command:
        tail = [str(c) for c in command]
        if len(tail) >= 3 and tail[-2] in ("-Command", "-c", "-lc", "/c", "-NoProfile"):
            return tail[-1]
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and parsed[0].get("cmd"):
            return " && ".join(str(p.get("cmd")) for p in parsed if isinstance(p, dict) and p.get("cmd"))
        return " ".join(tail)
    return str(command or "")


def parse_codex(path: Path, codex_home: Path, since, titles: dict, stats: Counter, redact: Redactor):
    session = None
    seen_items: set = set()
    for record in iter_jsonl(path, stats):
        kind = record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if kind == "session_meta" and session is None:
            session_id = str(payload.get("id") or payload.get("session_id") or path.stem)
            session = Session("codex", session_id, path, codex_home)
            source = payload.get("source")
            originator = str(payload.get("originator", "")).lower()
            if "sdk" in originator:
                session.skip_reason = "sdk"
            elif isinstance(source, dict) and "subagent" in source or payload.get("thread_source") == "subagent":
                session.skip_reason = "subagent"
            elif payload.get("thread_source") == "automation":
                session.skip_reason = "automation"
            session.add_cwd(payload.get("cwd"))
            if session_id in titles:
                session.title = redact(titles[session_id])
            continue
        if session is None:
            return None
        stamp = parse_ts(record.get("timestamp"))
        if stamp is not None and stamp < since:
            if kind == "event_msg" and payload.get("type") == "user_message" and payload.get("message"):
                session.prior_prompt = str(payload["message"])
            elif kind == "event_msg" and payload.get("type") == "item_completed":
                item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
                if item.get("type") == "UserMessage" and part_text(item.get("content")).strip():
                    session.prior_prompt = part_text(item.get("content"))
            continue
        if kind == "turn_context":
            session.add_cwd(payload.get("cwd"))
            continue
        if kind == "compacted":
            if payload.get("message"):
                session.compact_tail = redact(str(payload["message"])[-1500:])
            continue
        if kind != "event_msg":
            continue
        event = payload.get("type")
        if event in CODEX_ACTIVITY:  # Opening a thread or a token report is not work.
            session.touch(stamp)
        if event == "user_message":
            session.add_prompt(redact(payload.get("message", "")))
            session.last_event = "working"
        elif event == "item_completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            item_id = item.get("id")
            if item_id in seen_items:
                continue
            seen_items.add(item_id)
            codex_item(session, item, redact)
        elif event == "agent_message" and payload.get("phase") in (None, "final_answer"):
            session.last_agent_text = redact(payload.get("message", ""))
        elif event == "exec_command_end":
            output = payload.get("aggregated_output") or payload.get("stderr") or payload.get("formatted_output") or ""
            exit_code = payload.get("exit_code")
            ok = exit_code in (0, "0") and payload.get("status", "completed") != "failed"
            session.add_command(redact(codex_command_text(payload)), ok, exit_code, redact(output))
        elif event == "patch_apply_end":
            if str(payload.get("success", "")).lower() == "true" and isinstance(payload.get("changes"), dict):
                for target in payload["changes"]:
                    session.files.setdefault(redact(display_path(target, session.cwd)), None)
        elif event == "task_started":
            session.last_event = "working"
        elif event == "task_complete":
            session.last_event = "finished"
            if payload.get("last_agent_message"):
                session.last_agent_text = redact(payload["last_agent_message"])
        elif event == "turn_aborted":
            session.last_event = "interrupted"
        elif event == "error":
            session.last_event = "api_error"
            session.error_text = redact(one_line(payload.get("message", ""), 200))
        elif event == "thread_name_updated" and payload.get("thread_name"):
            session.title = redact(payload["thread_name"])
    return session.finish(redact) if session is not None else None


def codex_item(session: Session, item: dict, redact: Redactor) -> None:
    item_type = item.get("type")
    if item_type == "UserMessage":
        session.add_prompt(redact(part_text(item.get("content"))))
        session.last_event = "working"
    elif item_type == "AgentMessage" and item.get("phase") in (None, "final_answer"):
        text = part_text(item.get("content"))
        if text.strip():
            session.last_agent_text = redact(text)
    elif item_type == "CommandExecution":
        exit_code = item.get("exit_code")
        ok = exit_code in (0, "0") and item.get("status") != "failed"
        output = item.get("aggregated_output") or item.get("stderr") or item.get("formatted_output") or ""
        session.add_command(redact(codex_command_text(item)), ok, exit_code, redact(output))
    elif item_type == "FileChange" and item.get("status") == "completed":
        changes = item.get("changes")
        if isinstance(changes, dict):
            for target in changes:
                session.files.setdefault(redact(display_path(target, session.cwd)), None)


# --------------------------------------------------------------------------------------------
# Gather


def claude_default_dir(home: Path) -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else home / ".claude"


def codex_default_home(home: Path) -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else home / ".codex"


def recent_files(pattern_root: Path, since_epoch: float, depth_glob: str):
    if not pattern_root.is_dir():
        return []
    found = []
    for candidate in pattern_root.glob(depth_glob):
        try:
            if candidate.is_file() and candidate.stat().st_mtime >= since_epoch:
                found.append(candidate)
        except OSError:
            continue
    return found


def window_start(args, now):
    if args.since:
        stamp = None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                stamp = dt.datetime.strptime(args.since, fmt)
                break
            except ValueError:
                continue
        if stamp is None:
            raise SystemExit(usage_error("--since must look like 2026-10-11 or 2026-10-11T09:00 (local time)."))
        return stamp.astimezone(dt.timezone.utc)  # A naive value is read as local time.
    if args.hours <= 0:
        raise SystemExit(usage_error("--hours must be greater than 0."))
    return now - dt.timedelta(hours=args.hours)


def usage_error(message: str) -> int:
    print(json.dumps({"error": message}), file=sys.stdout)
    print("Error: " + message, file=sys.stderr)
    return 2


def local(stamp) -> str:
    return stamp.astimezone().strftime("%b %d %H:%M") if stamp else "?"


def cmd_gather(args) -> int:
    home = Path.home()
    now = dt.datetime.now(dt.timezone.utc)
    since = window_start(args, now)
    since_epoch = since.timestamp()
    redact = Redactor(home)
    stats: Counter = Counter()

    claude_dirs = [] if args.no_claude else [claude_default_dir(home)] + [Path(d).expanduser() for d in args.claude_dir]
    claude_dirs = list(OrderedDict((norm_path(str(d)), d) for d in claude_dirs).values())
    codex_home = Path(args.codex_home).expanduser() if args.codex_home else codex_default_home(home)
    exclude = set(args.exclude_session)
    for variable in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID"):
        if os.environ.get(variable) and not args.include_current:
            exclude.add(os.environ[variable])

    sessions = []
    for config_dir in claude_dirs:
        for path in recent_files(config_dir / "projects", since_epoch, "*/*.jsonl"):
            session = parse_claude(path, config_dir, since, stats, redact)
            sessions.append(session)
    if not args.no_codex:
        titles = codex_titles(codex_home, stats)
        for path in recent_files(codex_home / "sessions", since_epoch, "*/*/*/rollout-*.jsonl"):
            session = parse_codex(path, codex_home, since, titles, stats, redact)
            if session is not None:
                sessions.append(session)

    kept = []
    for session in sessions:
        if session.skip_reason:
            stats["skipped_" + session.skip_reason] += 1
        elif session.session_id in exclude:
            stats["skipped_current_or_excluded"] += 1
        elif session.last_ts is None or not session.prompts:
            stats["skipped_no_activity_in_window"] += 1
        elif args.project_dir and not any(inside(c, d) for c in session.cwds for d in args.project_dir):
            stats["skipped_outside_project_dir"] += 1
        else:
            kept.append(session)
    kept.sort(key=lambda s: s.last_ts, reverse=True)
    if len(kept) > args.max_sessions:
        stats["skipped_over_max_sessions"] += len(kept) - args.max_sessions
        kept = kept[: args.max_sessions]

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
    if not kept:
        summary["error"] = "no sessions found"
        summary["hint"] = "No Claude Code or Codex session with a prompt in this window matched. Widen --hours, add --claude-dir, or drop --project-dir."
        print(json.dumps(summary, indent=2))
        return 3

    out_dir = Path(args.out).expanduser() if args.out else Path(tempfile.gettempdir()) / "pick-up-sessions" / now.astimezone().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    projects = group_by_project(kept, redact)
    digest = render_digest(projects, kept, since, now, args.max_chars)
    write_text_lf(out_dir / "digest.md", digest)
    index = build_index(projects, since, now, home)
    write_text_lf(out_dir / "sessions.json", json.dumps(index, indent=2))
    inputs_path = out_dir / "inputs.json"
    warnings = []
    if inputs_path.exists():
        warnings.append("inputs.json already existed and was left as it is; check its session tags against the new digest.")
    else:
        write_text_lf(inputs_path, json.dumps(inputs_template(since, now, args), indent=2))

    summary.update(
        {
            "out_dir": str(out_dir),
            "digest": str(out_dir / "digest.md"),
            "sessions_index": str(out_dir / "sessions.json"),
            "inputs": str(inputs_path),
            "digest_chars": len(digest),
            "sessions": {
                "total": len(kept),
                "claude_code": sum(1 for s in kept if s.tool == "claude-code"),
                "codex": sum(1 for s in kept if s.tool == "codex"),
                "projects": len(projects),
            },
            "redactions_in_digest": digest_markers(digest),
            "table": [
                {"tag": e["tag"], "tool": e["tool"], "project": e["project"], "title": e["title"], "ended": e["ended"], "may_still_be_running": e["may_still_be_running"]}
                for e in index["sessions"]
            ],
            "warnings": warnings,
        }
    )
    print(json.dumps(summary, indent=2))
    return 0


def group_by_project(sessions: list, redact: Redactor):
    """Orders projects by latest activity and assigns tags S1..Sn in document order."""
    groups: "OrderedDict[str, list]" = OrderedDict()
    for session in sessions:  # Already sorted newest first.
        label = redact(session.cwd.replace("\\", "/")) if session.cwd else "(unknown folder)"
        groups.setdefault(label, []).append(session)
    number = 0
    for members in groups.values():
        for session in members:
            number += 1
            session.tag = "S%d" % number
    return groups


def inputs_template(since, now, args) -> dict:
    if args.since:
        window = "since %s" % local(since)
    elif args.hours == 24:
        window = "of the last 24 hours"
    else:
        window = "of the last %g hours" % args.hours
    return {
        "goal": "Choose what to do next with the work in progress from my coding-agent sessions %s: what to finish, what to fix, and what to check first in my next working session." % window,
        "project": None,
        **{kind: [] for kind in FLAG_KINDS},
    }


def build_index(projects, since, now, home: Path) -> dict:
    entries = []
    default_claude = norm_path(str(home / ".claude"))
    default_codex = norm_path(str(home / ".codex"))
    for project, members in projects.items():
        for session in members:
            if session.tool == "claude-code":
                command = "claude --resume %s" % session.session_id
                env = {} if norm_path(str(session.home_dir)) == default_claude else {"CLAUDE_CONFIG_DIR": str(session.home_dir)}
            else:
                command = "codex resume %s" % session.session_id
                env = {} if norm_path(str(session.home_dir)) == default_codex else {"CODEX_HOME": str(session.home_dir)}
            entries.append(
                {
                    "tag": session.tag,
                    "tool": session.tool,
                    "session_id": session.session_id,
                    "title": session.display_title(),
                    "project": project,
                    "cwd": session.cwd,
                    "first_activity": session.first_ts.isoformat() if session.first_ts else None,
                    "last_activity": session.last_ts.isoformat() if session.last_ts else None,
                    "ended": session.ended(now),
                    "may_still_be_running": session.recent(now),
                    "prompts": len(session.prompts),
                    "resume": {"cwd": session.cwd, "command": command, "env": env},
                    "transcript": str(session.path),
                }
            )
    return {"schema": 1, "window": {"since": since.isoformat(), "until": now.isoformat()}, "sessions": entries}


LEVELS = [
    {"first": 2, "last": 4, "prompt": 300, "agent": 1200, "recap": 900, "fails": 6, "files": 15, "lists": 4},
    {"first": 1, "last": 3, "prompt": 200, "agent": 800, "recap": 500, "fails": 4, "files": 10, "lists": 3},
    {"first": 1, "last": 1, "prompt": 150, "agent": 450, "recap": 250, "fails": 2, "files": 6, "lists": 2},
]


def render_session(session: Session, now, level: dict) -> str:
    tool = "Claude Code" if session.tool == "claude-code" else "Codex"
    lines = ["### %s · %s · %s" % (session.tag, tool, one_line(session.display_title(), 80))]
    end = local(session.last_ts)
    if session.first_ts and session.last_ts and session.first_ts.astimezone().date() == session.last_ts.astimezone().date():
        end = end.split(" ")[-1]
    count = 0 if session.prompt_before_window else len(session.prompts)
    active = "Active %s to %s, %d prompt%s in the window" % (local(session.first_ts), end, count, "" if count == 1 else "s")
    if session.branch and session.branch != "HEAD":
        active += ", branch `%s`" % one_line(session.branch, 60)
    lines.append("- " + active)
    ended = ENDED_TEXT[session.ended(now)]
    if session.error_text and session.ended(now) == "api_error":
        ended += ": " + session.error_text
    if session.recent(now):
        ended += "; last activity under %d minutes ago, so it may still be running" % RECENT_MINUTES
    lines.append("- How it ended: " + ended)
    prompts = session.prompts
    first, last = level["first"], level["last"]
    if session.prompt_before_window:
        chosen = [(1, prompts[0])]
        lines.append("- Prompts: none in the window; the last one before it:")
    elif len(prompts) <= first + last:
        chosen = list(enumerate(prompts, 1))
        lines.append("- Prompts:")
    else:
        chosen = list(enumerate(prompts[:first], 1)) + list(enumerate(prompts[-last:], len(prompts) - last + 1))
        lines.append("- Prompts (first %d and last %d of %d):" % (first, last, len(prompts)))
    for number, text in chosen:
        lines.append("  %d. %s" % (number, one_line(text, level["prompt"])))
    if session.last_agent_text.strip():
        lines.append("- Last agent message: " + one_line(session.last_agent_text, level["agent"]))
    recap = " ".join(x for x in (session.recap, session.compact_tail) if x.strip())
    if recap:
        lines.append("- Recap saved by the tool: " + one_line(recap, level["recap"]))
    if session.files:
        names = list(session.files)
        shown = ", ".join("`%s`" % one_line(n, 90) for n in names[: level["files"]])
        more = " and %d more" % (len(names) - level["files"]) if len(names) > level["files"] else ""
        lines.append("- Files edited (%d): %s%s" % (len(names), shown, more))
    unresolved, resolved = split_failures(session.commands)
    if unresolved:
        lines.append("- Failed commands with no later success of the same command (%d):" % len(unresolved))
        for entry in unresolved[-level["fails"]:]:
            detail = ": " + entry["err"] if entry["err"] else ""
            lines.append("  - `%s` (exit %s)%s" % (one_line(entry["cmd"], 140), entry["exit"], detail))
    if resolved:
        lines.append("- Failed commands that later succeeded: %d" % resolved)
    for label, items in (("Tool calls the user rejected", session.rejected), ("Permission denied", session.denied)):
        if items:
            lines.append("- %s (%d): %s" % (label, len(items), "; ".join("`%s`" % i for i in items[-level["lists"]:])))
    checks = last_checks(session.commands)
    if checks:
        lines.append("- Checks, last result of each: " + "; ".join("`%s` %s" % (k, "passed" if ok else "failed") for k, ok in checks[: level["lists"] + 2]))
    if session.commits:
        lines.append("- Commits (%d): %s" % (len(session.commits), "; ".join('"%s"' % c for c in session.commits[-level["lists"]:])))
    if session.pushes:
        lines.append("- Pushes: %d" % session.pushes)
    if session.prs:
        lines.append("- Pull requests: " + "; ".join("%s %s" % (ref, state) for ref, state in list(session.prs.items())[-level["lists"]:]))
    return "\n".join(lines) + "\n"


def split_failures(commands: list):
    unresolved, resolved = [], 0
    for index, entry in enumerate(commands):
        if entry["ok"]:
            continue
        if any(later["ok"] and later["key"] == entry["key"] for later in commands[index + 1:]):
            resolved += 1
        elif not unresolved or unresolved[-1]["cmd"] != entry["cmd"]:
            unresolved.append(entry)
    return unresolved, resolved


def last_checks(commands: list):
    latest: "OrderedDict[str, bool]" = OrderedDict()
    for entry in commands:
        key = entry["key"]
        if any(word in key for word in CHECK_WORDS):
            latest.pop(key, None)
            latest[key] = entry["ok"]
    return list(reversed(list(latest.items())))


def render_digest(projects, sessions: list, since, now, max_chars: int) -> str:
    tz = now.astimezone().strftime("%Z") or now.astimezone().strftime("UTC%z")
    claude = sum(1 for s in sessions if s.tool == "claude-code")
    header = [
        "# Coding-agent sessions from %s to %s (%s)" % (local(since), local(now), tz),
        "",
        "%d sessions (%d Claude Code, %d Codex) in %d project folders. Each session has a tag, S1 to S%d, in the order below; folders are ordered by most recent activity." % (len(sessions), claude, len(sessions) - claude, len(projects), len(sessions)),
        "\"How it ended\" is read from the last records of each transcript, not from the agent's own words. Secrets, tokens, email addresses, and home-folder paths were removed before this file was written; home folders appear as ~.",
        "",
    ]
    budget = max(1500, (max_chars - 1200) // max(len(sessions), 1))
    body = []
    for project, members in projects.items():
        body.append("## Folder %s\n" % project)
        for session in members:
            text = ""
            for level in LEVELS:
                text = render_session(session, now, level)
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
    return "\n".join(header) + "\n" + text


def digest_markers(text: str) -> dict:
    return {"redacted": text.count("[REDACTED"), "email": text.count("[email]"), "user": text.count("[user]")}


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
            if kind in UNTAGGED_KINDS:
                continue
            match = TAG_RE.match(value)
            if not match:
                problems.append("Every %s value must start with session tags such as [S2] or [S1 S4], or [all]: %s" % (kind, one_line(value, 80)))
                continue
            for tag in re.findall(r"S\d+", match.group(1)):
                if tag not in known_tags:
                    problems.append("%s names %s, which is not in sessions.json: %s" % (kind, tag, one_line(value, 80)))
        values[kind] = cleaned
    if not any(values.get(k) for k in ("trajectory", "completed", "in_progress")):
        problems.append("Write at least one trajectory, completed, or in_progress value from the digest before running.")
    return {"goal": goal, "project": project, "values": values}, problems


def build_argv(exe: str, inputs: dict, dry_run: bool) -> list:
    argv = [exe, "--json", "--no-input", "agents", "suggest", "run", "--goal", inputs["goal"]]
    for category in CATEGORIES:
        argv += ["--category", category]
    argv += ["--count", str(COUNT)]
    for kind, (flag, _most) in FLAG_KINDS.items():
        for value in inputs["values"].get(kind, []):
            argv += [flag, value]
    argv += ["--files", "digest.md"]
    if inputs["project"]:
        argv += ["--project", inputs["project"]]
    argv += ["--max-total-tokens", str(MAX_TOTAL_TOKENS)]
    if dry_run:
        argv.append("--dry-run")
    return argv


def estimate_usd(usage: dict, models: list):
    """Estimates a finished run's cost in dollars, or None when it made no call or left gpt-6-sol."""
    if not usage.get("model_calls") or any(model != MODEL for model in models):
        return None
    cached = int(usage.get("cached_input_tokens") or 0)
    fresh = max(int(usage.get("input_tokens") or 0) - cached, 0)
    output = int(usage.get("output_tokens") or 0)
    rates = RATES_PER_MILLION
    dollars = (fresh * rates["input"] + cached * rates["cached_input"] + output * rates["output"]) / 1_000_000
    return round(dollars * MARKUP, 2)


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


def display_command(argv: list) -> str:
    shown = ["vidbyte-cli"] + argv[1:]
    return " ".join(shlex.quote(a) for a in shown)


def cmd_suggest(args) -> int:
    out_dir = Path(args.out).expanduser()
    if args.dry_run == args.paid:
        return usage_error("Pass exactly one of --dry-run (free check) or --paid (the usage-billed run, only after the user said yes).")
    if not (out_dir / "digest.md").is_file():
        return usage_error("digest.md not found in %s. Run `gather` first and pass the out_dir it printed." % out_dir)
    index = load_json(out_dir / "sessions.json", "sessions.json")
    known = {e.get("tag") for e in index.get("sessions", []) if isinstance(e, dict)}
    redact = Redactor(Path.home())
    inputs, problems = validate_inputs(load_json(out_dir / "inputs.json", "inputs.json"), known, redact)
    if problems:
        print(json.dumps({"error": "inputs.json needs fixing", "problems": problems}, indent=2))
        for problem in problems:
            print("inputs.json: " + problem, file=sys.stderr)
        return 2
    exe = shutil.which("vidbyte-cli")
    if not exe:
        print(json.dumps({"error": "vidbyte-cli is not on PATH. Follow references/setup.md step 1."}))
        return 127
    marker = out_dir / "paid-run.txt"
    if args.paid and (marker.exists() or (out_dir / "result.json").exists()):
        # The model calls an earlier paid run here finished were billed, and nothing recovers
        # them. Another run is a new charge, so it needs the user's yes and a new folder.
        return usage_error(
            "A paid run was already started in this folder (see paid-run.txt and result.json), and the model calls it finished were billed. "
            "Read result.json if it is there. Any other run is a new usage-billed run: ask the user first, then gather into a new folder."
        )
    argv = build_argv(exe, inputs, args.dry_run)
    length = sum(len(a) + 3 for a in argv)
    if length > MAX_ARGV_CHARS:
        return usage_error("The command would be %d characters; shorten the inputs.json values to stay under %d." % (length, MAX_ARGV_CHARS))
    if args.paid:
        marker.write_text("Paid run started %s with --max-total-tokens %d.\n" % (dt.datetime.now().isoformat(timespec="seconds"), MAX_TOTAL_TOKENS), encoding="utf-8")
    print("Running in %s:\n%s" % (out_dir, display_command(argv)), file=sys.stderr)
    code, stdout_path, stderr_path, error = run_cli(argv, out_dir, "dry-run.json" if args.dry_run else "result.json")
    report: dict = {"exit_code": code, "stderr_file": str(stderr_path)}
    if code == 0:
        try:
            document = json.loads(stdout_path.read_text(encoding="utf-8"))
            data = document.get("data", {})
        except ValueError:
            document, data = {}, {}
        settings = data.get("settings") or {}
        models = list(OrderedDict.fromkeys(m for m in (settings.get("model"), settings.get("critic_model")) if m))
        usage = data.get("usage") or {}
        report.update(
            {
                "result_file": str(stdout_path),
                "kind": document.get("kind"),
                "status": data.get("status"),
                "stop_reason": data.get("stop_reason"),
                "ideas": len(data.get("ideas") or []),
                "models": models,
                "usage": {key: int(usage.get(key) or 0) for key in USAGE_KEYS},
                "estimated_cost_usd": estimate_usd(usage, models),
                "context_manifest": [{"ref": m.get("ref"), "kind": m.get("kind"), "status": m.get("status")} for m in data.get("context_manifest") or []],
                "warnings": data.get("warnings") or [],
            }
        )
    elif error:
        report["error"] = {k: error.get(k) for k in ("code", "message", "hint", "retryable") if k in error}
    print(json.dumps(report, indent=2))
    return code


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
    exe = shutil.which("vidbyte-cli")
    if not exe:
        print(json.dumps({"error": "vidbyte-cli is not on PATH. Follow references/setup.md step 1."}))
        return 127
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
        prog="sessions.py",
        description="Turn recent Claude Code and Codex sessions into ranked next steps with the Vidbyte Suggestion Agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 sessions.py gather                      # last 24 hours, every project\n"
            "  python3 sessions.py gather --hours 72 --project-dir .\n"
            "  python3 sessions.py gather --claude-dir ~/.claude-work\n"
            "  python3 sessions.py suggest --out <out_dir> --dry-run   # free\n"
            "  python3 sessions.py suggest --out <out_dir> --paid      # billed by usage; ask the user first\n"
            "  python3 sessions.py feedback --out <out_dir> --idea idea-002 --reject --reason \"too big for tomorrow\"\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    gather = sub.add_parser("gather", help="Read, distill, and redact recent sessions into digest.md (free, local).", description="Read the session transcripts of a time window, distill each session, redact secrets, emails, and home paths, and write digest.md, sessions.json, and an inputs.json template into a new folder. Prints a JSON summary. Exit 3 means no session matched.")
    gather.add_argument("--hours", type=float, default=24.0, help="Window length in hours, ending now (default: 24).")
    gather.add_argument("--since", help="Window start in local time, such as 2026-10-11 or 2026-10-11T09:00. Overrides --hours.")
    gather.add_argument("--project-dir", action="append", default=[], help="Keep only sessions that ran in this folder or below it. Repeat for more folders.")
    gather.add_argument("--claude-dir", action="append", default=[], help="An extra Claude Code config folder to read, such as ~/.claude-work. Repeatable. $CLAUDE_CONFIG_DIR, else ~/.claude, is always read.")
    gather.add_argument("--codex-home", help="The Codex folder to read (default: $CODEX_HOME, else ~/.codex).")
    gather.add_argument("--no-claude", action="store_true", help="Skip Claude Code sessions.")
    gather.add_argument("--no-codex", action="store_true", help="Skip Codex sessions.")
    gather.add_argument("--exclude-session", action="append", default=[], help="A session id to leave out. Repeatable.")
    gather.add_argument("--include-current", action="store_true", help="Keep the session this command runs in (left out by default when the host exposes its id).")
    gather.add_argument("--max-sessions", type=int, default=MAX_SESSIONS, help="Keep at most this many of the most recent sessions (default: %d)." % MAX_SESSIONS)
    gather.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Size limit for digest.md in characters (default: %d)." % DEFAULT_MAX_CHARS)
    gather.add_argument("--out", help="Output folder (default: a new folder under the system temp folder).")

    suggest = sub.add_parser("suggest", help="Check inputs.json and run vidbyte-cli agents suggest run (--dry-run is free; --paid is billed by usage).", description="Validate <out>/inputs.json against the digest, then run `vidbyte-cli --json --no-input agents suggest run` from inside <out>. Saves stdout to dry-run.json or result.json and stderr to a .stderr.txt file, prints a JSON summary, and exits with vidbyte-cli's exit code.")
    suggest.add_argument("--out", required=True, help="The out_dir that gather printed.")
    suggest.add_argument("--dry-run", action="store_true", help="Validate the full request for free: no model call, no charge.")
    suggest.add_argument("--paid", action="store_true", help="The usage-billed run. Use only after the user agreed to the cost.")

    feedback = sub.add_parser("feedback", help="Record the user's explicit accept or reject of one idea (free, local).", description="Copy the exact title of one idea from <out>/result.json into `vidbyte-cli --json agents suggest feedback accept|reject`.")
    feedback.add_argument("--out", required=True, help="The out_dir that holds result.json.")
    feedback.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
    verdict = feedback.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accept", action="store_true", help="The user said they want this idea.")
    verdict.add_argument("--reject", action="store_true", help="The user said no to this idea.")
    feedback.add_argument("--reason", help="The user's own reason, if they gave one.")
    feedback.add_argument("--project", help="The suggestion project key (default: project in inputs.json).")

    args = parser.parse_args(argv)
    if args.command == "gather":
        return cmd_gather(args)
    if args.command == "suggest":
        return cmd_suggest(args)
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
