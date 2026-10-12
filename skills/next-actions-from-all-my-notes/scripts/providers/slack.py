"""Slack adapter for next-actions-from-all-my-notes. Standard library only, Python 3.8+.

Copied from skills/next-actions-from-slack/scripts/slack_adapter.py (cerredz/vidbyte-cli PR #14),
with score() from that skill's next_actions.py. Keep them in sync: a fix to a Slack call, a limit,
or the task finder there belongs here too. Changes from the source: collect is renamed
slack_collect, test_base_url raises ValueError instead of exiting, USER_AGENT names this skill,
messages name the --slack-... options, and the adapter interface at the end of the file (detect,
check, collect) is new; it turns Slack errors into notes_common.ProviderError.

slack_collect() reads one person's recent Slack activity with their own user token and returns
items in the shared notes-digest/1 shape. Redaction, the size budget, and the output files are not
done here: all_notes.py applies them to every provider the same way. Only read methods are called:

  auth.test               who "me" is, and the workspace URL              no scope
  search.messages         the user's own messages in the window           search:read
  users.conversations     the channels the user belongs to                channels:read, groups:read
                                                                          (+ im:read, mpim:read with DMs)
  conversations.history   top-level messages in the window, per channel   channels:history, groups:history
                                                                          (+ im:history, mpim:history with DMs)
  conversations.replies   the replies of each active thread               same as conversations.history
  files.list              files created in the window                     files:read
  chat.getPermalink       a real link for each kept item without one      no scope

The token is sent only to https://slack.com/api/ and https://files.slack.com/, never through a
redirect. NEXT_ACTIONS_SLACK_TEST_BASE_URL is for tests only: it points every call at a local mock
server and accepts nothing but http://127.0.0.1:<port>/ or http://localhost:<port>/.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict

API_BASE = "https://slack.com/api/"
FILE_HOSTS = ("files.slack.com",)
TEST_BASE_ENV = "NEXT_ACTIONS_SLACK_TEST_BASE_URL"
USER_AGENT = "next-actions-from-all-my-notes/1 (+https://github.com/cerredz/vidbyte-cli)"

# Bounds. Each one keeps a large workspace from turning one gather into hundreds of calls.
SEARCH_PAGES = 5                 # search.messages pages of 100 of the user's own messages
MAX_CHANNELS = 100               # channels read with conversations.history
MAX_MESSAGES_PER_CHANNEL = 300   # newest top-level messages kept per channel
MAX_THREADS = 60                 # threads expanded with conversations.replies
MAX_REPLIES = 200                # replies read per thread (one page)
FILE_PAGES = 3                   # files.list pages of 100
MAX_DOWNLOAD_BYTES = 256 * 1024  # text files larger than this are listed, not read
RUN_GAP_SECONDS = 30 * 60        # top-level messages closer than this form one conversation item
MAX_RETRIES = 4                  # retries of one call after HTTP 429
SLOW_RETRY_SECONDS = 30          # a wait this long means the Tier 1 limit, not a burst
MAX_TASKS = 5
MAX_TASK_CHARS = 200

AUTH_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive",
               "no_user_token", "bot_token", "session_token", "not_allowed_token_type"}
SKIP_SUBTYPES = {
    "channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
    "channel_archive", "channel_unarchive", "channel_convert_to_private", "channel_convert_to_public",
    "group_join", "group_leave", "group_topic", "group_purpose", "group_name", "group_archive",
    "group_unarchive", "pinned_item", "unpinned_item", "reminder_add", "bot_add", "bot_remove",
    "tombstone", "joiner_notification_for_inviter", "huddle_thread", "sh_room_created",
}
CHECK_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check", "done",
                   "check", "white_tick", "approved"}
TEXT_FILETYPES = {"text", "markdown", "md", "post", "csv", "json", "yaml", "python", "javascript",
                  "typescript", "shell", "sql", "html", "xml", "go", "java", "ruby", "rust"}

MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
CHANNEL_RE = re.compile(r"<#([CGD][A-Z0-9]+)(?:\|([^>]*))?>")
SUBTEAM_RE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|([^>]*))?>")
SPECIAL_RE = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
LINK_RE = re.compile(r"<((?:https?|mailto|slack):[^|>]+)(?:\|([^>]*))?>")
REQUEST_RE = re.compile(
    r"(?i)\b(?:can|could|would|will) (?:you|u)\b|\bplease\b|\bpls\b|\bplz\b|\bfollow[- ]?up\b|"
    r"\bto-?do\b|\baction items?\b|\bneeds? you\b|\bwhen you (?:get|have) a (?:chance|sec|second|minute)\b|"
    r"\blet me know\b|\blmk\b|\bany updates?\b|\bwaiting on you\b|\?")
COMMIT_RE = re.compile(
    r"(?i)\bi'?ll\b|\bi will\b|\bwill do\b|\bon it\b|\blet me (?:check|look|take|get|find|ask|fix|send)\b|"
    r"\bto-?do\b|\bfollow[- ]?up\b|\bremind me\b|\bi owe\b|\bi need to\b")
DONE_RE = re.compile(r"(?i)\b(?:done|fixed|merged|shipped|sent|deployed|resolved|finished|closed)\b|:white_check_mark:|✅")
CHECKBOX_RE = re.compile(r"(?m)^\s*(?:[-*•]\s*)?(?:\[ \]|☐)\s+(.+?)\s*$")
TODO_LINE_RE = re.compile(r"(?mi)^\s*(?:[-*•]\s*)?(?:TODO|FIXME|ACTION)\s*[:\-]\s*(.+?)\s*$")


class SlackError(Exception):
    """One Slack call failed. error is Slack's own error code, such as missing_scope."""

    def __init__(self, method, error, needed=None, provided=None):
        self.method, self.error, self.needed, self.provided = method, error, needed, provided
        super().__init__(self.describe())

    def describe(self):
        text = "{} returned {}".format(self.method, self.error)
        if self.error == "missing_scope" and self.needed:
            text += " (the token needs the user scope {})".format(self.needed)
        return text


class WaitBudgetSpent(Exception):
    """Slack asked for more waiting than --slack-max-wait allows; the current phase stops."""

    def __init__(self, method, wait):
        self.method, self.wait = method, wait
        super().__init__("{}: Slack asked to wait {} s, beyond the wait budget".format(method, wait))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Returning None turns every 3xx into an HTTPError, so the token never follows a redirect.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def test_base_url():
    """The test-only base URL, or None. Anything but a local http address is refused."""
    value = os.environ.get(TEST_BASE_ENV, "").strip()
    if not value:
        return None
    if not re.match(r"^http://(?:127\.0\.0\.1|localhost):\d{2,5}/?$", value):
        raise ValueError("{} is for tests only and must be http://127.0.0.1:<port>/ or "
                         "http://localhost:<port>/.".format(TEST_BASE_ENV))
    return value.rstrip("/") + "/"


class SlackClient:
    """GET-only Slack Web API client with Retry-After handling and a total wait budget."""

    def __init__(self, token, max_wait=120.0, log=None, sleep=time.sleep):
        self.token = token
        self.test_base = test_base_url()
        self.base = (self.test_base + "api/") if self.test_base else API_BASE
        self.max_wait = float(max_wait)
        self.waited = 0.0
        self.log = log or (lambda message: print(message, file=sys.stderr))
        self.sleep = sleep
        self.stats = OrderedDict()
        self.slow_methods = set()
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _get(self, url, method):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        request.add_unredirected_header("Authorization", "Bearer " + self.token)
        try:
            with self.opener.open(request, timeout=30) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            body = b""
            try:
                body = error.read()
            except Exception:  # noqa: BLE001 - an unreadable error body changes nothing
                pass
            return error.code, error.headers, body
        except (urllib.error.URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise SlackError(method, "network_error: {}".format(reason))

    def api(self, method, params=None):
        query = urllib.parse.urlencode([(k, v) for k, v in (params or {}).items() if v is not None])
        url = self.base + method + ("?" + query if query else "")
        attempts = 0
        while True:
            attempts += 1
            stat = self.stats.setdefault(method, {"calls": 0, "rate_limited": 0, "waited_seconds": 0})
            stat["calls"] += 1
            status, headers, body = self._get(url, method)
            retry_after = None
            if status == 429:
                retry_after = headers.get("Retry-After") if headers else None
                retry_after = retry_after or "30"
            elif status != 200:
                raise SlackError(method, "http_{}".format(status))
            else:
                try:
                    data = json.loads(body.decode("utf-8"))
                except ValueError:
                    raise SlackError(method, "invalid_json")
                if data.get("ok"):
                    return data
                if data.get("error") != "ratelimited":
                    raise SlackError(method, data.get("error") or "unknown_error",
                                     data.get("needed"), data.get("provided"))
                retry_after = (headers.get("Retry-After") if headers else None) or "30"
            try:
                wait = max(1, int(float(retry_after)))
            except ValueError:
                wait = 30
            stat["rate_limited"] += 1
            if wait >= SLOW_RETRY_SECONDS:
                self.slow_methods.add(method)
            if attempts > MAX_RETRIES or self.waited + wait > self.max_wait:
                raise WaitBudgetSpent(method, wait)
            self.log("Slack rate limit (HTTP 429) on {}: waiting {} s as Retry-After asks.".format(method, wait))
            self.sleep(wait)
            self.waited += wait
            stat["waited_seconds"] += wait

    def paged(self, method, params, key, max_items):
        """Follows response_metadata.next_cursor. Returns (items, more_left)."""
        items, cursor = [], None
        while True:
            page = dict(params)
            if cursor:
                page["cursor"] = cursor
            data = self.api(method, page)
            items.extend(data.get(key) or [])
            cursor = ((data.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor or len(items) >= max_items:
                return items[:max_items], bool(cursor) or len(items) > max_items

    def download_text(self, url):
        """Reads a small private file. Returns text, or None when it is not allowed or not text."""
        parsed = urllib.parse.urlparse(url or "")
        if self.test_base:
            if not (url or "").startswith(self.test_base):
                return None
        elif parsed.scheme != "https" or parsed.hostname not in FILE_HOSTS:
            return None
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        request.add_unredirected_header("Authorization", "Bearer " + self.token)
        stat = self.stats.setdefault("files (download)", {"calls": 0, "rate_limited": 0, "waited_seconds": 0})
        stat["calls"] += 1
        try:
            with self.opener.open(request, timeout=30) as response:
                if "text/html" in (response.headers.get("Content-Type") or ""):
                    return None  # a login page, not the file
                raw = response.read(MAX_DOWNLOAD_BYTES + 1)
        except (urllib.error.URLError, OSError):
            return None
        if len(raw) > MAX_DOWNLOAD_BYTES or b"\x00" in raw:
            return None
        return raw.decode("utf-8-sig", errors="replace")


# ---------------------------------------------------------------- text helpers

class People:
    """Stable pseudonyms: the user is @me, everyone else @person-1, @person-2, ..."""

    def __init__(self, me_id):
        self.me_id = me_id
        self.names = {}

    def name(self, user_id):
        if not user_id:
            return "@unknown"
        if user_id == self.me_id:
            return "@me"
        if user_id not in self.names:
            self.names[user_id] = "@person-{}".format(len(self.names) + 1)
        return self.names[user_id]


def plain_text(raw, people, channel_names):
    """Slack mrkdwn to readable text: mentions become pseudonyms, links keep their URL."""
    text = raw or ""
    text = MENTION_RE.sub(lambda m: people.name(m.group(1)), text)
    text = CHANNEL_RE.sub(lambda m: "#" + (m.group(2) or channel_names.get(m.group(1)) or "channel"), text)
    text = SUBTEAM_RE.sub(lambda m: m.group(1) or "@team", text)
    text = SPECIAL_RE.sub(lambda m: "@" + m.group(1), text)

    def link(match):
        url, label = match.group(1), match.group(2)
        if url.startswith("mailto:"):
            return label or url[7:]
        return url if not label or label == url else "{} ({})".format(label, url)

    text = LINK_RE.sub(link, text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def one_line(text, limit=None):
    flat = " ".join((text or "").split())
    if limit and len(flat) > limit:
        return flat[: limit - 3].rstrip() + "..."
    return flat


def sentence_with(text, pattern, limit=MAX_TASK_CHARS):
    """The sentence of text that matches pattern, on one line."""
    flat = one_line(text)
    match = pattern.search(flat)
    if not match:
        return one_line(flat, limit)
    start = max(flat.rfind(". ", 0, match.start()), flat.rfind("! ", 0, match.start()),
                flat.rfind("? ", 0, match.start()))
    start = 0 if start < 0 else start + 2
    ends = [i for i in (flat.find(". ", match.end()), flat.find("! ", match.end()),
                        flat.find("? ", match.end())) if i >= 0]
    end = min(ends) + 1 if ends else len(flat)
    return one_line(flat[start:end], limit)


def iso(ts):
    try:
        seconds = float(ts)
    except (TypeError, ValueError):
        return None
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def local_stamp(ts):
    try:
        return dt.datetime.fromtimestamp(float(ts)).strftime("%b %d %H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "?"


def ts_float(ts):
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def thread_ts_from_permalink(permalink):
    query = urllib.parse.urlparse(permalink or "").query
    values = urllib.parse.parse_qs(query).get("thread_ts")
    return values[0] if values else None


# ---------------------------------------------------------------- channels and filters

def normalize_names(values):
    out = set()
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip().lstrip("#").strip()
            if part:
                out.add(part.lower())
    return out


class Channel:
    def __init__(self, cid, name, is_im=False, is_mpim=False, is_private=False, other_user=None):
        self.id = cid
        self.name = name or ""
        self.is_im = is_im
        self.is_mpim = is_mpim
        self.is_private = is_private
        self.other_user = other_user

    @classmethod
    def from_conversation(cls, data):
        cid = data.get("id") or ""
        is_im = bool(data.get("is_im")) or cid.startswith("D")
        is_mpim = bool(data.get("is_mpim")) or str(data.get("name", "")).startswith("mpdm-")
        return cls(cid, data.get("name") or "", is_im, is_mpim,
                   bool(data.get("is_private") or data.get("is_group")), data.get("user"))

    @classmethod
    def from_search(cls, match):
        info = match.get("channel") or {}
        cid = info.get("id") or ""
        kind = match.get("type") or ""
        is_im = bool(info.get("is_im")) or kind == "im" or cid.startswith("D")
        is_mpim = bool(info.get("is_mpim")) or kind == "mpim" or str(info.get("name", "")).startswith("mpdm-")
        return cls(cid, info.get("name") or "", is_im, is_mpim, bool(info.get("is_private")))

    def label(self):
        if self.is_im:
            return "DM"
        if self.is_mpim:
            return "group DM"
        return "#{}{}".format(self.name, " (private)" if self.is_private else "")

    def matches(self, names):
        return self.id.lower() in names or (self.name and self.name.lower() in names)


class Filters:
    def __init__(self, channels, exclude, include_dms):
        self.only = normalize_names(channels)
        self.exclude = normalize_names(exclude)
        self.include_dms = include_dms

    def allowed(self, channel):
        if channel.matches(self.exclude):
            return False
        if (channel.is_im or channel.is_mpim) and not self.include_dms:
            return False
        if self.only and not channel.matches(self.only):
            return False
        return True


# ---------------------------------------------------------------- the collection

class Collection:
    """What collect() returns. finalize() fills real permalinks for the items that were kept."""

    def __init__(self, client, team_url):
        self.client = client
        self.team_url = (team_url or "").rstrip("/") + "/" if team_url else ""
        self.items = []
        self.sources = OrderedDict()
        self.warnings = []
        self.identity = {}
        self.calls = {}

    def finalize(self, items):
        spent = False
        for item in items:
            ref = item.pop("_permalink_ref", None)
            if item.get("link") or not ref:
                continue
            channel, ts, thread_ts = ref
            if not spent:
                try:
                    data = self.client.api("chat.getPermalink", {"channel": channel, "message_ts": ts})
                    item["link"] = data.get("permalink") or ""
                except WaitBudgetSpent:
                    spent = True
                except SlackError as error:
                    if error.error in AUTH_ERRORS:
                        raise
            if not item.get("link") and self.team_url:
                # Slack's documented permalink shape, used only when chat.getPermalink failed.
                link = "{}archives/{}/p{}".format(self.team_url, channel, ts.replace(".", ""))
                if thread_ts and thread_ts != ts:
                    link += "?thread_ts={}&cid={}".format(thread_ts, channel)
                item["link"] = link
        for item in self.items:
            item.pop("_permalink_ref", None)
        self.calls = dict(self.client.stats)


def slack_collect(token, since, channels=(), exclude=(), include_dms=False, include_bots=False,
                  max_wait=120.0, log=None):
    """Reads the window's Slack activity. Raises SlackError for a bad token (an AUTH_ERRORS code)."""
    log = log or (lambda message: print(message, file=sys.stderr))
    if not token.startswith("xoxp-"):
        if token.startswith("xoxb-"):
            raise SlackError("token check", "bot_token")
        if token.startswith(("xoxc-", "xoxd-")):
            raise SlackError("token check", "session_token")
        raise SlackError("token check", "no_user_token")
    client = SlackClient(token, max_wait=max_wait, log=log)
    since_ts = since.timestamp()
    filters = Filters(channels, exclude, include_dms)

    me = client.api("auth.test")
    if me.get("bot_id") or me.get("user") == "bot":
        raise SlackError("auth.test", "bot_token")
    me_id = me.get("user_id") or ""
    result = Collection(client, me.get("url"))
    result.identity = {"handle": me.get("user") or "", "team_url": result.team_url}
    people = People(me_id)
    known = OrderedDict()   # channel id -> Channel, for every channel seen
    names = {}              # channel id -> name, for <#C123> without a label

    # 1. The user's own messages in the window, from any channel, including old threads.
    hits = []
    after = (since.astimezone() - dt.timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        page = 1
        while page <= SEARCH_PAGES:
            data = client.api("search.messages", {
                "query": "from:<@{}> after:{}".format(me_id, after), "sort": "timestamp",
                "sort_dir": "desc", "count": 100, "page": page})
            block = data.get("messages") or {}
            for match in block.get("matches") or []:
                if ts_float(match.get("ts")) < since_ts:
                    continue
                channel = Channel.from_search(match)
                if not channel.id:
                    continue
                known.setdefault(channel.id, channel)
                hits.append({"channel": channel.id, "ts": match.get("ts"), "text": match.get("text") or "",
                             "permalink": match.get("permalink") or "",
                             "thread_ts": thread_ts_from_permalink(match.get("permalink"))})
            pages = int((block.get("paging") or {}).get("pages") or 1)
            if page >= pages:
                break
            page += 1
        result.sources["search.messages"] = "{} of your messages in the window".format(len(hits))
    except WaitBudgetSpent as error:
        result.sources["search.messages"] = "partial: {} of your messages, then {}".format(len(hits), error)
    except SlackError as error:
        if error.error in AUTH_ERRORS:
            raise
        result.sources["search.messages"] = "unavailable ({})".format(error.describe())
        result.warnings.append("Your own messages in older threads were not searched: {}.".format(error.describe()))

    # 2. The channels the user belongs to.
    types = "public_channel,private_channel" + (",mpim,im" if include_dms else "")
    member = []
    try:
        convs, _ = client.paged("users.conversations", {"types": types, "exclude_archived": "true",
                                                        "limit": 200}, "channels", 5000)
        for conv in convs:
            channel = Channel.from_conversation(conv)
            known[channel.id] = channel
            member.append(channel)
    except (SlackError, WaitBudgetSpent) as error:
        if isinstance(error, SlackError) and error.error in AUTH_ERRORS:
            raise
        result.warnings.append("Could not list your channels: {}.".format(error))
    for channel in known.values():
        if channel.name:
            names[channel.id] = channel.name
    allowed = [c for c in member if filters.allowed(c)]
    hit_channels = {h["channel"] for h in hits}
    allowed.sort(key=lambda c: 0 if c.id in hit_channels else 1)  # stable: keeps Slack's order otherwise
    excluded = sum(1 for c in member if c.matches(filters.exclude))
    over_cap = allowed[MAX_CHANNELS:]
    allowed = allowed[:MAX_CHANNELS]
    result.sources["users.conversations"] = "{} channels you belong to; {} read, {} excluded{}{}".format(
        len(member), len(allowed), excluded,
        "" if include_dms else ", DMs skipped",
        ", {} over the {}-channel cap".format(len(over_cap), MAX_CHANNELS) if over_cap else "")

    # 3. Top-level messages in the window, per channel.
    history = OrderedDict()   # channel id -> messages, oldest first
    read_ok, skipped = 0, []
    for index, channel in enumerate(allowed):
        try:
            messages, more = client.paged("conversations.history", {
                "channel": channel.id, "oldest": "{:.6f}".format(since_ts), "limit": 200},
                "messages", MAX_MESSAGES_PER_CHANNEL)
        except WaitBudgetSpent:
            skipped.extend(c.label() for c in allowed[index:])
            result.warnings.append("Stopped reading channels after {} of {}: Slack's rate limit used up "
                                   "the wait budget (--slack-max-wait).".format(index, len(allowed)))
            break
        except SlackError as error:
            if error.error in AUTH_ERRORS:
                raise
            if error.error == "missing_scope":
                result.warnings.append("Could not read {}: {}.".format(channel.label(), error.describe()))
            skipped.append(channel.label())
            continue
        read_ok += 1
        if more:
            result.warnings.append("{} had more than {} messages in the window; the newest {} were read."
                                   .format(channel.label(), MAX_MESSAGES_PER_CHANNEL, MAX_MESSAGES_PER_CHANNEL))
        kept = [m for m in messages if keep_message(m, include_bots)]
        history[channel.id] = sorted(kept, key=lambda m: ts_float(m.get("ts")))
    result.sources["conversations.history"] = "{} channels read{}".format(
        read_ok, ", {} skipped".format(len(skipped)) if skipped else "")

    # 4. Threads: parents in the window, broadcasts, and old threads the user replied in.
    threads = OrderedDict()   # (channel, thread_ts) -> {"parent": msg or None, "hit": hit or None}
    standalone = OrderedDict()
    for cid, messages in history.items():
        for msg in messages:
            if int(msg.get("reply_count") or 0) > 0:
                threads.setdefault((cid, msg["ts"]), {"parent": None, "hit": None})["parent"] = msg
            elif msg.get("thread_ts") and msg.get("thread_ts") != msg.get("ts"):
                threads.setdefault((cid, msg["thread_ts"]), {"parent": None, "hit": None})
            else:
                standalone.setdefault(cid, []).append(msg)
    for hit in hits:
        channel = known.get(hit["channel"])
        if channel is None or not filters.allowed(channel):
            continue
        if hit["thread_ts"] and hit["thread_ts"] != hit["ts"]:
            entry = threads.setdefault((hit["channel"], hit["thread_ts"]), {"parent": None, "hit": None})
            entry["hit"] = entry["hit"] or hit
        elif hit["channel"] not in history:
            # A top-level message in a channel that was not read (over the cap or not a member).
            standalone.setdefault(hit["channel"], []).append(
                {"ts": hit["ts"], "user": me_id, "text": hit["text"], "_permalink": hit["permalink"]})
        elif (hit["channel"], hit["ts"]) in threads:
            threads[(hit["channel"], hit["ts"])]["hit"] = hit

    def involvement(key):
        entry = threads[key]
        parent = entry["parent"] or {}
        score = 0
        if entry["hit"] or me_id in (parent.get("reply_users") or []) or parent.get("user") == me_id:
            score += 3
        if "<@{}>".format(me_id) in (parent.get("text") or ""):
            score += 2
        return (score, ts_float(parent.get("latest_reply") or parent.get("ts") or key[1]))

    order = sorted(threads, key=involvement, reverse=True)
    expanded, budget_hit = {}, False
    for key in order[:MAX_THREADS]:
        cid, thread_ts = key
        try:
            data = client.api("conversations.replies", {"channel": cid, "ts": thread_ts, "limit": MAX_REPLIES})
        except WaitBudgetSpent:
            budget_hit = True
            break
        except SlackError as error:
            if error.error in AUTH_ERRORS:
                raise
            continue
        replies = [m for m in data.get("messages") or [] if keep_message(m, include_bots)]
        expanded[key] = (sorted(replies, key=lambda m: ts_float(m.get("ts"))), bool(data.get("has_more")))
    unexpanded = len(threads) - len(expanded)
    result.sources["conversations.replies"] = "{} threads read{}".format(
        len(expanded), ", {} not read (cap {} or rate limit)".format(unexpanded, MAX_THREADS) if unexpanded else "")
    if budget_hit:
        result.warnings.append("Some threads were not read: Slack's rate limit used up the wait budget.")

    # 5. Items: one per thread, one per run of top-level messages.
    for key in threads:
        item = thread_item(key, threads[key], expanded.get(key), known, names, people, me_id, include_dms,
                           since_ts)
        if item:
            result.items.append(item)
    for cid, messages in standalone.items():
        channel = known.get(cid) or Channel(cid, names.get(cid, ""))
        for run in split_runs(sorted(messages, key=lambda m: ts_float(m.get("ts")))):
            result.items.append(run_item(channel, run, names, people, me_id, include_dms))

    # 6. Files created in the window that the user uploaded or that reached a channel read here.
    files_seen, file_items = 0, []
    read_ids = set(history) | {c.id for c in allowed}
    try:
        for page in range(1, FILE_PAGES + 1):
            data = client.api("files.list", {"ts_from": str(int(since_ts)), "count": 100, "page": page})
            batch = data.get("files") or []
            files_seen += len(batch)
            for meta in batch:
                item = file_item(meta, client, known, filters, read_ids, me_id, since_ts)
                if item:
                    file_items.append(item)
            pages = int((data.get("paging") or {}).get("pages") or 1)
            if page >= pages:
                break
        result.sources["files.list"] = "{} files in the window, {} kept".format(files_seen, len(file_items))
    except WaitBudgetSpent as error:
        result.sources["files.list"] = "partial ({})".format(error)
    except SlackError as error:
        if error.error in AUTH_ERRORS:
            raise
        result.sources["files.list"] = "unavailable ({})".format(error.describe())
    result.items.extend(file_items)
    if client.slow_methods & {"conversations.history", "conversations.replies"}:
        result.warnings.append(
            "Slack limited conversations.history or conversations.replies to about one call per "
            "minute. That limit applies to apps distributed outside the Slack Marketplace that were "
            "installed on or after May 29, 2025. An internal app created in your own workspace keeps "
            "the normal limit: see references/slack-app.md, or narrow the run with --slack-channels.")
    result.calls = dict(client.stats)
    return result


def keep_message(msg, include_bots):
    if msg.get("subtype") in SKIP_SUBTYPES:
        return False
    if (msg.get("bot_id") or msg.get("subtype") == "bot_message") and not include_bots:
        return False
    return bool((msg.get("text") or "").strip() or msg.get("files"))


def checked_by(msg, me_id):
    for reaction in msg.get("reactions") or []:
        if reaction.get("name") in CHECK_REACTIONS and me_id in (reaction.get("users") or []):
            return True
    return False


def find_open_tasks(messages, me_id, people, names, is_dm):
    """Requests aimed at the user with no later answer from them, their own open promises, and boxes."""
    tasks = []
    mention = "<@{}>".format(me_id)
    for index, msg in enumerate(messages):
        raw = msg.get("text") or ""
        text = plain_text(raw, people, names)
        later = messages[index + 1:]
        answered = any(m.get("user") == me_id for m in later)
        if checked_by(msg, me_id):
            continue
        if msg.get("user") != me_id:
            if (mention in raw or is_dm) and REQUEST_RE.search(text) and not answered:
                tasks.append("{} asked you: {}".format(people.name(msg.get("user")), sentence_with(text, REQUEST_RE)))
        elif COMMIT_RE.search(text):
            closed = any(m.get("user") == me_id and DONE_RE.search(m.get("text") or "") for m in later)
            if not closed:
                tasks.append("You said: {}".format(sentence_with(text, COMMIT_RE)))
        for box in CHECKBOX_RE.findall(text):
            tasks.append(one_line(box, MAX_TASK_CHARS))
    unique = list(OrderedDict((t, None) for t in tasks))
    return unique[:MAX_TASKS]


def render_lines(messages, people, names):
    lines = []
    for msg in messages:
        who = "@bot" if (msg.get("bot_id") and not msg.get("user")) else people.name(msg.get("user"))
        text = one_line(plain_text(msg.get("text") or "", people, names))
        files = [f.get("title") or f.get("name") for f in msg.get("files") or [] if isinstance(f, dict)]
        if files:
            text = (text + " " if text else "") + "[shared file: {}]".format(", ".join(one_line(f, 80) for f in files if f))
        lines.append("{} {}: {}".format(local_stamp(msg.get("ts")), who, text))
    return "\n".join(lines)


def title_for(channel, text, people, names):
    head = one_line(plain_text(text, people, names), 90) or "(no text)"
    if channel.is_im:
        prefix = "DM" + (" with {}".format(people.name(channel.other_user)) if channel.other_user else "")
    else:
        prefix = channel.label()
    return "{}: {}".format(prefix, head)


def thread_item(key, entry, expanded, known, names, people, me_id, include_dms, since_ts):
    cid, thread_ts = key
    channel = known.get(cid) or Channel(cid, names.get(cid, ""))
    note = ""
    if expanded:
        messages, more = expanded
        if more:
            note = "[... more replies not read]"
    elif entry["parent"]:
        messages = [entry["parent"]]
        count = int(entry["parent"].get("reply_count") or 0)
        note = "[... {} replies not read]".format(count) if count else ""
    elif entry["hit"]:
        hit = entry["hit"]
        messages = [{"ts": hit["ts"], "user": me_id, "text": hit["text"]}]
        note = "[your reply in an older thread; the rest of the thread was not read]"
    else:
        return None
    if not messages:
        return None
    parent = messages[0] if messages[0].get("ts") == thread_ts else (entry["parent"] or messages[0])
    last = max([ts_float(m.get("ts")) for m in messages] +
               [ts_float((entry["parent"] or {}).get("latest_reply"))])
    if last < since_ts:
        return None
    excerpt = render_lines(messages, people, names)
    if note:
        excerpt += "\n" + note
    is_dm = channel.is_im or channel.is_mpim
    mention = "<@{}>".format(me_id)
    involved = any(m.get("user") == me_id for m in messages) or bool(entry["hit"])
    return {
        "provider": "slack",
        "id": "{}:{}".format(cid, thread_ts),
        "title": title_for(channel, parent.get("text") or "", people, names),
        "link": (entry["hit"] or {}).get("permalink") or "",
        "kind": "thread",
        "container": channel.label(),
        "created": iso(thread_ts),
        "last_edited": iso(last),
        "author": "me" if parent.get("user") == me_id else ("other" if parent.get("user") else None),
        "open_tasks": find_open_tasks(messages, me_id, people, names, is_dm),
        "excerpt": excerpt,
        "_permalink_ref": (cid, thread_ts, thread_ts),
        "_signal": {"mentions_me": any(mention in (m.get("text") or "") for m in messages) or is_dm,
                    "involved": involved, "replies": max(len(messages) - 1, 0)},
    }


def split_runs(messages):
    runs, current = [], []
    for msg in messages:
        if current and ts_float(msg.get("ts")) - ts_float(current[-1].get("ts")) > RUN_GAP_SECONDS:
            runs.append(current)
            current = []
        current.append(msg)
    if current:
        runs.append(current)
    return runs


def run_item(channel, run, names, people, me_id, include_dms):
    first, last = run[0], run[-1]
    is_dm = channel.is_im or channel.is_mpim
    mention = "<@{}>".format(me_id)
    return {
        "provider": "slack",
        "id": "{}:{}".format(channel.id, first.get("ts")),
        "title": title_for(channel, first.get("text") or "", people, names),
        "link": first.get("_permalink") or "",
        "kind": "message",
        "container": channel.label(),
        "created": iso(first.get("ts")),
        "last_edited": iso(max(ts_float(m.get("ts")) for m in run)),
        "author": "me" if first.get("user") == me_id else ("other" if first.get("user") else None),
        "open_tasks": find_open_tasks(run, me_id, people, names, is_dm),
        "excerpt": render_lines(run, people, names),
        "_permalink_ref": (channel.id, first.get("ts"), None),
        "_signal": {"mentions_me": any(mention in (m.get("text") or "") for m in run) or is_dm,
                    "involved": any(m.get("user") == me_id for m in run), "replies": len(run) - 1},
    }


def is_canvas(meta):
    return meta.get("filetype") in ("quip", "canvas") or str(meta.get("pretty_type", "")).lower() == "canvas"


def file_item(meta, client, known, filters, read_ids, me_id, since_ts):
    created = ts_float(meta.get("created") or meta.get("timestamp"))
    if created < since_ts:
        return None
    shares = list(meta.get("channels") or []) + list(meta.get("groups") or []) + list(meta.get("ims") or [])
    share_channels = [known.get(cid) or Channel(cid, "", is_im=cid.startswith("D")) for cid in shares]
    if any(c.matches(filters.exclude) for c in share_channels):
        return None
    visible = [c for c in share_channels if c.id in read_ids]
    mine = meta.get("user") == me_id
    if filters.only and not visible:
        return None
    if not mine and not visible:
        return None
    if share_channels and not visible and all(c.is_im or c.is_mpim for c in share_channels) and not filters.include_dms:
        return None
    title = meta.get("title") or meta.get("name") or "(untitled file)"
    tasks = []
    if is_canvas(meta):
        body = "Canvas. Its text is not read: Slack documents no method that returns a canvas's content."
    else:
        text = meta.get("preview") if isinstance(meta.get("preview"), str) else None
        size = int(meta.get("size") or 0)
        textual = str(meta.get("mimetype", "")).startswith("text/") or meta.get("filetype") in TEXT_FILETYPES
        if text is None and textual and 0 < size <= MAX_DOWNLOAD_BYTES:
            text = client.download_text(meta.get("url_private_download") or meta.get("url_private"))
        if text:
            body = text.strip()
            tasks = [one_line(t, MAX_TASK_CHARS) for t in CHECKBOX_RE.findall(text) + TODO_LINE_RE.findall(text)]
        else:
            body = "{} file, {} KB. Its contents were not read.".format(
                meta.get("pretty_type") or meta.get("filetype") or "A", max(1, size // 1024) if size else "?")
    container = visible[0].label() if visible else "files"
    return {
        "provider": "slack",
        "id": meta.get("id") or "",
        "title": one_line(title, 120),
        "link": meta.get("permalink") or "",
        "kind": "file",
        "container": container,
        "created": iso(created),
        "last_edited": iso(max(created, ts_float(meta.get("updated")))),
        "author": "me" if mine else ("other" if meta.get("user") else None),
        "open_tasks": list(OrderedDict((t, None) for t in tasks))[:MAX_TASKS],
        "excerpt": body,
        "_signal": {"mentions_me": False, "involved": mine, "replies": 0},
    }


# ---------------------------------------------------------------- score (from next_actions.py)

from notes_common import ProviderError, parse_iso  # noqa: E402 - scripts/ is on sys.path (all_notes.py)

CHAT_KINDS = ("message", "thread")  # keep the first and newest lines when cut; others keep the head


def score(item, now, since) -> float:
    """How much an item matters to the user: open requests, mentions, involvement, replies, recency."""
    signal = item.get("_signal") or {}
    value = 5.0 if item["open_tasks"] else 0.0
    value += 4.0 if signal.get("mentions_me") else 0.0
    value += 3.0 if (signal.get("involved") or item["author"] == "me") else 0.0
    value += min(3, int(signal.get("replies") or 0) // 5)
    edited = parse_iso(item["last_edited"]) or since
    span = max((now - since).total_seconds(), 1.0)
    value += 2.0 * max(0.0, min(1.0, 1.0 - (now - edited).total_seconds() / span))
    if item["kind"] == "file":
        value += 1.0
    return value


# ---------------------------------------------------------------- adapter interface (new)
# all_notes.py calls detect() and check() from `providers`, and collect() and from_items() from
# `gather`. Every failure becomes a ProviderError, so the other providers still run.

NAME = "slack"
LABEL = "Slack"
ITEM_WORDS = ("item", "threads, messages, and files")
REFERENCE = "references/slack-app.md"
TOKEN_ENV = "SLACK_USER_TOKEN"
SECTION_NOTE = ("@me is the user; other people appear as @person-1, @person-2, and so on. Open requests to the "
                "user and the user's own open promises are listed as tasks.")
TOKEN_HINTS = {
    "bot_token": "SLACK_USER_TOKEN holds a bot token (xoxb-). Use the app's User OAuth Token (xoxp-) instead (references/slack-app.md).",
    "session_token": "SLACK_USER_TOKEN holds a browser session token. Create a Slack app and use its User OAuth Token (xoxp-) (references/slack-app.md).",
    "no_user_token": "SLACK_USER_TOKEN does not look like a Slack user token (xoxp-) (references/slack-app.md).",
}
SETUP_FIX = ("Create your own Slack app with read-only user scopes, install it, and set SLACK_USER_TOKEN to its "
             "User OAuth Token (xoxp-) in your own terminal (references/slack-app.md).")


def detect(opts) -> dict:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        return {"available": False, "source": None, "detail": "SLACK_USER_TOKEN is not set", "fix": SETUP_FIX}
    if token.startswith("xoxp-"):
        return {"available": True, "source": "SLACK_USER_TOKEN", "detail": "SLACK_USER_TOKEN is set (a user token)", "fix": ""}
    if token.startswith("xoxb-"):
        return {"available": False, "source": None, "detail": "SLACK_USER_TOKEN holds a bot token (xoxb-)", "fix": TOKEN_HINTS["bot_token"]}
    if token.startswith(("xoxc-", "xoxd-")):
        return {"available": False, "source": None, "detail": "SLACK_USER_TOKEN holds a browser session token", "fix": TOKEN_HINTS["session_token"]}
    return {"available": False, "source": None, "detail": "SLACK_USER_TOKEN is not a Slack user token", "fix": TOKEN_HINTS["no_user_token"]}


def _provider_error(error) -> ProviderError:
    if isinstance(error, WaitBudgetSpent):
        return ProviderError("rate_limit", str(error), "Wait a few minutes, raise --slack-max-wait, or narrow the run with --slack-channels.")
    if isinstance(error, ValueError):
        return ProviderError("config", str(error), "")
    if error.error in AUTH_ERRORS:
        return ProviderError("auth", "Slack refused the token: " + error.describe(), TOKEN_HINTS.get(error.error, SETUP_FIX))
    if error.error == "missing_scope":
        return ProviderError("scope", error.describe(), "Add the missing user scope to the Slack app, reinstall it, and set SLACK_USER_TOKEN again (references/slack-app.md).")
    if str(error.error).startswith("network_error"):
        return ProviderError("network", error.describe(), "Check the network connection and try again.")
    return ProviderError("api_error", error.describe(), "Try again in a few minutes.")


def check(opts, log=None) -> dict:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token.startswith("xoxp-"):
        raise ProviderError("no_token", detect(opts)["detail"], detect(opts)["fix"])
    try:
        client = SlackClient(token, max_wait=min(float(opts.slack_max_wait), 30.0), log=log)
        data = client.api("auth.test")
    except (SlackError, WaitBudgetSpent, ValueError) as error:
        raise _provider_error(error)
    if data.get("bot_id") or data.get("user") == "bot":
        raise ProviderError("auth", "SLACK_USER_TOKEN belongs to a bot.", TOKEN_HINTS["bot_token"])
    return {"call": "auth.test", "ok": True}


def _decorate(items, now, since):
    for item in items:
        item["_priority"] = score(item, now, since)
        item["_cut"] = "chat" if item.get("kind") in CHAT_KINDS else "head"
    return items


def collect(opts, since, now, log=None) -> dict:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise ProviderError("no_token", "SLACK_USER_TOKEN is not set.", SETUP_FIX)
    try:
        found = slack_collect(token, since, channels=opts.slack_channels, exclude=list(opts.slack_exclude) + list(opts.exclude),
                              include_dms=opts.slack_include_dms, include_bots=opts.slack_include_bots,
                              max_wait=opts.slack_max_wait, log=log)
    except (SlackError, WaitBudgetSpent, ValueError) as error:
        raise _provider_error(error)

    def finalize(kept):
        try:
            found.finalize(kept)
        except SlackError as error:  # A revoked token while filling links: keep the items.
            found.warnings.append("Some links were not filled: {}.".format(error.describe()))
        return dict(found.client.stats)

    return {
        "items": _decorate(found.items, now, since),
        "stats": OrderedDict([("sources", found.sources)]),
        "warnings": found.warnings,
        "calls": dict(found.calls),
        "names": [found.identity.get("handle") or ""],
        "finalize": finalize,
        "local": {},
    }


def from_items(opts, items, since, now) -> tuple:
    """Filters host-written Slack items the way next_actions.py does: channels, excludes, DMs."""
    only = normalize_names(opts.slack_channels)
    exclude = normalize_names(list(opts.slack_exclude) + list(opts.exclude))
    kept, skipped = [], 0
    for item in items:
        container = str(item.get("container") or "").lstrip("#").split(" (")[0].lower()
        if container in exclude or (only and container not in only) or (container in ("dm", "group dm") and not opts.slack_include_dms):
            skipped += 1
            continue
        kept.append(item)
    return _decorate(kept, now, since), {"skipped_by_channel_filters": skipped}


def section_notes(kept) -> list:
    return [SECTION_NOTE]
