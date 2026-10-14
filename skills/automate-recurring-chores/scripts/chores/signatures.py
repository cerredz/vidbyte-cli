"""Turn one session into the features chore detection compares: opener terms, resources, and run facts.

Every feature here is a normalized, comparable form (stemmed words, folder-relative paths with dates
replaced, command signatures, web domains, MCP tool names). Raw prompt text stays on the Turn; the
digest writer redacts whatever it quotes. Credential values are never captured: only names are.
"""

from __future__ import annotations

import re
import shlex
from collections import OrderedDict
from urllib.parse import urlparse

from pathlib import Path

from sessionkit.model import ActionText
from sessionkit.redaction import Redactor

STOPWORDS = set(
    """
    a an the and or but if then else of to in on at by for with from into onto over under as is are was
    were be been being am do does did done doing have has had having it its it's this that these those
    there here i i'm me my mine you you're your yours we our us they them their he she his her him please
    pls plz just also so very really can could would should will shall may might must let lets let's what
    which who whom whose when where why how all any some each every no not dont don't ok okay yes yeah now
    again still only even more most much many one up out about after before same like make sure want need
    get got go going thing things something stuff way lot bit sure thanks thank hey hi hello can't won't
    isn't aren't didn't doesn't it's that's there's than time times today tomorrow yesterday new last next
    first second third help hand done start run use using used look check see show tell give put take keep
    aren isn didn doesn don won wasn weren couldn shouldn wouldn haven hasn hadn ll ve re im ive youre thats
    below above through great good nice
    pasted image text line lines file files
    """.split()
)
MONTHS = {
    "january", "february", "march", "april", "june", "july", "august", "september", "october", "november",
    "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
}
CADENCE_WORDS = {"daily", "weekly", "monthly", "quarterly", "biweekly", "fortnightly", "yearly", "annual", "annually", "weekday", "weekdays"}
RELATIVE_RE = re.compile(r"\b(?:this|last|next|previous) (?:week|month|quarter|year)\b|\b(?:month|quarter|year)[- ]end\b|\bend of (?:the )?(?:month|quarter|year)\b")
URL_RE = re.compile(r"https?://[^\s<>\"'`)\]]+")
PLACEHOLDER_RE = re.compile(r"\[(?:Pasted text|Image|Pasted image)[^\]]{0,40}\]", re.I)
DATE_RE = re.compile(r"\b(?:19|20)\d{2}[-/._]\d{1,2}(?:[-/._]\d{1,2})?\b|\b\d{1,2}[-/.]\d{1,2}[-/.](?:19|20)?\d{2}\b|\b(?:19|20)\d{6}\b")
YEAR_RE = re.compile(r"\b(?:fy)?(?:19|20)\d{2}\b")
QUARTER_RE = re.compile(r"\bq[1-4]\b|\b(?:1st|2nd|3rd|4th|first|second|third|fourth) quarter\b")
ORDINAL_RE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\b")
WORD_RE = re.compile(r"/?[a-z0-9][a-z0-9+#]*(?:[._-][a-z0-9+#]+)*")
CONTINUATION_RE = re.compile(
    r"^\s*(?:please\s+|ok(?:ay)?[,.]?\s+|now\s+)?(?:continue|keep going|resume|carry on|go on|pick up where|where (?:were|did) we|"
    r"where we left off|proceed|next step|let'?s continue|finish (?:up|the rest|what)|same session)\b",
    re.I,
)
SKILL_MARKER_RE = re.compile(r"Base directory for this skill:")
SKILL_HEADING_RE = re.compile(r"^\s*#\s+([A-Za-z0-9][\w.-]{0,63})")
GO_AHEAD_RE = re.compile(r"^\s*(?:yes|yep|yeah|ok(?:ay)?|sure|go ahead|do it|lgtm|sounds good|thanks|thank you)\b[\s.!]*$", re.I)
CREDENTIAL_NAME_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*_(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|PAT|CREDENTIALS?)\b|\b(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN)\b")
CREDENTIAL_MENTIONS = OrderedDict([
    (re.compile(r"(?<![\w.])\.env(?:\.[a-z]+)?\b", re.I), ".env file"),
    (re.compile(r"\bcredentials\.json\b", re.I), "credentials.json"),
    (re.compile(r"\bservice[- ]account\b", re.I), "service account key"),
    (re.compile(r"\.pem\b", re.I), ".pem key file"),
    (re.compile(r"\bkeychain\b", re.I), "keychain entry"),
    (re.compile(r"\b1password\b|\bop read\b", re.I), "1Password item"),
    (re.compile(r"\bbitwarden\b|\bbw get\b", re.I), "Bitwarden item"),
    (re.compile(r"\bgh auth\b", re.I), "GitHub CLI login (gh auth)"),
    (re.compile(r"\baws (?:configure|sso login)\b|\baws_profile\b", re.I), "AWS credentials"),
    (re.compile(r"\bgcloud auth\b", re.I), "Google Cloud login (gcloud auth)"),
    (re.compile(r"\bapi[- ]key\b", re.I), "an API key"),
    (re.compile(r"\bapp password\b", re.I), "an app password"),
    (re.compile(r"\boauth\b", re.I), "an OAuth login"),
])
EXTERNAL_PROMPT_RE = re.compile(
    r"\b(?P<verb>send|email|e-mail|mail|post|upload|submit|pay|transfer|publish|deploy|tweet|invoice)\b"
    r"(?:\s+(?:it|them|this|that|the|my|our|out|to|off|a|an)\b)"
    r"|\b(?P<filing>file)\s+(?:(?:my|our|the|a|an|this|that)\s+)?(?:\w+\s+)?(?:tax(?:es)?|returns?|claims?|expense reports?|forms?|1099s?|w-?2s?|vat|filings?)\b",
    re.I,
)
EXTERNAL_VERBS = {"email": "send", "e-mail": "send", "mail": "send", "tweet": "post", "invoice": "send an invoice", "file": "file a form or return", "transfer": "transfer money"}
EXTERNAL_TOOL_RE = re.compile(r"send|post|reply|publish|upload|submit|pay|transfer|create_invoice|create_payment|create_charge|draft_and_send|forward|share", re.I)
DATA_EXTS = {
    "csv", "tsv", "xlsx", "xls", "xlsm", "ods", "numbers", "json", "ndjson", "jsonl", "pdf", "ofx", "qfx", "qif", "txt",
    "md", "log", "xml", "parquet", "sqlite", "db", "ics", "eml", "mbox", "zip", "docx", "pptx", "html", "yaml", "yml", "toml", "lock",
}
DATA_FILE_EXTS = {"csv", "tsv", "xlsx", "xls", "xlsm", "ods", "numbers", "pdf", "ofx", "qfx", "qif", "parquet", "sqlite", "db", "ics", "eml", "mbox", "jsonl", "ndjson"}
GENERIC_PROGRAMS = {
    "ls", "cat", "grep", "rg", "egrep", "find", "fd", "head", "tail", "sed", "awk", "wc", "echo", "pwd", "cd", "dir", "type",
    "get-childitem", "select-string", "get-content", "set-location", "which", "where", "true", "false", "test", "sort", "uniq",
    "cut", "tr", "xargs", "tee", "less", "more", "clear", "cls", "printf", "date", "sleep", "mkdir", "touch", "cp", "mv",
    "rm", "chmod", "stat", "file", "du", "df", "tree", "jq", "diff", "basename", "dirname", "realpath", "export", "set",
    "source", ".", "pushd", "popd", "write-output", "write-host", "out-file", "test-path", "copy-item", "move-item",
    "remove-item", "new-item", "start-sleep", "measure-object", "sort-object", "where-object", "select-object", "foreach-object", "nl",
}
WRAPPERS = {"sudo", "env", "time", "nohup", "command", "exec", "caffeinate"}
SUBCOMMAND_DEPTH = {
    "git": 1, "npm": 1, "pnpm": 1, "yarn": 1, "bun": 1, "cargo": 1, "go": 1, "make": 1, "pip": 1, "pip3": 1, "brew": 1,
    "npx": 1, "bunx": 1, "deno": 1, "rake": 1, "bundle": 1, "mvn": 1, "gradle": 1, "terraform": 1, "dotnet": 1, "poetry": 1,
    "uv": 1, "helm": 1, "gh": 2, "aws": 2, "gcloud": 2, "az": 2, "stripe": 2, "kubectl": 1, "docker": 1, "firebase": 1,
    "supabase": 2, "heroku": 1, "flyctl": 1, "fly": 1, "vercel": 1, "wrangler": 1, "glab": 2,
}
INTERPRETERS = {"python", "python3", "py", "node", "ruby", "perl", "bash", "sh", "zsh", "pwsh", "powershell", "ts-node", "tsx", "rscript", "php"}
DATA_CLIS = {"gh", "glab", "stripe", "aws", "gcloud", "az", "psql", "mysql", "sqlite3", "bq", "duckdb", "curl", "wget", "firebase", "supabase", "kubectl", "snowsql"}
EXTERNAL_COMMANDS = (
    (re.compile(r"^git push\b"), "git push"),
    (re.compile(r"^gh (?:pr (?:create|merge|comment|review|close|edit)|release create|issue (?:create|comment|close|edit)|gist create)\b"), None),
    (re.compile(r"^gh api\b.*(?:-X|--method)\s*(?:POST|PATCH|PUT|DELETE)", re.I), "gh api write"),
    (re.compile(r"^(?:npm|pnpm|yarn|cargo|gem) (?:publish|push)\b"), None),
    (re.compile(r"^twine upload\b"), "twine upload"),
    (re.compile(r"^docker push\b"), "docker push"),
    (re.compile(r"^curl\b.*(?:-X\s*(?:POST|PUT|PATCH|DELETE)|--request\s+(?:POST|PUT|PATCH|DELETE)|\s-d\s|--data|\s-F\s|--form|\s-T\s|--upload-file)", re.I), "curl request that sends data"),
    (re.compile(r"^wget\b.*--post-(?:data|file)", re.I), "wget request that sends data"),
    (re.compile(r"^(?:sendmail|mail|mailx|mutt|msmtp)\b"), "send email"),
    (re.compile(r"^stripe (?:\S+ )?(?:create|update|delete|pay|finalize|send|void)\b"), "stripe write"),
    (re.compile(r"^aws (?:s3 (?:cp|sync|mv)\b.*s3://|ses send|sns publish)"), "aws upload or send"),
    (re.compile(r"^(?:vercel (?:deploy|--prod)|firebase deploy|fly(?:ctl)? deploy|heroku (?:deploy|releases)|wrangler (?:deploy|publish)|gcloud \S+ deploy|kubectl (?:apply|delete)|terraform apply|helm (?:install|upgrade))\b"), "deploy"),
)
DESTRUCTIVE_COMMANDS = re.compile(r"^(?:rm|del|rmdir|remove-item|git (?:reset|clean|push|checkout|rebase)|format|mkfs|dd|shutdown|reboot)\b", re.I)


class TermExtractor:
    """Stemmed content words, with dates, months, years, and quarter labels removed so runs of one chore match."""

    MASK = Redactor(Path("/"))  # Secret and email rules only: a credential value never becomes a term.

    def terms(self, text: object) -> frozenset:
        # Returns the stemmed content terms of text; period values and secrets never become terms.
        lowered = self._strip_periods(self.MASK(str(text or "")).lower())
        out = set()
        for word in WORD_RE.findall(lowered):
            word = word.strip("._-")
            if word.startswith("/") and len(word) > 2:
                out.add(word)
                continue
            word = word.lstrip("/")
            if len(word) < 2 or word in STOPWORDS or word in MONTHS or word.isdigit() or ORDINAL_RE.fullmatch(word):
                continue
            out.add(self.stem(word))
        return frozenset(out)

    def period_mentions(self, text: object) -> list:
        # Lists which kinds of period the text names: month, year, date, quarter, cadence word, or relative period.
        lowered = str(text or "").lower()
        found = []
        words = set(WORD_RE.findall(lowered))
        checks = (
            ("month", bool(words & MONTHS)), ("date", bool(DATE_RE.search(lowered))), ("year", bool(YEAR_RE.search(lowered))),
            ("quarter", bool(QUARTER_RE.search(lowered))), ("cadence", bool(words & CADENCE_WORDS)), ("relative", bool(RELATIVE_RE.search(lowered))),
        )
        for name, hit in checks:
            if hit:
                found.append(name)
        return found

    @staticmethod
    def stem(word: str) -> str:
        # A light suffix stemmer: -ing, -ies, -ed, and plural -s.
        if len(word) > 5 and word.endswith("ing"):
            return word[:-3]
        if len(word) > 4 and word.endswith("ies"):
            return word[:-3] + "y"
        if len(word) > 4 and word.endswith("ed"):
            return word[:-2]
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            return word[:-1]
        return word

    @staticmethod
    def _strip_periods(text: str) -> str:
        # Removes URLs, paste placeholders, dates, years, and quarter labels.
        for pattern in (URL_RE, PLACEHOLDER_RE, DATE_RE, QUARTER_RE, YEAR_RE):
            text = pattern.sub(" ", text)
        return text


class PathNormalizer:
    """Folder-relative, lower-case paths with dates, months, quarters, and numbers replaced by placeholders."""

    SEGMENT_MONTH_RE = re.compile(r"(?<![a-z])(?:%s)(?![a-z])" % "|".join(sorted(MONTHS, key=len, reverse=True)))
    PATH_DATE_RE = re.compile(r"(?:19|20)\d{2}(?:[-_.]?\d{2}){1,2}|\d{2}[-_.]\d{2}[-_.](?:19|20)\d{2}")
    PATH_QUARTER_RE = re.compile(r"(?<![a-z])q[1-4](?![0-9])")
    NUMBER_RE = re.compile(r"(?<![a-z0-9<])\d{2,}(?![a-z0-9>])")

    def normalize(self, path: object, cwd: object = "") -> str:
        # Returns the comparable form of path, relative to cwd when it is inside it, or "" for no path.
        raw = str(path or "").strip().strip("\"'`")
        if not raw or raw.startswith("(") or raw.endswith("\u2026"):
            return ""  # A path sessionkit cut at its action length limit names no real file.
        value = re.sub(r"/+", "/", raw.replace("\\", "/"))
        base = re.sub(r"/+", "/", str(cwd or "").replace("\\", "/")).rstrip("/")
        if base and (value.lower() == base.lower() or value.lower().startswith(base.lower() + "/")):
            value = value[len(base) + 1:] or "."
        value = value.lower()
        if value.startswith("./"):
            value = value[2:]
        value = self.PATH_DATE_RE.sub("<date>", value)
        value = self.SEGMENT_MONTH_RE.sub("<month>", value)
        value = self.PATH_QUARTER_RE.sub("<quarter>", value)
        return self.NUMBER_RE.sub("<n>", value)

    @staticmethod
    def parent(path: str) -> str:
        # The folder part of a normalized path, or "" when the path has no folder.
        return path.rsplit("/", 1)[0] if "/" in path.strip("/") else ""

    @staticmethod
    def extension(path: str) -> str:
        # The lower-case extension without the dot, or "".
        name = path.rsplit("/", 1)[-1]
        return name.rsplit(".", 1)[-1] if "." in name.strip(".") else ""


class CommandInfo:
    """What one shell command tells about a run: its signature, data files it read, and outside actions."""

    __slots__ = ("signature", "program", "reads", "external")

    def __init__(self, signature: str, program: str, reads: list, external: list) -> None:
        # Holds the parsed command facts.
        self.signature = signature
        self.program = program
        self.reads = reads
        self.external = external


class CommandSignature:
    """Reduces a shell command to a stable signature such as `npm outdated` or `python scripts/report.py`."""

    SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
    ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

    def __init__(self, paths: PathNormalizer) -> None:
        # Uses paths to normalize script and data-file arguments.
        self.paths = paths

    def of(self, command: object, cwd: str = "") -> CommandInfo:
        # Parses a command line; the signature is "" for generic commands such as ls or cat.
        text = " ".join(str(command or "").split())
        segments = [s for s in self.SPLIT_RE.split(text) if s.strip()]
        signature, program, reads, external = "", "", [], []
        for segment in segments:
            tokens = self._tokens(segment)
            if not tokens:
                continue
            seg_program, seg_signature = self._signature(tokens, cwd)
            reads.extend(self._data_reads(tokens[1:], cwd))
            label = self._external(" ".join(tokens))
            if label:
                external.append(label)
            if not signature and seg_signature:
                signature, program = seg_signature, seg_program
            elif not program and seg_program not in GENERIC_PROGRAMS:
                program = seg_program
        return CommandInfo(signature, program, reads, external)

    def is_destructive(self, signature: str) -> bool:
        # True for commands a routine must not be pre-approved to run (deletes, resets, pushes).
        return bool(DESTRUCTIVE_COMMANDS.match(signature or ""))

    def _tokens(self, segment: str) -> list:
        # Splits a segment like a POSIX shell, dropping wrappers (sudo, env) and NAME=value assignments.
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            tokens = segment.split()
        while tokens and (self.ASSIGN_RE.match(tokens[0]) or tokens[0].lower() in WRAPPERS):
            tokens = tokens[1:]
        if len(tokens) >= 2 and tokens[0].lower() in ("uv", "poetry", "pipenv", "pdm") and tokens[1] == "run":
            tokens = tokens[2:]
        return tokens

    def _signature(self, tokens: list, cwd: str):
        # Returns (program, signature) for one segment's tokens.
        first = tokens[0].replace("\\", "/")
        program = re.sub(r"\.(?:exe|cmd|bat)$", "", first.rsplit("/", 1)[-1].lower())
        if program in GENERIC_PROGRAMS:
            return program, ""
        if "/" in first or re.search(r"\.(?:sh|ps1|py|rb|js|ts|mjs)$", first.lower()):
            return program, self.paths.normalize(first, cwd)
        rest = [t for t in tokens[1:] if not t.startswith("-")]
        if program in INTERPRETERS:
            if "-m" in tokens[1:]:
                index = tokens.index("-m")
                return program, "%s -m %s" % (program, tokens[index + 1] if index + 1 < len(tokens) else "")
            if "-c" in tokens[1:] or "-Command" in tokens[1:] or "-command" in tokens[1:]:
                return program, "%s (inline code)" % program
            return program, ("%s %s" % (program, self.paths.normalize(rest[0], cwd))).strip() if rest else program
        depth = SUBCOMMAND_DEPTH.get(program, 0)
        return program, " ".join([program] + [t.lower() for t in rest[:depth]])

    def _data_reads(self, args: list, cwd: str) -> list:
        # Normalized data-file arguments (by extension) that a command read or touched.
        found = []
        for arg in args:
            if arg.startswith("-") or ">" in arg or "://" in arg:
                continue
            normalized = self.paths.normalize(arg, cwd)
            if normalized and self.paths.extension(normalized) in DATA_EXTS:
                found.append(normalized)
        return found

    @staticmethod
    def _external(segment: str):
        # A short label when the segment sends, publishes, pays, or deploys something outside this machine.
        for pattern, label in EXTERNAL_COMMANDS:
            match = pattern.search(segment)
            if match:
                return label or " ".join(match.group(0).split()[:3])
        return None


class ParsedAction:
    """One tool call reduced to its kind and the comparable facts it contributes."""

    __slots__ = ("kind", "reads", "writes", "command", "domain", "mcp", "external")

    def __init__(self, kind: str) -> None:
        # Starts an action of one kind with no facts.
        self.kind = kind
        self.reads: list = []
        self.writes: list = []
        self.command = None
        self.domain = ""
        self.mcp = ""
        self.external: list = []


class ActionParser:
    """Parses sessionkit action strings ("Edit x", "Bash: cmd", "Read y", "WebFetch url", "mcp__srv__tool arg")."""

    SHELL_RE = re.compile(r"^([\w.\-]+): (.*)$", re.S)

    def __init__(self, paths: PathNormalizer, commands: CommandSignature) -> None:
        # Uses the shared path and command normalizers.
        self.paths = paths
        self.commands = commands

    def parse(self, action: object, cwd: str = "") -> ParsedAction:
        # Returns the action's kind (read, write, command, web, mcp, search, agent, other) and facts.
        text = str(action or "")
        if text.startswith("Agent: "):
            return ParsedAction("agent")
        shell = self.SHELL_RE.match(text)
        if shell and shell.group(1).lower().rsplit(".", 1)[-1] in ActionText.SHELL_TOOLS:
            parsed = ParsedAction("command")
            parsed.command = self.commands.of(shell.group(2), cwd)
            parsed.reads = list(parsed.command.reads)
            parsed.external = list(parsed.command.external)
            return parsed
        for prefix, kind in (("Edit ", "write"), ("Read ", "read")):
            if text.startswith(prefix):
                parsed = ParsedAction(kind)
                path = self.paths.normalize(text[len(prefix):], cwd)
                if path:
                    (parsed.writes if kind == "write" else parsed.reads).append(path)
                return parsed
        if text.startswith("WebFetch "):
            parsed = ParsedAction("web")
            parsed.domain = self._domain(text[len("WebFetch "):])
            return parsed
        if text.startswith("WebSearch "):
            return ParsedAction("search")
        return self._tool(text)

    def _tool(self, text: str) -> ParsedAction:
        # An MCP tool (mcp__server__tool) or another named tool.
        label = text.split(" ", 1)[0]
        if label.lower().startswith("mcp__"):
            parsed = ParsedAction("mcp")
            parts = [p for p in label.split("__") if p]
            server = parts[1] if len(parts) > 1 else "mcp"
            tool = "__".join(parts[2:]) if len(parts) > 2 else ""
            parsed.mcp = ("%s:%s" % (server, tool)).lower().strip(":")
            if EXTERNAL_TOOL_RE.search(tool):
                parsed.external.append("MCP %s %s" % (server, tool))
            return parsed
        key = label.lower().rsplit(".", 1)[-1]
        return ParsedAction("search" if key in ActionText.SEARCH_TOOLS else "other")

    @staticmethod
    def _domain(url: str) -> str:
        # The host of a URL without "www.", or "".
        try:
            host = urlparse(url.strip()).netloc.lower()
        except ValueError:
            return ""
        host = host.rsplit("@", 1)[-1].split(":", 1)[0]
        return host[4:] if host.startswith("www.") else host


class TemplatePrefixes:
    """Long openings pasted at the start of different tasks (prompt templates); their text is not the task.

    Openers that share their first 120 characters are grouped. When at least three of them differ
    yet share a common prefix of 200 or more characters, that prefix is a template: the task is
    whatever follows it. Openers that are identical in full are left alone (a chore typed from a
    saved prompt, or a scheduled job), so they still match each other.
    """

    MIN_PREFIX = 200
    KEY_CHARS = 120
    MIN_SESSIONS = 3

    def __init__(self, texts: list) -> None:
        # Finds the template prefixes among the given opener texts.
        groups = {}
        for text in texts:
            flat = self.flatten(text)
            if len(flat) >= self.MIN_PREFIX:
                groups.setdefault(flat[: self.KEY_CHARS], []).append(flat)
        self.prefixes = []
        self.sessions = {}
        for members in groups.values():
            distinct = sorted(set(members))
            if len(members) < self.MIN_SESSIONS or len(distinct) < 2:
                continue
            prefix = self._common_prefix(distinct)
            if len(prefix) >= self.MIN_PREFIX:
                self.prefixes.append(prefix)
                self.sessions[prefix] = len(members)
        self.prefixes.sort(key=len, reverse=True)

    @staticmethod
    def flatten(text: object) -> str:
        # The text with whitespace runs collapsed to one space.
        return " ".join(str(text or "").split())

    def strip(self, text: object):
        # Returns (task text after the template, template length); (text, 0) when no template starts it.
        flat = self.flatten(text)
        for prefix in self.prefixes:
            if flat.startswith(prefix) and len(flat) > len(prefix):
                return flat[len(prefix):].strip(), len(prefix)
        return str(text or ""), 0

    @staticmethod
    def _common_prefix(texts: list) -> str:
        # The longest common prefix, cut back to a word boundary.
        first, last = texts[0], texts[-1]
        size = 0
        while size < min(len(first), len(last)) and first[size] == last[size]:
            size += 1
        prefix = first[:size]
        return prefix if size == len(first) or " " not in prefix else prefix[: prefix.rfind(" ") + 1]


class SessionSignature:
    """The comparable features of one session as a possible run of a chore."""

    OPENER_TURNS = 3
    MIN_OPENER_TERMS = 3
    TERM_CHARS = 600
    MAX_SESSION_MINUTES = 180

    def __init__(self, session, extractor: TermExtractor, parser: ActionParser, templates: TemplatePrefixes = None) -> None:
        # Builds every feature of the session; a session without prompts has opener None.
        self.session = session
        self.extractor = extractor
        self.parser = parser
        self.templates = templates
        self.template_chars = 0
        self.opener_index, self.opener = self._pick_opener()
        self.skill_name = self.skill_of(self.opener.text) if self.opener else ""
        self.task_text = self._task_text(self.opener.text) if self.opener else ""
        if self.opener is not None and templates is not None and not self.skill_name:
            self.task_text, self.template_chars = templates.strip(self.opener.text)
        self.continuation = self._is_continuation()
        self.terms = self._opener_terms()
        self.periods = extractor.period_mentions(self.opener.text if self.opener else "")
        self.project_key = self.folder_key(session.cwd)
        self.start = (self.opener.ts if self.opener and self.opener.ts else None) or session.first_ts
        self.reads, self.writes, self.commands, self.domains, self.mcp = set(), set(), set(), set(), set()
        self.external: list = []  # (label, turn index)
        self.credentials: list = []
        self._read_actions()
        self._read_prompts()
        self.resources = self._resources()

    @property
    def minutes(self) -> float:
        # Session time from first to last activity, capped at three hours.
        first, last = self.session.first_ts, self.session.last_ts
        if not first or not last:
            return 0.0
        return min(max((last - first).total_seconds() / 60.0, 0.0), float(self.MAX_SESSION_MINUTES))

    @staticmethod
    def folder_key(cwd: object) -> str:
        # A comparable form of a working folder: forward slashes, lower case, no trailing slash.
        return re.sub(r"/+", "/", str(cwd or "").replace("\\", "/")).rstrip("/").lower()

    def same_folder(self, other: "SessionSignature") -> bool:
        # True when both sessions ran in one folder, or one ran inside the other's folder.
        a, b = self.project_key, other.project_key
        return bool(a and b and (a == b or a.startswith(b + "/") or b.startswith(a + "/")))

    def _pick_opener(self):
        # The first of the first three prompts with at least three content terms (after any template), else the first prompt.
        turns = self.session.turns[: self.OPENER_TURNS]
        for index, turn in enumerate(turns):
            if len(self.extractor.terms(self._task_text(turn.text)[: self.TERM_CHARS])) >= self.MIN_OPENER_TERMS:
                return index, turn
        return (0, turns[0]) if turns else (None, None)

    @staticmethod
    def skill_of(text: object) -> str:
        # The skill name when a harness sent a skill's body as the prompt ("Base directory for this skill:"), else "".
        value = str(text or "")
        if not SKILL_MARKER_RE.search(value):
            return ""
        heading = SKILL_HEADING_RE.match(value)
        return "/" + (heading.group(1).lower() if heading else "skill")

    def _task_text(self, text: str) -> str:
        # The task a prompt states: a skill body becomes its /name, and a known template prefix is dropped.
        skill = self.skill_of(text)
        if skill:
            return skill
        return text if self.templates is None else self.templates.strip(text)[0]

    def _is_continuation(self) -> bool:
        # True when the session continues earlier work instead of starting a run of a chore fresh.
        if self.opener is None:
            return True
        first = self.session.turns[0].text.strip()
        return bool(CONTINUATION_RE.match(first[:80]) or CONTINUATION_RE.match(self.opener.text.strip()[:80]) or GO_AHEAD_RE.match(self.opener.text))

    def _opener_terms(self) -> frozenset:
        # The opener's terms, plus the next prompt's terms when the opener is short.
        if self.opener is None:
            return frozenset()
        terms = self.extractor.terms(self.task_text[: self.TERM_CHARS])
        following = self.session.turns[self.opener_index + 1: self.opener_index + 2]
        if len(terms) < 5 and following:
            terms = terms | self.extractor.terms(following[0].text[: self.TERM_CHARS])
        return terms

    def _read_actions(self) -> None:
        # Collects files read and written, commands, domains, MCP tools, and outside actions from every turn.
        cwd = self.session.cwd
        for turn in self.session.turns:
            for action in turn.actions:
                parsed = self.parser.parse(action, cwd)
                self.reads.update(parsed.reads)
                self.writes.update(parsed.writes)
                if parsed.command is not None and parsed.command.signature:
                    self.commands.add(parsed.command.signature)
                if parsed.domain:
                    self.domains.add(parsed.domain)
                if parsed.mcp:
                    self.mcp.add(parsed.mcp)
                for label in parsed.external:
                    self.external.append((label, turn.index))
                for name in CREDENTIAL_NAME_RE.findall(str(action)):
                    self._add_credential(name)

    def _read_prompts(self) -> None:
        # Collects outside actions the user asked for and credential names they mentioned.
        for turn in self.session.turns:
            if SKILL_MARKER_RE.search(turn.text):
                continue
            head = turn.text[:4000]
            for match in EXTERNAL_PROMPT_RE.finditer(head[:800]):
                verb = (match.group("verb") or match.group("filing")).lower()
                self.external.append(("asked to " + EXTERNAL_VERBS.get(verb, verb), turn.index))
            for name in CREDENTIAL_NAME_RE.findall(head):
                self._add_credential(name)
            for pattern, label in CREDENTIAL_MENTIONS.items():
                if pattern.search(head):
                    self._add_credential(label)

    def _add_credential(self, name: str) -> None:
        # Records a credential name once; values are never passed in.
        if name not in self.credentials:
            self.credentials.append(name)

    def _resources(self) -> frozenset:
        # The comparable resources of the session: files, their folders, commands, domains, and MCP tools.
        found = set()
        for path in self.reads | self.writes:
            found.add("file:" + path)
        for path in self.reads:
            folder = PathNormalizer.parent(path)
            if folder:
                found.add("dir:" + folder)
        found.update("cmd:" + c for c in self.commands)
        found.update("url:" + d for d in self.domains)
        found.update("mcp:" + m for m in self.mcp)
        return frozenset(found)
