"""Google Docs adapter for next-actions-from-all-my-notes. Standard library only, Python 3.8+.

Copied from skills/next-actions-from-google-docs/scripts/google_docs.py (cerredz/vidbyte-cli
PR #15): the Drive settings, text cleanup and task finder, token handling, error classes, the
Drive client, folder paths, and drive_gather. Keep them in sync: a fix to a Drive call, a limit,
or the task finder there belongs here too. Changes from the source: options come from the
--google-... flags; resolve_token does not fall back to gcloud just because it is installed (it
uses gcloud only with --google-auth gcloud, or when a quota project is set, because many machines
have a gcloud with no Drive access); api_base and quota_project raise instead of exiting; and the
adapter interface at the end of the file (detect, check, collect, from_items) is new.

Only reads. The token is sent only to https://www.googleapis.com, never through a redirect.
Test-only: GOOGLE_DOCS_SKILL_TEST_API_BASE points the client at a local mock server and accepts
nothing but http://127.0.0.1:<port> or http://localhost:<port>.
"""

from __future__ import annotations

import fnmatch
import json
import os
import random
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict
from pathlib import Path

from notes_common import ProviderError, one_line, parse_iso as parse_ts, iso_utc as iso  # scripts/ is on sys.path

SLUG = "next-actions-from-all-my-notes"
PROVIDER = "google-docs"

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
QUOTA_PROJECT_RE = re.compile(r"^[A-Za-z0-9:._-]{4,64}$")
TEST_BASE_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost):\d{1,5}$")

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
    "exports %s before starting the coding agent or saves it to a file passed with --google-token-file. "
    "Never ask for the token in the chat." % TOKEN_ENV
)


def api_base() -> str:
    override = os.environ.get(TEST_BASE_ENV, "").strip()
    if not override:
        return DEFAULT_API_BASE
    if not TEST_BASE_RE.match(override):
        raise ProviderError("config", "%s is a test-only setting and must be http://127.0.0.1:<port> or http://localhost:<port>." % TEST_BASE_ENV)
    return override


def check_token_shape(token: str, source: str) -> str:
    token = token.strip()
    if len(token) < 20 or re.search(r"\s", token) or not all(32 < ord(c) < 127 for c in token):
        raise DriveError("no_token", "The access token from %s does not look like a Google access token (it is not printed)." % source, TOKEN_HELP)
    return token


def gcloud_token(redact) -> str:
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


def resolve_token(args, redact):
    """Returns (token, source). Never prints the token."""
    mode = args.google_auth or "auto"
    if mode in ("auto", "env"):
        value = os.environ.get(TOKEN_ENV, "")
        if value.strip():
            return check_token_shape(value, TOKEN_ENV), "env"
        if mode == "env":
            raise DriveError("no_token", "%s is not set." % TOKEN_ENV, TOKEN_HELP)
    if mode in ("auto", "file") and args.google_token_file:
        path = Path(args.google_token_file).expanduser()
        try:
            value = path.read_text(encoding="utf-8-sig")
        except OSError:
            raise DriveError("no_token", "The token file %s cannot be read." % redact(str(path)), TOKEN_HELP)
        return check_token_shape(value, "the token file"), "file"
    if mode == "file":
        raise DriveError("no_token", "--google-auth file needs --google-token-file PATH.", TOKEN_HELP)
    # Unlike the sibling, an installed gcloud is used only when the user chose it, or set a quota
    # project, which only someone who set gcloud up for Drive does.
    if mode == "gcloud" or (mode == "auto" and shutil.which("gcloud") and quota_project(args)):
        return gcloud_token(redact), "gcloud"
    raise DriveError("no_token", "No Google access token was found: %s is not set, no --google-token-file was given, and gcloud was not chosen with --google-auth gcloud." % TOKEN_ENV, TOKEN_HELP)


def quota_project(args) -> str:
    value = (args.google_quota_project or os.environ.get(QUOTA_ENV, "")).strip()
    if value and not QUOTA_PROJECT_RE.match(value):
        raise ProviderError("config", "--google-quota-project must be a Google Cloud project id, such as my-notes-project.")
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
                          "Pass --google-quota-project <project id> for a Google Cloud project where you ran `gcloud services enable drive.googleapis.com --project <project id>`, or use a token from the OAuth 2.0 Playground (references/google-drive-access.md).", status, reason)
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
    for name in args.google_include:
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


def fetch_text(drive: Drive, info: dict, warnings: list, redact):
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


def open_comments(drive: Drive, info: dict, redact) -> list:
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


def drive_gather(drive: Drive, since, args, redact, warnings: list, stats: Counter) -> list:
    types = selected_types(args)
    mimes = [m for t in types for m in TYPE_MIMES[t]]
    mime_terms = " or ".join("mimeType = '%s'" % m for m in mimes)
    params = OrderedDict(
        [
            ("q", "modifiedTime > '%s' and trashed = false and (%s)" % (iso(since), mime_terms)),
            ("orderBy", "modifiedTime desc"),
            ("pageSize", PAGE_SIZE),
            ("fields", LIST_FIELDS),
            ("corpora", "allDrives" if args.google_shared_drives else "user"),
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
        if not mine and not args.google_include_others:
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
        if excluded(list(args.google_exclude) + list(args.exclude), str(info.get("name") or ""), container):
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
        if args.google_comments:
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
# Adapter interface (new). all_notes.py calls detect() and check() from `providers`, and
# collect() and from_items() from `gather`. Every failure becomes a ProviderError.

NAME = PROVIDER
LABEL = "Google Docs"
ITEM_WORDS = ("document", "documents")
REFERENCE = "references/google-drive-access.md"
SETUP_FIX = ("Get a read-only Drive token in your own browser and terminal, then set GOOGLE_OAUTH_ACCESS_TOKEN or pass "
             "--google-token-file; or pass --google-auth gcloud --google-quota-project <project id> if gcloud has Drive "
             "access (references/google-drive-access.md).")


def _provider_error(error: DriveError) -> ProviderError:
    return ProviderError(error.kind, error.message, error.hint)


def detect(opts) -> dict:
    mode = opts.google_auth or "auto"
    if mode in ("auto", "env") and os.environ.get(TOKEN_ENV, "").strip():
        return {"available": True, "source": TOKEN_ENV, "detail": "GOOGLE_OAUTH_ACCESS_TOKEN is set", "fix": ""}
    if mode == "env":
        return {"available": False, "source": None, "detail": "--google-auth env, but GOOGLE_OAUTH_ACCESS_TOKEN is not set", "fix": SETUP_FIX}
    if mode in ("auto", "file") and opts.google_token_file:
        if Path(opts.google_token_file).expanduser().is_file():
            return {"available": True, "source": "--google-token-file", "detail": "a token file was given", "fix": ""}
        return {"available": False, "source": None, "detail": "the --google-token-file file does not exist", "fix": SETUP_FIX}
    if mode == "file":
        return {"available": False, "source": None, "detail": "--google-auth file needs --google-token-file", "fix": SETUP_FIX}
    gcloud = shutil.which("gcloud")
    try:
        quota = quota_project(opts)
    except ProviderError as error:
        return {"available": False, "source": None, "detail": error.message, "fix": "Pass a Google Cloud project id, such as my-notes-project."}
    if mode == "gcloud":
        if gcloud:
            return {"available": True, "source": "gcloud", "detail": "--google-auth gcloud" + ("" if quota else "; no quota project is set, so Drive may refuse the calls"), "fix": ""}
        return {"available": False, "source": None, "detail": "--google-auth gcloud, but gcloud is not on PATH", "fix": SETUP_FIX}
    if gcloud and quota:
        return {"available": True, "source": "gcloud", "detail": "gcloud is installed and a quota project is set", "fix": ""}
    if gcloud:
        return {"available": False, "possible": True, "source": None, "detail": "gcloud is installed, but it is used only when chosen",
                "fix": "If gcloud is logged in with Drive access (gcloud auth login --enable-gdrive-access), pass --google-auth gcloud "
                       "--google-quota-project <project id>. Otherwise: " + SETUP_FIX}
    return {"available": False, "source": None, "detail": "no Google token was found", "fix": SETUP_FIX}


def check(opts, log=None) -> dict:
    from notes_common import Redactor
    try:
        token, source = resolve_token(opts, Redactor(Path.home()))
        drive = Drive(token, quota_project(opts), api_base())
        drive.get_json("/drive/v3/files", {"pageSize": 1, "fields": "files(id)", "q": "trashed = false"}, "files.list")
    except DriveError as error:
        raise _provider_error(error)
    return {"call": "files.list", "ok": True, "auth_source": source}


def collect(opts, since, now, log=None) -> dict:
    from notes_common import Redactor
    redact = Redactor(Path.home())
    stats, warnings = Counter(), []
    try:
        token, source = resolve_token(opts, redact)
        drive = Drive(token, quota_project(opts), api_base())
        items = drive_gather(drive, since, opts, redact, warnings, stats)
    except DriveError as error:
        raise _provider_error(error)
    for item in items:
        item["_priority"] = (parse_ts(item["last_edited"]) or since).timestamp()
        item["_cut"] = "head"
    return {"items": items, "stats": dict(stats), "warnings": warnings, "calls": dict(drive.calls), "names": [],
            "finalize": None, "local": {"auth_source": source, "types": selected_types(opts)}}


def from_items(opts, items, since, now) -> tuple:
    """Prepares host-written Google items the way google_docs.py does: exclude, clean, find tasks."""
    patterns = list(opts.google_exclude) + list(opts.exclude)
    kept, skipped = [], 0
    for item in items:
        if excluded(patterns, item["title"], item["container"]):
            skipped += 1
            continue
        body = clean_text(item["excerpt"][:MAX_RAW_TEXT_CHARS])
        tasks = OrderedDict()
        for task in list(item["open_tasks"]) + find_tasks(body):
            task = one_line(task, MAX_TASK_CHARS)
            if task:
                tasks.setdefault(task, None)
        item["open_tasks"] = list(tasks)[: MAX_TASKS_PER_ITEM + MAX_COMMENTS_PER_ITEM]
        item["excerpt"] = format_excerpt(body)
        item["container"] = item["container"] or "Google Drive"
        item["_priority"] = (parse_ts(item["last_edited"]) or since).timestamp()
        item["_cut"] = "head"
        kept.append(item)
    return kept, {"skipped_excluded": skipped}


def section_notes(kept) -> list:
    return []
