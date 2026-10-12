"""The shared part of every next-actions-from-* skill, used by next-actions-from-all-my-notes.

Copied from part 1 of skills/next-actions-from-obsidian/scripts/obsidian_next_actions.py
(cerredz/vidbyte-cli PR #11). Keep the two in sync: a fix to redaction, the notes-digest/1
format, the run helpers, or the report matcher there belongs here too.

Changes from the source, and why:
- Redactor also removes the Notion (ntn_, secret_), Slack (xox?-), and Google (ya29., 1//) token
  formats, and the extra names it is given, such as the Slack handle, because this skill reads all
  four providers at once.
- parse_iso accepts the fractional seconds that Notion, Slack, and Drive send, on Python 3.8
  (copied from parse_ts in next-actions-from-notion, PR #13).
- validate_items_doc is the strict notes-digest/1 check from next-actions-from-slack (PR #14),
  extended to the merged "all" document.
- cut_chat comes from next-actions-from-slack and cut_head from next-actions-from-google-docs
  (PR #15), so each provider's text is cut the way its own skill cuts it.
- resolve_cli and codex_logged_in come from next-actions-from-notion: --cli names the vidbyte-cli
  executable, and the paid run checks the Codex login, which the CLI does not.

Only the Python standard library is used, and Python 3.8 or newer is enough.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import Counter, OrderedDict
from pathlib import Path

SCHEMA = "notes-digest/1"
TOP_FIELDS = ("schema", "provider", "generated_at", "since", "items")
ITEM_FIELDS = ("provider", "id", "title", "link", "kind", "container", "created", "last_edited", "author", "open_tasks", "excerpt")
KINDS = ("note", "page", "database-row", "message", "thread", "file", "doc")
PROVIDERS = ("obsidian", "notion", "slack", "google-docs")
PROJECT_KEY = "notes-next-actions"  # The same key in every family skill, so feedback carries over.
CATEGORIES = ("immediate_next_steps", "prioritization", "completion", "continuation")
HORIZON = "any"  # Any other value drops ideas after review, and the charge stays the same.

# Values the user may add to a run, with the vidbyte-cli flag each becomes and the most passed.
USER_VALUES = OrderedDict([("trajectory", ("--trajectory", 2)), ("constraint", ("--constraint", 3)), ("avoid", ("--avoid", 3))])
MAX_VALUE_CHARS = 400
KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class UsageError(Exception):
    """A problem the caller can fix; reported as JSON on stdout and exit code 2."""


class ProviderError(Exception):
    """One provider could not be read. The gather records it and goes on with the others.

    kind is one of: no_token, config, auth, scope, api_disabled, quota_project_denied,
    domain_policy, rate_limit, network, server, not_found, bad_request, forbidden, read_error,
    api_error, internal.
    """

    def __init__(self, kind: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint

    def as_dict(self, redact=None) -> dict:
        clean = redact or (lambda text: text)
        data = OrderedDict([("kind", self.kind), ("message", clean(self.message))])
        if self.hint:
            data["hint"] = clean(self.hint)
        return data


# --------------------------------------------------------------------------------------------
# Redaction


class Redactor:
    """Removes secrets, emails, phone numbers, home paths, and the user's names from any text."""

    TOKEN_RULES = [
        ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S), "[REDACTED private key]"),
        ("token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
        ("token", re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"), "[REDACTED]"),  # Google OAuth access tokens.
        ("token", re.compile(r"(?<![A-Za-z0-9])1//[A-Za-z0-9_-]{20,}"), "[REDACTED]"),  # Google refresh tokens.
        ("token", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED]"),
        ("token", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"), "[REDACTED]"),
        ("token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),
        ("token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
        ("token", re.compile(r"\bxox[abposrcde]-[A-Za-z0-9-]{10,}"), "[REDACTED]"),  # Slack user, bot, and session tokens.
        ("token", re.compile(r"\bxapp-[A-Za-z0-9-]{10,}"), "[REDACTED]"),
        ("token", re.compile(r"\bntn_[A-Za-z0-9]{20,}"), "[REDACTED]"),  # Notion tokens since 2024.
        ("token", re.compile(r"\bsecret_[A-Za-z0-9]{30,}"), "[REDACTED]"),  # Older Notion tokens.
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
    GENERIC_LOGINS = {"user", "users", "admin", "root", "test", "dev", "home", "guest", "ubuntu", "runner", "vscode", "codespace", "pi", "build", "app", "node", "owner", "me", "bot"}
    EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})\b")
    # A number with a leading + and digit groups, or a 3-3-4 number with separators. Dates
    # (2026-10-11), times, version numbers, and Slack timestamps have neither shape, so they stay.
    PHONE_RULES = [
        re.compile(r"(?<![\w+])\+\d{1,3}(?:[ .-]?\(?\d{1,4}\)?){2,5}(?![\w-]|\.\d)"),
        re.compile(r"(?<![\w.-])\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w-]|\.\d)"),
    ]
    HOME_RULES = [
        re.compile(r"(?i)\b[A-Z]:(?:\\\\|\\|/)+Users(?:\\\\|\\|/)+[^\\/\s\"'<>|:*?`]+"),
        re.compile(r"(?i)(?<![A-Za-z0-9])/[a-z]/Users/[^/\s\"'`]+"),
        re.compile(r"(?<![A-Za-z0-9~])/Users/[^/\s\"'`]+"),
        re.compile(r"(?<![A-Za-z0-9~])/home/[^/\s\"'`]+"),
    ]

    def __init__(self, home: Path, extra_names=()) -> None:
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
        names = [home.name] + [str(n) for n in extra_names if n]
        usable = sorted({n for n in names if len(n) >= 3 and n.lower() not in self.GENERIC_LOGINS}, key=len, reverse=True)
        self.login_re = None
        if usable:
            self.login_re = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(n) for n in usable) + r")(?![A-Za-z0-9])", re.I)

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
        for pattern in self.PHONE_RULES:
            out = pattern.sub(self._phone, out)
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
        digits = sum(ch.isdigit() for ch in match.group(0))
        if not 8 <= digits <= 15:
            return match.group(0)
        self.counts["phone"] += 1
        return "[phone]"


def digest_markers(text: str) -> dict:
    return {"redacted": text.count("[REDACTED"), "email": text.count("[email]"), "phone": text.count("[phone]"), "user": text.count("[user]")}


# --------------------------------------------------------------------------------------------
# Small helpers


def usage_error_exit(message: str) -> int:
    print(json.dumps({"error": message}, indent=2))
    print("Error: " + message, file=sys.stderr)
    return 2


def one_line(text: object, limit: int = 0) -> str:
    flat = " ".join(str(text if text is not None else "").split())
    if not limit or len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)].rstrip() + "…"


def iso_utc(stamp) -> str:
    """ISO 8601 in UTC with a Z, from an aware datetime or epoch seconds."""
    if isinstance(stamp, (int, float)):
        stamp = dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    """Returns an aware UTC datetime for an ISO 8601 string, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = re.sub(r"(\.\d{6})\d+", r"\1", text)  # Python 3.8-3.10 accept 3 or 6 digits only.
    text = re.sub(r"\.(\d{1,5})(?=[+-]|$)", lambda m: "." + m.group(1).ljust(6, "0"), text)
    try:
        stamp = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def local_text(stamp, with_time: bool = True) -> str:
    if stamp is None:
        return "?"
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def norm(path) -> str:
    return os.path.normcase(os.path.realpath(os.path.expanduser(str(path))))


def is_inside(path, folder) -> bool:
    p, f = norm(path), norm(folder)
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise UsageError("%s not found: %s. Run `gather` first and pass the out_dir it printed as --out." % (label, path))
    except ValueError as error:
        raise UsageError("%s is not valid JSON (%s): %s" % (label, error, path))


def write_text(path: Path, text: str) -> None:
    # Path.write_text has no newline= before Python 3.10.
    with open(str(path), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def write_json(path: Path, data: object) -> None:
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def window_start(args, now):
    """Returns (start, phrase); the phrase finishes the goal's "... over the last 7 days?"."""
    if args.since:
        try:
            day = dt.datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            raise UsageError("--since must be a date such as 2026-10-04.")
        start = day.astimezone(dt.timezone.utc)  # A naive value is read as local midnight.
        if start >= now:
            raise UsageError("--since must be in the past.")
        return start, "since %s" % args.since
    if not 1 <= args.days <= 365:
        raise UsageError("--days must be between 1 and 365.")
    phrase = "over the last day" if args.days == 1 else "over the last %d days" % args.days
    return now - dt.timedelta(days=args.days), phrase


# --------------------------------------------------------------------------------------------
# Cutting text to size. Each provider's text is cut the way its own skill cuts it.


def truncate_middle(text: str, limit: int) -> str:
    """Keeps the start and the end of a long text; notes and logs often grow at the bottom."""
    if len(text) <= limit:
        return text
    marker = "\n[... %d characters cut ...]\n"
    room = max(limit - len(marker % len(text)), 40)
    head_len, tail_len = int(room * 0.7), room - int(room * 0.7)
    head = text[:head_len]
    if "\n" in head[head_len // 2:]:
        head = head[: head.rindex("\n")]
    tail = text[len(text) - tail_len:]
    if "\n" in tail[: tail_len // 2]:
        tail = tail[tail.index("\n") + 1:]
    return head.rstrip() + marker % (len(text) - len(head) - len(tail)) + tail.lstrip()


def cut_chat(text: str, limit: int) -> str:
    """Keeps the first message and the newest ones (next-actions-from-slack)."""
    if len(text) <= limit:
        return text
    if "\n" not in text:
        return cut_head(text, limit)
    lines = text.split("\n")
    head = one_line(lines[0], max(limit // 3, 80))
    kept, used = [], len(head) + 40
    for line in reversed(lines[1:]):
        line = one_line(line, max(limit - len(head) - 40, 80))
        if used + len(line) + 1 > limit:
            break
        kept.insert(0, line)
        used += len(line) + 1
    left_out = len(lines) - 1 - len(kept)
    middle = ["[... %d earlier message%s left out]" % (left_out, "" if left_out == 1 else "s")] if left_out else []
    return "\n".join([head] + middle + kept)


def cut_head(text: str, limit: int) -> str:
    """Cuts on a word boundary and keeps the start (next-actions-from-google-docs)."""
    if len(text) <= limit:
        return text
    marker = " [... cut]"
    keep = max(limit - len(marker), 0)
    head = text[:keep]
    space = max(head.rfind(" "), head.rfind("\n"))
    if space >= keep - 80 and space > 0:
        head = head[:space]
    return head.rstrip() + marker


def cut_excerpt(text: str, limit: int, style: str) -> str:
    if style == "middle":
        return truncate_middle(text, limit)
    if style == "chat":
        return cut_chat(text, limit)
    return cut_head(text, limit)


HEADING_LINE_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")


def demote_headings(text: str) -> str:
    """Turns Markdown headings into bold lines, so an excerpt can never start a new digest item."""
    lines = []
    for line in str(text or "").split("\n"):
        heading = HEADING_LINE_RE.match(line)
        if heading:
            line = "**%s**" % heading.group(2) if heading.group(2) else ""
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# --------------------------------------------------------------------------------------------
# The notes-digest/1 format


def validate_items_doc(doc, provider: str) -> list:
    """Checks one items.json document field by field. Returns a list of problems (empty: valid).

    provider is the value the top-level "provider" must have: one provider's name, or "all" for
    the merged document, whose items may come from any provider.
    """
    problems = []
    if not isinstance(doc, dict):
        return ["items.json must hold one JSON object."]
    if list(doc) != list(TOP_FIELDS):
        problems.append("items.json must have exactly these keys, in this order: %s." % ", ".join(TOP_FIELDS))
    if doc.get("schema") != SCHEMA:
        problems.append('schema must be "%s".' % SCHEMA)
    if doc.get("provider") != provider:
        problems.append('provider must be "%s".' % provider)
    for key in ("generated_at", "since"):
        if parse_iso(doc.get(key)) is None:
            problems.append("%s must be an ISO 8601 time, such as 2026-10-04T00:00:00Z." % key)
    items = doc.get("items")
    if not isinstance(items, list):
        return problems + ["items must be a list."]
    allowed = PROVIDERS if provider == "all" else (provider,)
    for index, item in enumerate(items):
        where = "items[%d]" % index
        if not isinstance(item, dict):
            problems.append(where + " must be an object.")
            continue
        if set(item) != set(ITEM_FIELDS):
            missing = [k for k in ITEM_FIELDS if k not in item]
            extra = [k for k in item if k not in ITEM_FIELDS]
            problems.append("%s must have exactly the shared fields (missing: %s; extra: %s)." % (where, ", ".join(missing) or "none", ", ".join(extra) or "none"))
            continue
        if item["provider"] not in allowed:
            problems.append("%s.provider must be %s." % (where, " or ".join('"%s"' % p for p in allowed)))
        for key in ("id", "title"):
            if not isinstance(item[key], str) or not item[key].strip():
                problems.append("%s.%s must be a non-empty string." % (where, key))
        for key in ("link", "container", "excerpt"):
            if not isinstance(item[key], str):
                problems.append("%s.%s must be a string." % (where, key))
        if item["kind"] not in KINDS:
            problems.append("%s.kind must be one of %s." % (where, ", ".join(KINDS)))
        if item["created"] is not None and parse_iso(item["created"]) is None:
            problems.append("%s.created must be an ISO 8601 time or null." % where)
        if parse_iso(item["last_edited"]) is None:
            problems.append("%s.last_edited must be an ISO 8601 time." % where)
        if item["author"] not in ("me", "other", None):
            problems.append('%s.author must be "me", "other", or null.' % where)
        tasks = item["open_tasks"]
        if not isinstance(tasks, list) or any(not isinstance(t, str) for t in tasks):
            problems.append("%s.open_tasks must be a list of strings." % where)
        if len(problems) >= 40:
            problems.append("(stopped after 40 problems)")
            break
    return problems


# --------------------------------------------------------------------------------------------
# Running vidbyte-cli


def build_argv(exe: str, request: dict, dry_run: bool, key: str) -> list:
    argv = [exe, "--json", "--no-input", "agents", "suggest", "run", "--goal", request["goal"]]
    for category in request["categories"]:
        argv += ["--category", category]
    argv += ["--horizon", request["horizon"], "--count", str(request["count"])]
    for kind, (flag, _most) in USER_VALUES.items():
        for value in request["values"].get(kind, []):
            argv += [flag, value]
    for name in request["files"]:
        argv += ["--files", name]  # Relative: the path reaches the model, a home path must not.
    argv += ["--project", request["project"]]
    argv += ["--dry-run"] if dry_run else ["--idempotency-key", key]
    return argv


def display_command(argv: list) -> str:
    shown = ["vidbyte-cli"] + argv[1:]
    if os.name == "nt":
        return subprocess.list2cmdline(shown)
    return " ".join(shlex.quote(a) for a in shown)


def resolve_cli(value):
    """Returns the vidbyte-cli executable to run, or None. --cli takes a name looked up on PATH
    (the default, vidbyte-cli) or a path to an executable, which is used as given."""
    value = (value or "vidbyte-cli").strip()
    if os.path.dirname(value):
        path = os.path.abspath(os.path.expanduser(value))
        return path if os.path.isfile(path) else None
    return shutil.which(value)


def missing_cli(value) -> int:
    if value and value != "vidbyte-cli":
        message = "--cli %s is not an executable file or a command on PATH." % value
    else:
        message = "vidbyte-cli is not on PATH. Follow references/setup.md step 1."
    print(json.dumps({"error": message}, indent=2))
    print("Error: " + message, file=sys.stderr)
    return 127


def codex_logged_in():
    """Returns None when Codex is logged in, else a message. The CLI checks only PATH."""
    exe = shutil.which("codex")
    if not exe:
        return "codex is not on PATH. Install Codex and run `codex login` (references/setup.md step 3)."
    try:
        proc = subprocess.run([exe, "login", "status"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return "`codex login status` could not run: %s" % error
    if proc.returncode != 0:
        text = (proc.stdout or b"").decode("utf-8", "replace") + (proc.stderr or b"").decode("utf-8", "replace")
        return "Codex is not logged in (`codex login status` said: %s). Ask the user to run `codex login` in their own terminal." % one_line(text, 120)
    return None


def run_cli(argv: list, out_dir: Path, stdout_name: str):
    """Runs vidbyte-cli in out_dir; saves stdout to a file, and shows and saves stderr."""
    stdout_path = out_dir / stdout_name
    stderr_path = out_dir / (Path(stdout_name).stem + ".stderr.txt")
    captured = []
    with open(str(stdout_path), "wb") as sink:
        process = subprocess.Popen(argv, cwd=str(out_dir), stdout=sink, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
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


def read_result(path: Path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    data = document.get("data") if isinstance(document, dict) else None
    return document, data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------------------------
# Report: which items an idea names

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "have", "about", "what", "when", "will",
    "into", "over", "they", "them", "then", "than", "been", "just", "here", "there", "were", "would",
    "could", "should", "their", "which", "while", "where", "after", "before", "also", "more", "some",
    "each", "only", "other", "such", "very", "most", "make", "made", "next", "week", "today", "notes",
    "note", "page", "pages", "thread", "message", "file", "files", "channel", "private", "group",
    "untitled", "draft", "copy", "meeting", "daily", "weekly", "doc", "docs", "document",
}


def idea_text(idea: dict) -> str:
    parts = [idea.get(k) or "" for k in ("title", "summary", "first_action", "why_now", "expected_benefit", "completion_criteria")]
    parts += [str(a) for a in idea.get("suggested_actions") or []]
    parts += [str(a) for a in idea.get("decision_points") or []]
    for evidence in (idea.get("handoff") or {}).get("evidence") or []:
        # A file's evidence is only the first 4,096 characters of the digest, so it names the
        # first items whatever the idea is about; typed values are specific.
        if isinstance(evidence, dict) and str(evidence.get("source", "")).startswith("flag:"):
            parts.append(str(evidence.get("content") or ""))
    return "\n".join(str(p) for p in parts)


def significant_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9'-]{3,}", str(text).lower()) if w not in STOPWORDS}


def match_items(idea: dict, entries: list) -> list:
    """Items an idea names: by link or a distinctive id, then by exact title, in order of mention.

    A one-word title must match with its own capitals, so a note named "Ideas" is not matched by
    every use of the word "ideas". A title found only inside a longer matched title ("Launch" in
    "Launch plan") is not a second source unless it also appears on its own. When nothing matches,
    a title with two or more significant words matches if 60% of them appear in the idea.
    """
    text = idea_text(idea)
    lower = text.lower()
    found = []
    for entry in entries:
        positions = []
        link = entry.get("link") or ""
        if len(link) >= 12:
            at = text.find(link)
            if at >= 0:
                positions.append(at)
        ident = str(entry.get("id") or "")
        for needle in OrderedDict.fromkeys([ident, re.sub(r"\.md$", "", ident)]):
            # Only ids that cannot be ordinary words: long, and with a digit, a slash, or a colon.
            if len(needle) >= 8 and re.search(r"[0-9/:]", needle):
                at = lower.find(needle.lower())
                if at >= 0:
                    positions.append(at)
        title = str(entry.get("title") or "").strip()
        if len(title) >= 4 and title.lower() not in ("untitled", "(untitled)"):
            flags = re.I if " " in title else 0
            hit = re.search(r"(?<![\w])" + re.escape(title) + r"(?![\w])", text, flags)
            if hit:
                positions.append(hit.start())
        if positions:
            found.append((min(positions), entry))
    found.sort(key=lambda pair: pair[0])
    matched = [entry for _at, entry in found]
    titles = {id(e): str(e.get("title") or "").lower() for e in matched}
    kept = []
    for entry in matched:
        mine = titles[id(entry)]
        longer = [titles[id(o)] for o in matched if o is not entry and mine != titles[id(o)] and mine and mine in titles[id(o)]]
        if longer and lower.count(mine) <= sum(lower.count(t) for t in longer):
            continue
        kept.append(entry)
    if kept:
        return kept
    words = significant_words(text)
    scored = []
    for entry in entries:
        title = str(entry.get("title") or "").split(": ", 1)[-1]  # Slack titles start with "#channel: ".
        mine = significant_words(title)
        if len(mine) >= 2:
            share = len(mine & words) / float(len(mine))
            if share >= 0.6:
                scored.append((share, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _share, entry in scored[:3]]
