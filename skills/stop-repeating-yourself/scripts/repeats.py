#!/usr/bin/env python3
"""Find what you keep repeating to Claude Code and Codex, and prepare it for the Vidbyte Suggestion Agent.

Four subcommands, used in this order by the stop-repeating-yourself skill:

  gather    Free and local. Reads the Claude Code and Codex transcripts and history files of a
            time window, finds the instructions, requests, context, approvals, follow-ups, and
            failures that repeat across sessions, checks the current agent setup, compares the
            result with the state file of earlier runs, redacts secrets, emails, and home paths,
            and writes digest.md (the only file that is ever sent), patterns.json (a local index),
            and an inputs.json template into a new output folder. Then it updates the state file.
  suggest   Checks inputs.json (the values the coding agent wrote from the digest), then runs
            `vidbyte-cli --json --no-input agents suggest run ...` from inside the output folder.
            --dry-run is free; --paid is the one billed step and needs the user's yes first.
  feedback  Records the user's explicit accept or reject of one idea with
            `vidbyte-cli agents suggest feedback`, and records in the state file which repetition
            patterns that fix is meant to stop, so the next run can check whether it worked.
  status    Reads only the state file. With --remind it prints one line when a new run is due.

Only the Python standard library is used, and Python 3.8 or newer is enough. vidbyte-cli is
started without a shell, so no argument quoting differs between bash, zsh, PowerShell, and cmd.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, OrderedDict, defaultdict
from pathlib import Path

SLUG = "stop-repeating-yourself"
PROJECT_KEY = "stop-repeating-yourself"
CATEGORIES = ("leverage", "quick_wins", "simplification", "risk_prevention")
COUNT = 10
# The run starts no new model call once it has used this many tokens, which bounds its cost.
MAX_TOTAL_TOKENS = 400_000

# vidbyte-cli's default model for both roles, and its regular rates in US dollars per million
# tokens (https://developers.openai.com/api/docs/models/gpt-6-sol), which Vidbyte's model gateway
# bills plus a 2% markup. They only estimate a finished run's cost; vidbyte.pro/usage is exact.
MODEL = "gpt-6-sol"
RATES_PER_MILLION = {"input": 2.0, "cached_input": 0.2, "output": 10.0}
MARKUP = 1.02
USAGE_KEYS = ("model_calls", "input_tokens", "cached_input_tokens", "output_tokens")
FORBIDDEN = (
    "allowlisting commands that write, delete, push, or spend money",
    "switching off permission prompts or the sandbox altogether",
)
DEFAULT_DAYS = 30
DEFAULT_MAX_CHARS = 60_000
MAX_PATTERNS = 25
MAX_PER_KIND = 8
MAX_ERRORS = 4  # Places in the digest for the agent's own repeated failures.
DUE_DAYS = 7
MIN_SESSIONS_AFTER_FIX = 3
STATE_SCHEMA = 1

# Values the coding agent may put in inputs.json, in the order they are passed, with the
# vidbyte-cli flag each one becomes and the most values this skill passes for it.
FLAG_KINDS = OrderedDict(
    [
        ("mistakes", ("--mistakes", 8)),
        ("success", ("--success", 4)),
        ("outcomes", ("--outcomes", 4)),
        ("context", ("--context", 5)),
        ("capability", ("--capability", 2)),
        ("constraint", ("--constraint", 3)),
        ("avoid", ("--avoid", 3)),
    ]
)
UNTAGGED_KINDS = ("constraint", "avoid")  # The user's own words; --avoid must match verbatim.
MAX_VALUE_CHARS = 400
MAX_GOAL_CHARS = 600
MAX_ARGV_CHARS = 24_000  # Windows refuses command lines over 32,767 characters.
TAG_RE = re.compile(r"^\[((?:P\d+)(?:[ ,]+P\d+)*|setup|all)\]\s+\S")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

KIND_TEXT = {
    "correction": "correction",
    "start_preference": "preference restated at session start",
    "repeated_request": "repeated request",
    "repeated_context": "repeated context",
    "approval": "permission approval",
    "rejected_action": "rejected tool call",
    "follow_up": "follow-up after the agent finished",
    "go_ahead": "go-ahead after the agent stopped",
    "repeated_error": "repeated failing command",
}
KIND_PREFIX = {
    "correction": "corr",
    "start_preference": "pref",
    "repeated_request": "req",
    "repeated_context": "ctx",
    "approval": "appr",
    "rejected_action": "rej",
    "follow_up": "fup",
    "go_ahead": "go",
    "repeated_error": "err",
}


# --------------------------------------------------------------------------------------------
# Redaction (the rules of the pick-up-where-my-sessions-left-off skill, plus webhook links)


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
        # Webhook links are secrets in their path, so the whole link goes.
        ("token", re.compile(r"(?i)https?://hooks\.slack\.com/(?:services|workflows|triggers)/[^\s\"'<>)]+"), "[REDACTED webhook]"),
        ("token", re.compile(r"(?i)https?://(?:[a-z]+\.)?discord(?:app)?\.com/api/webhooks/[^\s\"'<>)]+"), "[REDACTED webhook]"),
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


def iso(stamp) -> str:
    return stamp.astimezone(dt.timezone.utc).isoformat() if stamp else ""


def iter_lines(path: Path, stats: Counter):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                yield line
    except OSError:
        stats["unreadable_files"] += 1


def load_line(line: str, stats: Counter):
    line = line.strip()
    if not line:
        return None
    try:
        record = json.loads(line)
    except ValueError:
        stats["unreadable_lines"] += 1
        return None
    return record if isinstance(record, dict) else None


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


def read_text(path: Path, limit: int = 400_000) -> str:
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def load_json_file(path: Path):
    try:
        return json.loads(read_text(path, 2_000_000))
    except ValueError:
        return None


def usage_error(message: str) -> int:
    print(json.dumps({"error": message}), file=sys.stdout)
    print("Error: " + message, file=sys.stderr)
    return 2


def local(stamp) -> str:
    return stamp.astimezone().strftime("%b %d") if stamp else "?"


def local_full(stamp) -> str:
    return stamp.astimezone().strftime("%b %d %H:%M") if stamp else "?"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    write_file(temp, text)
    os.replace(str(temp), str(path))


def write_file(path: Path, text: str) -> None:
    """Writes UTF-8 text with LF line endings on every system (Path.write_text has no newline= before 3.10)."""
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


# --------------------------------------------------------------------------------------------
# Words and commands

STOPWORDS = set(
    """
    a an the and or but if then else of to in on at by for with from into onto over under as is are was
    were be been being am do does did done doing have has had having it its it's this that these those
    there here i i'm me my mine you you're your yours we our us they them their he she his her him please
    pls plz just also so very really can could would should will shall may might must let lets let's what
    which who whom whose when where why how all any some each every no not dont don't do not ok okay yes
    yeah now again still only even more most much many one up out about after before same like make sure
    want need get got go going thing things something stuff way lot bit sure thanks thank hey hi hello
    always never instead rather stop remember use using used keep actually wait nope nah wrong said told
    already than every each time times from then there's that's isn't aren't won't can't didn't doesn't
    pasted image text line lines
    """.split()
)
WORD_RE = re.compile(r"[a-z0-9][a-z0-9+#]*(?:[._-][a-z0-9+#]+)*")
URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+")
PLACEHOLDER_RE = re.compile(r"\[(?:Pasted text|Image|Pasted image)[^\]]{0,40}\]", re.I)
PATH_RE = re.compile(r"(?<![\w@/\\.:-])(?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/]|[\\/])?(?:[\w.@-]+[\\/])+[\w.@-]+")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+|;\s+")


def stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def content_terms(text: str) -> frozenset:
    lowered = PLACEHOLDER_RE.sub(" ", URL_RE.sub(" ", str(text).lower()))
    out = set()
    for word in WORD_RE.findall(lowered):
        word = word.strip("._-")
        if len(word) < 2 or word in STOPWORDS or word.isdigit():
            continue
        out.add(stem(word))
    return frozenset(out)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def sentences(text: str):
    for raw in SENT_SPLIT.split(PLACEHOLDER_RE.sub(" ", str(text))):
        sentence = raw.strip(" \t-*•>#`\"'")
        if 6 <= len(sentence) <= 300:
            yield sentence


CHECK_WORDS = ("test", "pytest", "jest", "vitest", "lint", "ruff", "mypy", "pyright", "tsc", "eslint", "flake8", "clippy", "build", "check", "typecheck")
SKIP_PREFIX_WORDS = {"cd", "pushd", "set-location", "export", "source", ".", "set", "sl"}


def is_check_command(key: str) -> bool:
    """True for test, lint, type-check, CI, and build commands, judged by their words and by the parts
    of a script's path, such as lint/run.py or scripts/run_ci.py."""
    for word in key.split():
        parts = re.split(r"[\\/._-]+", word.strip("-:")) if re.search(r"[\\/.]", word.strip(".")) else [word.strip("-:")]
        for part in parts:
            if part in CHECK_WORDS or part in ("tests", "verify", "ci") or part.startswith(("test", "lint")):
                return True
    return False


def command_key(command: str) -> str:
    first = str(command).strip().splitlines()[0] if str(command).strip() else ""
    for segment in re.split(r"&&|\|\||;|\|", first):
        tokens = [t.strip("\"'") for t in segment.split()]
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if not tokens or tokens[0].lower() in SKIP_PREFIX_WORDS:
            continue
        width = 3 if len(tokens) > 2 and tokens[1] in ("-m", "run", "exec", "x", "pr", "issue", "run", "repo", "api", "workflow") else 2
        return " ".join(tokens[:width]).lower()
    return one_line(first, 40).lower()


WRITE_MARKERS = re.compile(
    r"(?i)(?:^|[\s;&|(])(?:rm|rmdir|del|erase|mv|move|cp|copy|mkdir|touch|chmod|chown|kill|pkill|taskkill|"
    r"remove-item|move-item|copy-item|new-item|set-content|add-content|out-file|stop-process|set-itemproperty|"
    r"sed\s+-i|tee|dd|truncate|shred|vidbyte-cli|vidbyte)(?=$|[\s;&|)])"
    r"|\bgit\s+(?:push|commit|reset|checkout|switch|merge|rebase|clean|stash|tag|branch\s+-[dDmM]|worktree\s+(?:add|remove|prune)|cherry-pick|revert|am|apply|restore|rm|mv|pull|fetch|init|clone)\b"
    r"|\b(?:npm|pnpm|yarn|bun)\s+(?:install|i|add|remove|uninstall|publish|update|upgrade|ci|link)\b"
    r"|\b(?:pip|pip3|pipx|uv|poetry|cargo|go|gem|brew|apt|apt-get|choco|winget|scoop)\s+(?:install|uninstall|add|remove|publish|upgrade|update|get)\b"
    r"|\bgh\s+(?:pr\s+(?:create|merge|close|edit|comment|review|ready)|issue\s+(?:create|close|edit|comment)|repo\s+(?:create|delete|fork)|release\s+create|api\s+.*-X\s*(?:POST|PUT|PATCH|DELETE))\b"
    r"|\bcurl\b.*\s-(?:X\s*(?:POST|PUT|PATCH|DELETE)|d|F|T)\b"
    r"|--force\b|(?<![0-9&])>(?!&|\s*/dev/null|\s*nul\b)"
)
READ_ONLY_FIRST = {
    "ls", "dir", "cat", "head", "tail", "less", "more", "wc", "pwd", "which", "where", "whoami", "echo",
    "printf", "tree", "stat", "file", "du", "df", "env", "printenv", "date", "uname", "rg", "grep",
    "find", "fd", "jq", "sort", "uniq", "cut", "diff", "type", "get-childitem", "gci", "get-content",
    "gc", "select-string", "sls", "get-item", "gi", "get-location", "test-path", "get-command",
    "resolve-path", "measure-object", "get-date", "get-process", "hostname", "nproc", "id",
}
READ_ONLY_SECOND = {
    "git": {"status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "remote", "describe", "shortlog", "reflog", "grep", "ls-tree", "cat-file", "branch", "config", "--version", "version"},
    "gh": {"pr view", "pr list", "pr checks", "pr status", "pr diff", "issue view", "issue list", "run view", "run list", "run watch", "repo view", "auth status", "workflow list", "workflow view", "search prs", "search issues", "--version"},
    "npm": {"ls", "list", "view", "outdated", "--version", "-v", "config"},
    "pnpm": {"ls", "list", "outdated", "--version"},
    "pip": {"list", "show", "freeze", "--version"},
    "python": {"--version", "-V"},
    "python3": {"--version", "-V"},
    "py": {"--version", "-V"},
    "node": {"--version", "-v"},
    "docker": {"ps", "images", "logs", "inspect", "version"},
    "kubectl": {"get", "describe", "logs", "version"},
    "cargo": {"tree", "--version"},
    "go": {"version", "env", "list"},
    "codex": {"--version", "login"},
    "claude": {"--version"},
}
RUNS_CODE_FIRST = {
    "pytest", "jest", "vitest", "tsc", "mypy", "pyright", "ruff", "eslint", "prettier", "make", "cargo", "go",
    "npm", "pnpm", "yarn", "bun", "npx", "python", "python3", "py", "node", "deno", "uv", "poetry", "dotnet",
    "mvn", "gradle", "./gradlew", "bash", "sh", "zsh", "pwsh", "powershell", "powershell.exe", "ruby", "rake",
    "php", "composer", "java", "swift", "xcodebuild", "terraform", "pre-commit", "tox", "nox",
}


def split_command(command: str) -> list:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def safety_class(command: str) -> str:
    """read_only, runs_code, writes, or unknown, from the command's first words."""
    text = str(command).strip()
    if not text:
        return "unknown"
    if WRITE_MARKERS.search(text):
        return "writes"
    classes = set()
    for segment in re.split(r"&&|\|\||;|\|", text.splitlines()[0]):
        tokens = [t.strip("\"'") for t in segment.split()]
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        if not tokens or tokens[0].lower() in SKIP_PREFIX_WORDS:
            continue
        first = tokens[0].lower()
        second = " ".join(t.lower() for t in tokens[1:3])
        if first == "find" and re.search(r"\s-(?:delete|exec|execdir|ok)\b", segment):
            return "writes"
        if first in READ_ONLY_FIRST:
            classes.add("read_only")
        elif first in READ_ONLY_SECOND and any(second == s or second.startswith(s + " ") or (tokens[1:2] and tokens[1].lower() == s) for s in READ_ONLY_SECOND[first]):
            classes.add("read_only")
        elif first in RUNS_CODE_FIRST:
            classes.add("runs_code")
        else:
            classes.add("unknown")
    if not classes:
        return "unknown"
    for name in ("unknown", "runs_code", "read_only"):
        if name in classes:
            return name
    return "unknown"


TOOL_MISSING_RE = re.compile(
    r"(?i)command not found|is not recognized as|not recognized as the name|no module named|modulenotfounderror|"
    r"cannot find module|executable file not found|not a valid statement separator|can.t open file|cannot open file|"
    r"is not installed"
)
USAGE_ERROR_RE = re.compile(
    r"(?i)unknown (?:option|flag|argument|command|subcommand)|unrecognized (?:option|arguments?)|invalid (?:option|choice)|"
    r"unexpected argument|no such option|no such command|^usage:|did you mean|permission denied|access is denied|"
    r"operation not permitted"
)


def setup_error(signature: str, check: bool) -> bool:
    """True when a failure is a setup or usage mistake: a missing tool, module, or shell feature, or a
    wrong flag or subcommand. For a check command only a missing tool counts."""
    text = signature.split(": ", 1)[-1]
    return bool(TOOL_MISSING_RE.search(text) or (not check and USAGE_ERROR_RE.search(text)))
ERROR_WORDS = re.compile(r"(?i)error|not found|no such|not recognized|denied|unknown|invalid|cannot|can't|failed|fatal|exception|traceback|unexpected|refused|missing")


def error_signature(exit_code, output: str) -> str:
    lines = [ln.strip() for ln in str(output).splitlines() if ln.strip()]
    lines = [ln for ln in lines if not re.match(r"^(?:Exit code \d+|Wall time:.*|Output:)$", ln)]
    picked = (
        next((ln for ln in lines if TOOL_MISSING_RE.search(ln)), None)
        or next((ln for ln in lines if USAGE_ERROR_RE.search(ln)), None)
        or next((ln for ln in lines if ERROR_WORDS.search(ln)), lines[0] if lines else "")
    )
    picked = re.sub(r"https?://\S+", "<url>", picked.lower())
    picked = re.sub(r"^(?:\S*[\\/])?(?:bash|sh|zsh|dash)(?:\.exe)?:\s*(?:line \d+:\s*)?", "", picked)  # "/usr/bin/bash: x" = "bash: x"
    picked = PATH_RE.sub("<path>", picked)
    picked = re.sub(r"\b\d+\b", "#", picked)
    picked = re.sub(r"[\"'`]", "", picked)
    return "exit %s: %s" % (exit_code, one_line(picked, 80))


# --------------------------------------------------------------------------------------------
# Session model


class Prompt:
    __slots__ = ("text", "ts", "index", "after_finished", "after_question", "session", "pid", "request")

    def __init__(self, text: str, ts, index: int, after_finished, after_question) -> None:
        self.text = text
        self.ts = ts
        self.index = index  # Position among every human prompt of the session, 0 = the first.
        self.after_finished = after_finished  # True, False, or None when unknown (history only).
        self.after_question = after_question
        self.session = None
        self.pid = 0
        self.request = None


class Session:
    def __init__(self, tool: str, session_id: str, path, home_dir: Path, source: str = "transcript") -> None:
        self.tool = tool
        self.session_id = session_id
        self.path = path
        self.home_dir = home_dir
        self.source = source
        self.cwds: list = []
        self.first_ts = None
        self.last_ts = None
        self.prompts: list = []
        self.prompt_count = 0
        self.last_prompt = ("", None)
        self.approvals: list = []  # (key, command, ts)
        self.rejections: list = []
        self.failures: list = []  # (command key, signature, command, ts)
        self.skip_reason = ""
        self.last_event = ""
        self.last_agent_text = ""

    @property
    def key(self):
        return (self.tool, self.session_id)

    @property
    def cwd(self) -> str:
        return self.cwds[0] if self.cwds else ""

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

    def add_prompt(self, text: str, stamp, since, after_finished=None, after_question=None) -> None:
        text = str(text).strip()
        if not text:
            return
        previous_text, previous_ts = self.last_prompt
        if previous_text == text and (previous_ts is None or stamp is None or abs((stamp - previous_ts).total_seconds()) < 10):
            return  # Codex can record one prompt twice, as an event and as an item.
        self.last_prompt = (text, stamp)
        index = self.prompt_count
        self.prompt_count += 1
        if stamp is not None and stamp < since:
            return
        self.touch(stamp)
        self.prompts.append(Prompt(text, stamp, index, after_finished, after_question))


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
COMMAND_TOOLS = {"Bash", "PowerShell", "BashOutput"}
USER_REJECT_RE = re.compile(r"doesn't want to (?:proceed|take this action)|user rejected|was rejected by the user|rejected by user", re.I)


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


NOT_PERMISSION_TOOLS = {"AskUserQuestion", "ExitPlanMode", "TodoWrite", "Task", "Agent"}


def approval_target(name: str, tool_input: dict):
    """Returns (key, shown command) for a tool call the user approved or rejected."""
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, str) and command.strip():
        return "%s:%s" % (name or "Bash", command_key(command)), command
    if name == "WebFetch" and isinstance(tool_input.get("url"), str):
        host = re.sub(r"^https?://", "", tool_input["url"]).split("/")[0]
        return "WebFetch:%s" % host.lower(), "WebFetch " + host
    if name:
        return name, name
    return None, None


def history_lookup(index: dict, session_id: str, stamp):
    """The display text of the history entry typed for this prompt, if one is within 15 seconds."""
    if stamp is None:
        return None
    best, best_gap = None, 15.0
    for when, display in index.get(session_id, ()):
        gap = abs((when - stamp).total_seconds())
        if gap <= best_gap:
            best, best_gap = display, gap
    return best


def parse_claude(path: Path, config_dir: Path, since, stats: Counter, history_index: dict) -> Session:
    session = Session("claude-code", path.stem, path, config_dir)
    pending: dict = {}
    last_message_id = None
    for line in iter_lines(path, stats):
        if '"type":"user"' not in line and '"type":"assistant"' not in line and '"type": "' not in line:
            continue  # Attachments, modes, titles, and progress records hold nothing this needs.
        record = load_line(line, stats)
        if record is None:
            continue
        kind = record.get("type")
        if str(record.get("entrypoint", "")).startswith("sdk"):
            session.skip_reason = "sdk"
            break
        if record.get("isSidechain") or kind not in ("user", "assistant"):
            continue
        stamp = parse_ts(record.get("timestamp"))
        in_window = stamp is None or stamp >= since
        session.add_cwd(record.get("cwd"))
        message = record.get("message") if isinstance(record.get("message"), dict) else {}
        content = message.get("content")
        if kind == "user":
            if record.get("isCompactSummary"):
                continue
            results = [p for p in content if isinstance(p, dict) and p.get("type") == "tool_result"] if isinstance(content, list) else []
            if results:
                for result in results:
                    use = pending.pop(result.get("tool_use_id"), None)
                    if in_window:
                        claude_result(session, use, result, record, stamp)
                session.last_event = "working"
                continue
            text = part_text(content)
            if text.lstrip().startswith("[Request interrupted"):
                session.last_event = "interrupted"
                continue
            prompt = claude_prompt(record, text)
            if prompt is None:
                continue
            finished = session.last_event == "finished"
            question = finished and session.last_agent_text.rstrip().endswith("?")
            typed = history_lookup(history_index, session.session_id, stamp) if in_window else None
            session.add_prompt(typed or prompt, stamp, since, finished, question)
            session.last_event = "working"
            continue
        if in_window:
            session.touch(stamp)
        if record.get("isApiErrorMessage"):
            session.last_event = "api_error"
            continue
        parts = content if isinstance(content, list) else []
        texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text" and p.get("text")]
        uses = [p for p in parts if isinstance(p, dict) and p.get("type") == "tool_use"]
        for use in uses:
            pending[use.get("id")] = use
        if texts:
            joined = "\n".join(texts)
            if message.get("id") and message.get("id") == last_message_id and session.last_agent_text:
                session.last_agent_text += "\n" + joined
            else:
                session.last_agent_text = joined
            last_message_id = message.get("id")
        if uses:
            session.last_event = "working"
        elif texts and message.get("stop_reason") in ("end_turn", "stop_sequence", None):
            session.last_event = "finished"
    return session


def claude_result(session: Session, use, result: dict, record: dict, stamp) -> None:
    text = part_text(result.get("content"))
    is_error = bool(result.get("is_error"))
    name = use.get("name", "") if isinstance(use, dict) else ""
    tool_input = use.get("input", {}) if isinstance(use, dict) and isinstance(use.get("input"), dict) else {}
    decision = record.get("permissionDecision") if isinstance(record.get("permissionDecision"), dict) else {}
    source = str(decision.get("source") or "")
    verdict = str(decision.get("decision") or "")
    key, shown = approval_target(name, tool_input) if name not in NOT_PERMISSION_TOOLS else (None, None)
    if key and ((verdict == "reject" and source.startswith("user")) or (is_error and USER_REJECT_RE.search(text))):
        session.rejections.append((key, shown, stamp))
        return
    if key and verdict == "accept" and source.startswith("user") and "perm" not in source:
        session.approvals.append((key, shown, stamp))  # Approved once, by hand, at a prompt.
    command = tool_input.get("command")
    if is_error and name in COMMAND_TOOLS and isinstance(command, str):
        extra = record.get("toolUseResult") if isinstance(record.get("toolUseResult"), dict) else {}
        if extra.get("interrupted"):
            return
        match = re.match(r"\s*Exit code (\d+)", text)
        exit_code = int(match.group(1)) if match else 1
        session.failures.append((command_key(command), error_signature(exit_code, text), command, stamp))


# --------------------------------------------------------------------------------------------
# Codex rollouts

CODEX_HEAD = re.compile(r'"type":\s*"([a-z_]+)",\s*"payload":\s*\{\s*"type":\s*"([A-Za-z_]+)"')
CODEX_SKIP_TOP = {"token_usage_record", "world_state", "inter_agent_communication_metadata", "compacted"}
CODEX_EVENTS = {"user_message", "item_completed", "agent_message", "exec_command_end", "task_started", "task_complete", "turn_aborted", "error"}
CODEX_SHELL_TOOLS = {"shell_command", "shell", "exec_command", "local_shell", "container.exec"}


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


def load_codex_rules(codex_home: Path) -> list:
    """Every allow prefix from Codex's rules files, as token lists."""
    rules = []
    folder = codex_home / "rules"
    if not folder.is_dir():
        return rules
    for path in sorted(folder.glob("*.rules")):
        text = read_text(path, 2_000_000)
        for match in re.finditer(r"prefix_rule\(\s*pattern\s*=\s*\[(.*?)\]\s*,\s*decision\s*=\s*\"(\w+)\"", text, re.S):
            if match.group(2) != "allow":
                continue
            tokens = [json.loads('"%s"' % t) if "\\" in t else t for t in re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(1))]
            if tokens:
                rules.append(tokens)
    return rules


SHELL_WRAPPERS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe", "bash", "sh", "zsh", "cmd", "cmd.exe"}


def rule_covers(rules: list, command: str) -> bool:
    tokens = split_command(command)
    text = command.strip()
    for pattern in rules:
        if tokens[: len(pattern)] == pattern:
            return True
        if len(pattern) >= 3 and pattern[0].lower() in SHELL_WRAPPERS and pattern[1].startswith(("-", "/")):
            inner = pattern[2]
            if text == inner or text.startswith(inner + " "):
                return True
    return False


def parse_codex(path: Path, codex_home: Path, since, stats: Counter, rules: list):
    session = None
    seen_items: set = set()
    pending: dict = {}
    policy, reviewer = "", ""
    for line in iter_lines(path, stats):
        head = CODEX_HEAD.search(line[:260])
        top, sub = (head.group(1), head.group(2)) if head else (None, None)
        if top in CODEX_SKIP_TOP:
            continue
        if top == "response_item":
            if sub == "function_call":
                if "require_escalated" not in line and "with_escalated_permissions" not in line:
                    continue
            elif sub == "function_call_output":
                if not pending:
                    continue
            else:
                continue
        if top == "event_msg" and sub not in CODEX_EVENTS:
            continue
        record = load_line(line, stats)
        if record is None:
            continue
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
            elif originator == "codex_exec" or source == "exec":
                session.skip_reason = "non_interactive"  # `codex exec`: a script, not a person, wrote the prompt.
            session.add_cwd(payload.get("cwd"))
            if session.skip_reason:
                return session
            continue
        if session is None:
            return None
        stamp = parse_ts(record.get("timestamp"))
        in_window = stamp is None or stamp >= since
        if kind == "turn_context":
            session.add_cwd(payload.get("cwd"))
            policy = str(payload.get("approval_policy") or "")
            reviewer = str(payload.get("approvals_reviewer") or "")
            continue
        if kind == "response_item":
            ptype = payload.get("type")
            if ptype == "function_call" and payload.get("name") in CODEX_SHELL_TOOLS:
                try:
                    arguments = json.loads(payload.get("arguments") or "{}")
                except ValueError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    continue
                escalated = arguments.get("sandbox_permissions") == "require_escalated" or arguments.get("with_escalated_permissions") is True
                if not escalated or not in_window:
                    continue
                if policy == "never" or reviewer not in ("", "user"):
                    continue  # Nobody was asked: the policy never prompts, or a reviewer answers.
                command = codex_command_text(arguments)
                if command and not rule_covers(rules, command):
                    pending[payload.get("call_id")] = (command, stamp)
            elif ptype == "function_call_output" and payload.get("call_id") in pending:
                command, when = pending.pop(payload.get("call_id"))
                output = payload.get("output")
                output = output if isinstance(output, str) else json.dumps(output)
                key = "shell:%s" % command_key(command)
                if "rejected by user" in output.lower():
                    session.rejections.append((key, command, when))
                else:
                    session.approvals.append((key, command, when))
            continue
        if kind != "event_msg":
            continue
        event = payload.get("type")
        if event in CODEX_EVENTS and in_window:
            session.touch(stamp)
        if event == "user_message":
            codex_prompt(session, str(payload.get("message", "")), stamp, since)
        elif event == "item_completed":
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            item_id = item.get("id")
            if item_id in seen_items:
                continue
            seen_items.add(item_id)
            item_type = item.get("type")
            if item_type == "UserMessage":
                codex_prompt(session, part_text(item.get("content")), stamp, since)
            elif item_type == "AgentMessage" and item.get("phase") in (None, "final_answer"):
                text = part_text(item.get("content"))
                if text.strip():
                    session.last_agent_text = text
            elif item_type == "CommandExecution" and in_window:
                codex_failure(session, item, stamp)
        elif event == "agent_message" and payload.get("phase") in (None, "final_answer"):
            session.last_agent_text = str(payload.get("message", ""))
        elif event == "exec_command_end" and in_window:
            codex_failure(session, payload, stamp)
        elif event == "task_started":
            session.last_event = "working"
        elif event == "task_complete":
            session.last_event = "finished"
            if payload.get("last_agent_message"):
                session.last_agent_text = str(payload["last_agent_message"])
        elif event == "turn_aborted":
            session.last_event = "interrupted"
        elif event == "error":
            session.last_event = "api_error"
    return session


def codex_prompt(session: Session, text: str, stamp, since) -> None:
    finished = session.last_event == "finished"
    question = finished and session.last_agent_text.rstrip().endswith("?")
    session.add_prompt(text, stamp, since, finished, question)
    session.last_event = "working"


def codex_failure(session: Session, item: dict, stamp) -> None:
    exit_code = item.get("exit_code")
    if exit_code in (0, "0", None) and item.get("status") != "failed":
        return
    command = codex_command_text(item)
    if not command:
        return
    output = item.get("aggregated_output") or item.get("stderr") or item.get("formatted_output") or ""
    session.failures.append((command_key(command), error_signature(exit_code, output), command, stamp))


# --------------------------------------------------------------------------------------------
# History files


def claude_history(config_dir: Path, since, stats: Counter) -> list:
    entries = []
    path = config_dir / "history.jsonl"
    if not path.is_file():
        return entries
    for line in iter_lines(path, stats):
        record = load_line(line, stats)
        if record is None:
            continue
        stamp = parse_ts(record.get("timestamp"))
        if stamp is None or stamp < since:
            continue
        entries.append(
            {
                "ts": stamp,
                "display": str(record.get("display") or ""),
                "pastes": record.get("pastedContents") if isinstance(record.get("pastedContents"), dict) else {},
                "project": str(record.get("project") or ""),
                "session": str(record.get("sessionId") or ""),
            }
        )
    return entries


def codex_history(codex_home: Path, since, stats: Counter) -> list:
    entries = []
    path = codex_home / "history.jsonl"
    if not path.is_file():
        return entries
    for line in iter_lines(path, stats):
        record = load_line(line, stats)
        if record is None:
            continue
        stamp = parse_ts(record.get("ts"))
        if stamp is None or stamp < since:
            continue
        entries.append({"ts": stamp, "display": str(record.get("text") or ""), "session": str(record.get("session_id") or ""), "project": ""})
    return entries


def paste_records(config_dir: Path, entries: list) -> list:
    """One record per pasted text block: (hash, first line, line count, session id, ts, project)."""
    found = []
    for entry in entries:
        for item in entry["pastes"].values():
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
            elif isinstance(item.get("contentHash"), str):
                digest = item["contentHash"]
                content = read_text(config_dir / "paste-cache" / ("%s.txt" % digest), 200_000) if re.match(r"^[0-9a-f]{8,64}$", digest) else ""
            else:
                continue
            lines = [ln for ln in str(content or "").splitlines() if ln.strip()]
            first = lines[0] if lines else "(text not available)"
            found.append((digest, first, len(lines), entry["session"], entry["ts"], entry["project"]))
    return found


# --------------------------------------------------------------------------------------------
# Repetition detection

DIRECTIVE_START = re.compile(
    r"^\s*(?:no+|nope|nah|wrong|stop|wait|don'?t|dont|do not|never|always|"
    r"please\s+(?:don'?t|do not|stop|always|never|use|remember|make sure|keep|run|avoid|only)|"
    r"make sure|remember(?: to)?|you (?:forgot|didn'?t|did not|keep|always|never|should(?:n'?t)?|need to|have to|must|missed)|"
    r"i (?:said|told you|asked|already)|not (?:like )?(?:that|this)|that'?s (?:not|wrong)|this is (?:not|wrong)|"
    r"instead|actually|avoid|only use|use\s+\S+\s+(?:instead|not|rather|for))\b",
    re.I,
)
DIRECTIVE_ANY = re.compile(
    r"\b(?:instead of|rather than|from now on|every time|each time|as i (?:said|mentioned)|i (?:already|just) (?:said|told)|"
    r"stop (?:doing|adding|using|asking|creating|writing|running)|keep forgetting|how many times|please stop|"
    r"always|never|don'?t|do not|make sure|remember to|prefer|without asking|no emojis?|be (?:concise|brief|terse|direct)|"
    r"in (?:a|the|its own) (?:new |separate )?worktree)\b",
    re.I,
)
GO_AHEAD = re.compile(
    r"^(?:y|yes|yep|yeah|yup|ok|okay|sure|k|continue|go|go ahead|go on|proceed|do it|keep going|carry on|"
    r"sounds good|looks good|lgtm|yes please|please continue|please proceed|go for it|approved|ship it|next|"
    r"do that|yes do it|yes go ahead|ok go ahead|ok continue|yes continue)"
    r"(?:[\s,.!]+(?:please|thanks|thank you|go ahead|do it|continue|proceed))*[\s.!]*$",
    re.I,
)
FOLLOW_UPS = [
    ("commit", re.compile(r"\bcommit\b", re.I)),
    ("push", re.compile(r"\bpush\b", re.I)),
    ("pull request", re.compile(r"\b(?:pr|pull request)\b", re.I)),
    ("tests", re.compile(r"\b(?:tests?|pytest|jest|vitest)\b", re.I)),
    ("lint", re.compile(r"\b(?:lint(?:er|ing)?|format(?:ter)?|prettier|ruff|eslint)\b", re.I)),
    ("type check", re.compile(r"\b(?:type ?check|tsc|mypy|pyright)\b", re.I)),
    ("build", re.compile(r"\b(?:run|rerun|check)\s+(?:the\s+)?build\b|\bbuild\s+(?:again|it|passes?)\b|\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?build\b|\bmake\s+build\b", re.I)),
    ("ci", re.compile(r"\b(?:ci|pr checks|gh pr checks|checks? (?:pass|green|status))\b", re.I)),
    ("docs", re.compile(r"\b(?:update|write|add|fix|regenerate)\b.{0,30}\b(?:docs?|documentation|readme|changelog)\b", re.I)),
    ("merge", re.compile(r"\bmerge\b", re.I)),
    ("worktree cleanup", re.compile(r"\bworktree\b.*\b(?:remove|clean|delete)\b|\b(?:remove|clean|delete)\b.*\bworktree\b", re.I)),
]


class Pattern:
    def __init__(self, kind: str, key: str = "", terms: frozenset = frozenset()) -> None:
        self.kind = kind
        self.key = key  # Exact fingerprint for commands, paths, links, pastes, follow-ups; "" for text.
        self.terms = terms  # Key words for text kinds; also used to look for an existing rule.
        self.occurrences: list = []  # (session, ts, text, pid)
        self.label = ""
        self.extra: dict = {}
        self.tag = ""
        self.fid = ""
        self.status = "new"
        self.state_note = ""
        self.since_fix = None
        self.covered = None
        self.score = 0.0

    def add(self, session: Session, stamp, text: str, pid=None) -> None:
        self.occurrences.append((session, stamp, text, pid))

    def sessions(self) -> set:
        return {o[0].key for o in self.occurrences}

    def count(self) -> int:
        pids = {o[3] for o in self.occurrences if o[3] is not None}
        return len(pids) + sum(1 for o in self.occurrences if o[3] is None)

    def first_ts(self):
        stamps = [o[1] for o in self.occurrences if o[1] is not None]
        return min(stamps) if stamps else None

    def last_ts(self):
        stamps = [o[1] for o in self.occurrences if o[1] is not None]
        return max(stamps) if stamps else None


def cluster(items: list, threshold: float) -> list:
    """Greedy grouping by word-set similarity to each group's first member, indexed by word."""
    groups: list = []
    index: dict = defaultdict(list)
    for terms, payload in items:
        if not terms:
            continue
        candidates = set()
        for term in terms:
            candidates.update(index.get(term, ()))
        best, best_score = None, 0.0
        for gid in candidates:
            score = jaccard(terms, groups[gid][0])
            if score > best_score:
                best, best_score = gid, score
        if best is not None and best_score >= threshold:
            groups[best][1].append(payload)
        else:
            gid = len(groups)
            groups.append((terms, [payload]))
            for term in terms:
                index[term].append(gid)
    return groups


SKILL_MENTION = re.compile(r"^\$[A-Za-z][\w:-]*(?=\s|$)")  # Codex invokes a skill as $name.


def detect(sessions: list, pastes: list, min_sessions: int) -> tuple:
    prompts = []
    for session in sessions:
        for prompt in session.prompts:
            prompt.session = session
            prompt.pid = len(prompts)
            prompts.append(prompt)
    prompts.sort(key=lambda p: (p.ts or dt.datetime.min.replace(tzinfo=dt.timezone.utc), p.pid))
    by_session = {s.key: s for s in sessions}
    patterns: list = []
    candidates: dict = defaultdict(list)  # kind -> [(terms or key, session, ts, text)] for later re-counts
    slash = Counter()
    go_aheads, follow_ups, request_items, directive_items = [], [], [], []
    for prompt in prompts:
        text = prompt.text.strip()
        flat = one_line(text, 4000)
        if text.startswith("/") or SKILL_MENTION.match(text):
            slash[text.split()[0].lower()] += 1
        if prompt.index > 0 and GO_AHEAD.match(flat):
            go_aheads.append(prompt)
            candidates["go_ahead"].append(("go_ahead", prompt.session, prompt.ts, flat))
            continue
        words = len(flat.split())
        if prompt.index > 0 and prompt.after_finished is not False and len(flat) <= 300 and words <= 30:
            bare = PATH_RE.sub(" ", URL_RE.sub(" ", flat))  # "docs/x.md" is a path, not a request for docs.
            labels = sorted({label for label, rx in FOLLOW_UPS if rx.search(bare)})
            if labels:
                key = "+".join(labels)
                follow_ups.append((key, prompt))
                candidates["follow_up"].append((key, prompt.session, prompt.ts, flat))
                continue
        for sentence in sentences(text):
            if prompt.index == 0:
                hit = DIRECTIVE_ANY.search(sentence) or DIRECTIVE_START.match(sentence)
            else:
                hit = DIRECTIVE_START.match(sentence) or DIRECTIVE_ANY.search(sentence)
            if not hit:
                continue
            terms = content_terms(sentence)
            if len(terms) >= 2:
                directive_items.append((terms, prompt, sentence))
                candidates["instruction"].append((terms, prompt.session, prompt.ts, sentence))
        if not text.startswith("/") and not SKILL_MENTION.match(text) and 15 <= len(text) <= 40000:
            terms = content_terms(text)
            if len(terms) >= 4:
                request_items.append((terms, prompt))

    # Repeated requests: the same task asked again in different sessions.
    request_groups = cluster(request_items, 0.6)
    qualifying_requests: list = []
    for gid, (terms, members) in enumerate(request_groups):
        for prompt in members:
            prompt.request = gid
        if len({p.session.key for p in members}) >= max(3, min_sessions):
            pattern = Pattern("repeated_request", terms=terms)
            for prompt in members:
                pattern.add(prompt.session, prompt.ts, prompt.text, prompt.pid)
                candidates["repeated_request"].append((terms, prompt.session, prompt.ts, prompt.text))
            qualifying_requests.append(pattern)
    patterns.extend(qualifying_requests)
    request_pids = [{o[3] for o in p.occurrences} for p in qualifying_requests]

    # Instructions: corrections, and preferences restated at session start. They are grouped
    # together, so "don't add comments" counts once whether it opened a session or came later;
    # the kind follows where most of them were said.
    groups = []
    for terms, members in cluster([(terms, (prompt, sentence)) for terms, prompt, sentence in directive_items], 0.5):
        sessions_seen = {prompt.session.key for prompt, _s in members}
        pids = {prompt.pid for prompt, _s in members}
        if len(sessions_seen) < min_sessions or len(pids) < 2:
            continue
        if any(pids <= covered for covered in request_pids):
            continue  # Only ever inside one repeated request; that pattern already covers it.
        if len({prompt.request if prompt.request is not None else ("p", prompt.pid) for prompt, _s in members}) < 2:
            continue  # The same prompt sent to two sessions (a retry, or two agents at once), not a restated rule.
        at_start = sum(1 for prompt, _s in members if prompt.index == 0)
        pattern = Pattern("start_preference" if at_start * 2 > len(members) else "correction", terms=terms)
        for prompt, sentence in members:
            pattern.add(prompt.session, prompt.ts, sentence, prompt.pid)
        pattern.extra["at_start"] = at_start
        groups.append(pattern)
    # Instructions that always travel together (a block pasted into many sessions) become one.
    merged: "OrderedDict[tuple, Pattern]" = OrderedDict()
    for pattern in groups:
        sessions_key = tuple(sorted(str(k) for k in pattern.sessions()))
        if len(sessions_key) >= 3:
            signature = (pattern.kind, "sessions") + sessions_key
        else:
            signature = (pattern.kind, "prompts") + tuple(sorted({o[3] for o in pattern.occurrences}))
        if signature in merged:
            merged[signature].extra.setdefault("together", []).append(pattern)
            continue
        merged[signature] = pattern
    patterns.extend(merged.values())

    # Follow-ups after the agent said it was done, keyed by what was asked.
    grouped: dict = defaultdict(list)
    for key, prompt in follow_ups:
        grouped[key].append(prompt)
    for key, members in grouped.items():
        if len({p.session.key for p in members}) >= min_sessions and len(members) >= 2:
            pattern = Pattern("follow_up", key=key, terms=frozenset(stem(w) for w in key.replace("+", " ").split()))
            for prompt in members:
                pattern.add(prompt.session, prompt.ts, prompt.text, prompt.pid)
            patterns.append(pattern)

    # Go-aheads: "yes", "continue", "go ahead" after the agent stopped.
    if len(go_aheads) >= 5 and len({p.session.key for p in go_aheads}) >= min_sessions:
        pattern = Pattern("go_ahead", key="go_ahead", terms=frozenset({"ask", "confirm", "proceed", "continue"}))
        for prompt in go_aheads:
            pattern.add(prompt.session, prompt.ts, prompt.text, prompt.pid)
        pattern.extra["after_question"] = sum(1 for p in go_aheads if p.after_question)
        patterns.append(pattern)

    patterns.extend(detect_context(prompts, pastes, by_session, min_sessions, candidates))
    patterns.extend(detect_tool_patterns(sessions, min_sessions, candidates))
    return patterns, candidates, slash


def normalize_path_token(raw: str) -> str:
    token = raw.strip(".,;:!?)('\"`").replace("\\", "/")
    return token


def detect_context(prompts: list, pastes: list, by_session: dict, min_sessions: int, candidates: dict) -> list:
    found: dict = defaultdict(lambda: {"occ": [], "requests": set()})
    for prompt in prompts:
        text = PLACEHOLDER_RE.sub(" ", prompt.text)
        urls = {u.rstrip(".,;:!?") for u in URL_RE.findall(text)}
        for url in urls:
            key = "url:" + re.sub(r"[?#].*$", "", url)
            found[key]["occ"].append(prompt)
            found[key]["requests"].add(prompt.request if prompt.request is not None else ("p", prompt.pid))
        rest = URL_RE.sub(" ", text)
        for raw in set(PATH_RE.findall(rest)):
            token = normalize_path_token(raw)
            slashes = token.count("/")
            if len(token) < 6 or not (re.search(r"\.[A-Za-z0-9]{1,8}$", token) or slashes >= 2):
                continue
            if re.match(r"^(?:and/or|w/o|i/o|n/a|tcp/ip|a/b)$", token, re.I):
                continue
            key = "path:" + token
            found[key]["occ"].append(prompt)
            found[key]["requests"].add(prompt.request if prompt.request is not None else ("p", prompt.pid))
        for sentence in sentences(rest):
            if DIRECTIVE_START.match(sentence) or DIRECTIVE_ANY.search(sentence):
                continue
            words = re.findall(r"[a-z0-9']+", sentence.lower())
            if len(words) < 10:
                continue
            key = "sentence:" + " ".join(words)
            found[key]["occ"].append(prompt)
            found[key]["requests"].add(prompt.request if prompt.request is not None else ("p", prompt.pid))
            found[key].setdefault("text", sentence)
    patterns = []
    for key, data in found.items():
        members = data["occ"]
        sessions_seen = {p.session.key for p in members}
        need = min_sessions if key.startswith("sentence:") else max(3, min_sessions)
        if len(sessions_seen) < need or len(data["requests"]) < 2:
            continue
        sub, value = key.split(":", 1)
        pattern = Pattern("repeated_context", key=key, terms=content_terms(value))
        pattern.extra["sub"] = sub
        pattern.extra["value"] = data.get("text", value)
        for prompt in members:
            pattern.add(prompt.session, prompt.ts, prompt.text, prompt.pid)
        patterns.append(pattern)
    for key, data in found.items():
        for prompt in data["occ"]:
            candidates["repeated_context"].append((key, prompt.session, prompt.ts, ""))
    # Pasted blocks, by content hash, from Claude Code's history file.
    by_hash: dict = defaultdict(list)
    for digest, first, lines, session_id, stamp, project in pastes:
        by_hash[digest].append((first, lines, session_id, stamp, project))
    for digest, members in by_hash.items():
        keys = {("claude-code", m[2]) for m in members}
        for _first, _lines, session_id, stamp, _project in members:
            candidates["repeated_context"].append(("paste:" + digest, by_session.get(("claude-code", session_id)), stamp, ""))
        if len(keys) < min_sessions:
            continue
        pattern = Pattern("repeated_context", key="paste:" + digest, terms=content_terms(members[0][0]))
        pattern.extra["sub"] = "paste"
        pattern.extra["value"] = members[0][0]
        pattern.extra["lines"] = members[0][1]
        for first, lines, session_id, stamp, project in members:
            session = by_session.get(("claude-code", session_id))
            if session is None:
                session = Session("claude-code", session_id, None, Path("."), source="history")
                session.add_cwd(project)
                by_session[session.key] = session
            pattern.add(session, stamp, first, None)
        patterns.append(pattern)
    return patterns


def detect_tool_patterns(sessions: list, min_sessions: int, candidates: dict) -> list:
    patterns = []
    for kind, attribute, need_count, need_sessions in (("approval", "approvals", 3, 1), ("rejected_action", "rejections", 2, min_sessions)):
        grouped: dict = defaultdict(list)
        for session in sessions:
            for key, command, stamp in getattr(session, attribute):
                tool, _sep, prefix = key.partition(":")
                group = "cmd:" + prefix if tool in COMMAND_TOOLS | {"shell"} and prefix else key
                grouped[group].append((session, command, stamp, tool))
                candidates[kind].append((group, session, stamp, command))
        for key, members in grouped.items():
            if len(members) < need_count or len({m[0].key for m in members}) < need_sessions:
                continue
            pattern = Pattern(kind, key=key, terms=content_terms(key.split(":", 1)[-1]))
            for session, command, stamp, _tool in members:
                pattern.add(session, stamp, command, None)
            commands = Counter(m[1] for m in members if m[1])
            sample = commands.most_common(1)[0][0] if commands else key
            pattern.extra["tools"] = Counter(m[3] for m in members)
            pattern.extra["tool"] = key.split(":", 1)[0]
            pattern.extra["prefix"] = key.split(":", 1)[1] if ":" in key else ""
            pattern.extra["safety"] = safety_class(sample) if key.startswith("cmd:") else "unknown"
            patterns.append(pattern)
    grouped = defaultdict(list)
    for session in sessions:
        for key, signature, command, stamp in session.failures:
            if not re.search(r"[a-z]", key) or safety_class(command) == "read_only":
                continue  # A search that found nothing, or a read of a missing file, is ordinary work.
            if not setup_error(signature, is_check_command(key)):
                continue  # A failing test or a bug in the agent's own script is normal work, not a repetition to fix.
            grouped["%s | %s" % (key, signature)].append((session, command, stamp))
            candidates["repeated_error"].append(("%s | %s" % (key, signature), session, stamp, command))
    for key, members in grouped.items():
        if len({m[0].key for m in members}) < max(3, min_sessions):
            continue
        pattern = Pattern("repeated_error", key=key, terms=content_terms(key))
        for session, command, stamp in members:
            pattern.add(session, stamp, command, None)
        pattern.extra["command"], pattern.extra["signature"] = key.split(" | ", 1)
        patterns.append(pattern)
    return patterns


# --------------------------------------------------------------------------------------------
# Current setup


def doc_info(path: Path, redact: Redactor):
    if not path.is_file():
        return None
    text = read_text(path)
    lines = text.splitlines()
    headings = [redact(one_line(ln.lstrip("#").strip(), 60)) for ln in lines if re.match(r"^#{1,3}\s+\S", ln)]
    imports = [redact(ln.strip()) for ln in lines if re.match(r"^@\S+", ln.strip())]
    return {"path": redact(str(path).replace("\\", "/")), "bytes": len(text.encode("utf-8")), "lines": len(lines), "headings": headings[:12], "imports": imports[:6]}


def settings_info(path: Path, redact: Redactor):
    data = load_json_file(path) if path.is_file() else None
    if not isinstance(data, dict):
        return None
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    allow = [str(a) for a in perms.get("allow", []) if isinstance(a, str)] if isinstance(perms.get("allow"), list) else []
    return {
        "path": redact(str(path).replace("\\", "/")),
        "default_mode": perms.get("defaultMode"),
        "allow": [redact(one_line(a, 80)) for a in allow],
        "allow_raw": allow,
        "ask": len(perms.get("ask", []) or []) if isinstance(perms.get("ask"), list) else 0,
        "deny": len(perms.get("deny", []) or []) if isinstance(perms.get("deny"), list) else 0,
        "hooks": {event: sum(len(h.get("hooks", []) or []) for h in entries if isinstance(h, dict)) for event, entries in hooks.items() if isinstance(entries, list)},
    }


def toml_summary(path: Path, redact: Redactor):
    if not path.is_file():
        return None
    raw = read_text(path)
    data = None
    try:
        import tomllib  # Python 3.11+

        data = tomllib.loads(raw)
    except Exception:  # Older Python or a file tomllib refuses: fall back to a line scan.
        data = None
    info: dict = {"path": redact(str(path).replace("\\", "/"))}
    if isinstance(data, dict):
        for key in ("approval_policy", "sandbox_mode", "approvals_reviewer"):
            if isinstance(data.get(key), str):
                info[key] = data[key]
        servers = data.get("mcp_servers") if isinstance(data.get("mcp_servers"), dict) else {}
        info["mcp_servers"] = sorted(servers)
        features = data.get("features") if isinstance(data.get("features"), dict) else {}
        info["features_on"] = sorted(k for k, v in features.items() if v is True)
        return info
    for key in ("approval_policy", "sandbox_mode", "approvals_reviewer"):
        match = re.search(r"(?m)^\s*%s\s*=\s*\"([^\"]*)\"" % key, raw)
        if match:
            info[key] = match.group(1)
    info["mcp_servers"] = sorted(set(re.findall(r"(?m)^\s*\[mcp_servers\.([A-Za-z0-9_-]+)\]", raw)))
    return info


def git_root(folder: Path):
    current = folder
    for _ in range(12):
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent
    return None


CLAUDE_ONLY = ("claude-code",)
CODEX_ONLY = ("codex",)
BOTH_AGENTS = ("claude-code", "codex")
IMPORTS_AGENTS_MD = re.compile(r"(?m)^\s*@\S*AGENTS\.md\s*$")


def instruction_lines(path: Path, display: str, readers: tuple, scope) -> list:
    """Non-empty lines of an instruction file, with the agents that read it and the folder it applies in
    (None means every folder)."""
    lines = []
    for number, line in enumerate(read_text(path).splitlines(), 1):
        if line.strip():
            lines.append((display, number, line, content_terms(line), line.lower(), readers, scope))
    return lines


def inventory(claude_dirs: list, codex_home, project_folders: list, home: Path, redact: Redactor) -> tuple:
    setup: dict = {"claude": [], "codex": None, "projects": []}
    rules_text: list = []
    allow_entries: list = []
    for config_dir in claude_dirs:
        entry: dict = {"dir": redact(str(config_dir).replace("\\", "/"))}
        doc = doc_info(config_dir / "CLAUDE.md", redact)
        entry["claude_md"] = doc
        if doc:
            rules_text += instruction_lines(config_dir / "CLAUDE.md", doc["path"], CLAUDE_ONLY, None)
        entry["settings"] = [s for s in (settings_info(config_dir / n, redact) for n in ("settings.json", "settings.local.json")) if s]
        for settings in entry["settings"]:
            allow_entries += [(settings["path"], a, None) for a in settings.pop("allow_raw")]
        entry["skills"] = sorted(p.parent.name for p in (config_dir / "skills").glob("*/SKILL.md")) if (config_dir / "skills").is_dir() else []
        entry["commands"] = sorted(p.stem for p in (config_dir / "commands").glob("**/*.md")) if (config_dir / "commands").is_dir() else []
        memory = sorted((config_dir / "projects").glob("*/memory/*.md")) if (config_dir / "projects").is_dir() else []
        entry["memory_files"] = len(memory)
        for path in memory[:200]:
            rules_text += instruction_lines(path, redact(str(path).replace("\\", "/")), CLAUDE_ONLY, None)
        setup["claude"].append(entry)
    if codex_home is not None:
        entry = {"dir": redact(str(codex_home).replace("\\", "/"))}
        entry["config"] = toml_summary(codex_home / "config.toml", redact)
        # A global CLAUDE.md can import the Codex AGENTS.md with an @ line; then both agents read it.
        imported = any(IMPORTS_AGENTS_MD.search(read_text(d / "CLAUDE.md")) for d in claude_dirs if (d / "CLAUDE.md").is_file())
        for name in ("AGENTS.md", "AGENTS.override.md"):
            doc = doc_info(codex_home / name, redact)
            if doc:
                entry[name] = doc
                rules_text += instruction_lines(codex_home / name, doc["path"], BOTH_AGENTS if imported and name == "AGENTS.md" else CODEX_ONLY, None)
        entry["prefix_rules"] = len(load_codex_rules(codex_home))
        hooks = load_json_file(codex_home / "hooks.json") if (codex_home / "hooks.json").is_file() else None
        hooks = hooks.get("hooks") if isinstance(hooks, dict) and isinstance(hooks.get("hooks"), dict) else {}
        entry["hooks"] = sorted(hooks)
        skills = (codex_home / "skills")
        entry["skills"] = sorted({p.parent.name for p in skills.glob("*/SKILL.md")} | {p.parent.name for p in skills.glob("*/*/SKILL.md")}) if skills.is_dir() else []
        entry["prompts"] = sorted(p.stem for p in (codex_home / "prompts").glob("*.md")) if (codex_home / "prompts").is_dir() else []
        memories = sorted((codex_home / "memories").glob("**/*.md")) if (codex_home / "memories").is_dir() else []
        entry["memory_files"] = len(memories)
        for path in memories[:200]:
            rules_text += instruction_lines(path, redact(str(path).replace("\\", "/")), CODEX_ONLY, None)
        setup["codex"] = entry
    home_norm = norm_path(str(home))
    for folder in project_folders:
        root = git_root(folder) or folder
        entry = {"folder": redact(str(root).replace("\\", "/"))}
        docs = []
        seen = set()
        current = folder
        for _ in range(6):  # The folder and its parents, as Claude Code and Codex read them.
            if norm_path(str(current)) == home_norm:
                break
            # Claude Code reads AGENTS.md only when a CLAUDE.md beside it imports it with @AGENTS.md.
            imported = any(IMPORTS_AGENTS_MD.search(read_text(current / n)) for n in ("CLAUDE.md", ".claude/CLAUDE.md") if (current / n).is_file())
            for name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude/CLAUDE.md"):
                path = current / name
                key = norm_path(str(path))
                if key in seen:
                    continue
                seen.add(key)
                doc = doc_info(path, redact)
                if doc:
                    docs.append(doc)
                    readers = (BOTH_AGENTS if imported else CODEX_ONLY) if name == "AGENTS.md" else CLAUDE_ONLY
                    rules_text += instruction_lines(path, doc["path"], readers, str(current))
            if current == root or current.parent == current:
                break
            current = current.parent
        entry["docs"] = docs
        entry["settings"] = [s for s in (settings_info(root / ".claude" / n, redact) for n in ("settings.json", "settings.local.json")) if s]
        for settings in entry["settings"]:
            allow_entries += [(settings["path"], a, str(root)) for a in settings.pop("allow_raw")]
        mcp = load_json_file(root / ".mcp.json") if (root / ".mcp.json").is_file() else None
        entry["mcp_servers"] = sorted((mcp.get("mcpServers") or {}).keys()) if isinstance(mcp, dict) and isinstance(mcp.get("mcpServers"), dict) else []
        entry["skills"] = sorted(p.parent.name for p in (root / ".claude" / "skills").glob("*/SKILL.md")) if (root / ".claude" / "skills").is_dir() else []
        entry["commands"] = sorted(p.stem for p in (root / ".claude" / "commands").glob("**/*.md")) if (root / ".claude" / "commands").is_dir() else []
        setup["projects"].append(entry)
    return setup, rules_text, allow_entries


GO_AHEAD_RULE = re.compile(r"without asking|don'?t ask|do not ask|no need to ask|proceed without|autonom|don'?t stop to|do not stop to|keep going until", re.I)


AGENT_TEXT = {"claude-code": "Claude Code", "codex": "Codex"}


def rule_hit(pattern: Pattern, display: str, number, text: str, readers: tuple, scope, redact: Redactor) -> dict:
    """An existing rule, with how many of the pattern's sessions it could have reached: the agent must
    read the file, and a project file applies only inside its folder."""
    sessions = {o[0].key: o[0] for o in pattern.occurrences}
    reached = 0
    for session in sessions.values():
        folders = session.cwds or ([session.cwd] if session.cwd else [])
        if session.tool in readers and (scope is None or any(inside(c, scope) for c in folders)):
            reached += 1
    return {
        "file": display,
        "line": number,
        "text": redact(one_line(text, 120)),
        "read_by": [AGENT_TEXT[r] for r in readers],
        "applies_in": redact(scope.replace("\\", "/")) if scope else "every folder",
        "sessions_in_reach": reached,
        "sessions": len(sessions),
    }


def find_existing_rule(pattern: Pattern, rules_text: list, allow_entries: list, codex_rules: list, redact: Redactor):
    """The best line of an instruction or memory file, or the allow entry, that already covers this
    pattern, if any. A rule that reaches the sessions where the pattern repeats wins over one that does not."""
    found = []
    if pattern.kind in ("approval", "rejected_action"):
        prefix = pattern.extra.get("prefix", "")
        if pattern.kind == "approval" and prefix:
            for path, entry, scope in allow_entries:
                match = re.match(r"^(\w+)\((.*)\)$", entry)
                if match and prefix.startswith(match.group(2).rstrip("*").rstrip(":").strip().lower()) and match.group(2).strip():
                    found.append(rule_hit(pattern, path, None, entry, CLAUDE_ONLY, scope, redact))
            if codex_rules and rule_covers(codex_rules, prefix):
                found.append(rule_hit(pattern, "Codex rules", None, prefix, CODEX_ONLY, None, redact))
    elif pattern.kind == "go_ahead":
        for display, number, line, _terms, _lower, readers, scope in rules_text:
            if GO_AHEAD_RULE.search(line):
                found.append(rule_hit(pattern, display, number, line, readers, scope, redact))
    elif pattern.kind == "repeated_context" and pattern.extra.get("sub") in ("path", "url"):
        needle = pattern.key.split(":", 1)[1].lower()
        for display, number, line, _terms, lower, readers, scope in rules_text:
            if needle and needle in lower.replace("\\", "/"):
                found.append(rule_hit(pattern, display, number, line, readers, scope, redact))
    elif pattern.terms:
        terms = pattern.terms
        need = 1 if len(terms) == 1 else 2
        for display, number, line, line_terms, _lower, readers, scope in rules_text:
            hits = len(terms & line_terms)
            score = hits / float(len(terms))
            if hits >= need and score >= 0.5:
                hit = rule_hit(pattern, display, number, line, readers, scope, redact)
                hit["_score"] = score
                found.append(hit)
    if not found:
        return None
    best = max(found, key=lambda h: (h["sessions_in_reach"] > 0, h.get("_score", 1.0), h["sessions_in_reach"]))
    best.pop("_score", None)
    return best


def reach_note(covered: dict) -> str:
    """Why an existing rule did or did not reach the sessions where the pattern repeats."""
    readers = covered.get("read_by") or []
    where = "only inside %s" % covered["applies_in"] if covered.get("applies_in") not in (None, "every folder") else "in every folder"
    who = " and ".join(readers) if readers else "No agent"
    return "%s read%s it %s" % (who, "" if len(readers) > 1 else "s", where)


# --------------------------------------------------------------------------------------------
# State file


def default_state_dir() -> Path:
    """The per-user state folder, in the same platform locations vidbyte-cli uses for its own state."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Vidbyte" / "vidbyte-skills" / SLUG
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "vidbyte-skills" / SLUG
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "vidbyte-skills" / SLUG


def load_state(state_dir: Path, rescue: bool = True) -> dict:
    path = state_dir / "state.json"
    data = load_json_file(path) if path.is_file() else None
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        if rescue and path.is_file():  # Keep an unreadable or newer file instead of overwriting its history.
            kept = path.with_name("state.json.unreadable-%s" % dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
            os.replace(str(path), str(kept))
            print("The state file could not be read; it was kept as %s and a new one was started." % kept, file=sys.stderr)
        return {"schema": STATE_SCHEMA, "slug": SLUG, "runs": [], "patterns": {}}
    data.setdefault("runs", [])
    data.setdefault("patterns", {})
    return data


def save_state(state_dir: Path, state: dict) -> Path:
    path = state_dir / "state.json"
    state["runs"] = state.get("runs", [])[-52:]
    write_atomic(path, json.dumps(state, indent=2))
    return path


def family(kind: str) -> str:
    """Corrections and session-start preferences are one family: an instruction can move between them."""
    return "instruction" if kind in ("correction", "start_preference") else str(kind)


def match_stored(pattern: Pattern, stored: dict, taken: set):
    best, best_score = None, 0.0
    for fid, entry in stored.items():
        if fid in taken or family(entry.get("kind")) != family(pattern.kind):
            continue
        if pattern.key:
            if entry.get("key") == pattern.key:
                return fid
            continue
        score = jaccard(pattern.terms, frozenset(entry.get("terms") or ()))
        if score >= 0.5 and score > best_score:
            best, best_score = fid, score
    return best


def new_fid(pattern: Pattern) -> str:
    basis = pattern.kind + "|" + (pattern.key or " ".join(sorted(pattern.terms)))
    return "%s-%s" % (KIND_PREFIX[pattern.kind], hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10])


def matches_candidate(entry: dict, candidate_key) -> bool:
    if entry.get("key"):
        return candidate_key == entry["key"]
    if isinstance(candidate_key, frozenset):
        return jaccard(candidate_key, frozenset(entry.get("terms") or ())) >= 0.5
    return False


def since_fix(entry: dict, candidates: dict, sessions: list) -> dict:
    fixed_at = parse_ts((entry.get("fix") or {}).get("at"))
    if fixed_at is None:
        return {}
    hits = [c for c in candidates.get(family(entry.get("kind")), []) if c[2] is not None and c[2] > fixed_at and matches_candidate(entry, c[0])]
    active = [s for s in sessions if s.last_ts is not None and s.last_ts > fixed_at]
    return {
        "fixed_at": iso(fixed_at),
        "repeats_since_fix": len(hits),
        "sessions_with_repeats": len({c[1].key for c in hits if c[1] is not None}),
        "sessions_since_fix": len(active),
    }


def fix_status(entry: dict, info: dict) -> str:
    fix = entry.get("fix") or {}
    if fix.get("status") == "rejected":
        return "rejected"
    if fix.get("status") == "accepted":
        return "fix_pending"
    if fix.get("status") != "applied":
        return "open"
    if info.get("repeats_since_fix", 0) > 0:
        return "continues_despite_fix"
    if info.get("sessions_since_fix", 0) >= MIN_SESSIONS_AFTER_FIX:
        return "stopped_after_fix"
    return "too_early"


# --------------------------------------------------------------------------------------------
# Labels, fix kinds, ranking


def label_for(pattern: Pattern, redact: Redactor) -> str:
    kind = pattern.kind
    if kind in ("correction", "start_preference", "repeated_request"):
        texts = Counter(one_line(o[2], 200) for o in pattern.occurrences)
        common = texts.most_common(3)
        top = min((t for t, n in common if n == common[0][1]), key=len)
        label = '"%s"' % one_line(top, 140 if kind != "repeated_request" else 160)
        together = pattern.extra.get("together") or []
        if together:
            label += " (and %d more instruction%s repeated in the same sessions)" % (len(together), "" if len(together) == 1 else "s")
        return redact(label)
    if kind == "follow_up":
        return "asks for %s after the agent finished" % pattern.key.replace("+", " + ")
    if kind == "go_ahead":
        return '"yes", "continue", "go ahead" after the agent stopped (%d of them right after the agent asked a question)' % pattern.extra.get("after_question", 0)
    if kind == "repeated_context":
        sub = pattern.extra.get("sub")
        value = pattern.extra.get("value", "")
        if sub == "paste":
            return redact('pasted block of %d lines starting "%s"' % (pattern.extra.get("lines", 0), one_line(value, 90)))
        if sub == "sentence":
            return redact('repeats the same sentence: "%s"' % one_line(value, 150))
        return redact("points the agent at %s `%s`" % ("link" if sub == "url" else "path", one_line(value, 120)))
    if kind in ("approval", "rejected_action"):
        tool, prefix = pattern.extra.get("tool", ""), pattern.extra.get("prefix", "")
        verb = "approved by hand at a permission prompt" if kind == "approval" else "rejected at a permission prompt"
        tools = pattern.extra.get("tools") or Counter()
        where = ", ".join("%s %d" % (TOOL_TEXT.get(t, t), n) for t, n in tools.most_common())
        shown = "`%s`" % (prefix if tool == "cmd" else tool + (" " + prefix if prefix else ""))
        return redact("%s %s (%s)" % (shown, verb, where) if where else "%s %s" % (shown, verb))
    if kind == "repeated_error":
        return redact("`%s` fails: %s" % (pattern.extra.get("command", ""), pattern.extra.get("signature", "")))
    return kind


def fix_kinds(pattern: Pattern) -> str:
    kind = pattern.kind
    covered = pattern.covered is not None and pattern.covered.get("sessions_in_reach", 1) > 0
    if pattern.covered is not None and not covered and kind not in ("approval", "rejected_action"):
        return "the same rule in a file the agent that repeats it reads (CLAUDE.md for Claude Code, AGENTS.md for Codex), at the right level (global or project)"
    if kind in ("correction", "start_preference"):
        if covered:
            return "a hook, check, or script (an existing rule has not stopped it); or rewrite and move the rule"
        if pattern.extra.get("together"):
            return "an AGENTS.md or CLAUDE.md section, or a skill that carries the whole block"
        return "a CLAUDE.md or AGENTS.md rule, or a memory entry"
    if kind == "repeated_request":
        return "a skill or slash command"
    if kind == "repeated_context":
        sub = pattern.extra.get("sub")
        if sub == "paste":
            return "a reference file in the repository plus a rule that points to it, or a skill"
        if sub == "sentence":
            return "a rule or reference section in AGENTS.md or CLAUDE.md; a skill or slash command if the sentence asks for a task"
        return "a rule or reference section in AGENTS.md or CLAUDE.md"
    if kind == "approval":
        safety = pattern.extra.get("safety")
        if safety == "read_only":
            return "a permission allowlist entry (read-only command)"
        if safety == "writes":
            return "NOT an allowlist entry (it writes); a skill or script that does the step with one approval"
        if safety == "runs_code":
            return "NOT an allowlist entry by default (it runs project code); a hook that runs it, or ask the user"
        return "check what the command does before any allowlist entry; otherwise a rule"
    if kind == "rejected_action":
        return "a rule that tells the agent not to do it, or a permission deny entry"
    if kind == "follow_up":
        parts = set(pattern.key.split("+"))
        if parts & {"tests", "lint", "type check", "build"}:
            return "a hook (for example lint or tests when the agent stops) or a rule about what done means"
        return "a slash command or skill for the whole sequence, or a rule about what done means"
    if kind == "go_ahead":
        return "a rule that says when to proceed without asking"
    if kind == "repeated_error":
        return "a rule with the right command for this machine, or a small script"
    return "a rule"


TOOL_TEXT = {"Bash": "Claude Code Bash", "PowerShell": "Claude Code PowerShell", "BashOutput": "Claude Code", "shell": "Codex"}


def allowlist_entries(pattern: Pattern) -> str:
    """The exact allowlist lines for a read-only command, one per tool that asked for it."""
    if pattern.kind != "approval" or pattern.extra.get("safety") != "read_only" or pattern.extra.get("tool") != "cmd":
        return ""
    prefix = pattern.extra.get("prefix", "")
    tools = pattern.extra.get("tools") or Counter()
    entries = []
    if tools.get("Bash"):
        entries.append("Claude Code `permissions.allow`: `Bash(%s:*)`" % prefix)
    if tools.get("PowerShell"):
        entries.append("Claude Code `permissions.allow`: a rule for the PowerShell tool and the prefix `%s` (check the rule syntax of the installed Claude Code first)" % prefix)
    if tools.get("shell"):
        tokens = split_command(prefix)
        entries.append("Codex `rules/default.rules`: `prefix_rule(pattern=[%s], decision=\"allow\")`" % ", ".join(json.dumps(t) for t in tokens))
    return "; ".join(entries)


STATUS_WEIGHT = {"continues_despite_fix": 30, "new": 4, "open": 2, "fix_pending": 1, "too_early": 0, "rejected": -6, "stopped_after_fix": -20}


def rank(patterns: list, max_patterns: int) -> list:
    for pattern in patterns:
        sessions = len(pattern.sessions())
        if pattern.kind == "repeated_error":
            pattern.score = sessions * 2 + min(pattern.count(), 30) / 2.0 + STATUS_WEIGHT.get(pattern.status, 0)
        else:
            pattern.score = sessions * 3 + min(pattern.count(), 60) + STATUS_WEIGHT.get(pattern.status, 0)
        if pattern.covered is not None and pattern.covered.get("sessions_in_reach", 0) > 0 and pattern.status in ("new", "open"):
            pattern.score += 8  # A rule reaches these sessions and the user still repeats it: the strongest signal.
        elif pattern.covered is not None and pattern.status in ("new", "open"):
            pattern.score += 4  # A rule exists but sits where this agent or folder never reads it: a cheap fix.
    ordered = sorted(patterns, key=lambda p: (-p.score, p.kind, p.key or " ".join(sorted(p.terms))))
    kept, per_kind = [], Counter()
    for pattern in ordered:
        if per_kind[pattern.kind] >= (MAX_ERRORS if pattern.kind == "repeated_error" else MAX_PER_KIND):
            continue
        per_kind[pattern.kind] += 1
        kept.append(pattern)
        if len(kept) >= max_patterns:
            break
    return kept


# --------------------------------------------------------------------------------------------
# Digest


STATUS_TEXT = {
    "new": "new",
    "open": "seen before, no fix yet",
    "fix_pending": "fix accepted but not applied yet",
    "too_early": "fixed recently; too early to tell",
    "continues_despite_fix": "CONTINUES DESPITE ITS FIX",
    "stopped_after_fix": "stopped after its fix",
    "rejected": "a fix was rejected before",
}


def tools_text(pattern: Pattern) -> str:
    tools = Counter(o[0].tool for o in {o[0].key: o for o in pattern.occurrences}.values())
    parts = []
    if tools.get("claude-code"):
        parts.append("Claude Code %d" % tools["claude-code"])
    if tools.get("codex"):
        parts.append("Codex %d" % tools["codex"])
    return ", ".join(parts)


def folders_of(pattern: Pattern, redact: Redactor) -> Counter:
    folders = Counter()
    for key, occurrence in {o[0].key: o for o in pattern.occurrences}.items():
        cwd = occurrence[0].cwd
        folders[redact(cwd.replace("\\", "/")) if cwd else "(unknown folder)"] += 1
    return folders


def examples_of(pattern: Pattern, redact: Redactor, count: int, limit: int) -> list:
    if pattern.kind in ("go_ahead",):
        texts = Counter(one_line(o[2], 40).lower() for o in pattern.occurrences)
        return ['"%s" x%d' % (redact(t), n) for t, n in texts.most_common(count + 2)]
    seen, shown = set(), []
    ordered = sorted(pattern.occurrences, key=lambda o: (len(one_line(o[2], 400)), o[1] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)))
    for session, stamp, text, _pid in ordered:
        flat = one_line(text, limit)
        if not flat or flat.lower() in seen:
            continue
        seen.add(flat.lower())
        tool = "Claude Code" if session.tool == "claude-code" else "Codex"
        shown.append('"%s" (%s, %s)' % (redact(flat), tool, local(stamp)))
        if len(shown) >= count:
            break
    return shown


def render_pattern(pattern: Pattern, redact: Redactor, level: dict) -> str:
    sessions = len(pattern.sessions())
    count = pattern.count()
    folders = folders_of(pattern, redact)
    head = "### %s · %s · %d time%s in %d session%s (%s), %d folder%s · %s" % (
        pattern.tag, KIND_TEXT[pattern.kind], count, "" if count == 1 else "s", sessions, "" if sessions == 1 else "s",
        tools_text(pattern), len(folders), "" if len(folders) == 1 else "s", STATUS_TEXT.get(pattern.status, pattern.status),
    )
    lines = [head, "- What repeats: " + pattern.label]
    first, last = pattern.first_ts(), pattern.last_ts()
    if first and last:
        lines.append("- First and last time in this window: %s, %s" % (local(first), local(last)))
    examples = examples_of(pattern, redact, level["examples"], level["example_chars"])
    if examples:
        lines.append("- Examples: " + "; ".join(examples))
    together = pattern.extra.get("together") or []
    if together:
        lines.append("- Repeated together with it: " + "; ".join('"%s"' % redact(one_line(p.occurrences[0][2], 90)) for p in together[: level["together"]]))
    if pattern.kind == "approval":
        lines.append("- Safety of the command: %s" % pattern.extra.get("safety"))
        entry = allowlist_entries(pattern)
        if entry:
            lines.append("- Allowlist entry that would cover it: " + entry)
    lines.append("- Folders: " + ", ".join("%s (%d)" % (one_line(f, 70), n) for f, n in folders.most_common(level["folders"])))
    if pattern.covered is not None:
        covered = pattern.covered
        where = covered["file"] + (" line %d" % covered["line"] if covered.get("line") else "")
        reached, total = covered.get("sessions_in_reach", 0), covered.get("sessions", 0)
        if reached == 0:
            verdict = "%s, and none of the %d sessions were in its reach, so the rule never applied. Put it where the agent that repeats it will read it." % (reach_note(covered), total)
        elif reached < total:
            verdict = "%s. It reached %d of the %d sessions and the user still repeated it there, so the rule is not enough; the other %d ran where it does not apply." % (
                reach_note(covered), reached, total, total - reached)
        else:
            verdict = "%s. It reached every one of these sessions and the user still repeated it, so this rule is not enough." % reach_note(covered)
        lines.append('- Already configured: %s: "%s". %s' % (where, covered["text"], verdict))
    if pattern.state_note:
        lines.append("- Since the last run: " + pattern.state_note)
    lines.append("- Fix kinds that fit: " + fix_kinds(pattern))
    return "\n".join(lines) + "\n"


LEVELS = [
    {"examples": 2, "example_chars": 160, "folders": 3, "together": 4},
    {"examples": 2, "example_chars": 110, "folders": 2, "together": 2},
    {"examples": 1, "example_chars": 80, "folders": 1, "together": 1},
]


def render_setup(setup: dict, slash: Counter, redact: Redactor) -> list:
    lines = ["## Current setup", ""]
    for entry in setup["claude"]:
        lines.append("### Claude Code (%s)" % entry["dir"])
        doc = entry.get("claude_md")
        lines.append("- CLAUDE.md: " + (describe_doc(doc) if doc else "none"))
        for settings in entry.get("settings", []):
            lines.append("- %s: %s" % (settings["path"].split("/")[-1], describe_settings(settings)))
        if not entry.get("settings"):
            lines.append("- settings.json: none")
        lines.append("- Skills: %d; commands: %d (%s); memory files: %d" % (len(entry["skills"]), len(entry["commands"]), ", ".join(entry["commands"][:8]) or "none", entry["memory_files"]))
    codex = setup.get("codex")
    if codex:
        lines.append("### Codex (%s)" % codex["dir"])
        config = codex.get("config") or {}
        if config:
            bits = ["%s %s" % (k, config[k]) for k in ("approval_policy", "sandbox_mode", "approvals_reviewer") if config.get(k)]
            if config.get("mcp_servers"):
                bits.append("MCP servers: " + ", ".join(config["mcp_servers"][:8]))
            lines.append("- config.toml: " + ("; ".join(bits) or "present"))
        else:
            lines.append("- config.toml: none")
        doc = codex.get("AGENTS.md")
        lines.append("- AGENTS.md: " + (describe_doc(doc) if doc else "none"))
        lines.append("- rules: %d allow prefix rules; hooks: %s; skills: %d; prompts: %d; memory files: %d" % (
            codex["prefix_rules"], ", ".join(codex["hooks"]) or "none", len(codex["skills"]), len(codex["prompts"]), codex["memory_files"]))
    for entry in setup["projects"]:
        lines.append("### Project %s" % entry["folder"])
        for doc in entry["docs"]:
            lines.append("- %s: %s" % (doc["path"].split("/")[-1] if "/.claude/" not in doc["path"] else ".claude/CLAUDE.md", describe_doc(doc)))
        if not entry["docs"]:
            lines.append("- No CLAUDE.md or AGENTS.md")
        for settings in entry["settings"]:
            lines.append("- .claude/%s: %s" % (settings["path"].split("/")[-1], describe_settings(settings)))
        if entry["mcp_servers"]:
            lines.append("- .mcp.json servers: " + ", ".join(entry["mcp_servers"][:8]))
        if entry["skills"] or entry["commands"]:
            lines.append("- Project skills: %s; commands: %s" % (", ".join(entry["skills"][:8]) or "none", ", ".join(entry["commands"][:8]) or "none"))
    if slash:
        lines.append("### Skills and slash commands the user already runs (fixes that work)")
        lines.append("- " + ", ".join("`%s` x%d" % (redact(name), n) for name, n in slash.most_common(10)))
    lines.append("")
    return lines


def describe_doc(doc: dict) -> str:
    text = "%d lines, %.1f KB" % (doc["lines"], doc["bytes"] / 1024.0)
    if doc["headings"]:
        text += "; sections: " + ", ".join(doc["headings"][:8])
    if doc["imports"]:
        text += "; imports: " + ", ".join(doc["imports"][:4])
    return text


def describe_settings(settings: dict) -> str:
    bits = []
    if settings.get("default_mode"):
        bits.append("permission mode %s" % settings["default_mode"])
    bits.append("allow %d%s" % (len(settings["allow"]), (" (" + ", ".join("`%s`" % a for a in settings["allow"][:8]) + ")") if settings["allow"] else ""))
    if settings["ask"] or settings["deny"]:
        bits.append("ask %d, deny %d" % (settings["ask"], settings["deny"]))
    if settings["hooks"]:
        bits.append("hooks: " + ", ".join("%s (%d)" % (k, v) for k, v in settings["hooks"].items()))
    return "; ".join(bits)


def render_digest(meta: dict, patterns: list, fixed_only: list, since_lines: list, setup: dict, slash: Counter, redact: Redactor, max_chars: int) -> str:
    header = [
        "# Repetitions in coding-agent sessions from %s to %s" % (local_full(meta["since"]), local_full(meta["until"])),
        "",
        "%d sessions (%d Claude Code, %d Codex) with %d prompts in %d folders. %d repetition patterns, tagged P1 to P%d, strongest first." % (
            meta["sessions"], meta["claude"], meta["codex"], meta["prompts"], meta["folders"], len(patterns), len(patterns)),
        "Counts come from the local transcripts and history files. Secrets, tokens, email addresses, and home-folder paths were removed before this file was written; home folders appear as ~.",
        "",
    ]
    body = []
    if since_lines:
        body += ["## Since the last run", ""] + since_lines + [""]
    body += render_setup(setup, slash, redact)
    body.append("## Repetitions, strongest first\n")
    budget = max(900, (max_chars - 6000 - len("\n".join(header + body))) // max(len(patterns) + len(fixed_only), 1))
    for pattern in patterns:
        text = ""
        for level in LEVELS:
            text = render_pattern(pattern, redact, level)
            if len(text) <= budget:
                break
        if len(text) > budget:
            text = text[: budget - 40].rstrip() + "\n- [... cut to fit the digest size limit]\n"
        body.append(text)
    if fixed_only:
        body.append("## Fixes from earlier runs that no longer repeat often enough to be listed above\n")
        for pattern in fixed_only:
            body.append("### %s · %s · %s\n- What repeated: %s\n- %s\n" % (pattern.tag, KIND_TEXT[pattern.kind], STATUS_TEXT.get(pattern.status, pattern.status), pattern.label, pattern.state_note))
    text = "\n".join(body)
    counts = digest_markers(text)
    if any(counts.values()):
        words = {"redacted": "[REDACTED]", "email": "[email]", "user": "[user]"}
        header.insert(4, "Redaction markers in this file: " + ", ".join("%s %d" % (words[k], v) for k, v in counts.items() if v) + ".")
    return "\n".join(header) + "\n" + text


def digest_markers(text: str) -> dict:
    return {"redacted": text.count("[REDACTED"), "email": text.count("[email]"), "user": text.count("[user]")}


# --------------------------------------------------------------------------------------------
# Gather


def claude_default_dir(home: Path) -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else home / ".claude"


def codex_default_home(home: Path) -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else home / ".codex"


def recent_files(root: Path, since_epoch: float, depth_glob: str):
    if not root.is_dir():
        return []
    found = []
    for candidate in root.glob(depth_glob):
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
            raise SystemExit(usage_error("--since must look like 2026-09-14 or 2026-09-14T09:00 (local time)."))
        return stamp.astimezone(dt.timezone.utc)  # A naive value is read as local time.
    if args.days <= 0:
        raise SystemExit(usage_error("--days must be greater than 0."))
    return now - dt.timedelta(days=args.days)


def cmd_gather(args) -> int:
    home = Path.home()
    now = dt.datetime.now(dt.timezone.utc)
    since = window_start(args, now)
    since_epoch = since.timestamp()
    redact = Redactor(home)
    stats: Counter = Counter()
    min_sessions = max(2, args.min_sessions)

    claude_dirs = [] if args.no_claude else [claude_default_dir(home)] + [Path(d).expanduser() for d in args.claude_dir]
    claude_dirs = list(OrderedDict((norm_path(str(d)), d) for d in claude_dirs).values())
    codex_home = None if args.no_codex else (Path(args.codex_home).expanduser() if args.codex_home else codex_default_home(home))
    exclude = set(args.exclude_session)
    for variable in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID"):
        if os.environ.get(variable) and not args.include_current:
            exclude.add(os.environ[variable])

    sessions: list = []
    seen_ids: set = set()
    pastes: list = []
    history_only: list = []
    for config_dir in claude_dirs:
        entries = claude_history(config_dir, since, stats)
        index: dict = defaultdict(list)
        for entry in entries:
            index[entry["session"]].append((entry["ts"], entry["display"]))
        for path in recent_files(config_dir / "projects", since_epoch, "*/*.jsonl"):
            session = parse_claude(path, config_dir, since, stats, index)
            seen_ids.add(("claude-code", session.session_id))
            sessions.append(session)
        pastes += [p for p in paste_records(config_dir, entries)]
        history_only += [("claude-code", config_dir, e) for e in entries]
    codex_rules = load_codex_rules(codex_home) if codex_home is not None else []
    if codex_home is not None:
        for path in recent_files(codex_home / "sessions", since_epoch, "*/*/*/rollout-*.jsonl"):
            session = parse_codex(path, codex_home, since, stats, codex_rules)
            if session is not None:
                seen_ids.add(("codex", session.session_id))
                sessions.append(session)
        history_only += [("codex", codex_home, e) for e in codex_history(codex_home, since, stats)]

    # Prompts whose session has no transcript left (Claude Code deletes old ones) come from history.
    extra: "OrderedDict[tuple, Session]" = OrderedDict()
    for tool, folder, entry in sorted(history_only, key=lambda t: t[2]["ts"]):
        key = (tool, entry["session"])
        if not entry["session"] or key in seen_ids:
            continue
        session = extra.get(key)
        if session is None:
            session = Session(tool, entry["session"], None, folder, source="history")
            session.add_cwd(entry["project"])
            extra[key] = session
        text = entry["display"]
        if text.strip().startswith("/") and text.strip().split()[0] in CLAUDE_BUILTIN_COMMANDS and len(text.split()) == 1:
            continue
        session.add_prompt(text, entry["ts"], since, None, None)
    sessions += list(extra.values())

    kept = []
    for session in sessions:
        if session.skip_reason:
            stats["skipped_" + session.skip_reason] += 1
        elif session.session_id in exclude:
            stats["skipped_current_or_excluded"] += 1
        elif session.last_ts is None or (not session.prompts and not session.approvals and not session.failures and not session.rejections):
            stats["skipped_no_activity_in_window"] += 1
        elif args.project_dir and not any(inside(c, d) for c in session.cwds for d in args.project_dir):
            stats["skipped_outside_project_dir"] += 1
        else:
            kept.append(session)
    kept_keys = {s.key for s in kept}
    pastes = [p for p in pastes if ("claude-code", p[3]) in kept_keys or not args.project_dir]

    other_dirs = []
    if not args.no_claude:
        read = {norm_path(str(d)) for d in claude_dirs}
        for candidate in sorted(home.glob(".claude*")):
            if candidate.is_dir() and (candidate / "projects").is_dir() and norm_path(str(candidate)) not in read:
                other_dirs.append(str(candidate))

    summary: dict = {
        "window": {"since": since.isoformat(), "until": now.isoformat(), "since_local": local_full(since), "until_local": local_full(now)},
        "read": {"claude_dirs": [str(d) for d in claude_dirs], "codex_home": None if codex_home is None else str(codex_home)},
        "other_claude_dirs_not_read": other_dirs,
        "skipped": {k[len("skipped_"):]: v for k, v in sorted(stats.items()) if k.startswith("skipped_")},
        "unreadable": {k: v for k, v in stats.items() if k.startswith("unreadable")},
    }
    if not kept:
        summary["error"] = "no sessions found"
        summary["hint"] = "No Claude Code or Codex session with activity in this window matched. Use a longer --days, add --claude-dir, or drop --project-dir."
        print(json.dumps(summary, indent=2))
        return 3

    patterns, candidates, slash = detect(kept, pastes, min_sessions)

    # Folders for the project part of the setup: the ones named, or the busiest ones.
    if args.project_dir:
        folders = [Path(d).expanduser().resolve() for d in args.project_dir]
    else:
        busiest = Counter(s.cwd for s in kept if s.cwd)
        folders = []
        for cwd, _n in busiest.most_common(12):
            path = Path(cwd)
            root = git_root(path) if path.is_dir() else None
            if root is not None and norm_path(str(root)) != norm_path(str(home)) and all(norm_path(str(root)) != norm_path(str(f)) for f in folders):
                folders.append(root)
            if len(folders) >= 4:
                break
    setup, rules_text, allow_entries = inventory(claude_dirs, codex_home, folders, home, redact)
    for pattern in patterns:
        pattern.covered = find_existing_rule(pattern, rules_text, allow_entries, codex_rules, redact)

    # Compare with the state file of earlier runs.
    state_dir = None if args.no_state else (Path(args.state_dir).expanduser() if args.state_dir else default_state_dir())
    state = load_state(state_dir) if state_dir is not None else {"schema": STATE_SCHEMA, "runs": [], "patterns": {}}
    stored = state["patterns"]
    last_run = state["runs"][-1]["at"] if state["runs"] else None
    taken: set = set()
    for pattern in patterns:
        fid = match_stored(pattern, stored, taken)
        if fid is None:
            pattern.fid = new_fid(pattern)
            pattern.status = "new"
            while pattern.fid in stored or pattern.fid in taken:
                pattern.fid += "x"
        else:
            entry = stored[fid]
            pattern.fid = fid
            info = since_fix(entry, candidates, kept)
            pattern.since_fix = info or None
            pattern.status = fix_status(entry, info)
            pattern.state_note = state_note(entry, info, pattern.status)
        taken.add(pattern.fid)
    for pattern in patterns:
        pattern.label = label_for(pattern, redact)

    ranked = rank(patterns, args.max_patterns)
    fixed_only = []
    for fid, entry in stored.items():
        if fid in {p.fid for p in ranked} or (entry.get("fix") or {}).get("status") not in ("applied", "accepted", "rejected"):
            continue
        holder = next((p for p in patterns if p.fid == fid), None) or Pattern(entry.get("kind", "correction"), key=entry.get("key", ""), terms=frozenset(entry.get("terms") or ()))
        holder.fid = fid
        info = since_fix(entry, candidates, kept)
        holder.since_fix = info or None
        holder.status = fix_status(entry, info)
        holder.state_note = state_note(entry, info, holder.status)
        holder.label = entry.get("label") or holder.label or KIND_TEXT.get(holder.kind, "")
        fixed_only.append(holder)
    for number, pattern in enumerate(ranked + fixed_only, 1):
        pattern.tag = "P%d" % number

    faded = [fid for fid, entry in stored.items() if fid not in taken and not entry.get("fix") and entry.get("last_seen")]
    groups: dict = defaultdict(list)
    for pattern in ranked + fixed_only:
        groups[pattern.status].append(pattern.tag)
    since_summary = {
        "last_run": last_run,
        "new": groups.get("new", []) if last_run else [],
        "stopped_after_fix": groups.get("stopped_after_fix", []),
        "continues_despite_fix": groups.get("continues_despite_fix", []),
        "too_early": groups.get("too_early", []),
        "fix_pending": groups.get("fix_pending", []),
        "rejected": groups.get("rejected", []),
        "faded_without_fix": len(faded),
    }
    since_lines = render_since(since_summary, ranked + fixed_only) if last_run else []

    out_dir = Path(args.out).expanduser() if args.out else Path(tempfile.gettempdir()) / SLUG / now.astimezone().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "since": since,
        "until": now,
        "sessions": len(kept),
        "claude": sum(1 for s in kept if s.tool == "claude-code"),
        "codex": sum(1 for s in kept if s.tool == "codex"),
        "prompts": sum(len(s.prompts) for s in kept),
        "folders": len({s.cwd for s in kept}),
    }
    digest = render_digest(meta, ranked, fixed_only, since_lines, setup, slash, redact, args.max_chars)
    write_file(out_dir / "digest.md", digest)
    index = build_index(ranked + fixed_only, since, now, redact)
    write_file(out_dir / "patterns.json", json.dumps(index, indent=2))
    inputs_path = out_dir / "inputs.json"
    warnings = []
    if inputs_path.exists():
        warnings.append("inputs.json already existed and was left as it is; check its pattern tags against the new digest.")
    else:
        write_file(inputs_path, json.dumps(inputs_template(), indent=2))

    state_path = None
    if state_dir is not None:
        update_state(state, ranked, now, since, len(kept))
        state_path = str(save_state(state_dir, state))

    summary.update(
        {
            "out_dir": str(out_dir),
            "digest": str(out_dir / "digest.md"),
            "patterns_index": str(out_dir / "patterns.json"),
            "inputs": str(inputs_path),
            "digest_chars": len(digest),
            "sessions": {
                "total": len(kept),
                "claude_code": meta["claude"],
                "codex": meta["codex"],
                "from_history_only": sum(1 for s in kept if s.source == "history"),
                "prompts": meta["prompts"],
            },
            "patterns": {"total": len(ranked), "found_before_ranking": len(patterns), "by_kind": dict(Counter(p.kind for p in ranked))},
            "since_last_run": since_summary,
            "state_file": state_path,
            "redactions_in_digest": digest_markers(digest),
            "table": [
                {
                    "tag": p.tag,
                    "kind": p.kind,
                    "label": p.label,
                    "count": p.count(),
                    "sessions": len(p.sessions()),
                    "status": p.status,
                    "already_configured": (p.covered or {}).get("file"),
                }
                for p in ranked + fixed_only
            ],
            "warnings": warnings,
        }
    )
    print(json.dumps(summary, indent=2))
    return 0


def state_note(entry: dict, info: dict, status: str) -> str:
    fix = entry.get("fix") or {}
    when = parse_ts(fix.get("at"))
    idea = one_line(fix.get("idea") or "a fix", 100)
    first = parse_ts(entry.get("first_seen"))
    seen = "first seen %s" % local(first) if first else "seen before"
    if status == "continues_despite_fix":
        return "%s; fixed %s with \"%s\", but it repeated %d time%s in %d session%s since. The fix did not hold." % (
            seen, local(when), idea, info["repeats_since_fix"], "" if info["repeats_since_fix"] == 1 else "s",
            info["sessions_with_repeats"], "" if info["sessions_with_repeats"] == 1 else "s")
    if status == "stopped_after_fix":
        return "%s; fixed %s with \"%s\"; 0 repeats in %d sessions since. The fix worked." % (seen, local(when), idea, info["sessions_since_fix"])
    if status == "too_early" and not info.get("sessions_since_fix"):
        return "%s; fixed %s with \"%s\"; no session since, too early to tell." % (seen, local(when), idea)
    if status == "too_early":
        return "%s; fixed %s with \"%s\"; only %d session%s since, too few to tell." % (seen, local(when), idea, info.get("sessions_since_fix", 0), "" if info.get("sessions_since_fix", 0) == 1 else "s")
    if status == "fix_pending":
        return "%s; the user accepted \"%s\" on %s but it is not applied yet." % (seen, idea, local(when))
    if status == "rejected":
        return "%s; the user rejected \"%s\" on %s. Propose a different kind of fix." % (seen, idea, local(when))
    runs = entry.get("seen_in_runs", 0)
    return "%s, found in %d earlier run%s, no fix yet." % (seen, runs, "" if runs == 1 else "s")


def render_since(summary: dict, patterns: list) -> list:
    by_tag = {p.tag: p for p in patterns}
    lines = ["Last run: %s." % local_full(parse_ts(summary["last_run"]))]
    for key, text in (
        ("continues_despite_fix", "Continues despite its fix (a rule or change was not enough)"),
        ("stopped_after_fix", "Stopped after its fix (this kind of fix works for this user)"),
        ("too_early", "Fixed recently, too early to tell"),
        ("fix_pending", "Fix accepted but not applied yet"),
        ("rejected", "A fix was rejected before"),
        ("new", "New since the last run"),
    ):
        tags = summary.get(key) or []
        if tags:
            lines.append("- %s: %s" % (text, "; ".join("%s %s" % (t, one_line(by_tag[t].label, 70)) for t in tags[:10])))
    if summary.get("faded_without_fix"):
        lines.append("- Patterns from earlier runs that no longer appear, with no fix recorded: %d" % summary["faded_without_fix"])
    return lines


def build_index(patterns: list, since, now, redact: Redactor) -> dict:
    entries = []
    for pattern in patterns:
        sessions = OrderedDict()
        for session, stamp, _text, _pid in pattern.occurrences:
            item = sessions.setdefault(session.key, {"tool": session.tool, "session_id": session.session_id, "cwd": session.cwd, "times": 0, "last": None})
            item["times"] += 1
            if stamp is not None and (item["last"] is None or stamp > item["last"]):
                item["last"] = stamp
        entries.append(
            {
                "tag": pattern.tag,
                "fingerprint": pattern.fid,
                "kind": pattern.kind,
                "label": pattern.label,
                "status": pattern.status,
                "count": pattern.count(),
                "session_count": len(sessions),
                "fix_kinds": fix_kinds(pattern),
                "allowlist_entry": allowlist_entries(pattern) or None,
                "safety": pattern.extra.get("safety"),
                "already_configured": pattern.covered,
                "since_fix": pattern.since_fix,
                "examples": examples_of(pattern, redact, 3, 200) if pattern.occurrences else [],
                "sessions": [dict(v, last=iso(v["last"])) for v in sessions.values()],
            }
        )
    return {"schema": 1, "window": {"since": since.isoformat(), "until": now.isoformat()}, "patterns": entries}


def update_state(state: dict, patterns: list, now, since, session_count: int) -> None:
    stored = state["patterns"]
    for pattern in patterns:
        entry = stored.get(pattern.fid)
        first, last = pattern.first_ts(), pattern.last_ts()
        if entry is None:
            entry = {"kind": pattern.kind, "key": pattern.key, "terms": sorted(pattern.terms), "label": pattern.label, "first_seen": iso(first), "last_seen": iso(last), "seen_in_runs": 0, "history": [], "fix": None}
            stored[pattern.fid] = entry
        entry["label"] = pattern.label
        if first and (not entry.get("first_seen") or first < parse_ts(entry["first_seen"])):
            entry["first_seen"] = iso(first)
        if last and (not entry.get("last_seen") or last > parse_ts(entry["last_seen"])):
            entry["last_seen"] = iso(last)
        entry["seen_in_runs"] = int(entry.get("seen_in_runs", 0)) + 1
        entry["history"] = (entry.get("history") or [])[-11:] + [{"at": iso(now), "count": pattern.count(), "sessions": len(pattern.sessions())}]
    state["runs"].append({"at": iso(now), "since": iso(since), "sessions": session_count, "patterns": len(patterns)})


def inputs_template() -> dict:
    return {
        "goal": "Find the changes that would stop me from having to repeat myself to my coding agents (Claude Code and Codex): for each repetition in the digest, the durable fix (a CLAUDE.md or AGENTS.md rule, a skill or slash command, a hook, a permission allowlist entry for a read-only command, a memory entry, or a small script) and the exact change to make.",
        "project": PROJECT_KEY,
        **{kind: [] for kind in FLAG_KINDS},
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
            if kind in UNTAGGED_KINDS:
                continue
            match = TAG_RE.match(value)
            if not match:
                problems.append("Every %s value must start with pattern tags such as [P2] or [P1 P4], or [setup] or [all]: %s" % (kind, one_line(value, 80)))
                continue
            for tag in re.findall(r"P\d+", match.group(1)):
                if tag not in known_tags:
                    problems.append("%s names %s, which is not in patterns.json: %s" % (kind, tag, one_line(value, 80)))
        values[kind] = cleaned
    if not values.get("mistakes"):
        problems.append("Write at least one mistakes value (the strongest repetitions from the digest) before running.")
    return {"goal": goal, "project": project, "values": values}, problems


def build_argv(exe: str, inputs: dict, dry_run: bool) -> list:
    argv = [exe, "--json", "--no-input", "agents", "suggest", "run", "--goal", inputs["goal"]]
    for category in CATEGORIES:
        argv += ["--category", category]
    argv += ["--count", str(COUNT), "--horizon", "any"]
    for kind, (flag, _most) in FLAG_KINDS.items():
        for value in inputs["values"].get(kind, []):
            argv += [flag, value]
    for value in FORBIDDEN:
        argv += ["--forbidden", value]
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


def resolve_cli(cli: str):
    """The full path of the vidbyte-cli to run: --cli as given, or the first vidbyte-cli on PATH."""
    exe = shutil.which(cli)
    if not exe:
        where = "on PATH" if cli == "vidbyte-cli" else "at %s" % cli
        print(json.dumps({"error": "No runnable vidbyte-cli %s. Follow references/setup.md step 1, or pass --cli with its full path." % where}))
        return None
    print("Using vidbyte-cli at %s" % exe, file=sys.stderr)
    return exe


def display_command(argv: list) -> str:
    shown = ["vidbyte-cli"] + argv[1:]
    return " ".join(shlex.quote(a) for a in shown)


def cmd_suggest(args) -> int:
    out_dir = Path(args.out).expanduser()
    if args.dry_run == args.paid:
        return usage_error("Pass exactly one of --dry-run (free check) or --paid (the usage-billed run, only after the user said yes).")
    if not (out_dir / "digest.md").is_file():
        return usage_error("digest.md not found in %s. Run `gather` first and pass the out_dir it printed." % out_dir)
    index = load_json(out_dir / "patterns.json", "patterns.json")
    known = {e.get("tag") for e in index.get("patterns", []) if isinstance(e, dict)}
    redact = Redactor(Path.home())
    inputs, problems = validate_inputs(load_json(out_dir / "inputs.json", "inputs.json"), known, redact)
    if problems:
        print(json.dumps({"error": "inputs.json needs fixing", "problems": problems}, indent=2))
        for problem in problems:
            print("inputs.json: " + problem, file=sys.stderr)
        return 2
    exe = resolve_cli(args.cli)
    if not exe:
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
        write_file(marker, "Paid run started %s with --max-total-tokens %d.\n" % (dt.datetime.now().isoformat(timespec="seconds"), MAX_TOTAL_TOKENS))
    print("Running in %s:\n%s" % (out_dir, display_command(argv)), file=sys.stderr)
    code, stdout_path, stderr_path, error = run_cli(argv, out_dir, "dry-run.json" if args.dry_run else "result.json")
    report: dict = {"exit_code": code, "cli": exe, "stderr_file": str(stderr_path)}
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


# --------------------------------------------------------------------------------------------
# Feedback


def idea_tags(idea: dict) -> list:
    texts = [idea.get("title", ""), idea.get("summary", ""), idea.get("first_action", "")]
    texts += [str(a) for a in idea.get("suggested_actions") or []]
    handoff = idea.get("handoff") if isinstance(idea.get("handoff"), dict) else {}
    for item in handoff.get("evidence") or []:
        if isinstance(item, dict):
            texts.append(str(item.get("content", ""))[:2000])
    found = []
    for text in texts:
        for tag in re.findall(r"\bP(\d+)\b", str(text)):
            if "P" + tag not in found:
                found.append("P" + tag)
    return found


def cmd_feedback(args) -> int:
    out_dir = Path(args.out).expanduser()
    if not (args.accept or args.reject):
        return usage_error("Pass --accept or --reject, with the user's explicit verdict.")
    if args.applied and not args.accept:
        return usage_error("--applied goes with --accept: record it only after the change is in place.")
    result = load_json(out_dir / "result.json", "result.json")
    ideas = (result.get("data") or {}).get("ideas") or [] if isinstance(result, dict) else []
    match = [i for i in ideas if isinstance(i, dict) and i.get("id") == args.idea]
    if not match:
        return usage_error("No idea with id %s in result.json. Ids look like idea-001." % args.idea)
    idea = match[0]
    index = load_json(out_dir / "patterns.json", "patterns.json")
    by_tag = {e.get("tag"): e for e in index.get("patterns", []) if isinstance(e, dict)}
    tags = [t.strip() for t in args.pattern] if args.pattern else idea_tags(idea)
    unknown = [t for t in tags if t not in by_tag]
    if args.pattern and unknown:
        return usage_error("Unknown pattern tag(s): %s. Use the tags in patterns.json, such as P3." % ", ".join(unknown))
    tags = [t for t in tags if t in by_tag]
    redact = Redactor(Path.home())
    if not args.state_only:
        project = args.project
        if not project:
            inputs = load_json(out_dir / "inputs.json", "inputs.json")
            project = inputs.get("project") if isinstance(inputs, dict) else None
        if not project or not PROJECT_RE.match(project):
            return usage_error("No suggestion project. Create it first (see the skill's Gather inputs section), then pass --project or set project in inputs.json.")
        exe = resolve_cli(args.cli)
        if not exe:
            return 127
        verb = "accept" if args.accept else "reject"
        argv = [exe, "--json", "--no-input", "agents", "suggest", "feedback", verb, "--project", project, "--suggestion", idea["title"]]
        if args.reason and args.reason.strip():
            argv += ["--reason", redact(" ".join(args.reason.split()))]
        print(display_command(argv), file=sys.stderr)
        completed = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cli_out = completed.stdout.decode("utf-8", "replace").strip()
        cli_err = completed.stderr.decode("utf-8", "replace").strip()
        if completed.returncode != 0:
            failure: dict = {"error": "vidbyte-cli feedback failed; the state file was not changed.", "exit_code": completed.returncode}
            for line in reversed(cli_err.splitlines()):
                try:
                    document = json.loads(line)
                except ValueError:
                    continue
                if isinstance(document, dict) and document.get("kind") == "error" and isinstance(document.get("data"), dict):
                    failure["cli_error"] = {k: document["data"].get(k) for k in ("code", "message", "hint") if k in document["data"]}
                    break
            print(json.dumps(failure, indent=2))
            return completed.returncode
        try:
            recorded_kind = json.loads(cli_out).get("kind")
        except (ValueError, AttributeError):
            recorded_kind = None
    report: dict = {"idea": args.idea, "verdict": "accept" if args.accept else "reject", "patterns": tags, "cli": None, "cli_kind": None, "state_file": None}
    if not args.state_only:
        report["cli"], report["cli_kind"] = exe, recorded_kind
    if not args.no_state and tags:
        state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
        state = load_state(state_dir)
        now = dt.datetime.now(dt.timezone.utc)
        status = "rejected" if args.reject else ("applied" if args.applied else "accepted")
        fix = {"status": status, "at": iso(now), "idea": redact(one_line(idea.get("title", ""), 200)), "summary": redact(one_line(idea.get("summary", ""), 300)), "reason": redact(one_line(args.reason or "", 200)) or None}
        for tag in tags:
            entry = by_tag[tag]
            stored = state["patterns"].setdefault(
                entry["fingerprint"],
                {"kind": entry["kind"], "key": "", "terms": [], "label": entry.get("label", ""), "first_seen": None, "last_seen": None, "seen_in_runs": 0, "history": [], "fix": None},
            )
            if stored.get("fix"):
                stored.setdefault("earlier_fixes", []).append(stored["fix"])
                stored["earlier_fixes"] = stored["earlier_fixes"][-5:]
            stored["fix"] = fix
        report["state_file"] = str(save_state(state_dir, state))
        report["recorded"] = status
    elif not tags:
        report["warning"] = "No pattern tag found in this idea; pass --pattern P<n> so the next run can check the fix."
    print(json.dumps(report, indent=2))
    return 0


# --------------------------------------------------------------------------------------------
# Status


def cmd_status(args) -> int:
    state_dir = Path(args.state_dir).expanduser() if args.state_dir else default_state_dir()
    path = state_dir / "state.json"
    state = load_state(state_dir, rescue=False) if path.is_file() else None
    now = dt.datetime.now(dt.timezone.utc)
    if state is None or not state["runs"]:
        if not args.remind:
            print(json.dumps({"state_file": str(path), "runs": 0, "due": True}, indent=2))
        return 0
    last = parse_ts(state["runs"][-1]["at"])
    days = (now - last).days if last else None
    fixes = Counter((e.get("fix") or {}).get("status") or "none" for e in state["patterns"].values())
    due = days is None or days >= args.due_days
    if args.remind:
        if due:
            waiting = fixes.get("applied", 0)
            print(
                "stop-repeating-yourself: the last check for instructions the user keeps repeating to coding agents was %d days ago%s. "
                "Offer to run the stop-repeating-yourself skill again; it asks before it spends anything (model usage billed to the user's Vidbyte balance)." % (
                    days or 0, ", and %d applied fix%s can now be checked" % (waiting, "" if waiting == 1 else "es") if waiting else "")
            )
        return 0
    print(json.dumps({"state_file": str(path), "runs": len(state["runs"]), "last_run": state["runs"][-1]["at"], "days_since_last_run": days, "due": due, "patterns": len(state["patterns"]), "fixes": dict(fixes)}, indent=2))
    return 0


# --------------------------------------------------------------------------------------------
# Command line


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="repeats.py",
        description="Find what you keep repeating to Claude Code and Codex, and turn it into durable fixes with the Vidbyte Suggestion Agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 repeats.py gather                       # last 30 days, every project\n"
            "  python3 repeats.py gather --days 14 --project-dir .\n"
            "  python3 repeats.py gather --claude-dir ~/.claude-work\n"
            "  python3 repeats.py suggest --out <out_dir> --dry-run   # free\n"
            "  python3 repeats.py suggest --out <out_dir> --paid      # billed by usage; ask the user first\n"
            "  python3 repeats.py feedback --out <out_dir> --idea idea-002 --accept --applied\n"
            "  python3 repeats.py status --remind\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    gather = sub.add_parser("gather", help="Find repetitions in recent sessions and write digest.md (free, local).", description="Read the transcripts and history files of a time window, find what repeats across sessions, check the current agent setup, compare with the state file, redact, and write digest.md, patterns.json, and an inputs.json template into a new folder. Prints a JSON summary. Exit 3 means no session matched.")
    gather.add_argument("--days", type=float, default=float(DEFAULT_DAYS), help="Window length in days, ending now (default: %d)." % DEFAULT_DAYS)
    gather.add_argument("--since", help="Window start in local time, such as 2026-09-14 or 2026-09-14T09:00. Overrides --days.")
    gather.add_argument("--project-dir", action="append", default=[], help="Keep only sessions that ran in this folder or below it, and check its CLAUDE.md, AGENTS.md, and settings. Repeatable.")
    gather.add_argument("--claude-dir", action="append", default=[], help="An extra Claude Code config folder to read, such as ~/.claude-work. Repeatable. $CLAUDE_CONFIG_DIR, else ~/.claude, is always read.")
    gather.add_argument("--codex-home", help="The Codex folder to read (default: $CODEX_HOME, else ~/.codex).")
    gather.add_argument("--no-claude", action="store_true", help="Skip Claude Code.")
    gather.add_argument("--no-codex", action="store_true", help="Skip Codex.")
    gather.add_argument("--exclude-session", action="append", default=[], help="A session id to leave out. Repeatable.")
    gather.add_argument("--include-current", action="store_true", help="Keep the session this command runs in (left out by default when the host exposes its id).")
    gather.add_argument("--min-sessions", type=int, default=2, help="A pattern must appear in at least this many sessions (default: 2; some kinds need 3).")
    gather.add_argument("--max-patterns", type=int, default=MAX_PATTERNS, help="Keep at most this many patterns in the digest (default: %d)." % MAX_PATTERNS)
    gather.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Size limit for digest.md in characters (default: %d)." % DEFAULT_MAX_CHARS)
    gather.add_argument("--state-dir", help="The folder that holds state.json (default: a per-user state folder outside every repository).")
    gather.add_argument("--no-state", action="store_true", help="Neither read nor update the state file.")
    gather.add_argument("--out", help="Output folder (default: a new folder under the system temp folder).")

    suggest = sub.add_parser("suggest", help="Check inputs.json and run vidbyte-cli agents suggest run (--dry-run is free; --paid is billed by usage).", description="Validate <out>/inputs.json against patterns.json, then run `vidbyte-cli --json --no-input agents suggest run` from inside <out>. Saves stdout to dry-run.json or result.json and stderr to a .stderr.txt file, prints a JSON summary, and exits with vidbyte-cli's exit code.")
    suggest.add_argument("--out", required=True, help="The out_dir that gather printed.")
    suggest.add_argument("--dry-run", action="store_true", help="Validate the full request for free: no model call, no charge.")
    suggest.add_argument("--paid", action="store_true", help="The usage-billed run. Use only after the user agreed to the cost.")
    suggest.add_argument("--cli", default="vidbyte-cli", help="The vidbyte-cli to run: a full path, or a name looked up on PATH (default: vidbyte-cli).")

    feedback = sub.add_parser("feedback", help="Record the user's explicit accept or reject of one idea, and which patterns it fixes (free, local).", description="Copy the exact title of one idea from <out>/result.json into `vidbyte-cli --json agents suggest feedback accept|reject`, then record the fix for the idea's patterns in the state file.")
    feedback.add_argument("--out", required=True, help="The out_dir that holds result.json.")
    feedback.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
    verdict = feedback.add_mutually_exclusive_group()
    verdict.add_argument("--accept", action="store_true", help="The user said they want this fix.")
    verdict.add_argument("--reject", action="store_true", help="The user said no to this fix.")
    feedback.add_argument("--applied", action="store_true", help="With --accept: the change is now in place, so later runs check whether the repetition stopped.")
    feedback.add_argument("--pattern", action="append", default=[], help="A pattern tag this fix is meant to stop, such as P3. Repeatable. Default: the tags the idea itself cites.")
    feedback.add_argument("--reason", help="The user's own reason, if they gave one.")
    feedback.add_argument("--project", help="The suggestion project key (default: project in inputs.json).")
    feedback.add_argument("--state-only", action="store_true", help="Update only the state file, for example to mark an idea accepted earlier as applied now.")
    feedback.add_argument("--state-dir", help="The folder that holds state.json (default: the per-user state folder).")
    feedback.add_argument("--no-state", action="store_true", help="Do not update the state file.")
    feedback.add_argument("--cli", default="vidbyte-cli", help="The vidbyte-cli to run: a full path, or a name looked up on PATH (default: vidbyte-cli).")

    status = sub.add_parser("status", help="Show the state file's summary; --remind prints one line only when a run is due (free, fast).", description="Read only state.json. Without --remind, print a JSON summary. With --remind, print one reminder line when the last run is --due-days old or older, and nothing otherwise.")
    status.add_argument("--remind", action="store_true", help="Print one plain line only when a new run is due, for a session-start hook.")
    status.add_argument("--due-days", type=int, default=DUE_DAYS, help="Days after which a new run is due (default: %d)." % DUE_DAYS)
    status.add_argument("--state-dir", help="The folder that holds state.json (default: the per-user state folder).")

    args = parser.parse_args(argv)
    if args.command == "gather":
        return cmd_gather(args)
    if args.command == "suggest":
        return cmd_suggest(args)
    if args.command == "feedback":
        return cmd_feedback(args)
    if args.command == "status":
        return cmd_status(args)
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
