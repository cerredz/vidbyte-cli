"""Finds every harness store, merges their sessions newest first, and keeps the interactive ones."""

from __future__ import annotations

import argparse
import datetime as dt
import os
from collections import Counter
from pathlib import Path

from .claude_code import ClaudeCodeReader
from .codex import CodexReader
from .gemini import GeminiReader
from .model import Clock
from .opencode import OpenCodeReader

MAX_SESSIONS_LIMIT = 500
OPEN_FACTOR = 3  # Open at most 3 x max_sessions candidates, so a store full of skipped sessions stays fast.
CURRENT_SESSION_VARS = ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID")
HARNESSES = ("claude", "codex", "opencode", "gemini", "qwen")


class OptionTypes:
    """argparse type functions, so bad values exit 2 with a usage message before anything is read."""

    @staticmethod
    def max_sessions(value: str) -> int:
        # Accepts an integer from 1 to 500.
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be a whole number from 1 to %d" % MAX_SESSIONS_LIMIT)
        if not 1 <= number <= MAX_SESSIONS_LIMIT:
            raise argparse.ArgumentTypeError("must be from 1 to %d, got %d" % (MAX_SESSIONS_LIMIT, number))
        return number

    @staticmethod
    def days(value: str) -> float:
        # Accepts a positive number of days, fractions allowed.
        try:
            number = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be a number of days greater than 0")
        if number <= 0:
            raise argparse.ArgumentTypeError("must be greater than 0")
        return number

    @staticmethod
    def since(value: str):
        # Parses a local date or date-time (2026-10-01, 2026-10-01T09:00) into aware UTC.
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(value.strip(), fmt).astimezone(dt.timezone.utc)
            except ValueError:
                continue
        raise argparse.ArgumentTypeError("must look like 2026-10-01 or 2026-10-01T09:00 (local time)")


class CollectOptions:
    """Which stores to read, how many sessions to keep, and which sessions to leave out."""

    def __init__(self, max_sessions: int = MAX_SESSIONS_LIMIT, since=None, project_dirs=(), claude_dirs=(), codex_home=None, opencode_dir=None, gemini_dir=None, qwen_dir=None, disabled=(), exclude_sessions=(), include_current: bool = False) -> None:
        # Holds already-validated options; use from_args to build them from the standard flags.
        self.max_sessions = max_sessions
        self.since = since
        self.project_dirs = [str(p) for p in project_dirs]
        self.claude_dirs = [str(p) for p in claude_dirs]
        self.codex_home = codex_home
        self.opencode_dir = opencode_dir
        self.gemini_dir = gemini_dir
        self.qwen_dir = qwen_dir
        self.disabled = set(disabled)
        self.exclude_sessions = set(exclude_sessions)
        self.include_current = include_current

    @staticmethod
    def add_arguments(parser: argparse.ArgumentParser, default_max_sessions: int = MAX_SESSIONS_LIMIT, default_days=None) -> None:
        # Adds the standard session-selection flags shared by every session-reading skill.
        group = parser.add_argument_group("which sessions to read")
        group.add_argument("--max-sessions", type=OptionTypes.max_sessions, default=default_max_sessions, help="Keep at most this many of the most recent interactive sessions, across harnesses (1-500, default %d)." % default_max_sessions)
        group.add_argument("--days", type=OptionTypes.days, default=default_days, help="Only sessions active in the last N days%s." % (" (default %s)" % default_days if default_days else " (default: no limit)"))
        group.add_argument("--since", type=OptionTypes.since, default=None, help="Only sessions active since this local date or time, e.g. 2026-10-01 or 2026-10-01T09:00. Overrides --days.")
        group.add_argument("--project-dir", action="append", default=[], help="Only sessions whose working folder is this folder or inside it. Repeat for several.")
        group.add_argument("--claude-dir", action="append", default=[], help="Also read this Claude Code config folder (one holding projects/). Repeat for several.")
        group.add_argument("--codex-home", default=None, help="Codex home folder (default $CODEX_HOME or ~/.codex).")
        group.add_argument("--opencode-dir", default=None, help="OpenCode data folder (default $XDG_DATA_HOME/opencode or ~/.local/share/opencode).")
        group.add_argument("--gemini-dir", default=None, help="Gemini CLI folder (default ~/.gemini).")
        group.add_argument("--qwen-dir", default=None, help="Qwen Code folder (default ~/.qwen).")
        for name in HARNESSES:
            group.add_argument("--no-%s" % name, action="store_true", help="Do not read %s sessions." % name)
        group.add_argument("--exclude-session", action="append", default=[], help="Leave out the session with this id. Repeat for several.")
        group.add_argument("--include-current", action="store_true", help="Keep the session this command runs in (left out by default).")

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "CollectOptions":
        # Builds options from parsed flags; --since wins over --days.
        since = args.since
        if since is None and args.days:
            since = Clock.now() - dt.timedelta(days=args.days)
        return cls(
            max_sessions=args.max_sessions, since=since, project_dirs=args.project_dir, claude_dirs=args.claude_dir,
            codex_home=args.codex_home, opencode_dir=args.opencode_dir, gemini_dir=args.gemini_dir, qwen_dir=args.qwen_dir,
            disabled={name for name in HARNESSES if getattr(args, "no_%s" % name, False)},
            exclude_sessions=args.exclude_session, include_current=args.include_current,
        )


class CollectResult:
    """The kept sessions plus an account of what was read, skipped, and not read."""

    def __init__(self, since, until) -> None:
        # Starts an empty result for the window [since, until].
        self.sessions: list = []
        self.read: list = []
        self.skipped: Counter = Counter()
        self.unreadable: Counter = Counter()
        self.other_claude_dirs_not_read: list = []
        self.not_read: list = []
        self.notes: list = []
        self.since = since
        self.until = until
        self.max_sessions = MAX_SESSIONS_LIMIT

    def by_harness(self) -> Counter:
        # Counts kept sessions per harness.
        return Counter(s.harness for s in self.sessions)

    @staticmethod
    def _redacted_entry(entry: dict, redactor) -> dict:
        # One read[] entry with its root folder and any error message redacted.
        redacted = dict(entry, root=redactor(entry["root"]))
        if "error" in entry:
            redacted["error"] = redactor(entry["error"])
        return redacted

    def summary(self, redactor) -> dict:
        # A JSON-ready, redacted account of the collection for gather output and sessions.json.
        return {
            "since": Clock.iso(self.since) or None,
            "until": Clock.iso(self.until),
            "max_sessions": self.max_sessions,
            "kept": len(self.sessions),
            "kept_by_harness": dict(self.by_harness()),
            "prompts": sum(s.prompt_count for s in self.sessions),
            "injected_prompts_dropped": sum(s.injected_prompts for s in self.sessions),
            "read": [self._redacted_entry(entry, redactor) for entry in self.read],
            "skipped": dict(self.skipped),
            "unreadable": dict(self.unreadable),
            "other_claude_dirs_not_read": [redactor(p) for p in self.other_claude_dirs_not_read],
            "not_read": list(self.not_read),
            "notes": [redactor(n) for n in self.notes],
        }


class SessionCollector:
    """Builds the readers, opens candidates newest first, and keeps up to max_sessions interactive sessions."""

    NOT_READ_STORES = (
        ("Cursor", (".cursor", "chats")),
        ("GitHub Copilot CLI", (".copilot", "session-state")),
    )
    VSCODE_EXTENSIONS = (
        ("Cline", "saoudrizwan.claude-dev"),
        ("Roo Code", "rooveterinaryinc.roo-cline"),
        ("Kilo Code", "kilocode.kilo-code"),
    )

    def __init__(self, options: CollectOptions, home: Path = None) -> None:
        # Resolves every store folder from flags, then environment, then defaults under home.
        self.options = options
        self.home = Path(home) if home is not None else Path.home()
        self.readers = self._build_readers()

    def collect(self) -> CollectResult:
        # Reads candidates newest first until the cap is reached, counting every skip by reason.
        result = CollectResult(self.options.since, Clock.now())
        result.max_sessions = self.options.max_sessions
        candidates = self._gather_candidates(result)
        excluded = self._excluded_ids()
        budget = self.options.max_sessions * OPEN_FACTOR
        stats: Counter = Counter()
        opened = 0
        for candidate in candidates:
            if len(result.sessions) >= self.options.max_sessions:
                break
            if opened >= budget:
                result.skipped["not_opened_cap"] += len(candidates) - opened
                break
            opened += 1
            session = self._open(candidate, stats, result)
            reason = self._skip_reason(session, excluded)
            if reason:
                result.skipped[reason] += 1
                continue
            result.sessions.append(session)
        for reader in self.readers.values():
            if hasattr(reader, "close"):
                reader.close()
            result.notes.extend(getattr(reader, "notes", []))
        result.unreadable.update(stats)
        result.sessions.sort(key=lambda s: s.last_ts or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
        self._count_kept(result)
        result.other_claude_dirs_not_read = self._other_claude_dirs()
        result.not_read = self._stores_not_read()
        return result

    def _build_readers(self) -> dict:
        # Creates one reader per enabled harness (both Gemini-format harnesses share GeminiReader).
        env = os.environ
        readers: dict = {}
        if "claude" not in self.options.disabled:
            default = Path(env["CLAUDE_CONFIG_DIR"]).expanduser() if env.get("CLAUDE_CONFIG_DIR") else self.home / ".claude"
            readers["claude"] = ClaudeCodeReader([default] + [Path(p) for p in self.options.claude_dirs])
        if "codex" not in self.options.disabled:
            readers["codex"] = CodexReader(Path(self.options.codex_home or env.get("CODEX_HOME") or self.home / ".codex"))
        if "opencode" not in self.options.disabled:
            xdg = Path(env["XDG_DATA_HOME"]) / "opencode" if env.get("XDG_DATA_HOME") else self.home / ".local" / "share" / "opencode"
            readers["opencode"] = OpenCodeReader(Path(self.options.opencode_dir) if self.options.opencode_dir else xdg)
        if "gemini" not in self.options.disabled:
            readers["gemini"] = GeminiReader(Path(self.options.gemini_dir) if self.options.gemini_dir else self.home / ".gemini", "gemini-cli")
        if "qwen" not in self.options.disabled:
            readers["qwen"] = GeminiReader(Path(self.options.qwen_dir) if self.options.qwen_dir else self.home / ".qwen", "qwen-code")
        return readers

    def _gather_candidates(self, result: CollectResult) -> list:
        # Lists every reader's candidates, records one read[] entry per root, and sorts newest first.
        everything = []
        for reader in self.readers.values():
            listing_error = ""
            try:
                found = reader.candidates(self.options.since)
            except Exception as exc:  # A store with an unexpected layout must not stop the others.
                found = []
                listing_error = "candidates not listed: %s" % exc
                result.notes.append("%s %s" % (reader.harness, listing_error))
            for root in reader.roots:
                mine = [c for c in found if Path(c.root) == Path(root)]
                entry = {"harness": reader.harness, "root": str(root), "status": "read" if Path(root).exists() else "missing", "candidates": len(mine), "kept": 0}
                error = listing_error or getattr(reader, "root_errors", {}).get(str(root), "")
                if error:
                    entry.update(status="error", error=error)
                result.read.append(entry)
            everything.extend(found)
        floor = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        everything.sort(key=lambda c: c.last_activity or floor, reverse=True)
        return everything

    def _open(self, candidate, stats: Counter, result: CollectResult):
        # Reads one candidate with its reader, turning any unexpected error into an unreadable count.
        reader = next(r for r in self.readers.values() if r.harness == candidate.harness and Path(candidate.root) in [Path(x) for x in r.roots])
        try:
            return reader.read(candidate, self.options.since, stats)
        except Exception as exc:  # One malformed session must not stop the collection.
            result.unreadable["%s_errors" % candidate.harness] += 1
            if len(result.notes) < 20:
                result.notes.append("%s session not read: %s" % (candidate.harness, type(exc).__name__))
            return None

    def _skip_reason(self, session, excluded: dict) -> str:
        # Returns why a session is left out, or "" to keep it.
        if session is None:
            return "unreadable"
        if session.skip_reason:
            return session.skip_reason
        if session.session_id in excluded:
            return excluded[session.session_id]
        if self.options.project_dirs and not self._inside_project(session):
            return "outside_project"
        if not session.turns:
            return "no_prompts"
        if self.options.since is not None:
            stamps = [t.ts for t in session.turns if t.ts is not None] or [session.last_ts]
            if all(s is None or s < self.options.since for s in stamps):
                return "outside_window"
        return ""

    def _excluded_ids(self) -> dict:
        # Maps excluded session ids to their reason: the running session (unless kept) and --exclude-session ids.
        excluded = {sid: "excluded" for sid in self.options.exclude_sessions}
        if not self.options.include_current:
            for name in CURRENT_SESSION_VARS:
                value = os.environ.get(name, "").strip()
                if value:
                    excluded[value] = "current_session"
        return excluded

    def _inside_project(self, session) -> bool:
        # True when any working folder of the session is a --project-dir or inside one.
        wanted = [self._norm(p) for p in self.options.project_dirs]
        for cwd in session.cwds:
            folder = self._norm(cwd)
            if any(folder == w or folder.startswith(w + "/") for w in wanted):
                return True
        return False

    def _count_kept(self, result: CollectResult) -> None:
        # Fills read[].kept with the number of kept sessions from each root.
        for entry in result.read:
            root = self._norm(entry["root"])
            entry["kept"] = sum(1 for s in result.sessions if s.harness == entry["harness"] and self._norm(s.source).startswith(root))

    def _other_claude_dirs(self) -> list:
        # Lists ~/.claude* folders with a projects/ folder that this run did not read.
        reader = self.readers.get("claude")
        if reader is None:
            return []  # --no-claude: the user chose not to read Claude Code at all.
        read = {self._norm(str(r)) for r in reader.roots}
        found = []
        try:
            for path in sorted(self.home.glob(".claude*")):
                if path.is_dir() and (path / "projects").is_dir() and self._norm(str(path)) not in read:
                    found.append(str(path))
        except OSError:
            pass
        return found

    def _stores_not_read(self) -> list:
        # Names coding-agent stores that exist on this machine but have no reader yet.
        found = [label for label, parts in self.NOT_READ_STORES if self.home.joinpath(*parts).exists()]
        for base in self._vscode_storage_dirs():
            for label, extension in self.VSCODE_EXTENSIONS:
                if (base / extension).is_dir() and label not in found:
                    found.append(label)
        return found

    def _vscode_storage_dirs(self) -> list:
        # VS Code globalStorage folders on Windows, macOS, and Linux.
        bases = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            bases.append(Path(appdata) / "Code" / "User" / "globalStorage")
        bases.append(self.home / "Library" / "Application Support" / "Code" / "User" / "globalStorage")
        bases.append(self.home / ".config" / "Code" / "User" / "globalStorage")
        return [b for b in bases if b.is_dir()]

    @staticmethod
    def _norm(path: str) -> str:
        # Normalizes a path for comparison: forward slashes, no trailing slash, case-folded on Windows.
        value = str(path or "").replace("\\", "/").rstrip("/")
        return value.lower() if os.name == "nt" else value
