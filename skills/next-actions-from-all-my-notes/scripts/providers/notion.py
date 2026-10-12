"""Notion adapter for next-actions-from-all-my-notes. Standard library only, Python 3.8+.

Copied from skills/next-actions-from-notion/scripts/notion_next_actions.py (cerredz/vidbyte-cli
PR #13): the Notion settings, Excluder, NotionClient, the block renderer, PageReader,
row_properties, Ancestry, and gather_from_api. Keep them in sync: a fix to a Notion call, a limit,
or the renderer there belongs here too. Changes from the source: gather_from_api raises
notes_common.ProviderError instead of printing and exiting, takes its options from the
--notion-... flags, and returns raw items (all_notes.py redacts them and fits them to the budget);
notion_base raises instead of exiting; and the adapter interface at the end of the file (detect,
check, collect, from_items) is new.

Only reads: GET, and POST /v1/search. The token is sent only to https://api.notion.com, never
through a redirect. Test-only: NOTION_NEXT_ACTIONS_TEST_API_BASE points the client at a local mock
server and accepts nothing but http://127.0.0.1 or http://localhost (any port).
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

from notes_common import ProviderError, one_line as _one_line, parse_iso as parse_ts  # scripts/ is on sys.path

SLUG = "next-actions-from-all-my-notes"
MAX_OPEN_TASKS = 25


def one_line(text: object, limit: int = 0) -> str:
    return _one_line(text or "", limit)


def compact_id(value: object) -> str:
    return re.sub(r"[^0-9a-f]", "", str(value or "").lower())


# ---------------------------------------------------------------- Notion settings
# Verified against developers.notion.com on 2026-10-11: 2026-03-11 is the latest Notion-Version.

NOTION_API = "https://api.notion.com"
NOTION_VERSION = "2026-03-11"
TEST_BASE_ENV = "NOTION_NEXT_ACTIONS_TEST_API_BASE"
TEST_BASE_RE = re.compile(r"^http://(?:127\.0\.0\.1|localhost)(?::\d{1,5})?$")
DEFAULT_DEPTH = 3
DEFAULT_MAX_REQUESTS = 500
MIN_INTERVAL = 0.35      # About 3 requests per second, Notion's average limit per integration.
MAX_RETRIES = 5
MAX_RETRY_WAIT = 120     # Seconds. A longer Retry-After stops the gather instead of hanging it.
RETRY_STATUSES = (429, 500, 502, 503, 504, 529)
MAX_LOOKUPS = 80         # Parent pages, data sources, and blocks fetched for container names.
MAX_ANCESTORS = 8
SCAN_LIMIT = 1000        # Search results read before giving up on reaching the window's start.
MAX_CHILD_PAGES = 10     # Pages of 100 child blocks read for one parent block.
MAX_LEVELS = 8           # Hard recursion guard for nested blocks.
DONE_WORDS = {"done", "complete", "completed", "finished", "closed", "shipped", "resolved",
              "archived", "cancelled", "canceled", "won't do", "wont do"}
DONE_CHECKBOXES = {"done", "complete", "completed", "finished", "closed", "resolved"}



# ---------------------------------------------------------------- exclusions


class Excluder:
    """Matches --exclude values against titles, containers, ancestors, ids, and URLs."""

    UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    HEX_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])")

    def __init__(self, values) -> None:
        self.ids = set()
        self.texts = []
        for value in values or []:
            value = value.strip()
            if not value:
                continue
            found = self.ids_in(value)
            if found:
                self.ids.update(found)
                continue
            prefix = value.endswith("*")
            text = " ".join(value.rstrip("*").split()).casefold()
            if text:
                self.texts.append((text, prefix))

    @classmethod
    def ids_in(cls, value: object) -> list:
        """Notion ids in a page id, a dashed UUID, or a URL such as .../My-Page-<32 hex>?pvs=4."""
        low = str(value or "").lower()
        return [compact_id(f) for f in (cls.UUID_RE.findall(low) or cls.HEX_RE.findall(low))]

    @property
    def active(self) -> bool:
        return bool(self.ids or self.texts)

    def matches(self, titles, ids) -> bool:
        if any(found in self.ids for value in ids if value for found in self.ids_in(value)):
            return True
        for title in titles:
            norm = " ".join(str(title or "").split()).casefold()
            if not norm:
                continue
            for text, prefix in self.texts:
                if norm == text or (prefix and norm.startswith(text)):
                    return True
        return False



# ---------------------------------------------------------------- Notion client


class NotionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class BudgetExhausted(Exception):
    """The --max-requests budget is used up."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would forward the Authorization header to a redirect target on any host.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def notion_base() -> str:
    override = os.environ.get(TEST_BASE_ENV, "").strip()
    if not override:
        return NOTION_API
    override = override.rstrip("/")
    if not TEST_BASE_RE.match(override):
        raise ProviderError("config", "%s is a test-only setting and accepts only http://127.0.0.1 or http://localhost. Unset it." % TEST_BASE_ENV)
    print("Using the test-only Notion base URL %s." % override, file=sys.stderr)
    return override


def notion_token() -> str:
    return os.environ.get("NOTION_TOKEN", "").strip()


class NotionClient:
    """A small, rate-limited Notion REST client. Read-only by construction: GET and search only."""

    def __init__(self, token: str, base: str, max_requests: int, redact: Redactor) -> None:
        self._token = token
        self.base = base
        self.max_requests = max_requests
        self.redact = redact
        self.requests = 0
        self.retries = 0
        self.waited = 0.0
        self._last = 0.0
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _throttle(self) -> None:
        gap = time.monotonic() - self._last
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        self._last = time.monotonic()

    def call(self, method: str, path: str, body=None, query=None):
        if method not in ("GET", "POST") or (method == "POST" and path != "/v1/search"):
            raise ValueError("This client only reads: GET, and POST /v1/search.")
        if self.requests >= self.max_requests:
            raise BudgetExhausted()
        url = self.base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": "Bearer " + self._token,
            "Notion-Version": NOTION_VERSION,
            "Accept": "application/json",
            "User-Agent": SLUG + "/1",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            self.requests += 1
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._opener.open(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                payload = {}
                try:
                    payload = json.loads(error.read().decode("utf-8", "replace"))
                except (ValueError, OSError):
                    pass
                code = str(payload.get("code") or "http_%d" % error.code) if isinstance(payload, dict) else "http_%d" % error.code
                message = str(payload.get("message") or error.reason) if isinstance(payload, dict) else str(error.reason)
                if error.code in RETRY_STATUSES and attempt < MAX_RETRIES:
                    wait = self._retry_after(error, payload) or min(2 ** attempt, 30)
                    if wait > MAX_RETRY_WAIT:
                        raise NotionError(error.code, code, "Notion asked to wait %d seconds before the next request; try again later." % wait)
                    self.retries += 1
                    self.waited += wait
                    print("Notion answered %d %s; waiting %d seconds before retrying." % (error.code, code, wait), file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise NotionError(error.code, code, self.redact(one_line(message, 300)))
            except (urllib.error.URLError, socket.timeout, ConnectionError) as error:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                reason = getattr(error, "reason", error)
                raise NotionError(0, "network_error", self.redact(one_line("Could not reach Notion: %s" % reason, 300)))
        raise NotionError(0, "retries_exhausted", "Notion kept failing after %d retries." % MAX_RETRIES)

    @staticmethod
    def _retry_after(error, payload) -> int:
        for value in (error.headers.get("Retry-After") if error.headers else None,
                      ((payload or {}).get("additional_data") or {}).get("retry_after") if isinstance(payload, dict) else None):
            try:
                seconds = int(float(str(value).strip()))
                if seconds >= 0:
                    return max(seconds, 1)
            except (TypeError, ValueError):
                continue
        return 0


def auth_failure(error: NotionError):
    """Turns a 401 or 403 into the message and exit code 4, or returns None."""
    if error.status == 401:
        return "Notion rejected NOTION_TOKEN (401 %s). The user should check the token, or create a new one, and set it again." % error.code
    if error.status == 403:
        return "Notion refused the request (403 %s). The integration needs the 'Read content' capability." % error.code
    return None


# ---------------------------------------------------------------- Notion: objects to text


def rich(parts) -> str:
    if not isinstance(parts, list):
        return ""
    return "".join(p.get("plain_text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("plain_text"), str))


def is_trashed(obj) -> bool:
    return bool(obj.get("in_trash") or obj.get("archived"))  # `archived` before 2026-03-11.


def page_title(page) -> str:
    for prop in (page.get("properties") or {}).values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return rich(prop.get("title")).strip()
    return ""


def page_kind(page) -> str:
    parent_type = (page.get("parent") or {}).get("type")
    return "database-row" if parent_type in ("data_source_id", "database_id") else "page"


def render_block(btype: str, body: dict):
    """Returns (line or None, open task text or None) for one block."""
    text = rich(body.get("rich_text")).strip()
    heading = re.match(r"^heading_([1-6])$", btype)
    if btype == "paragraph" or btype == "template":
        return (text or None), None
    if heading:
        return ("#" * int(heading.group(1)) + " " + text if text else None), None
    if btype == "bulleted_list_item":
        return ("- " + text if text else None), None
    if btype == "numbered_list_item":
        return ("1. " + text if text else None), None
    if btype == "to_do":
        if not text:
            return None, None
        if body.get("checked"):
            return "[x] " + text, None
        return "[ ] " + text, text
    if btype == "toggle":
        return ("- " + text if text else None), None
    if btype in ("quote", "callout"):
        return ("> " + text if text else None), None
    if btype == "code":
        return ("```%s\n%s\n```" % (body.get("language") or "", text) if text else None), None
    if btype == "child_page":
        return "[Subpage: %s]" % one_line(body.get("title")), None
    if btype == "child_database":
        return "[Database: %s]" % one_line(body.get("title")), None
    if btype == "table_row":
        cells = [one_line(rich(cell)) for cell in body.get("cells") or []]
        return ("| " + " | ".join(cells) + " |" if any(cells) else None), None
    if btype in ("meeting_notes", "transcription"):  # Renamed to meeting_notes in 2026-03-11.
        title = rich(body.get("title")).strip()
        return ("Meeting notes: " + title if title else "Meeting notes"), None
    if btype in ("bookmark", "embed", "link_preview"):
        url = body.get("url") or ""
        caption = rich(body.get("caption")).strip()
        return (" ".join(x for x in (url, caption) if x) or None), None
    if btype == "equation":
        return (body.get("expression") or None), None
    if btype in ("image", "video", "audio", "file", "pdf"):
        caption = rich(body.get("caption")).strip()
        return ("[%s: %s]" % (btype, caption) if caption else None), None
    if btype == "divider":
        return "---", None
    return None, None  # column_list, column, synced_block, tab, table, breadcrumb, unsupported...


STRUCTURAL = {"column_list", "column", "synced_block", "tab", "table"}


class PageReader:
    """Reads one page's blocks into plain text, recursing with a depth and a size limit."""

    def __init__(self, client: NotionClient, max_depth: int, budget: int) -> None:
        self.client = client
        self.max_depth = max_depth
        self.budget = budget

    def read(self, page_id: str):
        self.lines: list = []
        self.tasks: list = []
        self.chars = 0
        self.full = False
        self.notes: list = []
        try:
            self._walk(page_id, 0, 0)
        except BudgetExhausted:
            self.notes.append("[... the rest was not read: the request budget ran out]")
        except NotionError as error:
            if error.status in (401,):
                raise
            self.notes.append("[... the rest could not be read: Notion answered %s %s]" % (error.status or "", error.code))
        if self.full:
            self.notes.append("[... more text in Notion]")
        return "\n".join(self.lines + self.notes), self.tasks

    def _children(self, block_id: str):
        cursor, pages = None, 0
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            data = self.client.call("GET", "/v1/blocks/%s/children" % block_id, query=query)
            for block in data.get("results") or []:
                yield block
            pages += 1
            cursor = data.get("next_cursor")
            if not data.get("has_more") or not cursor or pages >= MAX_CHILD_PAGES:
                return

    def _walk(self, block_id: str, depth: int, level: int) -> None:
        for block in self._children(block_id):
            if self.full:
                return
            if not isinstance(block, dict) or is_trashed(block):
                continue
            btype = str(block.get("type") or "")
            body = block.get(btype) if isinstance(block.get(btype), dict) else {}
            line, task = render_block(btype, body)
            if task and len(self.tasks) < MAX_OPEN_TASKS:
                self.tasks.append(task)
            if line:
                indent = "  " * depth
                self.lines.append("\n".join(indent + part for part in line.split("\n")))
                self.chars += len(self.lines[-1]) + 1
                if self.chars >= self.budget:
                    self.full = True
                    return
            if not block.get("has_children") or btype in ("child_page", "child_database") or level >= MAX_LEVELS:
                continue
            if btype in STRUCTURAL:
                self._walk(str(block.get("id")), depth, level + 1)
            elif depth < self.max_depth:
                self._walk(str(block.get("id")), depth + 1, level + 1)


def row_properties(page, title: str):
    """Returns (properties line, open-task text or None) for a database row."""
    parts, open_label, done = [], None, False
    for name, prop in (page.get("properties") or {}).items():
        if not isinstance(prop, dict) or len(parts) >= 12:
            continue
        ptype = prop.get("type")
        value = ""
        if ptype == "status" or ptype == "select":
            value = str((prop.get(ptype) or {}).get("name") or "")
            if ptype == "status" and value:
                if value.casefold() in DONE_WORDS:
                    done = True
                else:
                    open_label = open_label or "%s: %s" % (name, value)
        elif ptype == "multi_select":
            value = ", ".join(str(o.get("name")) for o in prop.get("multi_select") or [] if isinstance(o, dict) and o.get("name"))
        elif ptype == "date":
            date = prop.get("date") or {}
            value = " to ".join(str(x) for x in (date.get("start"), date.get("end")) if x)
        elif ptype == "checkbox":
            checked = bool(prop.get("checkbox"))
            value = "yes" if checked else "no"
            if str(name).strip().casefold() in DONE_CHECKBOXES:
                if checked:
                    done = True
                else:
                    open_label = open_label or "%s: no" % name
        elif ptype == "number" and prop.get("number") is not None:
            value = str(prop.get("number"))
        elif ptype == "rich_text":
            value = one_line(rich(prop.get("rich_text")), 200)
        elif ptype == "url":
            value = str(prop.get("url") or "")
        elif ptype == "unique_id":
            uid = prop.get("unique_id") or {}
            if uid.get("number") is not None:
                value = "%s%s" % ((uid.get("prefix") + "-") if uid.get("prefix") else "", uid.get("number"))
        elif ptype == "formula":
            formula = prop.get("formula") or {}
            raw = formula.get(formula.get("type")) if formula.get("type") else None
            value = str(raw.get("start") or "") if isinstance(raw, dict) else ("" if raw is None else str(raw))
        if value:
            parts.append("%s: %s" % (one_line(name, 60), value))
    line = "Properties: " + "; ".join(parts) if parts else ""
    task = "%s (%s)" % (title or "Untitled", open_label) if open_label and not done else None
    return line, task


class Ancestry:
    """Resolves container names and ancestor chains through cached, capped parent lookups."""

    def __init__(self, client: NotionClient) -> None:
        self.client = client
        self.cache: dict = {}
        self.lookups = 0

    def node(self, parent):
        """Returns {"ids": [...], "title": str, "parent": dict or None} for one parent object."""
        if not isinstance(parent, dict):
            return {"ids": [], "title": "", "parent": None}
        ptype = parent.get("type")
        if ptype == "workspace":
            return {"ids": [], "title": "Top level", "parent": None}
        if ptype not in ("page_id", "data_source_id", "database_id", "block_id"):
            return {"ids": [], "title": "", "parent": None}
        pid = str(parent.get(ptype) or "")
        key = (ptype, pid)
        if key in self.cache:
            return self.cache[key]
        ids = [pid] + ([str(parent["database_id"])] if ptype == "data_source_id" and parent.get("database_id") else [])
        result = {"ids": ids, "title": "", "parent": None}
        if self.lookups >= MAX_LOOKUPS:
            return result
        self.lookups += 1
        path = {"page_id": "/v1/pages/%s", "data_source_id": "/v1/data_sources/%s",
                "database_id": "/v1/databases/%s", "block_id": "/v1/blocks/%s"}[ptype] % pid
        try:
            obj = self.client.call("GET", path)
        except BudgetExhausted:
            return result
        except NotionError as error:
            if error.status == 401:
                raise
            result["title"] = "(not shared with the integration)" if error.status in (403, 404) else ""
            self.cache[key] = result
            return result
        if ptype == "page_id":
            result["title"] = page_title(obj) or "Untitled"
            result["parent"] = obj.get("parent")
        elif ptype == "data_source_id":
            result["title"] = rich(obj.get("title")).strip() or "Untitled database"
            result["parent"] = obj.get("database_parent")
        elif ptype == "database_id":
            result["title"] = rich(obj.get("title")).strip() or "Untitled database"
            result["parent"] = obj.get("parent")
        else:  # block_id: blocks have no title; the container is further up.
            result["parent"] = obj.get("parent")
        self.cache[key] = result
        return result

    def chain(self, page, limit: int = MAX_ANCESTORS, until_titled: bool = False):
        """Walks up the parents. until_titled stops at the first titled node (the container)."""
        nodes, parent = [], page.get("parent")
        for _ in range(limit):
            node = self.node(parent)
            if not node["ids"] and not node["title"]:
                break
            nodes.append(node)
            if node["parent"] is None or (until_titled and node["title"]):
                break
            parent = node["parent"]
        return nodes

    def container(self, page, nodes=None) -> str:
        for node in (nodes if nodes is not None else self.chain(page, limit=3, until_titled=True)):
            if node["title"]:
                return node["title"]
        return ""


def gather_from_api(args, since, redact, excluder: Excluder, stats: Counter, warnings: list):
    token = notion_token()
    if not token:
        raise ProviderError("no_token", "NOTION_TOKEN is not set.", SETUP_FIX)
    client = NotionClient(token, notion_base(), args.notion_max_requests, redact)
    candidates, cursor, scanned, reached_start = [], None, 0, False
    try:
        while True:
            body = {"filter": {"property": "object", "value": "page"},
                    "sort": {"timestamp": "last_edited_time", "direction": "descending"},
                    "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = client.call("POST", "/v1/search", body=body)
            if (data.get("request_status") or {}).get("type") == "incomplete":
                warnings.append("Notion stopped the search early (%s); older edits in the window may be missing." % ((data.get("request_status") or {}).get("incomplete_reason") or "incomplete"))
            for obj in data.get("results") or []:
                scanned += 1
                if not isinstance(obj, dict) or obj.get("object") != "page":
                    continue
                if "last_edited_time" not in obj:  # A partial object holds only object and id.
                    try:
                        obj = client.call("GET", "/v1/pages/%s" % obj.get("id"))
                    except NotionError as error:
                        if error.status == 401:
                            raise
                        stats["partial_unreadable"] += 1
                        continue
                if is_trashed(obj):
                    stats["in_trash"] += 1
                    continue
                edited = parse_ts(obj.get("last_edited_time"))
                if edited is None:
                    stats["no_timestamp"] += 1
                    continue
                if edited < since:
                    reached_start = True
                    continue
                candidates.append((edited, obj))
            cursor = data.get("next_cursor")
            if reached_start or not data.get("has_more") or not cursor:
                break
            if scanned >= SCAN_LIMIT:
                warnings.append("Read %d search results without reaching the window's start; older edits may be missing." % scanned)
                break
    except NotionError as error:
        raise provider_error(error, "Notion search failed")
    except BudgetExhausted:
        warnings.append("The request budget ran out during the search; raise --notion-max-requests to read more.")

    candidates.sort(key=lambda pair: pair[0], reverse=True)
    ancestry = Ancestry(client)
    reader = PageReader(client, args.notion_max_depth, max(4000, min(args.excerpt_chars * 2, 50000)))
    items = []
    for edited, page in candidates:
        title = page_title(page) or "Untitled"
        pid = str(page.get("id") or "")
        try:
            nodes = ancestry.chain(page) if excluder.active else ancestry.chain(page, limit=3, until_titled=True)
        except NotionError as error:
            raise provider_error(error, "Notion failed")
        if excluder.active and excluder.matches([title] + [n["title"] for n in nodes],
                                                [pid, page.get("url")] + [i for n in nodes for i in n["ids"]]):
            stats["excluded"] += 1
            continue
        if len(items) >= args.max_items:
            stats["over_max_items"] += 1
            continue
        kind = page_kind(page)
        lines, tasks = [], []
        if kind == "database-row":
            props, row_task = row_properties(page, title)
            if props:
                lines.append(props)
            if row_task:
                tasks.append(row_task)
        if client.requests >= client.max_requests:
            lines.append("[text not read: the request budget ran out]")
            stats["text_not_read"] += 1
        else:
            try:
                text, page_tasks = reader.read(pid)
            except NotionError as error:
                raise provider_error(error, "Notion failed")
            if text:
                lines.append(text)
            tasks.extend(page_tasks)
        author = None
        if args.notion_my_user_id:
            editor = compact_id((page.get("last_edited_by") or {}).get("id"))
            author = ("me" if editor == compact_id(args.notion_my_user_id) else "other") if editor else None
        items.append({
            "provider": PROVIDER,
            "id": pid,
            "title": title,
            "link": str(page.get("url") or ""),
            "kind": kind,
            "container": ancestry.container(page, nodes),
            "created": page.get("created_time") if isinstance(page.get("created_time"), str) else None,
            "last_edited": str(page.get("last_edited_time")),
            "author": author,
            "open_tasks": tasks,
            "excerpt": "\n".join(lines),
        })
    stats["search_results_read"] = scanned
    stats["notion_requests"] = client.requests
    if client.retries:
        stats["notion_retries"] = client.retries
        stats["notion_wait_seconds"] = int(client.waited)
    if client.requests >= client.max_requests:
        warnings.append("Used all %d Notion requests (--notion-max-requests); some text may be missing." % client.max_requests)
    return items


# ---------------------------------------------------------------- adapter interface (new)
# all_notes.py calls detect() and check() from `providers`, and collect() and from_items() from
# `gather`. Every failure becomes a ProviderError, so the other providers still run.

PROVIDER = NAME = "notion"
LABEL = "Notion"
ITEM_WORDS = ("page", "pages and database rows")
REFERENCE = "references/notion-setup.md"
SETUP_FIX = ("Create a Notion internal integration with only the Read content capability, share the pages with it, "
             "and set NOTION_TOKEN in your own terminal (references/notion-setup.md).")
SECTION_NOTE = "Pages and database rows the integration can see; a database row starts with its properties."


def provider_error(error: NotionError, what: str) -> ProviderError:
    if error.status == 401:
        return ProviderError("auth", "Notion rejected NOTION_TOKEN (401 %s)." % error.code, "Check the token, or create a new one, and set NOTION_TOKEN again (references/notion-setup.md).")
    if error.status == 403:
        return ProviderError("scope", "Notion refused the request (403 %s)." % error.code, "The integration needs the Read content capability (references/notion-setup.md).")
    if error.status == 429 or error.code == "rate_limited":
        return ProviderError("rate_limit", "%s: %s" % (what, error.message), "Wait a few minutes and run gather again, or lower --notion-max-requests.")
    if error.status == 0:
        return ProviderError("network", "%s: %s" % (what, error.message), "Check the network connection and try again.")
    if error.status >= 500:
        return ProviderError("server", "%s: %s %s: %s" % (what, error.status, error.code, error.message), "Try again in a few minutes.")
    return ProviderError("api_error", "%s: %s %s: %s" % (what, error.status or "", error.code, error.message), "")


def detect(opts) -> dict:
    token = notion_token()
    if not token:
        return {"available": False, "source": None, "detail": "NOTION_TOKEN is not set", "fix": SETUP_FIX}
    if token.startswith(("ntn_", "secret_")):
        return {"available": True, "source": "NOTION_TOKEN", "detail": "NOTION_TOKEN is set", "fix": ""}
    return {"available": True, "source": "NOTION_TOKEN",
            "detail": "NOTION_TOKEN is set but does not start with ntn_ or secret_; `providers --check` tells whether Notion accepts it", "fix": ""}


def check(opts, log=None) -> dict:
    token = notion_token()
    if not token:
        raise ProviderError("no_token", "NOTION_TOKEN is not set.", SETUP_FIX)
    from notes_common import Redactor
    from pathlib import Path
    client = NotionClient(token, notion_base(), 10, Redactor(Path.home()))
    try:
        client.call("GET", "/v1/users/me")
        found = client.call("POST", "/v1/search", body={"filter": {"property": "object", "value": "page"}, "page_size": 1})
    except NotionError as error:
        raise provider_error(error, "Notion check failed")
    if not found.get("results"):
        raise ProviderError("not_found", "The token works, but no page is shared with this integration.",
                            "In Notion, open a page's ... menu, choose Connections, and pick the integration (references/notion-setup.md).")
    return {"call": "GET /v1/users/me, POST /v1/search", "ok": True}


def collect(opts, since, now, log=None) -> dict:
    from notes_common import Redactor
    from pathlib import Path
    stats, warnings = Counter(), []
    excluder = Excluder(list(opts.notion_exclude) + list(opts.exclude))
    items = gather_from_api(opts, since, Redactor(Path.home()), excluder, stats, warnings)
    for item in items:
        item["_priority"] = (parse_ts(item["last_edited"]) or since).timestamp()
        item["_cut"] = "head"
    calls = {k: stats.pop(k) for k in ("notion_requests", "notion_retries", "notion_wait_seconds") if k in stats}
    return {"items": items, "stats": dict(stats), "warnings": warnings, "calls": calls, "names": [], "finalize": None, "local": {}}


def from_items(opts, items, since, now) -> tuple:
    excluder = Excluder(list(opts.notion_exclude) + list(opts.exclude))
    kept, skipped = [], 0
    for item in items:
        if excluder.active and excluder.matches([item["title"], item["container"]], [item["id"], item["link"]]):
            skipped += 1
            continue
        item["_priority"] = (parse_ts(item["last_edited"]) or since).timestamp()
        item["_cut"] = "head"
        kept.append(item)
    return kept, {"excluded": skipped}


def section_notes(kept) -> list:
    return [SECTION_NOTE]
