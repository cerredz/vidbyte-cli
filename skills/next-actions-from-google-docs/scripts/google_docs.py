#!/usr/bin/env python3
"""Turn recently edited Google Docs into input for the Vidbyte Suggestion Agent.

Five subcommands, run in this order by the next-actions-from-google-docs skill:

  check     Free. Finds a Google access token (GOOGLE_OAUTH_ACCESS_TOKEN, --token-file, or
            `gcloud auth print-access-token`) and makes one tiny Drive API call to prove it works.
            Never prints the token.
  gather    Free and read-only on Google Drive. Lists the Docs (and, on request, Sheets, Slides,
            and Markdown or text files) edited in a time window, exports their text, finds lines
            that look like open tasks, redacts secrets, emails, phone numbers, home paths, and the
            login name, and writes items.json and digest.md in the shared notes-digest/1 format,
            plus inputs.json. With --from-items it reads an items.json written by the host
            agent's own Google Drive connector instead of calling Google.
  run       Checks inputs.json, then runs `vidbyte-cli --json --no-input agents suggest run ...`
            from inside the output folder. --dry-run is free; --paid is the one billed step and
            needs the user's yes first.
  report    Free and offline. Maps each idea in result.json to the documents it names and prints
            the next actions grouped by horizon, each with its source titles and links.
  feedback  Records the user's explicit accept or reject of one idea, copying the idea's exact
            title from result.json into `vidbyte-cli agents suggest feedback`.

Only the Python standard library is used, and Python 3.8 or newer is enough. External commands
(vidbyte-cli, gcloud) are started from argument lists, never through a shell.

Test-only: GOOGLE_DOCS_SKILL_TEST_API_BASE points the Drive calls at a local mock server. It
accepts only http://127.0.0.1:<port> or http://localhost:<port>, so a token can never be sent to
any other host through it. Users never set it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict
from pathlib import Path

SLUG = "next-actions-from-google-docs"
PROVIDER = "google-docs"
SCHEMA = "notes-digest/1"
PROJECT_KEY = "notes-next-actions"
CATEGORIES = ("immediate_next_steps", "prioritization", "completion", "continuation")
HORIZON = "any"
COUNT = 6
# The run starts no new model call once it has used this many tokens, which bounds its cost.
MAX_TOTAL_TOKENS = 400_000

# vidbyte-cli's default model for both roles, and its regular rates in US dollars per million
# tokens (https://developers.openai.com/api/docs/models/gpt-6-sol), which Vidbyte's model gateway
# bills plus a 2% markup. They only estimate a finished run's cost; vidbyte.pro/usage is exact.
MODEL = "gpt-6-sol"
RATES_PER_MILLION = {"input": 2.0, "cached_input": 0.2, "output": 10.0}
MARKUP = 1.02
USAGE_KEYS = ("model_calls", "input_tokens", "cached_input_tokens", "output_tokens")

DEFAULT_DAYS = 7
DEFAULT_MAX_ITEMS = 30
DEFAULT_MAX_ITEM_CHARS = 3_000
DEFAULT_MAX_CHARS = 60_000
HARD_MAX_CHARS = 1_000_000  # The CLI accepts 5,000,000 per --files file (MAX_CONTEXT_CHARS).
MIN_EXCERPT_CHARS = 200
MAX_TASKS_PER_ITEM = 15
MAX_TASK_CHARS = 240
MAX_COMMENTS_PER_ITEM = 5
MAX_RAW_TEXT_CHARS = 200_000  # Text read per document before redaction and truncation.
MAX_LIST_PAGES = 10
PAGE_SIZE = 100
MAX_FOLDER_LOOKUPS = 150
MAX_FOLDER_DEPTH = 8
MAX_TEXT_FILE_BYTES = 2_000_000
MAX_EXPORT_BYTES = 11_000_000  # Google limits exported content to 10 MB.
MAX_RETRIES = 5
MAX_BACKOFF_SECONDS = 32
HTTP_TIMEOUT_SECONDS = 60

TOKEN_ENV = "GOOGLE_OAUTH_ACCESS_TOKEN"
QUOTA_ENV = "GOOGLE_CLOUD_QUOTA_PROJECT"
TEST_BASE_ENV = "GOOGLE_DOCS_SKILL_TEST_API_BASE"
DEFAULT_API_BASE = "https://www.googleapis.com"

DOC_MIME = "application/vnd.google-apps.document"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SLIDES_MIME = "application/vnd.google-apps.presentation"
TEXT_MIMES = ("text/markdown", "text/x-markdown", "text/plain")
TYPE_MIMES = OrderedDict(
    [
        ("docs", (DOC_MIME,)),
        ("sheets", (SHEET_MIME,)),
        ("slides", (SLIDES_MIME,)),
        ("text", TEXT_MIMES),
    ]
)
EXPORT_MIMES = {DOC_MIME: ("text/markdown", "text/plain"), SHEET_MIME: ("text/csv",), SLIDES_MIME: ("text/plain",)}
LIST_FIELDS = (
    "nextPageToken,incompleteSearch,files(id,name,mimeType,webViewLink,createdTime,modifiedTime,"
    "modifiedByMeTime,ownedByMe,lastModifyingUser(me),parents,size)"
)

TOP_FIELDS = ("schema", "provider", "generated_at", "since", "items")
ITEM_FIELDS = ("provider", "id", "title", "link", "kind", "container", "created", "last_edited", "author", "open_tasks", "excerpt")
KINDS = ("note", "page", "database-row", "message", "thread", "file", "doc")

# Values the coding agent may put in inputs.json, with the vidbyte-cli flag each becomes and the
# most values this skill passes for it.
FLAG_KINDS = OrderedDict([("trajectory", ("--trajectory", 2)), ("constraint", ("--constraint", 3)), ("avoid", ("--avoid", 3))])
MAX_VALUE_CHARS = 400
MAX_GOAL_CHARS = 1_000
MAX_ARGV_CHARS = 24_000  # Windows refuses command lines over 32,767 characters.
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
QUOTA_PROJECT_RE = re.compile(r"^[A-Za-z0-9:._-]{4,64}$")
TEST_BASE_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):\d{1,5}$")


# --------------------------------------------------------------------------------------------
# Redaction (copied from pick-up-where-my-sessions-left-off, plus Google tokens and phone numbers)


class Redactor:
    """Removes secrets, emails, phone numbers, home paths, and the login name from any text."""

    TOKEN_RULES = [
        ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED private key]"),
        ("token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
        ("token", re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
        ("token", re.compile(r"(?<![A-Za-z0-9])1//[A-Za-z0-9_-]{20,}"), "[REDACTED]"),
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
    # A phone number: an optional +country code, an optional (area code), then 2 to 5 groups of
    # digits joined by spaces, dots, or hyphens. _phone keeps dates, IP addresses, and short numbers.
    PHONE_RE = re.compile(r"(?<![\w+/.:-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{1,4}\)[ .-]?)?\d{2,4}(?:[ .-]\d{2,4}){1,4}(?![\w/:-])")
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
        out = self.PHONE_RE.sub(self._phone, out)
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

    def _secret(self, match: "re.Match") -> str:
        name, value = match.group("name"), match.group("value")
        plain = not match.group("q1") and not match.group("q2") and not re.search(r"[_-]", name) and not name.isupper()
        # Prose such as "auth: flow" keeps its word; "password: hunter2" or a long value does not.
        if ":" in match.group("sep") and plain and not (re.search(r"\d", value) or len(value) >= 12):
            return match.group(0)
        self.counts["env_value"] += 1
        return "%s%s%s%s%s[REDACTED]" % (match.group("q1"), name, match.group("q1"), match.group("sep"), match.group("q2"))

    def _email(self, match: "re.Match") -> str:
        if match.group(1).lower() == "git":  # git@github.com:owner/repo is an address, not a person.
            return match.group(0)
        self.counts["email"] += 1
        return "[email]"

    def _phone(self, match: "re.Match") -> str:
        text = match.group(0)
        digits = re.sub(r"\D", "", text)
        if not 9 <= len(digits) <= 15:
            return text
        if re.search(r"\b\d{4}[-./]\d{1,2}[-./]\d{1,2}\b|\b\d{1,2}[-./]\d{1,2}[-./]\d{2,4}\b", text):
            return text  # A date, such as 2026-10-04 or 04/10/2026.
        groups = re.split(r"[ .-]+", text.strip("+"))
        if "." in text and not re.search(r"[ ()-]", text) and len(groups) == 4 and all(len(g) <= 3 for g in groups):
            return text  # An IPv4 address.
        separated = text.startswith("+") or "(" in text or re.search(r"\d[.-]\d", text) or text.startswith("0")
        if not separated:
            return text  # Plain space-separated numbers, such as "2025 2026 2027", are not phones.
        self.counts["phone"] += 1
        return "[phone]"


# --------------------------------------------------------------------------------------------
# Small helpers


UTC = dt.timezone.utc


def parse_ts(value: object):
    """Returns an aware UTC datetime for ISO strings or epoch numbers, or None."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000.0 if value > 1e11 else float(value)
            return dt.datetime.fromtimestamp(seconds, tz=UTC)
        text = str(value).strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # Python 3.8-3.10 accept 6 digits at most.
        text = re.sub(r"\.(\d{1,5})(?=[+-]|$)", lambda m: "." + m.group(1).ljust(6, "0"), text)
        stamp = dt.datetime.fromisoformat(text)
    except (ValueError, OverflowError, OSError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC)


def iso(stamp) -> str:
    return stamp.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_date(stamp) -> str:
    return stamp.astimezone().strftime("%Y-%m-%d") if stamp else "?"


def one_line(text: object, limit: int) -> str:
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)].rstrip() + "…"


def cut_text(text: str, limit: int) -> str:
    """Cuts text to at most `limit` characters, on a word boundary, with a visible marker."""
    if len(text) <= limit:
        return text
    marker = " [... cut]"
    keep = max(limit - len(marker), 0)
    head = text[:keep]
    space = max(head.rfind(" "), head.rfind("\n"))
    if space >= keep - 80 and space > 0:
        head = head[:space]
    return head.rstrip() + marker


def emit(document: dict) -> None:
    print(json.dumps(document, indent=2, ensure_ascii=False))


def usage_error(message: str) -> int:
    emit({"error": message})
    print("Error: " + message, file=sys.stderr)
    return 2


def write_text(path: Path, text: str) -> None:
    # Path.write_text only accepts newline= from Python 3.10, so open the file directly.
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_json(path: Path, data: object) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def load_json(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(usage_error("%s not found: %s. Run `gather` first, or pass the folder it printed as --out." % (label, path)))
    except ValueError as error:
        raise SystemExit(usage_error("%s is not valid JSON (%s): %s" % (label, error, path)))


# --------------------------------------------------------------------------------------------
# Text extraction: cleanup, open tasks, excerpt


DATA_URI_RE = re.compile(r"data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=\s]{16,}")
IMAGE_REF_RE = re.compile(r"!\[[^\]\n]*\](?:\[[^\]\n]*\]|\([^)\n]*\))")
IMAGE_DEF_RE = re.compile(r"(?m)^\s*\[[^\]\n]+\]:\s*<?\[image\]>?\s*$\n?")
TASK_RES = [
    re.compile(r"^\s*(?:[-*+]\s+)?\\?\[\s?\\?\]\s+(?P<t>\S.*)$"),
    re.compile(r"^\s*(?:[-*+]\s+)?[\u2610\u25a1\u25fb]\s*(?P<t>\S.*)$"),
    re.compile(
        r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?(?:TODO|To[- ]?do|Action items?|Action|Next steps?|Follow[- ]?up)(?:\*\*|__)?"
        r"\s*[:\u2013\u2014-]\s*(?:\*\*|__)?\s*(?P<t>\S.*)$",
        re.I,
    ),
]


def clean_text(text: str) -> str:
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    text = DATA_URI_RE.sub("[image]", text)  # Markdown export embeds images as base64 data URIs.
    text = IMAGE_REF_RE.sub("[image]", text)
    text = IMAGE_DEF_RE.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def find_tasks(text: str) -> list:
    tasks: "OrderedDict[str, None]" = OrderedDict()
    for line in text.split("\n"):
        for pattern in TASK_RES:
            match = pattern.match(line)
            if match:
                task = re.sub(r"\\([\[\]*_#`>-])", r"\1", match.group("t"))
                task = one_line(task.strip(" *_"), MAX_TASK_CHARS)
                if task and not re.match(r"^\[\s?[xX]\s?\]", task):
                    tasks.setdefault(task, None)
                break
        if len(tasks) >= MAX_TASKS_PER_ITEM:
            break
    return list(tasks)


def format_excerpt(text: str) -> str:
    """Keeps the text readable inside one digest section: a heading cannot start a new section."""
    lines = []
    for line in text.split("\n"):
        heading = re.match(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$", line)
        if heading:
            line = "**%s**" % heading.group(1) if heading.group(1) else ""
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --------------------------------------------------------------------------------------------
# Google access: tokens and the Drive API


class DriveError(Exception):
    """A Google access or Drive API failure, with a kind the skill branches on."""

    def __init__(self, kind: str, message: str, hint: str = "", status=None, reason: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.status = status
        self.reason = reason

    def as_dict(self) -> dict:
        data = {"kind": self.kind, "message": self.message}
        if self.status is not None:
            data["http_status"] = self.status
        if self.reason:
            data["reason"] = self.reason
        if self.hint:
            data["hint"] = self.hint
        return data


TOKEN_HELP = (
    "Read references/google-drive-access.md. The user gets a token in their own terminal and either "
    "exports %s before starting the coding agent or saves it to a file passed with --token-file. "
    "Never ask for the token in the chat." % TOKEN_ENV
)


def api_base() -> str:
    override = os.environ.get(TEST_BASE_ENV, "").strip()
    if not override:
        return DEFAULT_API_BASE
    if not TEST_BASE_RE.match(override):
        raise SystemExit(usage_error("%s is a test-only setting and must be http://127.0.0.1:<port> or http://localhost:<port>." % TEST_BASE_ENV))
    return override


def check_token_shape(token: str, source: str) -> str:
    token = token.strip()
    if len(token) < 20 or re.search(r"\s", token) or not all(32 < ord(c) < 127 for c in token):
        raise DriveError("no_token", "The access token from %s does not look like a Google access token (it is not printed)." % source, TOKEN_HELP)
    return token


def gcloud_token(redact: Redactor) -> str:
    exe = shutil.which("gcloud")
    if not exe:
        raise DriveError("no_token", "gcloud is not on PATH.", "Install the Google Cloud CLI, or use another way to get a token: " + TOKEN_HELP)
    env = dict(os.environ, CLOUDSDK_CORE_DISABLE_PROMPTS="1")
    try:
        proc = subprocess.run([exe, "auth", "print-access-token"], capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DriveError("auth", "gcloud auth print-access-token did not finish (%s)." % type(error).__name__, "Run `gcloud auth login --enable-gdrive-access` in your own terminal, then try again.")
    token = (proc.stdout or "").strip().splitlines()[-1].strip() if (proc.stdout or "").strip() else ""
    if proc.returncode != 0 or not token:
        lines = [ln.strip() for ln in (proc.stderr or "").splitlines() if ln.strip()]
        detail = redact(one_line(lines[0], 200)) if lines else "exit %s" % proc.returncode
        raise DriveError("auth", "gcloud could not print an access token: %s" % detail, "Ask the user to run `gcloud auth login --enable-gdrive-access` in their own terminal.")
    return check_token_shape(token, "gcloud")


def resolve_token(args, redact: Redactor):
    """Returns (token, source). Never prints the token."""
    mode = args.auth
    if mode in ("auto", "env"):
        value = os.environ.get(TOKEN_ENV, "")
        if value.strip():
            return check_token_shape(value, TOKEN_ENV), "env"
        if mode == "env":
            raise DriveError("no_token", "%s is not set." % TOKEN_ENV, TOKEN_HELP)
    if mode in ("auto", "file") and args.token_file:
        path = Path(args.token_file).expanduser()
        try:
            value = path.read_text(encoding="utf-8-sig")
        except OSError:
            raise DriveError("no_token", "The token file %s cannot be read." % redact(str(path)), TOKEN_HELP)
        return check_token_shape(value, "the token file"), "file"
    if mode == "file":
        raise DriveError("no_token", "--auth file needs --token-file PATH.", TOKEN_HELP)
    if mode == "gcloud" or (mode == "auto" and shutil.which("gcloud")):
        return gcloud_token(redact), "gcloud"
    raise DriveError("no_token", "No Google access token was found: %s is not set, no --token-file was given, and gcloud is not installed." % TOKEN_ENV, TOKEN_HELP)


def quota_project(args) -> str:
    value = (args.quota_project or os.environ.get(QUOTA_ENV, "")).strip()
    if value and not QUOTA_PROJECT_RE.match(value):
        raise SystemExit(usage_error("--quota-project must be a Google Cloud project id, such as my-notes-project."))
    return value


def _error_details(status: int, body: bytes):
    message, reasons = "", []
    try:
        error = json.loads(body.decode("utf-8", "replace")).get("error")
    except (ValueError, AttributeError):
        error = None
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        for entry in error.get("errors") or []:
            if isinstance(entry, dict) and entry.get("reason"):
                reasons.append(str(entry["reason"]))
        if error.get("status"):
            reasons.append(str(error["status"]))
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason"):
                reasons.append(str(detail["reason"]))
    elif isinstance(error, str):
        message = error
    return message or "HTTP %d" % status, reasons


RATE_REASONS = {"userRateLimitExceeded", "rateLimitExceeded", "RATE_LIMIT_EXCEEDED", "RESOURCE_EXHAUSTED"}


def _retryable(status: int, reasons: list) -> bool:
    if status == 429 or status in (500, 502, 503, 504):
        return True
    return status == 403 and bool(RATE_REASONS & set(reasons))


def classify(status: int, message: str, reasons: list) -> DriveError:
    text = message.lower()
    reason = reasons[0] if reasons else ""
    names = set(reasons)
    if status == 401:
        return DriveError("auth", "Google rejected the access token (HTTP 401: %s)." % message,
                          "The token is expired or invalid; Google access tokens last about one hour. Get a new one (references/google-drive-access.md).", status, reason)
    if status == 403 and (names & {"accessNotConfigured", "SERVICE_DISABLED"} or "has not been used in project" in text):
        return DriveError("api_disabled", "The Google Drive API is not enabled for the project this token is billed to (HTTP 403: %s)." % message,
                          "Pass --quota-project <project id> for a Google Cloud project where you ran `gcloud services enable drive.googleapis.com --project <project id>`, or use a token from the OAuth 2.0 Playground (references/google-drive-access.md).", status, reason)
    if status == 403 and (names & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"} or "insufficient authentication scopes" in text):
        return DriveError("scope", "The access token does not include a Google Drive scope (HTTP 403: %s)." % message,
                          "Get a token with the https://www.googleapis.com/auth/drive.readonly scope, or run `gcloud auth login --enable-gdrive-access` (references/google-drive-access.md).", status, reason)
    if status == 403 and (names & {"USER_PROJECT_DENIED"} or "permission to use project" in text):
        return DriveError("quota_project_denied", "This Google account may not use the quota project (HTTP 403: %s)." % message,
                          "Use a project you own, or ask its owner for the Service Usage Consumer role (serviceusage.services.use).", status, reason)
    if status == 403 and "domainPolicy" in names:
        return DriveError("domain_policy", "The Google Workspace administrators block this access (HTTP 403: %s)." % message,
                          "Ask the Workspace administrator, or use the host agent's own Google Drive connector if it has one.", status, reason)
    if status == 429 or (status == 403 and names & (RATE_REASONS | {"dailyLimitExceeded"})):
        return DriveError("rate_limit", "Google Drive rate-limited the requests (HTTP %d: %s), and retries did not help." % (status, message),
                          "Wait a few minutes and run gather again, or use a smaller --max-items.", status, reason)
    if status == 404:
        return DriveError("not_found", "Not found (HTTP 404: %s)." % message, "", status, reason)
    if status >= 500:
        return DriveError("server", "Google Drive failed (HTTP %d: %s), and retries did not help." % (status, message), "Try again in a few minutes.", status, reason)
    if status == 400:
        return DriveError("bad_request", "Google Drive refused the request (HTTP 400: %s)." % message, "", status, reason)
    return DriveError("forbidden", "Google Drive refused the request (HTTP %d: %s)." % (status, message), "", status, reason)


class Drive:
    """A small Drive API v3 client over urllib, with retries and exponential backoff."""

    def __init__(self, token: str, quota: str, base: str, sleep=time.sleep) -> None:
        self._token = token
        self._quota = quota
        self._base = base.rstrip("/")
        self._sleep = sleep
        self.calls: Counter = Counter()

    def _get(self, path: str, params: dict, max_bytes: int, label: str, extra_headers=None) -> bytes:
        query = urllib.parse.urlencode(params) if params else ""
        url = self._base + path + ("?" + query if query else "")
        network_failures = 0
        for attempt in range(MAX_RETRIES + 1):
            request = urllib.request.Request(url, method="GET")
            # Unredirected: the token and quota project are never forwarded to a redirect target.
            request.add_unredirected_header("Authorization", "Bearer " + self._token)
            if self._quota:
                request.add_unredirected_header("X-Goog-User-Project", self._quota)
            request.add_header("User-Agent", SLUG + "/1")
            for name, value in (extra_headers or {}).items():
                request.add_header(name, value)
            self.calls[label] += 1
            try:
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    return response.read(max_bytes)
            except urllib.error.HTTPError as error:
                body = error.read(200_000) if error.fp is not None else b""
                message, reasons = _error_details(error.code, body)
                if _retryable(error.code, reasons) and attempt < MAX_RETRIES:
                    self.calls["retries"] += 1
                    self._sleep(self._backoff(attempt, error.headers))
                    continue
                raise classify(error.code, message, reasons)
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as error:
                network_failures += 1
                if network_failures <= 2 and attempt < MAX_RETRIES:
                    self.calls["retries"] += 1
                    self._sleep(self._backoff(attempt, None))
                    continue
                reason = getattr(error, "reason", error)
                raise DriveError("network", "Could not reach Google Drive (%s)." % one_line(reason, 160), "Check the network connection and try again.")
        raise DriveError("server", "Google Drive kept failing after %d retries." % MAX_RETRIES, "Try again in a few minutes.")

    @staticmethod
    def _backoff(attempt: int, headers) -> float:
        retry_after = headers.get("Retry-After") if headers is not None else None
        if retry_after and retry_after.strip().isdigit():
            return min(float(retry_after.strip()), MAX_BACKOFF_SECONDS)
        return min(2 ** attempt + random.random(), MAX_BACKOFF_SECONDS)

    def get_json(self, path: str, params: dict, label: str) -> dict:
        raw = self._get(path, params, 20_000_000, label)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise DriveError("server", "Google Drive answered %s with something that is not JSON." % label, "Try again in a few minutes.")
        return data if isinstance(data, dict) else {}

    def get_text(self, path: str, params: dict, max_bytes: int, label: str, extra_headers=None) -> str:
        return self._get(path, params, max_bytes, label, extra_headers).decode("utf-8", "replace")


def file_path(file_id: str, suffix: str = "") -> str:
    return "/drive/v3/files/" + urllib.parse.quote(file_id, safe="") + suffix


# --------------------------------------------------------------------------------------------
# Gather from the Drive API


class Folders:
    """Resolves parent folder ids to a readable path, with a cache and a lookup budget."""

    def __init__(self, drive: Drive) -> None:
        self.drive = drive
        self.cache: dict = {}
        self.lookups = 0

    def lookup(self, folder_id: str):
        if folder_id in self.cache:
            return self.cache[folder_id]
        if self.lookups >= MAX_FOLDER_LOOKUPS:
            return None
        self.lookups += 1
        try:
            data = self.drive.get_json(file_path(folder_id), {"fields": "id,name,parents", "supportsAllDrives": "true"}, "folders.get")
        except DriveError as error:
            if error.kind in ("not_found", "forbidden", "bad_request"):
                self.cache[folder_id] = None
                return None
            raise
        node = (str(data.get("name") or "(folder)"), list(data.get("parents") or []))
        self.cache[folder_id] = node
        return node

    def path_for(self, info: dict) -> str:
        parents = info.get("parents") or []
        if not parents:
            return "My Drive" if info.get("ownedByMe") else "Shared with me"
        chain, current, seen, reached_top = [], parents[0], set(), False
        for _ in range(MAX_FOLDER_DEPTH):
            if current in seen:
                break
            seen.add(current)
            node = self.lookup(current)
            if node is None:
                break
            chain.append(node[0])
            if not node[1]:
                reached_top = True
                break
            current = node[1][0]
        if not chain:
            return "My Drive" if info.get("ownedByMe") else "Shared with me"
        path = "/".join(reversed(chain))
        if not reached_top and not info.get("ownedByMe"):
            path = "Shared with me/" + path
        return path


def selected_types(args) -> list:
    wanted = ["docs"]
    for name in args.include:
        for part in name.split(","):
            part = part.strip().lower()
            if part == "all":
                wanted += ["sheets", "slides", "text"]
            elif part:
                wanted.append(part)
    return [t for t in TYPE_MIMES if t in wanted]


def provider_label(types: list) -> str:
    names = ["Google Docs"] + [{"sheets": "Sheets", "slides": "Slides", "text": "text files in Drive"}[t] for t in types if t != "docs"]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + ", and " + names[-1]


def excluded(patterns: list, title: str, container: str) -> bool:
    if not patterns:
        return False
    parts = [title] + [p for p in container.split("/") if p]
    for pattern in patterns:
        needle = pattern.strip().lower()
        if not needle:
            continue
        for part in parts:
            if fnmatch.fnmatchcase(part.strip().lower(), needle):
                return True
    return False


def fetch_text(drive: Drive, info: dict, warnings: list, redact: Redactor):
    """Returns the document's text, or None when it was skipped (a warning says why)."""
    mime = info.get("mimeType")
    name = one_line(redact(info.get("name") or "(untitled)"), 80)
    if mime in EXPORT_MIMES:
        last = None
        for export_mime in EXPORT_MIMES[mime]:
            try:
                return drive.get_text(file_path(info["id"], "/export"), {"mimeType": export_mime}, MAX_EXPORT_BYTES, "files.export")
            except DriveError as error:
                last = error
                if error.kind not in ("bad_request", "forbidden", "not_found"):
                    raise
                if error.reason in ("exportSizeLimitExceeded",) or error.kind == "not_found":
                    break
        if last is not None and last.reason == "exportSizeLimitExceeded":
            warnings.append('Skipped "%s": too large to export (Google exports at most 10 MB).' % name)
        else:
            warnings.append('Skipped "%s": it could not be exported (%s).' % (name, last.message if last else "unknown error"))
        return None
    size = int(info.get("size") or 0)
    try:
        text = drive.get_text(file_path(info["id"]), {"alt": "media", "supportsAllDrives": "true"}, MAX_TEXT_FILE_BYTES, "files.get(media)", {"Range": "bytes=0-%d" % (MAX_TEXT_FILE_BYTES - 1)})
    except DriveError as error:
        if error.kind not in ("bad_request", "forbidden", "not_found"):
            raise
        warnings.append('Skipped "%s": it could not be downloaded (%s).' % (name, error.message))
        return None
    if size > MAX_TEXT_FILE_BYTES:
        warnings.append('Read only the first 2 MB of "%s".' % name)
    return text


def open_comments(drive: Drive, info: dict, redact: Redactor) -> list:
    try:
        data = drive.get_json(file_path(info["id"], "/comments"), {"fields": "comments(content,resolved,deleted)", "pageSize": 100, "includeDeleted": "false"}, "comments.list")
    except DriveError as error:
        if error.kind in ("bad_request", "forbidden", "not_found"):
            return []
        raise
    found = []
    for comment in data.get("comments") or []:
        if isinstance(comment, dict) and not comment.get("resolved") and not comment.get("deleted") and str(comment.get("content") or "").strip():
            found.append("Open comment: " + one_line(redact(comment["content"]), MAX_TASK_CHARS - 14))
        if len(found) >= MAX_COMMENTS_PER_ITEM:
            break
    return found


def drive_gather(drive: Drive, since, args, redact: Redactor, warnings: list, stats: Counter) -> list:
    types = selected_types(args)
    mimes = [m for t in types for m in TYPE_MIMES[t]]
    mime_terms = " or ".join("mimeType = '%s'" % m for m in mimes)
    params = OrderedDict(
        [
            ("q", "modifiedTime > '%s' and trashed = false and (%s)" % (iso(since), mime_terms)),
            ("orderBy", "modifiedTime desc"),
            ("pageSize", PAGE_SIZE),
            ("fields", LIST_FIELDS),
            ("corpora", "allDrives" if args.shared_drives else "user"),
            ("includeItemsFromAllDrives", "true"),
            ("supportsAllDrives", "true"),
        ]
    )
    listed, page_token = [], None
    for _ in range(MAX_LIST_PAGES):
        if page_token:
            params["pageToken"] = page_token
        data = drive.get_json("/drive/v3/files", params, "files.list")
        stats["pages"] += 1
        listed.extend(f for f in data.get("files") or [] if isinstance(f, dict) and f.get("id"))
        if data.get("incompleteSearch") and "incomplete_search" not in stats:
            stats["incomplete_search"] = 1
            warnings.append("Google Drive said the search was incomplete, so some documents may be missing.")
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    if page_token:
        warnings.append("Stopped listing after %d pages (%d files); use a shorter window to see the rest." % (MAX_LIST_PAGES, len(listed)))
    stats["files_listed"] = len(listed)

    candidates, seen = [], set()
    for info in listed:
        if info["id"] in seen:
            continue
        seen.add(info["id"])
        edited = parse_ts(info.get("modifiedByMeTime"))
        created = parse_ts(info.get("createdTime"))
        mine = (edited is not None and edited >= since) or (bool(info.get("ownedByMe")) and created is not None and created >= since)
        if not mine and not args.include_others:
            stats["skipped_not_edited_by_you"] += 1
            continue
        if parse_ts(info.get("modifiedTime")) is None:
            stats["skipped_no_modified_time"] += 1
            continue
        candidates.append(info)

    folders = Folders(drive)
    kept = []
    for info in candidates:
        container = folders.path_for(info)
        if excluded(args.exclude, str(info.get("name") or ""), container):
            stats["skipped_excluded"] += 1
            continue
        info["_container"] = container
        kept.append(info)
    if folders.lookups >= MAX_FOLDER_LOOKUPS:
        warnings.append("Folder names were resolved for the first %d folders only." % MAX_FOLDER_LOOKUPS)
    kept.sort(key=lambda f: parse_ts(f.get("modifiedTime")), reverse=True)
    if len(kept) > args.max_items:
        stats["skipped_over_max_items"] += len(kept) - args.max_items
        kept = kept[: args.max_items]

    items = []
    for info in kept:
        text = fetch_text(drive, info, warnings, redact)
        if text is None:
            stats["skipped_unreadable"] += 1
            continue
        body = redact(clean_text(text[:MAX_RAW_TEXT_CHARS]))
        tasks = find_tasks(body)
        if args.comments:
            for comment in open_comments(drive, info, redact):
                if comment not in tasks and len(tasks) < MAX_TASKS_PER_ITEM + MAX_COMMENTS_PER_ITEM:
                    tasks.append(comment)
        last_user = info.get("lastModifyingUser") if isinstance(info.get("lastModifyingUser"), dict) else None
        author = None
        if last_user is not None and isinstance(last_user.get("me"), bool):
            author = "me" if last_user["me"] else "other"
        created = parse_ts(info.get("createdTime"))
        item = OrderedDict(
            [
                ("provider", PROVIDER),
                ("id", str(info["id"])),
                ("title", one_line(redact(info.get("name") or ""), 200) or "(untitled)"),
                ("link", str(info.get("webViewLink") or default_link(info))),
                ("kind", "file" if info.get("mimeType") in TEXT_MIMES else "doc"),
                ("container", one_line(redact(info["_container"]), 200)),
                ("created", iso(created) if created else None),
                ("last_edited", iso(parse_ts(info.get("modifiedTime")))),
                ("author", author),
                ("open_tasks", tasks),
                ("excerpt", format_excerpt(body)),
            ]
        )
        items.append(item)
    return items


def default_link(info: dict) -> str:
    file_id = urllib.parse.quote(str(info.get("id")), safe="")
    mime = info.get("mimeType")
    if mime == DOC_MIME:
        return "https://docs.google.com/document/d/%s/edit" % file_id
    if mime == SHEET_MIME:
        return "https://docs.google.com/spreadsheets/d/%s/edit" % file_id
    if mime == SLIDES_MIME:
        return "https://docs.google.com/presentation/d/%s/edit" % file_id
    return "https://drive.google.com/file/d/%s/view" % file_id


# --------------------------------------------------------------------------------------------
# Gather from an items.json the host agent wrote with its own Drive connector


def validate_items_file(data) -> list:
    problems = []
    if not isinstance(data, dict):
        return ["The file must hold one JSON object."]
    keys = list(data)
    if set(keys) != set(TOP_FIELDS):
        problems.append("Top-level keys must be exactly %s; found %s." % (", ".join(TOP_FIELDS), ", ".join(keys)))
    if data.get("schema") != SCHEMA:
        problems.append('schema must be "%s".' % SCHEMA)
    if data.get("provider") != PROVIDER:
        problems.append('provider must be "%s".' % PROVIDER)
    for key in ("generated_at", "since"):
        if parse_ts(data.get(key)) is None:
            problems.append("%s must be an ISO 8601 time, such as 2026-10-04T00:00:00Z." % key)
    items = data.get("items")
    if not isinstance(items, list):
        problems.append("items must be a list.")
        return problems
    for index, item in enumerate(items):
        where = "items[%d]" % index
        if not isinstance(item, dict):
            problems.append("%s must be an object." % where)
            continue
        if set(item) != set(ITEM_FIELDS):
            missing = [k for k in ITEM_FIELDS if k not in item]
            extra = [k for k in item if k not in ITEM_FIELDS]
            problems.append("%s must have exactly the shared fields (missing: %s; extra: %s)." % (where, ", ".join(missing) or "none", ", ".join(extra) or "none"))
            continue
        if item["provider"] != PROVIDER:
            problems.append('%s.provider must be "%s".' % (where, PROVIDER))
        for key in ("id", "title", "link", "container", "excerpt"):
            if not isinstance(item[key], str):
                problems.append("%s.%s must be a string." % (where, key))
        if isinstance(item["id"], str) and not item["id"].strip():
            problems.append("%s.id must not be empty." % where)
        if isinstance(item["link"], str) and not item["link"].startswith("https://"):
            problems.append("%s.link must be an https:// link to the document." % where)
        if item["kind"] not in KINDS:
            problems.append("%s.kind must be one of %s." % (where, ", ".join(KINDS)))
        if item["created"] is not None and parse_ts(item["created"]) is None:
            problems.append("%s.created must be an ISO 8601 time or null." % where)
        if parse_ts(item["last_edited"]) is None:
            problems.append("%s.last_edited must be an ISO 8601 time." % where)
        if item["author"] not in ("me", "other", None):
            problems.append('%s.author must be "me", "other", or null.' % where)
        if not isinstance(item["open_tasks"], list) or any(not isinstance(t, str) for t in item["open_tasks"]):
            problems.append("%s.open_tasks must be a list of strings." % where)
        if len(problems) >= 20:
            problems.append("(stopped after 20 problems)")
            break
    return problems


def items_from_file(path: Path, since, args, redact: Redactor, stats: Counter) -> list:
    data = load_json(path, "--from-items file")
    problems = validate_items_file(data)
    if problems:
        emit({"error": "The --from-items file does not follow the shared notes-digest/1 format.", "problems": problems})
        for problem in problems:
            print("items.json: " + problem, file=sys.stderr)
        raise SystemExit(2)
    items, seen = [], set()
    for raw in data["items"]:
        if raw["id"] in seen:
            stats["skipped_duplicate"] += 1
            continue
        seen.add(raw["id"])
        edited = parse_ts(raw["last_edited"])
        if edited < since:
            stats["skipped_before_window"] += 1
            continue
        title = one_line(redact(raw["title"]), 200) or "(untitled)"
        container = one_line(redact(raw["container"]), 200) or "Google Drive"
        if excluded(args.exclude, raw["title"], raw["container"]):
            stats["skipped_excluded"] += 1
            continue
        body = redact(clean_text(raw["excerpt"][:MAX_RAW_TEXT_CHARS]))
        tasks: "OrderedDict[str, None]" = OrderedDict()
        for task in list(raw["open_tasks"]) + find_tasks(body):
            task = one_line(redact(task), MAX_TASK_CHARS)
            if task:
                tasks.setdefault(task, None)
        created = parse_ts(raw["created"])
        items.append(
            OrderedDict(
                [
                    ("provider", PROVIDER),
                    ("id", raw["id"].strip()),
                    ("title", title),
                    ("link", raw["link"].strip()),
                    ("kind", raw["kind"]),
                    ("container", container),
                    ("created", iso(created) if created else None),
                    ("last_edited", iso(edited)),
                    ("author", raw["author"]),
                    ("open_tasks", list(tasks)[: MAX_TASKS_PER_ITEM + MAX_COMMENTS_PER_ITEM]),
                    ("excerpt", format_excerpt(body)),
                ]
            )
        )
    items.sort(key=lambda it: it["last_edited"], reverse=True)
    if len(items) > args.max_items:
        stats["skipped_over_max_items"] += len(items) - args.max_items
        items = items[: args.max_items]
    stats["files_listed"] = len(data["items"])
    return items


# --------------------------------------------------------------------------------------------
# Digest


def section_head(item: dict) -> str:
    edited = parse_ts(item["last_edited"])
    lines = [
        "## " + one_line(item["title"].lstrip("#").strip(), 200),
        "%s · %s · last edited %s · %s" % (item["kind"], item["container"], local_date(edited), item["link"]),
        "",
    ]
    for task in item["open_tasks"]:
        lines.append("- [ ] " + task)
    if item["open_tasks"]:
        lines.append("")
    return "\n".join(lines) + "\n"


def allocate(lengths: list, budget: int, cap: int) -> list:
    """Shares a character budget between excerpts: short ones keep everything, long ones split the rest."""
    alloc = [0] * len(lengths)
    remaining, left = max(budget, 0), len(lengths)
    for index in sorted(range(len(lengths)), key=lambda i: lengths[i]):
        share = remaining // left if left else 0
        give = min(lengths[index], cap, share)
        alloc[index] = give
        remaining -= give
        left -= 1
    return alloc


def render_digest(items: list, header: str, max_chars: int, max_item_chars: int, warnings: list):
    """Returns (digest text, items with their final excerpts). Drops the oldest items last."""
    items = list(items)
    dropped = 0
    while items:
        fixed = len(header) + 2 + sum(len(section_head(it)) + 2 for it in items)
        if max_chars - fixed >= MIN_EXCERPT_CHARS * len(items) or len(items) == 1:
            break
        items.pop()  # Items are newest first, so the oldest goes first.
        dropped += 1
    if dropped:
        warnings.append("Left out the %d oldest document(s) to keep digest.md under --max-chars (%d)." % (dropped, max_chars))
    if not items:
        return header + "\n", []
    fixed = len(header) + 2 + sum(len(section_head(it)) + 2 for it in items)
    budgets = allocate([len(it["excerpt"]) for it in items], max_chars - fixed, max_item_chars)
    final, sections = [], []
    for item, budget in zip(items, budgets):
        excerpt = cut_text(item["excerpt"], max(budget, 40)) if item["excerpt"] else ""
        if item["excerpt"] and len(excerpt) < len(item["excerpt"]):
            item = OrderedDict(item)
        item["excerpt"] = excerpt
        final.append(item)
        sections.append(section_head(item) + (excerpt or "(no text)") + "\n")
    return header + "\n\n" + "\n".join(sections), final


def window_header(label: str, since, now, count: int, from_days) -> str:
    span = "%s to %s" % (local_date(since), local_date(now))
    when = ("last %d day%s (%s)" % (from_days, "" if from_days == 1 else "s", span)) if from_days else span
    return "# %s · %s · %d item%s" % (label, when, count, "" if count == 1 else "s")


def window_phrase(since, from_days) -> str:
    if from_days:
        return "over the last day" if from_days == 1 else "over the last %d days" % from_days
    return "since %s" % since.astimezone().strftime("%Y-%m-%d")


def build_goal(label: str, since, from_days) -> str:
    return (
        "What are the most important next actions given what I've been writing and discussing in %s %s? "
        "Tie each action to the documents it comes from by their exact titles." % (label, window_phrase(since, from_days))
    )


# --------------------------------------------------------------------------------------------
# check and gather commands


def window_start(args, now):
    if args.since and args.days is not None:
        raise SystemExit(usage_error("Pass --since or --days, not both."))
    if args.since:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
            try:
                stamp = dt.datetime.strptime(args.since, fmt)
                break
            except ValueError:
                continue
        else:
            raise SystemExit(usage_error("--since must look like 2026-10-04 or 2026-10-04T09:00 (local time)."))
        start = stamp.astimezone(UTC)  # A naive value is read as local time.
        if start >= now:
            raise SystemExit(usage_error("--since must be in the past."))
        return start, None
    days = DEFAULT_DAYS if args.days is None else args.days
    if not 1 <= days <= 365:
        raise SystemExit(usage_error("--days must be between 1 and 365."))
    return now - dt.timedelta(days=days), days


def cmd_check(args) -> int:
    redact = Redactor(Path.home())
    report = {
        "env_token_set": bool(os.environ.get(TOKEN_ENV, "").strip()),
        "token_file_given": bool(args.token_file),
        "gcloud_on_path": bool(shutil.which("gcloud")),
    }
    try:
        token, source = resolve_token(args, redact)
        report["auth_source"] = source
        quota = quota_project(args)
        report["quota_project_set"] = bool(quota)
        drive = Drive(token, quota, api_base())
        drive.get_json("/drive/v3/files", {"pageSize": 1, "fields": "files(id)", "q": "trashed = false"}, "files.list")
    except DriveError as error:
        report["ok"] = False
        report["error"] = error.as_dict()
        emit(report)
        return 1
    report["ok"] = True
    report["drive"] = "Google Drive answered a files.list call with this token."
    emit(report)
    return 0


def cmd_gather(args) -> int:
    now = dt.datetime.now(UTC).replace(microsecond=0)
    redact = Redactor(Path.home())
    if not 1 <= args.max_items <= 200:
        return usage_error("--max-items must be between 1 and 200.")
    if not 5_000 <= args.max_chars <= HARD_MAX_CHARS:
        return usage_error("--max-chars must be between 5,000 and %d." % HARD_MAX_CHARS)
    if not 300 <= args.max_item_chars <= 50_000:
        return usage_error("--max-item-chars must be between 300 and 50,000.")
    for value in args.include:
        for part in value.split(","):
            if part.strip().lower() not in ("sheets", "slides", "text", "all", "docs", ""):
                return usage_error("--include takes sheets, slides, text, or all.")
    warnings: list = []
    stats: Counter = Counter()
    auth_source = None
    if args.from_items:
        explicit = args.since or args.days is not None
        source_path = Path(args.from_items).expanduser()
        preview = load_json(source_path, "--from-items file")
        file_since = parse_ts(preview.get("since")) if isinstance(preview, dict) else None
        if explicit or file_since is None:
            since, from_days = window_start(args, now)
        else:
            since, from_days = file_since, None
        items = items_from_file(source_path, since, args, redact, stats)
        label = "Google Docs"
        source = "from-items"
    else:
        since, from_days = window_start(args, now)
        try:
            token, auth_source = resolve_token(args, redact)
            drive = Drive(token, quota_project(args), api_base())
            items = drive_gather(drive, since, args, redact, warnings, stats)
        except DriveError as error:
            emit({"error": error.as_dict(), "auth_source": auth_source, "window": {"since": iso(since), "until": iso(now)}})
            return 1
        label = provider_label(selected_types(args))
        source = "drive-api"
        stats.update({"drive_" + k: v for k, v in drive.calls.items()})

    summary = OrderedDict(
        [
            ("window", {"since": iso(since), "until": iso(now), "label": window_phrase(since, from_days)}),
            ("source", source),
            ("auth_source", auth_source),
            ("stats", dict(stats)),
        ]
    )
    if not items:
        summary["error"] = "no documents found"
        summary["hint"] = "No document you created or edited matched this window. Offer a wider window (--days 14 or --since), --include sheets,slides,text, --include-others, or --shared-drives."
        summary["warnings"] = warnings
        emit(summary)
        return 3

    out_dir = Path(args.out).expanduser() if args.out else Path(tempfile.gettempdir()) / SLUG / now.astimezone().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    header = window_header(label, since, now, len(items), from_days)
    digest, final = render_digest(items, header, args.max_chars, args.max_item_chars, warnings)
    if len(final) != len(items):  # The header counts what is really in the digest.
        header = window_header(label, since, now, len(final), from_days)
        digest, final = render_digest(final, header, args.max_chars, args.max_item_chars, warnings[:])
    document = OrderedDict([("schema", SCHEMA), ("provider", PROVIDER), ("generated_at", iso(now)), ("since", iso(since)), ("items", final)])
    write_json(out_dir / "items.json", document)
    write_text(out_dir / "digest.md", digest)
    inputs_path = out_dir / "inputs.json"
    if inputs_path.exists():
        warnings.append("inputs.json already existed and was left as it is.")
    else:
        focus = [one_line(redact(f), MAX_VALUE_CHARS) for f in args.focus if f.strip()]
        time_left = [one_line(redact(t), MAX_VALUE_CHARS) for t in args.time if t.strip()]
        write_json(inputs_path, OrderedDict([("goal", build_goal(label, since, from_days)), ("project", PROJECT_KEY), ("trajectory", focus), ("constraint", time_left), ("avoid", [])]))
    summary.update(
        [
            ("out_dir", str(out_dir)),
            ("digest", str(out_dir / "digest.md")),
            ("items_file", str(out_dir / "items.json")),
            ("inputs", str(inputs_path)),
            ("digest_chars", len(digest)),
            ("documents", [OrderedDict([("title", it["title"]), ("kind", it["kind"]), ("container", it["container"]), ("last_edited", it["last_edited"]), ("author", it["author"]), ("open_tasks", len(it["open_tasks"])), ("excerpt_chars", len(it["excerpt"]))]) for it in final]),
            ("redactions", dict(redact.counts)),
            ("warnings", warnings),
        ]
    )
    emit(summary)
    return 0


# --------------------------------------------------------------------------------------------
# run


def validate_inputs(data, redact: Redactor):
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
    for kind, (_flag, most) in FLAG_KINDS.items():
        items = data.get(kind, []) or []
        if not isinstance(items, list) or any(not isinstance(i, str) for i in items):
            problems.append("%s must be a list of strings." % kind)
            continue
        cleaned = [redact(" ".join(i.split())) for i in items if i.strip()]
        if len(cleaned) > most:
            problems.append("%s has %d values; this skill passes at most %d." % (kind, len(cleaned), most))
        for value in cleaned:
            if len(value) > MAX_VALUE_CHARS:
                problems.append("A %s value has %d characters; keep each under %d: %s" % (kind, len(value), MAX_VALUE_CHARS, one_line(value, 80)))
        values[kind] = cleaned
    return {"goal": goal, "project": project, "values": values}, problems


def build_argv(exe: str, inputs: dict) -> list:
    """The paid command; run adds --dry-run to it for the free check of the same request."""
    argv = [exe, "--json", "--no-input", "agents", "suggest", "run", "--goal", inputs["goal"]]
    for category in CATEGORIES:
        argv += ["--category", category]
    argv += ["--horizon", HORIZON, "--count", str(COUNT)]
    for kind, (flag, _most) in FLAG_KINDS.items():
        for value in inputs["values"].get(kind, []):
            argv += [flag, value]
    argv += ["--files", "digest.md"]  # Relative: the run starts inside the output folder.
    if inputs["project"]:
        argv += ["--project", inputs["project"]]
    argv += ["--max-total-tokens", str(MAX_TOTAL_TOKENS)]
    return argv


def run_models(data: dict) -> list:
    """The models a run used for its generator and critic, from the result's settings."""
    settings = data.get("settings") or {}
    return list(OrderedDict.fromkeys(m for m in (settings.get("model"), settings.get("critic_model")) if m))


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


def fingerprint(argv: list, out_dir: Path) -> str:
    digest = hashlib.sha256()
    digest.update(json.dumps(argv[1:], ensure_ascii=False).encode("utf-8"))
    digest.update(b"\0")
    digest.update((out_dir / "digest.md").read_bytes())
    return digest.hexdigest()


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


def resolve_cli(value: str):
    """The vidbyte-cli executable to run: the --cli path or name, found the way a shell finds it."""
    exe = shutil.which(value or "vidbyte-cli")
    if not exe:
        emit({"error": "vidbyte-cli was not found (%s). Follow references/setup.md step 1, or pass --cli with the path to vidbyte-cli." % (value or "vidbyte-cli")})
    return exe


def display_command(argv: list) -> str:
    shown = ["vidbyte-cli"] + argv[1:]
    if os.name == "nt":
        return subprocess.list2cmdline(shown)
    return " ".join(shlex.quote(a) for a in shown)


def cmd_run(args) -> int:
    out_dir = Path(args.out).expanduser()
    if args.dry_run == args.paid:
        return usage_error("Pass exactly one of --dry-run (free check) or --paid (the usage-billed run, only after the user said yes).")
    if not (out_dir / "digest.md").is_file():
        return usage_error("digest.md not found in %s. Run `gather` first and pass the out_dir it printed." % out_dir)
    redact = Redactor(Path.home())
    inputs, problems = validate_inputs(load_json(out_dir / "inputs.json", "inputs.json"), redact)
    if problems:
        emit({"error": "inputs.json needs fixing", "problems": problems})
        for problem in problems:
            print("inputs.json: " + problem, file=sys.stderr)
        return 2
    exe = resolve_cli(args.cli)
    if not exe:
        return 127
    argv = build_argv(exe, inputs)
    length = sum(len(a) + 3 for a in argv)
    if length > MAX_ARGV_CHARS:
        return usage_error("The command would be %d characters; shorten the inputs.json values to stay under %d." % (length, MAX_ARGV_CHARS))
    print_ = fingerprint(argv, out_dir)
    marker = out_dir / "dry-run.ok"
    paid_marker = out_dir / "paid-run.txt"
    if args.paid:
        saved = load_json(marker, "dry-run.ok") if marker.is_file() else {}
        if not isinstance(saved, dict) or saved.get("fingerprint") != print_:
            return usage_error("No passing dry run matches this exact request (digest.md and inputs.json). Run `run --dry-run` first, show the user the result, and ask again.")
        if paid_marker.exists() or (out_dir / "result.json").exists():
            # The model calls an earlier paid run here finished were billed, and nothing recovers
            # them. Another run is a new charge, so it needs the user's yes and a new folder.
            return usage_error(
                "A paid run was already started in this folder (see paid-run.txt and result.json), and the model calls it finished were billed. "
                "Read result.json if it is there. Any other run is a new usage-billed run: ask the user first, then gather into a new folder."
            )
        write_text(paid_marker, "Paid run started %s with --max-total-tokens %d.\n" % (dt.datetime.now().isoformat(timespec="seconds"), MAX_TOTAL_TOKENS))
    else:
        argv = argv + ["--dry-run"]
    print("Running %s in %s:\n%s" % (exe, out_dir, display_command(argv)), file=sys.stderr)
    code, stdout_path, stderr_path, error = run_cli(argv, out_dir, "dry-run.json" if args.dry_run else "result.json")
    report: dict = {"exit_code": code, "cli": exe, "stderr_file": str(stderr_path)}
    if code == 0:
        try:
            document = json.loads(stdout_path.read_text(encoding="utf-8"))
            data = document.get("data", {}) if isinstance(document, dict) else {}
        except ValueError:
            document, data = {}, {}
        models = run_models(data)
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
                "context_manifest": [{"ref": m.get("ref"), "kind": m.get("kind"), "source": m.get("source"), "status": m.get("status")} for m in data.get("context_manifest") or []],
                "warnings": data.get("warnings") or [],
            }
        )
        if args.dry_run and data.get("stop_reason") == "dry_run":
            write_json(marker, {"fingerprint": print_, "at": iso(dt.datetime.now(UTC))})
    elif error:
        report["error"] = {k: error.get(k) for k in ("code", "message", "hint", "retryable") if k in error}
    emit(report)
    return code


# --------------------------------------------------------------------------------------------
# report and feedback


def idea_text(idea: dict) -> str:
    parts = [idea.get(k) or "" for k in ("title", "summary", "first_action", "why_now", "completion_criteria", "expected_benefit")]
    parts += [str(a) for a in idea.get("suggested_actions") or []]
    parts += [str(a) for a in idea.get("decision_points") or []]
    for evidence in (idea.get("handoff") or {}).get("evidence") or []:
        # A file's evidence is only the first 4,096 characters of the digest, so it names the
        # first documents whatever the idea is about; typed values are specific.
        if isinstance(evidence, dict) and str(evidence.get("source", "")).startswith("flag:"):
            parts.append(str(evidence.get("content") or ""))
    return "\n".join(parts)


def sources_for(idea: dict, items: list) -> list:
    text = idea_text(idea)
    low = text.lower()
    found = []
    for item in items:
        title = item.get("title") or ""
        link = item.get("link") or ""
        if link and link in text:
            found.append(item)
            continue
        if len(title) >= 4 and title != "(untitled)":
            pattern = r"(?<![A-Za-z0-9])" + re.escape(title.lower()) + r"(?![A-Za-z0-9])"
            if re.search(pattern, low):
                found.append(item)
    return found


def cmd_report(args) -> int:
    out_dir = Path(args.out).expanduser()
    result = load_json(out_dir / "result.json", "result.json")
    if not isinstance(result, dict) or result.get("kind") != "suggestions.result":
        return usage_error("result.json is not a suggestions.result document.")
    record = load_json(out_dir / "items.json", "items.json")
    items = record.get("items") or [] if isinstance(record, dict) else []
    inputs = load_json(out_dir / "inputs.json", "inputs.json") if (out_dir / "inputs.json").is_file() else {}
    data = result.get("data") or {}
    ideas = sorted([i for i in data.get("ideas") or [] if isinstance(i, dict)], key=lambda i: i.get("rank") or 0)
    goal = str((inputs or {}).get("goal") or data.get("goal") or "")
    window = re.search(r"(over the last [^?]+|since [0-9-]+)\?", goal)
    lines = ["# Next actions from your Google Docs" + (" (%s)" % window.group(1) if window else ""), ""]
    if data.get("status") not in ("complete", "partial"):
        lines.append("Run status: %s (stop reason: %s)." % (data.get("status"), data.get("stop_reason")))
        if data.get("parent_message"):
            lines.append("The agent asks: " + str(data["parent_message"]))
        lines.append("")
    if not ideas:
        lines.append("No next actions survived review. Stop reason: %s." % data.get("stop_reason"))
        lines.append("")
    cited = set()
    for horizon in ("now", "next", "later"):
        group = [i for i in ideas if (i.get("horizon") or "next") == horizon]
        if not group:
            continue
        lines += ["## " + horizon.capitalize(), ""]
        for idea in group:
            meta = [str(idea.get("id")), str(idea.get("primary_category")), "effort: %s" % idea.get("effort_estimate")]
            if idea.get("readiness") and idea.get("readiness") != "ready":
                meta.append("readiness: %s" % idea.get("readiness"))
            lines.append("%s. **%s** (%s)" % (idea.get("rank"), idea.get("title"), ", ".join(meta)))
            lines.append("   %s" % one_line(idea.get("summary"), 600))
            lines.append("   First action: %s" % one_line(idea.get("first_action"), 400))
            sources = sources_for(idea, items)
            cited.update(s.get("id") for s in sources)
            if sources:
                lines.append("   From: " + "; ".join("[%s](%s)" % (s.get("title"), s.get("link")) for s in sources))
            else:
                lines.append("   From: the digest as a whole (no single document named)")
            lines.append("")
    open_left = [it for it in items if it.get("open_tasks") and it.get("id") not in cited]
    if open_left:
        lines.append("Documents with open tasks that no action names: " + "; ".join("[%s](%s) (%d)" % (it["title"], it["link"], len(it["open_tasks"])) for it in open_left[:10]))
        lines.append("")
    usage = data.get("usage") or {}
    if usage.get("model_calls"):
        cost = estimate_usd(usage, run_models(data))
        lines.append(
            "This run made %d model calls with %d input and %d output tokens, %s from your Vidbyte balance; the exact amount is on vidbyte.pro/usage."
            % (int(usage["model_calls"]), int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), "billed by usage" if cost is None else "about $%.2f" % cost)
        )
    for warning in data.get("warnings") or []:
        lines.append("Warning: " + str(warning))
    text = "\n".join(lines).rstrip() + "\n"
    write_text(out_dir / "report.md", text)
    print(text, end="")
    return 0


def cmd_feedback(args) -> int:
    out_dir = Path(args.out).expanduser()
    result = load_json(out_dir / "result.json", "result.json")
    ideas = (result.get("data") or {}).get("ideas") or [] if isinstance(result, dict) else []
    match = [i for i in ideas if isinstance(i, dict) and i.get("id") == args.idea]
    if not match:
        return usage_error("No idea with id %s in result.json. Ids look like idea-001." % args.idea)
    project = args.project
    if not project:
        inputs = load_json(out_dir / "inputs.json", "inputs.json") if (out_dir / "inputs.json").is_file() else {}
        project = (inputs.get("project") if isinstance(inputs, dict) else None) or PROJECT_KEY
    if not PROJECT_RE.match(project):
        return usage_error("--project must be a key of 1-64 lowercase letters, digits, hyphens, or underscores.")
    exe = resolve_cli(args.cli)
    if not exe:
        return 127
    verb = "accept" if args.accept else "reject"
    argv = [exe, "--json", "agents", "suggest", "feedback", verb, "--project", project, "--suggestion", match[0]["title"]]
    if args.reason and args.reason.strip():
        argv += ["--reason", Redactor(Path.home())(" ".join(args.reason.split()))]
    print("Running %s:\n%s" % (exe, display_command(argv)), file=sys.stderr)
    return subprocess.call(argv)


# --------------------------------------------------------------------------------------------
# Command line


def add_auth_options(parser) -> None:
    parser.add_argument("--auth", choices=("auto", "env", "file", "gcloud"), default="auto", help="Where the Google access token comes from (default: auto = %s, then --token-file, then gcloud)." % TOKEN_ENV)
    parser.add_argument("--token-file", help="A file that holds only a Google OAuth access token, written by the user. Never printed.")
    parser.add_argument("--quota-project", help="Google Cloud project id sent as X-Goog-User-Project; needed with gcloud tokens (default: $%s)." % QUOTA_ENV)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="google_docs.py",
        description="Turn the Google Docs you edited recently into ranked next actions with the Vidbyte Suggestion Agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 google_docs.py check\n"
            "  python3 google_docs.py gather                          # Docs you edited in the last 7 days\n"
            "  python3 google_docs.py gather --days 14 --include sheets --exclude Journal\n"
            "  python3 google_docs.py gather --from-items items.json  # written by the host's Drive connector\n"
            "  python3 google_docs.py run --out <out_dir> --dry-run   # free\n"
            "  python3 google_docs.py run --out <out_dir> --paid      # billed by usage; ask the user first\n"
            "  python3 google_docs.py report --out <out_dir>\n"
            "  python3 google_docs.py feedback --out <out_dir> --idea idea-002 --reject --reason \"already done\"\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="Find a Google access token and prove Drive answers (free).", description="Find a Google access token and make one files.list call with it. Prints JSON; exit 0 means Drive answered, exit 1 means no working access (error.kind says why).")
    add_auth_options(check)

    gather = sub.add_parser("gather", help="Read, extract, and redact recent Google Docs into items.json and digest.md (free).", description="List the documents edited in the window, export their text, find open tasks, redact, and write items.json, digest.md, and inputs.json into a new folder. Prints a JSON summary. Exit 1: Google access failed; exit 3: no documents matched.")
    gather.add_argument("--days", type=int, default=None, help="Window length in days, ending now (default: %d)." % DEFAULT_DAYS)
    gather.add_argument("--since", help="Window start in local time, such as 2026-10-04 or 2026-10-04T09:00.")
    gather.add_argument("--include", action="append", default=[], help="Also read sheets, slides, or text (Markdown and text files in Drive); comma-separated or repeated, or all.")
    gather.add_argument("--include-others", action="store_true", help="Also keep documents that only other people edited in the window.")
    gather.add_argument("--shared-drives", action="store_true", help="Also search shared drives (corpora=allDrives).")
    gather.add_argument("--comments", action="store_true", help="Add up to %d unresolved comments per document as open tasks (one extra call per document)." % MAX_COMMENTS_PER_ITEM)
    gather.add_argument("--exclude", action="append", default=[], help="A folder name or document title never to read or send; * and ? wildcards; case-insensitive. Repeatable.")
    gather.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS, help="Keep at most this many of the newest documents (default: %d)." % DEFAULT_MAX_ITEMS)
    gather.add_argument("--max-item-chars", type=int, default=DEFAULT_MAX_ITEM_CHARS, help="Longest excerpt per document (default: %d)." % DEFAULT_MAX_ITEM_CHARS)
    gather.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Size limit for digest.md in characters (default: %d)." % DEFAULT_MAX_CHARS)
    gather.add_argument("--focus", action="append", default=[], help="The user's current focus, in their words; becomes --trajectory. At most 2.")
    gather.add_argument("--time", action="append", default=[], help="The time the user has, in their words; becomes --constraint. At most 3.")
    gather.add_argument("--from-items", help="Read an items.json the host agent wrote with its own Google Drive connector, instead of calling Google.")
    gather.add_argument("--out", help="Output folder (default: a new folder under the system temp folder).")
    add_auth_options(gather)

    run = sub.add_parser("run", help="Check inputs.json and run vidbyte-cli agents suggest run (--dry-run is free; --paid is billed by usage).", description="Validate <out>/inputs.json, then run `vidbyte-cli --json --no-input agents suggest run` from inside <out>. Saves stdout to dry-run.json or result.json and stderr to a .stderr.txt file, prints a JSON summary, and exits with vidbyte-cli's exit code. --paid needs a passing dry run of the same request.")
    run.add_argument("--out", required=True, help="The out_dir that gather printed.")
    run.add_argument("--dry-run", action="store_true", help="Validate the full request for free: no model call, no charge.")
    run.add_argument("--paid", action="store_true", help="The usage-billed run. Use only after the user agreed to the cost.")
    run.add_argument("--cli", default="vidbyte-cli", help="The vidbyte-cli executable to run (default: vidbyte-cli on PATH).")

    report = sub.add_parser("report", help="Print the next actions with their source documents (free).", description="Read <out>/result.json and items.json, map each idea to the documents it names, group by horizon, print the report, and save it as report.md.")
    report.add_argument("--out", required=True, help="The out_dir that holds result.json.")

    feedback = sub.add_parser("feedback", help="Record the user's explicit accept or reject of one idea (free, local).", description="Copy the exact title of one idea from <out>/result.json into `vidbyte-cli --json agents suggest feedback accept|reject`.")
    feedback.add_argument("--out", required=True, help="The out_dir that holds result.json.")
    feedback.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
    verdict = feedback.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accept", action="store_true", help="The user said they want this idea.")
    verdict.add_argument("--reject", action="store_true", help="The user said no to this idea.")
    feedback.add_argument("--reason", help="The user's own reason, if they gave one.")
    feedback.add_argument("--project", help="The suggestion project key (default: project in inputs.json, else %s)." % PROJECT_KEY)
    feedback.add_argument("--cli", default="vidbyte-cli", help="The vidbyte-cli executable to run (default: vidbyte-cli on PATH).")

    args = parser.parse_args(argv)
    if args.command == "gather" and (len(args.focus) > 2 or len(args.time) > 3):
        return usage_error("Pass --focus at most twice and --time at most three times.")
    commands = {"check": cmd_check, "gather": cmd_gather, "run": cmd_run, "report": cmd_report, "feedback": cmd_feedback}
    if args.command in commands:
        return commands[args.command](args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:  # A captured stream gets UTF-8; a console keeps its own encoding but never fails.
            if stream.isatty():
                stream.reconfigure(errors="backslashreplace")
            else:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
