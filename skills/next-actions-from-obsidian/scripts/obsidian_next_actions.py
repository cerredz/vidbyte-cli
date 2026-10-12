#!/usr/bin/env python3
"""Turn recently edited Obsidian notes into next actions with the Vidbyte Suggestion Agent.

Five subcommands, run in this order by the next-actions-from-obsidian skill:

  find-vaults  Free and local. Lists Obsidian vaults from Obsidian's own vault list
               (obsidian.json) and a bounded scan of common folders, for the user to pick one.
  gather       Free, local, and read-only. Reads the Markdown notes created or edited in a time
               window, extracts open tasks, tags, and links to notes not written yet, redacts
               secrets, emails, phone numbers, home paths, and the login name, and writes
               items.json and digest.md (the shared notes-digest/1 format) plus gather.json, a
               local index that is never sent.
  run          --dry-run is free: it records the exact request in request.json and checks it with
               `vidbyte-cli agents suggest run --dry-run`. --paid replays that same request once;
               it is the one charged step and needs the user's yes first.
  report       Free and local. Groups the next actions in result.json by horizon and ties each one
               to the notes it names.
  feedback     Free and local. Records the user's explicit accept or reject of one idea.

The file has two parts. Part 1 is the format every next-actions-from-* skill shares: redaction,
items.json, digest.md, the run guard, the report, and feedback. Part 2 is the Obsidian adapter:
finding vaults, walking notes, and parsing Markdown. A skill for another note provider keeps
part 1 unchanged and replaces part 2.

Only the Python standard library is used, and Python 3.8 or newer is enough. External commands
are started as argument lists without a shell, so quoting is the same on every operating system.
Nothing is ever written into the vault.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import quote, unquote

SLUG = "next-actions-from-obsidian"
PROVIDER = "obsidian"
PROVIDER_LABEL = "Obsidian"
ITEM_NOUN = ("note", "notes")
SCHEMA = "notes-digest/1"
PROJECT_KEY = "notes-next-actions"  # The same key in every family skill, so feedback carries over.
CATEGORIES = ("immediate_next_steps", "prioritization", "completion", "continuation")
HORIZON = "any"  # Any other value drops ideas after review, and the charge stays the same.
COUNT = 6  # One admission block covers up to ten ideas, so six cost one block.

DEFAULT_DAYS = 7
DEFAULT_MAX_ITEMS = 60
DEFAULT_MAX_CHARS = 60000  # The CLI accepts 5,000,000 per --files file; the model needs far less.
MIN_MAX_CHARS, MAX_MAX_CHARS = 5000, 200000
DEFAULT_EXCERPT_CHARS = 1500
MIN_EXCERPT_CHARS = 200
MAX_TASKS = 20
MAX_TASK_CHARS = 200
MAX_READ_CHARS = 1000000
MASS_CHANGE_ITEMS = 100

# Values the user may add to a run, with the vidbyte-cli flag each becomes and the most passed.
USER_VALUES = OrderedDict([("trajectory", ("--trajectory", 2)), ("constraint", ("--constraint", 3)), ("avoid", ("--avoid", 3))])
MAX_VALUE_CHARS = 400
MAX_GOAL_CHARS = 1000
KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
ITEM_FIELDS = ("provider", "id", "title", "link", "kind", "container", "created", "last_edited", "author", "open_tasks", "excerpt")


class UsageError(Exception):
    """A problem the caller can fix; reported as JSON on stdout and exit code 2."""


# ============================================================================================
# Part 1. The shared family format
# ============================================================================================

# --------------------------------------------------------------------------------------------
# Redaction


class Redactor:
    """Removes secrets, emails, phone numbers, home paths, and the login name from any text."""

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
    # A number with a leading + and digit groups, or a 3-3-4 number with separators. Dates
    # (2026-10-11), times, and version numbers do not have either shape, so they are kept.
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


def one_line(text: object, limit: int) -> str:
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 1)].rstrip() + "…"


def iso_utc(stamp) -> str:
    """ISO 8601 in UTC with a Z, from an aware datetime or epoch seconds."""
    if isinstance(stamp, (int, float)):
        stamp = dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    if not value:
        return None
    try:
        stamp = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)


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


def window_start(args, now):
    """Returns (start, phrase); the phrase finishes the goal's "... in Obsidian <phrase>?"."""
    if args.since:
        try:
            day = dt.datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            raise UsageError("--since must be a date such as 2026-10-04.")
        start = day.astimezone(dt.timezone.utc)  # A naive value is read as local midnight.
        if start >= now:
            raise UsageError("--since must be in the past.")
        return start, "since %s" % args.since
    if not 1 <= args.days <= 3650:
        raise UsageError("--days must be between 1 and 3650.")
    phrase = "over the last day" if args.days == 1 else "over the last %d days" % args.days
    return now - dt.timedelta(days=args.days), phrase


# --------------------------------------------------------------------------------------------
# items.json and digest.md


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


def finalize_item(item: dict, excerpt_limit: int) -> dict:
    """Builds one items.json entry, with exactly the shared fields, from a gathered note."""
    excerpt = item["excerpt_prefix"]
    body = truncate_middle(item["body"], excerpt_limit) if item["body"] else ""
    if body:
        excerpt = (excerpt + "\n\n" + body) if excerpt else body
    entry = OrderedDict((name, item[name]) for name in ITEM_FIELDS if name != "excerpt")
    entry["excerpt"] = excerpt
    return entry


def render_item(entry: dict) -> str:
    edited = local_text(parse_iso(entry["last_edited"]))
    lines = ["## " + one_line(entry["title"], 200), "%s · %s · last edited %s · %s" % (entry["kind"], entry["container"], edited, entry["link"])]
    if entry["open_tasks"]:
        lines.append("")
        lines.extend("- [ ] " + task for task in entry["open_tasks"])
    if entry["excerpt"]:
        lines.extend(["", entry["excerpt"]])
    return "\n".join(lines) + "\n"


def render_digest(entries: list, window: dict, notes: list) -> str:
    since, until = parse_iso(window["since"]), parse_iso(window["until"])
    tz = dt.datetime.now().astimezone().strftime("%Z") or "local time"
    count = len(entries)
    header = "# %s: %d %s created or edited %s (%s to %s)" % (
        PROVIDER_LABEL, count, ITEM_NOUN[0] if count == 1 else ITEM_NOUN[1], window["phrase"], local_text(since, False), local_text(until, False)
    )
    about = (
        "Newest first; times are local (%s). Each section lists the item's unchecked \"- [ ]\" tasks, then an excerpt of its text. "
        "Secrets, email addresses, phone numbers, home-folder paths, and the user's login name were replaced with "
        "[REDACTED], [email], [phone], ~, and [user] before this file was written." % tz
    )
    paragraph = " ".join([about] + [n for n in notes if n])
    return "\n".join([header, "", paragraph, ""]) + "\n" + "\n".join(render_item(e) for e in entries)


def fit_to_budget(items: list, window: dict, notes_fn, max_chars: int, excerpt_chars: int):
    """Shrinks excerpts, then drops the oldest items, until the digest fits max_chars."""
    kept = list(items)
    limit = excerpt_chars
    dropped = 0
    while True:
        entries = [finalize_item(item, limit) for item in kept]
        digest = render_digest(entries, window, notes_fn(kept))
        if len(digest) <= max_chars or not kept:
            return entries, digest, limit, dropped
        if limit > MIN_EXCERPT_CHARS:
            limit = max(MIN_EXCERPT_CHARS, int(limit * 0.8))
        elif len(kept) > 1:
            kept.pop()
            dropped += 1
        else:
            entries = [finalize_item(kept[0], MIN_EXCERPT_CHARS)]
            entries[0]["open_tasks"] = entries[0]["open_tasks"][:5]
            digest = render_digest(entries, window, notes_fn(kept))[:max_chars]
            return entries, digest, limit, dropped


def items_document(entries: list, now, since) -> dict:
    return OrderedDict(
        [("schema", SCHEMA), ("provider", PROVIDER), ("generated_at", iso_utc(now)), ("since", iso_utc(since)), ("items", entries)]
    )


# --------------------------------------------------------------------------------------------
# Run: the free dry run records the request; the paid run replays it once


def default_goal(phrase: str) -> str:
    return (
        "What are the most important next actions given what I've been writing and discussing in %s %s? "
        "Name the %s each action comes from." % (PROVIDER_LABEL, phrase, ITEM_NOUN[1])
    )


def build_request(args, gather: dict, digest_hash: str, redact: Redactor) -> dict:
    problems = []
    goal = redact(" ".join((args.goal or default_goal(gather["window"]["phrase"])).split()))
    if not goal:
        problems.append("--goal must not be empty.")
    if len(goal) > MAX_GOAL_CHARS:
        problems.append("--goal has %d characters; keep it under %d." % (len(goal), MAX_GOAL_CHARS))
    project = args.project or PROJECT_KEY
    if not PROJECT_RE.match(project):
        problems.append("--project must be 1-64 lowercase letters, digits, hyphens, or underscores.")
    values = OrderedDict()
    for kind, (flag, most) in USER_VALUES.items():
        cleaned = [redact(" ".join(v.split())) for v in (getattr(args, kind) or []) if v.strip()]
        if len(cleaned) > most:
            problems.append("%s was given %d times; this skill passes at most %d." % (flag, len(cleaned), most))
        for value in cleaned:
            if len(value) > MAX_VALUE_CHARS:
                problems.append("A %s value has %d characters; keep each under %d: %s" % (flag, len(value), MAX_VALUE_CHARS, one_line(value, 80)))
        values[kind] = cleaned
    if problems:
        raise UsageError(" ".join(problems))
    return OrderedDict(
        [
            ("goal", goal),
            ("categories", list(CATEGORIES)),
            ("horizon", HORIZON),
            ("count", COUNT),
            ("values", values),
            ("files", ["digest.md"]),
            ("project", project),
            ("digest_sha256", digest_hash),
            ("dry_run_ok", False),
        ]
    )


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


def run_cli(argv: list, out_dir: Path, stdout_name: str):
    """Runs vidbyte-cli in out_dir; saves stdout to a file, and shows and saves stderr."""
    stdout_path = out_dir / stdout_name
    stderr_path = out_dir / (Path(stdout_name).stem + ".stderr.txt")
    captured = []
    with open(str(stdout_path), "wb") as sink:
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


def read_result(path: Path):
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    data = document.get("data") if isinstance(document, dict) else None
    return document, data if isinstance(data, dict) else {}


def cmd_run(args) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    if args.dry_run == args.paid:
        raise UsageError("Pass exactly one of --dry-run (the free check) or --paid (the charged run, only after the user said yes).")
    for name in ("digest.md", "items.json", "gather.json"):
        if not (out_dir / name).is_file():
            raise UsageError("%s not found in %s. Run `gather` first and pass the out_dir it printed." % (name, out_dir))
    custom = [flag for flag, value in (("--goal", args.goal), ("--project", args.project), ("--trajectory", args.trajectory), ("--constraint", args.constraint), ("--avoid", args.avoid)) if value]
    if args.paid and custom:
        raise UsageError("--paid replays the request the dry run checked, so it takes no %s. Pass them to --dry-run, show the user, then run --paid." % ", ".join(custom))
    if args.dry_run and args.idempotency_key:
        raise UsageError("--idempotency-key belongs to --paid only.")
    exe = shutil.which("vidbyte-cli")
    if not exe:
        print(json.dumps({"error": "vidbyte-cli is not on PATH. Follow references/setup.md step 1."}, indent=2))
        return 127
    gather = load_json(out_dir / "gather.json", "gather.json")
    digest_hash = sha256_file(out_dir / "digest.md")
    request_path = out_dir / "request.json"
    key_path = out_dir / "idempotency-key.txt"
    result_path = out_dir / "result.json"
    key = ""
    if args.dry_run:
        if key_path.exists() or result_path.exists():
            raise UsageError("A paid run was already started in this folder. A new request needs a new folder: run `gather` again.")
        request = build_request(args, gather, digest_hash, Redactor(Path.home()))
        write_text(request_path, json.dumps(request, indent=2))
        argv = build_argv(exe, request, True, "")
        if "--dry-run" not in argv or "--idempotency-key" in argv:  # Belt and braces: never pay here.
            raise UsageError("Internal check failed: the dry-run command is not a dry run.")
    else:
        request = load_json(request_path, "request.json")
        if not request.get("dry_run_ok"):
            raise UsageError("No passing dry run is recorded in this folder. Run `run --dry-run`, show the user what will be sent, and ask before --paid.")
        if request.get("digest_sha256") != digest_hash:
            raise UsageError("digest.md changed after the dry run. Run `run --dry-run` again and show the user the new digest before --paid.")
        finished = result_path.is_file() and result_path.stat().st_size > 0
        if args.idempotency_key:
            if not KEY_RE.match(args.idempotency_key):
                raise UsageError("--idempotency-key must be 8-128 letters, digits, dots, underscores, colons, or hyphens.")
            if finished:
                raise UsageError("result.json already holds the output of a paid run, so there is nothing to recover. Read it instead.")
            key = args.idempotency_key
        elif finished or result_path.exists() or key_path.exists():
            # Any earlier paid attempt here may already have been charged. A fresh key would buy
            # a second admission, so only an explicit recovery key or a new folder may continue.
            raise UsageError(
                "A paid run was already started in this folder (see idempotency-key.txt and result.json). "
                "To recover an attempt that stopped before Vidbyte verified its admission, rerun with --idempotency-key and the saved key. "
                "Any other new run is a new charge: ask the user first, then gather into a new folder."
            )
        else:
            key = "%s:%s:%s" % (SLUG, dt.datetime.now().strftime("%Y%m%d"), secrets.token_hex(3))
            write_text(key_path, key + "\n")
        print("Idempotency key for this run: %s (saved in idempotency-key.txt)" % key, file=sys.stderr)
        argv = build_argv(exe, request, False, key)
        if "--dry-run" in argv:
            raise UsageError("Internal check failed: the paid command contains --dry-run.")
    print("Running in %s:\n%s" % (out_dir, display_command(argv)), file=sys.stderr)
    code, stdout_path, stderr_path, error = run_cli(argv, out_dir, "dry-run.json" if args.dry_run else "result.json")
    report = OrderedDict([("mode", "dry-run" if args.dry_run else "paid"), ("exit_code", code), ("cli", exe), ("command", display_command(argv)), ("stderr_file", str(stderr_path))])
    if args.paid:
        report["idempotency_key"] = key
    if code == 0:
        document, data = read_result(stdout_path)
        report.update(
            [
                ("result_file", str(stdout_path)),
                ("kind", document.get("kind")),
                ("status", data.get("status")),
                ("stop_reason", data.get("stop_reason")),
                ("ideas", len(data.get("ideas") or [])),
                ("charged_cents", data.get("charged_cents")),
                ("context_manifest", [{"ref": m.get("ref"), "kind": m.get("kind"), "source": m.get("source"), "chars": m.get("chars"), "status": m.get("status")} for m in data.get("context_manifest") or []]),
                ("warnings", data.get("warnings") or []),
            ]
        )
        if args.dry_run and data.get("stop_reason") == "dry_run":
            request["dry_run_ok"] = True
            write_text(request_path, json.dumps(request, indent=2))
    elif error:
        report["error"] = {k: error.get(k) for k in ("code", "message", "hint", "retryable") if k in error}
    print(json.dumps(report, indent=2))
    return code


# --------------------------------------------------------------------------------------------
# Report: next actions grouped by horizon, each tied to the items it names


def idea_text(idea: dict) -> str:
    parts = [idea.get(k) or "" for k in ("title", "summary", "first_action", "why_now", "expected_benefit", "completion_criteria")]
    parts += [str(a) for a in idea.get("suggested_actions") or []]
    return "\n".join(str(p) for p in parts)


def match_items(idea: dict, entries: list) -> list:
    """Items an idea names, by id or link first, then by title, in the order they appear."""
    text = idea_text(idea)
    lower = text.lower()
    found = []
    for entry in entries:
        positions = []
        for needle in {entry["id"], entry["link"], re.sub(r"\.md$", "", entry["id"])}:
            if needle and len(needle) >= 3:
                at = lower.find(needle.lower())
                if at >= 0:
                    positions.append(at)
        title = entry["title"].strip()
        if len(title) >= 4:
            # A one-word title must match with its own capitals, so a note named "Ideas" is not
            # matched by every use of the word "ideas".
            flags = re.I if " " in title else 0
            hit = re.search(r"(?<![\w])" + re.escape(title) + r"(?![\w])", text, flags)
            if hit:
                positions.append(hit.start())
        if positions:
            found.append((min(positions), entry))
    found.sort(key=lambda pair: pair[0])
    return [entry for _at, entry in found]


def cmd_report(args) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    result_path = Path(args.result).expanduser() if args.result else out_dir / "result.json"
    document = load_json(result_path, "result.json")
    if not isinstance(document, dict) or document.get("kind") != "suggestions.result":
        raise UsageError("%s is not a suggestions.result document." % result_path)
    entries = load_json(out_dir / "items.json", "items.json").get("items") or []
    gather = load_json(out_dir / "gather.json", "gather.json")
    data = document.get("data") or {}
    ideas = sorted((i for i in data.get("ideas") or [] if isinstance(i, dict)), key=lambda i: i.get("rank") or 0)
    lines = ["# Next actions from your %s notes, %s" % (PROVIDER_LABEL, gather["window"]["phrase"]), ""]
    if data.get("status") not in ("complete", None):
        lines.append("Run status: %s (stop reason: %s)." % (data.get("status"), data.get("stop_reason")))
        if data.get("parent_message"):
            lines.append("The agent asks: " + str(data["parent_message"]))
        lines.append("")
    named = set()
    groups = OrderedDict((h, []) for h in ("now", "next", "later", "other"))
    for idea in ideas:
        groups[idea.get("horizon") if idea.get("horizon") in groups else "other"].append(idea)
    for horizon, members in groups.items():
        if not members:
            continue
        lines += ["## " + horizon.capitalize(), ""]
        for idea in members:
            sources = match_items(idea, entries)
            named.update(e["id"] for e in sources)
            details = [idea.get("id"), idea.get("readiness"), "effort: %s" % idea.get("effort_estimate")]
            lines.append("%s. %s  (%s)" % (idea.get("rank"), idea.get("title"), " · ".join(str(d) for d in details if d)))
            if idea.get("summary"):
                lines.append("   " + one_line(idea["summary"], 500))
            if idea.get("first_action"):
                lines.append("   First action: " + one_line(idea["first_action"], 400))
            if sources:
                for entry in sources:
                    lines.append('   From: "%s" (%s) %s' % (entry["title"], entry["id"], entry["link"]))
            else:
                lines.append("   From: the digest as a whole (the idea names no single %s)" % ITEM_NOUN[0])
            lines.append("")
    if not ideas:
        lines += ["No next actions came back (stop reason: %s)." % data.get("stop_reason"), ""]
    unnamed = len([e for e in entries if e["id"] not in named])
    lines.append("%s no action named: %d of %d (all are in digest.md)." % (ITEM_NOUN[1].capitalize(), unnamed, len(entries)))
    if data.get("charged_cents") is not None:
        admission = (data.get("admission") or {}).get("admission_id")
        lines.append("This run cost %s cents from your Vidbyte balance%s." % (data["charged_cents"], " (admission %s)" % admission if admission else ""))
    for warning in data.get("warnings") or []:
        lines.append("Warning: " + str(warning))
    text = "\n".join(lines) + "\n"
    write_text(out_dir / "report.md", text)
    sys.stdout.write(text)
    return 0


# --------------------------------------------------------------------------------------------
# Feedback


def cmd_feedback(args) -> int:
    out_dir = Path(args.out).expanduser().resolve()
    result = load_json(out_dir / "result.json", "result.json")
    ideas = ((result.get("data") or {}).get("ideas") or []) if isinstance(result, dict) else []
    match = [i for i in ideas if isinstance(i, dict) and i.get("id") == args.idea]
    if not match:
        raise UsageError("No idea with id %s in result.json. Ids look like idea-001." % args.idea)
    project = args.project or PROJECT_KEY
    if not PROJECT_RE.match(project):
        raise UsageError("--project must be 1-64 lowercase letters, digits, hyphens, or underscores.")
    exe = shutil.which("vidbyte-cli")
    if not exe:
        print(json.dumps({"error": "vidbyte-cli is not on PATH. Follow references/setup.md step 1."}, indent=2))
        return 127
    verb = "accept" if args.accept else "reject"
    # Rejection removes later ideas that repeat this text word for word, so copy it exactly.
    argv = [exe, "--json", "agents", "suggest", "feedback", verb, "--project", project, "--suggestion", match[0]["title"]]
    if args.reason and args.reason.strip():
        argv += ["--reason", Redactor(Path.home())(" ".join(args.reason.split()))]
    print(display_command(argv), file=sys.stderr)
    return subprocess.call(argv)


# ============================================================================================
# Part 2. The Obsidian adapter
# ============================================================================================

# --------------------------------------------------------------------------------------------
# Finding vaults

SCAN_SKIP = {
    "node_modules", "appdata", "library", "applications", "program files", "program files (x86)", "windows",
    "site-packages", "__pycache__", "venv", "env", "dist", "build", "target", "go", "pictures", "music",
    "videos", "movies", "downloads", "snap",
}


def obsidian_settings_dirs(home: Path) -> list:
    """Obsidian's global settings folders, which hold obsidian.json, the app's vault list."""
    dirs = []
    if os.name == "nt":
        if os.environ.get("APPDATA"):
            dirs.append(Path(os.environ["APPDATA"]) / "obsidian")
    elif sys.platform == "darwin":
        dirs.append(home / "Library" / "Application Support" / "obsidian")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            dirs.append(Path(xdg) / "obsidian")
        dirs.append(home / ".config" / "obsidian")
        dirs.append(home / ".var" / "app" / "md.obsidian.Obsidian" / "config" / "obsidian")  # Flatpak
        dirs.append(home / "snap" / "obsidian" / "current" / ".config" / "obsidian")  # Snap
    return list(OrderedDict((norm(d), d) for d in dirs).values())


def read_vault_list(path: Path) -> list:
    """obsidian.json is {"vaults": {"<id>": {"path": "...", "ts": <epoch ms>, "open": true}}}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    vaults = data.get("vaults") if isinstance(data, dict) else None
    found = []
    for entry in (vaults or {}).values():
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            ts = entry.get("ts")
            found.append({"path": entry["path"], "open": bool(entry.get("open")), "last_opened": iso_utc(ts / 1000.0) if isinstance(ts, (int, float)) and ts > 0 else None})
    return found


def scan_for_vaults(roots: list, max_depth: int = 4, seconds: float = 15.0, max_dirs: int = 60000):
    found, visited, stopped = [], 0, False
    deadline = time.time() + seconds
    queue = [(Path(r), 0) for r in roots if Path(r).is_dir()]
    while queue:
        folder, depth = queue.pop(0)
        if visited >= max_dirs or time.time() > deadline:
            stopped = True
            break
        visited += 1
        try:
            with os.scandir(str(folder)) as it:
                children = [e for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            continue
        if any(c.name == ".obsidian" for c in children):
            found.append(folder)  # Keep going: a vault can hold another vault.
        if depth < max_depth:
            for child in children:
                if not child.name.startswith(".") and child.name.lower() not in SCAN_SKIP:
                    queue.append((Path(child.path), depth + 1))
    return found, visited, stopped


def cmd_find_vaults(args) -> int:
    home = Path.home()
    vaults: "OrderedDict[str, dict]" = OrderedDict()
    lists_read = []
    for folder in obsidian_settings_dirs(home):
        listing = folder / "obsidian.json"
        if not listing.is_file():
            continue
        lists_read.append(str(listing))
        for entry in read_vault_list(listing):
            path = Path(entry["path"]).expanduser()
            if (path / ".obsidian").is_dir():
                record = vaults.setdefault(norm(path), {"name": path.name, "path": str(path), "found_in": [], "open_in_obsidian": None, "last_opened": None})
                record["found_in"].append("obsidian.json")
                record["open_in_obsidian"] = entry["open"]
                record["last_opened"] = entry["last_opened"]
    scanned = None
    if not args.no_scan:
        roots = [Path(r).expanduser() for r in args.root] or [home]
        for extra in (home / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents", home / "iCloudDrive" / "iCloud~md~obsidian"):
            if extra.is_dir() and not args.root:
                roots.append(extra)
        found, visited, stopped = scan_for_vaults(roots)
        for path in found:
            record = vaults.setdefault(norm(path), {"name": path.name, "path": str(path), "found_in": [], "open_in_obsidian": None, "last_opened": None})
            record["found_in"].append("scan")
        scanned = {"roots": [str(r) for r in roots], "folders_visited": visited, "stopped_early": stopped}
    # Open in Obsidian first, then the most recently opened, then by name (stable sorts).
    ordered = sorted(vaults.values(), key=lambda v: v["name"].lower())
    ordered.sort(key=lambda v: v["last_opened"] or "", reverse=True)
    ordered.sort(key=lambda v: not v["open_in_obsidian"])
    summary = OrderedDict([("vaults", ordered), ("vault_lists_read", lists_read), ("scan", scanned)])
    if not ordered:
        summary["error"] = "no vaults found"
        summary["hint"] = "Ask the user for the vault folder (the folder that holds .obsidian), then pass it to gather --vault."
    print(json.dumps(summary, indent=2))
    return 0 if ordered else 3


# --------------------------------------------------------------------------------------------
# Vault settings that change what counts as a note


def read_json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def clean_rel(value: str) -> str:
    text = str(value).strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def moment_to_regex(fmt: str) -> "re.Pattern":
    """Turns a daily-note date format such as YYYY-MM-DD (moment.js tokens) into a regex."""
    tokens = [("YYYY", r"\d{4}"), ("MMMM", r"[^\W\d_]+"), ("dddd", r"[^\W\d_]+"), ("MMM", r"[^\W\d_]{3}"), ("ddd", r"[^\W\d_]{2,3}"),
              ("Do", r"\d{1,2}(?:st|nd|rd|th)"), ("YY", r"\d{2}"), ("MM", r"\d{2}"), ("DD", r"\d{2}"), ("M", r"\d{1,2}"), ("D", r"\d{1,2}")]
    out, i = "", 0
    while i < len(fmt):
        if fmt[i] == "[":  # moment.js escapes literal text in brackets.
            end = fmt.find("]", i)
            if end > i:
                out += re.escape(fmt[i + 1:end])
                i = end + 1
                continue
        for token, pattern in tokens:
            if fmt.startswith(token, i):
                out += pattern
                i += len(token)
                break
        else:
            out += re.escape(fmt[i])
            i += 1
    return re.compile("^" + out + "$")


class VaultSettings:
    """What the vault's own .obsidian settings say about templates, attachments, and daily notes."""

    def __init__(self, vault: Path) -> None:
        config = vault / ".obsidian"
        app = read_json_file(config / "app.json") or {}
        self.skip_prefixes = OrderedDict()  # folder -> reason
        templates = read_json_file(config / "templates.json") or {}
        templater = read_json_file(config / "plugins" / "templater-obsidian" / "data.json") or {}
        for value in (templates.get("folder"), templater.get("templates_folder")):
            if isinstance(value, str) and clean_rel(value):
                self.skip_prefixes[clean_rel(value)] = "templates folder"
        attach = app.get("attachmentFolderPath")
        self.attachment_segment = None
        if isinstance(attach, str) and attach.strip() not in ("", "/", "./", "."):
            if attach.strip().startswith("./"):
                self.attachment_segment = clean_rel(attach).lower()  # A subfolder beside each note.
            else:
                self.skip_prefixes[clean_rel(attach)] = "attachment folder"
        self.ignore_prefixes, self.ignore_patterns = [], []
        for value in app.get("userIgnoreFilters") or []:
            if not isinstance(value, str) or not value.strip():
                continue
            if len(value) > 2 and value.startswith("/") and value.endswith("/"):
                try:
                    self.ignore_patterns.append(re.compile(value[1:-1]))
                except re.error:
                    continue
            else:
                self.ignore_prefixes.append(clean_rel(value))
        # Daily notes are named by their date: YYYY-MM-DD unless the Daily notes core plugin or
        # the Periodic Notes plugin sets another format. A format with folders keeps its last part.
        daily = read_json_file(config / "daily-notes.json") or {}
        periodic = (read_json_file(config / "plugins" / "periodic-notes" / "data.json") or {}).get("daily") or {}
        self.daily_formats = [re.compile(r"^\d{4}-\d{2}-\d{2}$")]
        for source in (daily, periodic):
            if isinstance(source, dict) and isinstance(source.get("format"), str) and source["format"].strip():
                self.daily_formats.append(moment_to_regex(source["format"].strip().split("/")[-1]))

    def auto_skip(self, rel: str):
        """The reason the vault's own settings say this path is not a note, or None."""
        lower = rel.lower()
        for prefix, reason in self.skip_prefixes.items():
            if lower == prefix.lower() or lower.startswith(prefix.lower() + "/"):
                return "%s/ (%s)" % (prefix, reason)
        if self.attachment_segment and ("/" + self.attachment_segment + "/") in ("/" + lower):
            return "%s/ beside notes (attachment folder)" % self.attachment_segment
        for prefix in self.ignore_prefixes:
            if lower == prefix.lower() or lower.startswith(prefix.lower() + "/"):
                return "%s/ (Excluded files setting)" % prefix
        for pattern in self.ignore_patterns:
            if pattern.search(rel):
                return "/%s/ (Excluded files setting)" % pattern.pattern
        if lower.endswith(".excalidraw.md"):
            return "*.excalidraw.md (Excalidraw drawings)"
        return None

    def is_daily(self, stem: str) -> bool:
        return any(f.match(stem) for f in self.daily_formats)


def path_excluded(rel: str, patterns: list) -> bool:
    """--exclude: a vault-relative folder, a note, or a glob, compared without case."""
    lower = rel.lower()
    parts = lower.split("/")
    parents = ["/".join(parts[:i]) for i in range(1, len(parts))]
    for pattern in patterns:
        if any(ch in pattern for ch in "*?["):
            if any(fnmatch.fnmatchcase(c, pattern) for c in [lower] + parents):
                return True
        elif lower == pattern or lower == pattern + ".md" or lower.startswith(pattern + "/"):
            return True
    return False


# --------------------------------------------------------------------------------------------
# Parsing one note

FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w-]*)")
QUERY_FENCES = {"dataview", "dataviewjs", "query", "tasks", "base", "chart"}
TASK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[([ xX/>\-!?])\]\s*(.*?)\s*$")
HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
TAG_RE = re.compile(r"(?<!\S)#((?:[\w/-]*[^\W\d])[\w/-]*)")
WIKI_RE = re.compile(r"(?<!!)\[\[([^\[\]|#^]*)(?:[#^][^\[\]|]*)?(?:\|[^\[\]]*)?\]\]")
MD_LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(<?([^)<>\s]+?\.md)(?:#[^)]*)?>?\)")
EMBED_RE = re.compile(r"!\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)")
COMMENT_RE = re.compile(r"%%.*?%%|<!--.*?-->", re.S)
FRONTMATTER_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9 _-]*):\s*(.*?)\s*$")


def parse_frontmatter(lines: list) -> dict:
    """A small YAML reader for flat keys, inline lists, and block lists; enough for notes."""
    data, key = {}, None
    for line in lines:
        item = re.match(r"^\s+-\s+(.*?)\s*$", line) or re.match(r"^-\s+(.*?)\s*$", line)
        if item and key is not None:
            if not isinstance(data.get(key), list):
                data[key] = []
            data[key].append(item.group(1).strip("\"'"))
            continue
        match = FRONTMATTER_KEY_RE.match(line)
        if not match:
            continue
        key, value = match.group(1).strip().lower(), match.group(2)
        if value.startswith("[") and value.endswith("]"):
            data[key] = [v.strip().strip("\"'") for v in value[1:-1].split(",") if v.strip()]
        else:
            data[key] = value.strip("\"'") if value else []
    return data


def frontmatter_tags(data: dict) -> list:
    raw = data.get("tags", data.get("tag", []))
    values = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw))
    return [v.strip().lstrip("#") for v in values if v and v.strip().lstrip("#")]


def frontmatter_created(data: dict):
    for key in ("created", "created_at", "date created", "date-created", "creation date"):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip().replace("Z", "+00:00")
        for candidate in (text, text.replace(" ", "T", 1)):
            try:
                stamp = dt.datetime.fromisoformat(candidate)
            except ValueError:
                continue
            return (stamp if stamp.tzinfo else stamp.astimezone()).astimezone(dt.timezone.utc)
    return None


def parse_note(text: str) -> dict:
    """Splits a note into frontmatter, open tasks, tags, link targets, and a cleaned body."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    front = {}
    if lines and lines[0].strip() == "---":
        for end in range(1, min(len(lines), 300)):
            if lines[end].strip() in ("---", "..."):
                front = parse_frontmatter(lines[1:end])
                lines = lines[end + 1:]
                break
    body = COMMENT_RE.sub("", "\n".join(lines))
    tasks, tags, links, kept = [], [], [], []
    fence, drop_fence = None, False
    for line in body.split("\n"):
        fence_match = FENCE_RE.match(line)
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= len(fence):
                fence = None
                if not drop_fence:
                    kept.append((line, 0))
            elif not drop_fence:
                kept.append((line, 0))
            continue
        if fence_match:
            # Dataview and Tasks blocks are queries, not writing, so they are dropped whole.
            fence, drop_fence = fence_match.group(1), fence_match.group(2).lower() in QUERY_FENCES
            if not drop_fence:
                kept.append((line, 0))
            continue
        plain = re.sub(r"`[^`]*`", "", line)
        tags.extend(TAG_RE.findall(plain))
        links.extend(("wiki", t) for t in WIKI_RE.findall(plain))
        links.extend(("md", t) for t in MD_LINK_RE.findall(plain))
        task = TASK_RE.match(line)
        if task:
            if task.group(1) in (" ", "/") and task.group(2):
                tasks.append(task.group(2))
                continue  # Open tasks are listed on their own; the excerpt keeps finished ones.
            if not task.group(2):
                continue
        line = EMBED_RE.sub("", line).rstrip()
        heading = HEADING_RE.match(line)
        if heading:
            if heading.group(2).strip():
                # Bold, not "#", so a heading inside an excerpt never looks like a new digest item.
                kept.append(("**%s**" % heading.group(2).strip(), len(heading.group(1))))
            continue
        if re.match(r"^\s*(?:[-*+]|\d+[.)])\s*$", line):
            continue  # An empty bullet is template scaffolding.
        kept.append((line, 0))
    # Drop headings with nothing under them, such as the unfilled sections of a daily-note
    # template. Walking backwards lets a parent heading whose only children were dropped go too.
    cleaned, next_level = [], None  # None: end of note; 0: text; n: a heading of level n.
    for line, level in reversed(kept):
        if level:
            if next_level is None or 0 < next_level <= level:
                continue
            next_level = level
        elif line.strip():
            next_level = 0
        cleaned.append(line)
    cleaned.reverse()
    text_out = re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned)).strip()
    tags = list(OrderedDict((t, None) for t in frontmatter_tags(front) + tags))
    return {"front": front, "tasks": tasks, "tags": tags, "links": links, "body": text_out}


class LinkIndex:
    """Every file in the vault, to tell which links point at notes that do not exist yet."""

    def __init__(self, rel_paths: list) -> None:
        lower = [p.lower() for p in rel_paths]
        self.paths = set(lower)
        self.bases = {posixpath.basename(p) for p in lower}
        self.notes = {p[:-3] for p in lower if p.endswith(".md")}
        self.note_bases = {posixpath.basename(p) for p in self.notes}

    def resolves(self, kind: str, target: str, note_dir: str) -> bool:
        t = unquote(target).strip().replace("\\", "/").lower() if kind == "md" else target.strip().replace("\\", "/").lower()
        if not t or re.match(r"^[a-z][a-z0-9+.-]*:", t):
            return True  # A same-note link, or a URL such as https: or obsidian:.
        if kind == "md":
            joined = posixpath.normpath(posixpath.join(note_dir.lower(), t))  # Relative to the note.
            if joined in self.paths or t.lstrip("/") in self.paths:
                return True
        t = t.lstrip("/")
        if t in self.paths or t in self.bases:
            return True  # An attachment or another file named with its extension.
        stem = t[:-3] if t.endswith(".md") else t
        if "/" in stem:
            return stem in self.notes or any(n.endswith("/" + stem) for n in self.notes)
        return stem in self.note_bases


def link_name(kind: str, target: str) -> str:
    """How a link to a missing note is shown: [[Name]], decoded and without .md."""
    name = unquote(target).strip() if kind == "md" else target.strip()
    name = posixpath.basename(name.replace("\\", "/")) if kind == "md" else name
    return name[:-3] if name.lower().endswith(".md") else name


# --------------------------------------------------------------------------------------------
# Gather


def walk_vault(vault: Path):
    """Every file under the vault except dot-folders (.obsidian, .trash, .git), without symlinks."""
    files, dot_folders = [], Counter()
    for root, dirs, names in os.walk(str(vault), followlinks=False):
        rel_root = os.path.relpath(root, str(vault)).replace("\\", "/")
        rel_root = "" if rel_root == "." else rel_root
        for d in list(dirs):
            if d.startswith("."):
                if d != ".obsidian":
                    dot_folders[d] += 1
                dirs.remove(d)
        for name in names:
            if not name.startswith("."):
                files.append((rel_root + "/" + name) if rel_root else name)
    return files, sorted(dot_folders)


def birth_time(stat_result):
    value = getattr(stat_result, "st_birthtime", None)
    if value is None and os.name == "nt":
        value = stat_result.st_ctime  # On Windows, st_ctime is the creation time.
    return value


def cmd_gather(args) -> int:
    home = Path.home()
    now = dt.datetime.now(dt.timezone.utc)
    since, phrase = window_start(args, now)
    if not args.vault:
        raise UsageError("--vault is required: the folder that holds .obsidian. Run `find-vaults` and ask the user to confirm one.")
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UsageError("Vault folder not found: %s" % vault)
    if not (vault / ".obsidian").is_dir():
        raise UsageError("%s has no .obsidian folder, so it is not an Obsidian vault. Pass the folder that holds .obsidian." % vault)
    if not MIN_MAX_CHARS <= args.max_chars <= MAX_MAX_CHARS:
        raise UsageError("--max-chars must be between %d and %d." % (MIN_MAX_CHARS, MAX_MAX_CHARS))
    if not MIN_EXCERPT_CHARS <= args.excerpt_chars <= 10000:
        raise UsageError("--excerpt-chars must be between %d and 10000." % MIN_EXCERPT_CHARS)
    if not 1 <= args.max_items <= 500:
        raise UsageError("--max-items must be between 1 and 500.")
    out_dir = Path(args.out).expanduser().resolve() if args.out else Path(tempfile.gettempdir()) / SLUG / now.astimezone().strftime("%Y%m%d-%H%M%S")
    if is_inside(out_dir, vault):
        raise UsageError("--out must be outside the vault; this skill never writes into the vault.")
    excludes = []
    for value in args.exclude:
        path = Path(value).expanduser()
        if path.is_absolute() and is_inside(path, vault):
            value = os.path.relpath(str(path.resolve()), str(vault))
        if clean_rel(value):
            excludes.append(clean_rel(value).lower())
    exclude_tags = [t.strip().lstrip("#").lower() for t in args.exclude_tag if t.strip().lstrip("#")]

    redact = Redactor(home)
    settings = VaultSettings(vault)
    all_files, dot_folders = walk_vault(vault)
    index = LinkIndex(all_files)
    counts = Counter()
    auto_skipped = Counter()
    since_ts = since.timestamp()
    candidates = []
    for rel in all_files:
        if not rel.lower().endswith(".md"):
            continue
        counts["markdown_files"] += 1
        reason = settings.auto_skip(rel)
        if reason:
            auto_skipped[reason] += 1
            continue
        try:
            stat = os.stat(str(vault / rel))
        except OSError:
            counts["unreadable"] += 1
            continue
        # A note created in the window was also modified in it. Birth time is not used, because
        # copying or restoring a vault resets it and would make every note look new.
        if stat.st_mtime < since_ts:
            continue
        counts["in_window"] += 1
        if path_excluded(rel, excludes):
            counts["excluded_by_path"] += 1
            continue
        candidates.append((rel, stat))

    notes = []
    for rel, stat in candidates:
        try:
            with open(str(vault / rel), encoding="utf-8", errors="replace") as handle:
                parsed = parse_note(handle.read(MAX_READ_CHARS))
        except OSError:
            counts["unreadable"] += 1
            continue
        if exclude_tags and any(t.lower() == x or t.lower().startswith(x + "/") for t in parsed["tags"] for x in exclude_tags):
            counts["excluded_by_tag"] += 1
            continue
        if not parsed["body"] and not parsed["tasks"]:
            counts["skipped_empty"] += 1
            continue
        stem = posixpath.basename(rel)[:-3]
        folder = posixpath.dirname(rel)
        daily = settings.is_daily(stem)
        source = parsed["front"].get("source") or parsed["front"].get("url")
        clipped = isinstance(source, str) and bool(re.match(r"^https?://", source.strip()))
        unresolved = list(OrderedDict((link_name(kind, t), None) for kind, t in parsed["links"] if not index.resolves(kind, t, folder)))
        created = frontmatter_created(parsed["front"])
        born = birth_time(stat)
        if created is None and born is not None and born <= stat.st_mtime + 1:
            created = dt.datetime.fromtimestamp(born, tz=dt.timezone.utc)
        tasks = [redact(one_line(t, MAX_TASK_CHARS)) for t in parsed["tasks"]]
        prefix = []
        if daily:
            prefix.append("Daily note.")
        if clipped:
            prefix.append("Clipped from the web; the text is not the user's own.")
        if parsed["tags"]:
            prefix.append("Tags: " + " ".join("#" + t for t in parsed["tags"][:12]))
        if unresolved:
            prefix.append("Links to notes not written yet: " + ", ".join("[[%s]]" % t for t in unresolved[:8]))
        if len(tasks) > MAX_TASKS:
            prefix.append("Open tasks: %d in this note; the first %d are listed." % (len(tasks), MAX_TASKS))
        notes.append(
            {
                "provider": PROVIDER,
                "id": redact(rel),
                "title": redact(stem),
                "link": redact("obsidian://open?vault=%s&file=%s" % (quote(vault.name, safe=""), quote(rel, safe=""))),
                "kind": "note",
                "container": redact(folder) if folder else "(vault root)",
                "created": iso_utc(created) if created else None,
                "last_edited": iso_utc(stat.st_mtime),
                "author": "other" if clipped else "me",
                "open_tasks": tasks[:MAX_TASKS],
                "excerpt_prefix": redact("\n".join(prefix)),
                "body": redact(parsed["body"]),  # Redacted whole, before any cut, so no half secret survives.
                "_mtime": stat.st_mtime,
                "_tags": parsed["tags"],
                "_unresolved": unresolved,
                "_daily": daily,
            }
        )
    notes.sort(key=lambda n: n["_mtime"], reverse=True)
    over_max = max(len(notes) - args.max_items, 0)
    notes = notes[: args.max_items]
    window = {"since": iso_utc(since), "until": iso_utc(now), "phrase": phrase}

    def digest_notes(kept: list) -> list:
        tag_counts = Counter(t for n in kept for t in OrderedDict((x.lower(), None) for x in n["_tags"]))
        link_counts = Counter(t for n in kept for t in n["_unresolved"])
        recurring = [(t, c) for t, c in tag_counts.most_common(10) if c > 1]
        lines = []
        if recurring:
            lines.append("Tags used in more than one note: %s." % ", ".join("#%s (%d)" % (t, c) for t, c in recurring))
        if link_counts:
            lines.append("Notes linked but not written yet: %s." % ", ".join("[[%s]] (%d)" % (t, c) for t, c in link_counts.most_common(8)))
        return [redact(line) for line in lines]

    summary = OrderedDict(
        [
            ("vault", {"name": vault.name, "path": str(vault)}),
            ("window", {"since": since.astimezone().isoformat(timespec="minutes"), "until": now.astimezone().isoformat(timespec="minutes"), "phrase": phrase}),
            ("counts", OrderedDict([("markdown_files", counts["markdown_files"]), ("in_window", counts["in_window"]), ("kept", 0), ("excluded_by_path", counts["excluded_by_path"]),
                                    ("excluded_by_tag", counts["excluded_by_tag"]), ("skipped_empty", counts["skipped_empty"]), ("dropped_over_max_items", over_max),
                                    ("dropped_over_size_limit", 0), ("unreadable", counts["unreadable"])])),
            ("auto_skipped", OrderedDict([("dot_folders_not_read", dot_folders)] + sorted(auto_skipped.items()))),
        ]
    )
    if not notes:
        summary["error"] = "no notes in the window"
        summary["hint"] = "No note in the vault was edited in this window, or every one was excluded or empty. Offer a wider window, such as --days 30, or fewer exclusions."
        print(json.dumps(summary, indent=2))
        return 3

    entries, digest, excerpt_limit, dropped = fit_to_budget(notes, window, digest_notes, args.max_chars, args.excerpt_chars)
    kept_notes = notes[: len(entries)]
    out_dir.mkdir(parents=True, exist_ok=True)
    write_text(out_dir / "digest.md", digest)
    write_text(out_dir / "items.json", json.dumps(items_document(entries, now, since), indent=2, ensure_ascii=False) + "\n")
    counts_out = summary["counts"]
    counts_out["kept"] = len(entries)
    counts_out["dropped_over_size_limit"] = dropped
    warnings = []
    if counts["in_window"] > MASS_CHANGE_ITEMS or (counts["in_window"] > 20 and counts["in_window"] > counts["markdown_files"] / 2):
        warnings.append(
            "%d of %d notes changed in this window. A sync, a git pull, or a copied vault can touch many notes at once. "
            "Check the table with the user, and offer a shorter window or --exclude before running." % (counts["in_window"], counts["markdown_files"])
        )
    if over_max or dropped:
        warnings.append("%d older notes were left out to stay within --max-items and --max-chars." % (over_max + dropped))
    gather_index = OrderedDict(
        [
            ("skill", SLUG),
            ("vault", {"name": vault.name, "path": str(vault)}),
            ("window", window),
            ("excluded", {"paths": excludes, "tags": exclude_tags}),
            ("limits", {"max_items": args.max_items, "max_chars": args.max_chars, "excerpt_chars": args.excerpt_chars, "excerpt_chars_used": excerpt_limit}),
            ("counts", counts_out),
        ]
    )
    write_text(out_dir / "gather.json", json.dumps(gather_index, indent=2) + "\n")
    folders = Counter(e["container"] for e in entries)
    summary.update(
        [
            ("out_dir", str(out_dir)),
            ("digest", str(out_dir / "digest.md")),
            ("items_file", str(out_dir / "items.json")),
            ("digest_chars", len(digest)),
            ("excerpt_chars_used", excerpt_limit),
            ("folders", [{"container": c, "items": n} for c, n in folders.most_common()]),
            ("table", [{"title": e["title"], "container": e["container"], "last_edited": local_text(parse_iso(e["last_edited"])), "open_tasks": len(e["open_tasks"]), "daily_note": n["_daily"]} for e, n in zip(entries, kept_notes)]),
            ("open_tasks", sum(len(e["open_tasks"]) for e in entries)),
            ("redactions_in_digest", digest_markers("".join(render_item(e) for e in entries))),
            ("warnings", warnings),
        ]
    )
    print(json.dumps(summary, indent=2))
    return 0


# ============================================================================================
# Command line


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:  # A captured stream gets UTF-8; a console keeps its own encoding.
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="obsidian_next_actions.py",
        description="Turn recently edited Obsidian notes into next actions with the Vidbyte Suggestion Agent. Only `run --paid` costs money.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python3 obsidian_next_actions.py find-vaults\n"
            "  python3 obsidian_next_actions.py gather --vault ~/Notes                  # the last 7 days\n"
            "  python3 obsidian_next_actions.py gather --vault ~/Notes --days 30 --exclude Journal\n"
            "  python3 obsidian_next_actions.py run --out <out_dir> --dry-run           # free\n"
            "  python3 obsidian_next_actions.py run --out <out_dir> --paid              # charged; ask the user first\n"
            "  python3 obsidian_next_actions.py report --out <out_dir>\n"
            "  python3 obsidian_next_actions.py feedback --out <out_dir> --idea idea-002 --reject --reason \"not this month\"\n"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    find = sub.add_parser("find-vaults", help="List Obsidian vaults on this machine (free, local).", description="Read Obsidian's vault list (obsidian.json) and scan common folders for folders that hold .obsidian. Prints JSON. Exit 3 means none was found.")
    find.add_argument("--no-scan", action="store_true", help="Read only Obsidian's vault list; do not scan folders.")
    find.add_argument("--root", action="append", default=[], help="Scan this folder instead of the home folder. Repeatable.")

    gather = sub.add_parser("gather", help="Read and redact recently edited notes into items.json and digest.md (free, read-only).", description="Read the Markdown notes edited in a window, extract open tasks, tags, and links to notes not written yet, redact them, and write items.json, digest.md, and gather.json into a new folder outside the vault. Prints a JSON summary. Exit 3 means no note matched.")
    gather.add_argument("--vault", help="The vault folder: the folder that holds .obsidian.")
    gather.add_argument("--days", type=int, default=DEFAULT_DAYS, help="Window length in days, ending now (default: %d)." % DEFAULT_DAYS)
    gather.add_argument("--since", help="Window start as a local date, such as 2026-10-04. Overrides --days.")
    gather.add_argument("--exclude", action="append", default=[], help="A vault-relative folder, note, or glob never to read or send, such as Journal. Repeatable.")
    gather.add_argument("--exclude-tag", action="append", default=[], help="Leave out notes with this tag (and its nested tags), such as private. Repeatable.")
    gather.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS, help="Keep at most this many of the newest notes (default: %d)." % DEFAULT_MAX_ITEMS)
    gather.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Size limit for digest.md in characters (default: %d, at most %d)." % (DEFAULT_MAX_CHARS, MAX_MAX_CHARS))
    gather.add_argument("--excerpt-chars", type=int, default=DEFAULT_EXCERPT_CHARS, help="Longest excerpt per note before shrinking to fit (default: %d)." % DEFAULT_EXCERPT_CHARS)
    gather.add_argument("--out", help="Output folder outside the vault (default: a new folder under the system temp folder).")

    run = sub.add_parser("run", help="Run vidbyte-cli agents suggest run on the digest (--dry-run is free; --paid is charged).", description="--dry-run builds the request, saves it as request.json, and checks it with `vidbyte-cli --json --no-input agents suggest run ... --dry-run`. --paid replays that exact request once, after a passing dry run and with digest.md unchanged. Prints a JSON summary and exits with vidbyte-cli's exit code.")
    run.add_argument("--out", required=True, help="The out_dir that gather printed.")
    run.add_argument("--dry-run", action="store_true", help="Check the full request for free: no model, no admission, no charge.")
    run.add_argument("--paid", action="store_true", help="The charged run. Use only after the user agreed to the cost.")
    run.add_argument("--idempotency-key", help="With --paid: the key of an earlier attempt, to recover its admission without paying twice.")
    run.add_argument("--trajectory", action="append", default=[], help="With --dry-run: the user's current focus, in their words. At most 2.")
    run.add_argument("--constraint", action="append", default=[], help="With --dry-run: a limit the user stated, such as the time they have. At most 3.")
    run.add_argument("--avoid", action="append", default=[], help="With --dry-run: a direction the user does not want proposed. At most 3.")
    run.add_argument("--goal", help="With --dry-run: replace the default goal, only when the user asked something narrower.")
    run.add_argument("--project", help="With --dry-run: the suggestion project (default: %s)." % PROJECT_KEY)

    report = sub.add_parser("report", help="Group the next actions by horizon and tie each to its notes (free, local).", description="Read <out>/result.json and items.json, print the next actions grouped by now, next, and later with the notes each one names, and save the text as report.md.")
    report.add_argument("--out", required=True, help="The out_dir that holds result.json.")
    report.add_argument("--result", help="A result file other than <out>/result.json.")

    feedback = sub.add_parser("feedback", help="Record the user's explicit accept or reject of one idea (free, local).", description="Copy the exact title of one idea from <out>/result.json into `vidbyte-cli --json agents suggest feedback accept|reject`.")
    feedback.add_argument("--out", required=True, help="The out_dir that holds result.json.")
    feedback.add_argument("--idea", required=True, help="The idea id, such as idea-002.")
    verdict = feedback.add_mutually_exclusive_group(required=True)
    verdict.add_argument("--accept", action="store_true", help="The user said they want this idea.")
    verdict.add_argument("--reject", action="store_true", help="The user said no to this idea.")
    feedback.add_argument("--reason", help="The user's own reason, if they gave one.")
    feedback.add_argument("--project", help="The suggestion project (default: %s)." % PROJECT_KEY)

    args = parser.parse_args(argv)
    commands = {"find-vaults": cmd_find_vaults, "gather": cmd_gather, "run": cmd_run, "report": cmd_report, "feedback": cmd_feedback}
    if args.command not in commands:
        parser.print_help()
        return 2
    try:
        return commands[args.command](args)
    except UsageError as error:
        return usage_error_exit(str(error))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
