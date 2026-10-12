"""Obsidian adapter for next-actions-from-all-my-notes. Standard library only, Python 3.8+.

Copied from part 2 of skills/next-actions-from-obsidian/scripts/obsidian_next_actions.py
(cerredz/vidbyte-cli PR #11): finding vaults, the vault's own settings, parsing one note, the link
index, and walking the vault. Keep them in sync: a fix to note parsing or vault settings there
belongs here too. Changes from the source: cmd_find_vaults became find_vaults(), which returns the
vaults instead of printing them; cmd_gather became collect(), which returns raw items and raises
notes_common.ProviderError instead of printing and exiting, and leaves redaction, the budget, and
the output files to all_notes.py; options are named --vault, --obsidian-exclude, and
--obsidian-exclude-tag; and the adapter interface at the end of the file is new.

Read-only: nothing is ever written into the vault.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import os
import posixpath
import re
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path
from urllib.parse import quote, unquote

from notes_common import ProviderError, iso_utc, norm, one_line  # scripts/ is on sys.path

PROVIDER = "obsidian"
MAX_TASKS = 20
MAX_TASK_CHARS = 200
MAX_READ_CHARS = 1000000
MASS_CHANGE_ITEMS = 100

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



def find_vaults(scan: bool = False, roots=()) -> dict:
    """Vaults from Obsidian's own vault list, and with scan=True a bounded scan of the home folder."""
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
    if scan:
        scan_roots = [Path(r).expanduser() for r in roots] or [home]
        for extra in (home / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents", home / "iCloudDrive" / "iCloud~md~obsidian"):
            if extra.is_dir() and not roots:
                scan_roots.append(extra)
        found, visited, stopped = scan_for_vaults(scan_roots)
        for path in found:
            record = vaults.setdefault(norm(path), {"name": path.name, "path": str(path), "found_in": [], "open_in_obsidian": None, "last_opened": None})
            record["found_in"].append("scan")
        scanned = {"roots": [str(r) for r in scan_roots], "folders_visited": visited, "stopped_early": stopped}
    # Open in Obsidian first, then the most recently opened, then by name (stable sorts).
    ordered = sorted(vaults.values(), key=lambda v: v["name"].lower())
    ordered.sort(key=lambda v: v["last_opened"] or "", reverse=True)
    ordered.sort(key=lambda v: not v["open_in_obsidian"])
    return OrderedDict([("vaults", ordered), ("vault_lists_read", lists_read), ("scan", scanned)])


def is_inside(path, folder) -> bool:
    p, f = norm(path), norm(folder)
    return p == f or p.startswith(f.rstrip("\\/") + os.sep)


def vault_path(opts) -> Path:
    if not opts.vault:
        raise ProviderError("config", "No vault was given.", "Ask the user which vault to use, then pass --vault <the folder that holds .obsidian>.")
    vault = Path(opts.vault).expanduser().resolve()
    if not vault.is_dir():
        raise ProviderError("config", "Vault folder not found: %s" % vault, "Pass the folder that holds .obsidian as --vault.")
    if not (vault / ".obsidian").is_dir():
        raise ProviderError("config", "%s has no .obsidian folder, so it is not an Obsidian vault." % vault, "Pass the folder that holds .obsidian as --vault.")
    return vault


def collect(opts, since, now, log=None) -> dict:
    """Reads the notes modified in the window. Copied from cmd_gather; returns raw, unredacted items."""
    vault = vault_path(opts)
    excludes = []
    for value in list(opts.obsidian_exclude) + list(opts.exclude):
        path = Path(value).expanduser()
        if path.is_absolute() and is_inside(path, vault):
            value = os.path.relpath(str(path.resolve()), str(vault))
        if clean_rel(value):
            excludes.append(clean_rel(value).lower())
    exclude_tags = [t.strip().lstrip("#").lower() for t in opts.obsidian_exclude_tag if t.strip().lstrip("#")]

    settings = VaultSettings(vault)
    try:
        all_files, dot_folders = walk_vault(vault)
    except OSError as error:
        raise ProviderError("read_error", "Could not read the vault: %s" % error, "Check that the folder is readable.")
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
        prefix = []
        if daily:
            prefix.append("Daily note.")
        if clipped:
            prefix.append("Clipped from the web; the text is not the user's own.")
        if parsed["tags"]:
            prefix.append("Tags: " + " ".join("#" + t for t in parsed["tags"][:12]))
        if unresolved:
            prefix.append("Links to notes not written yet: " + ", ".join("[[%s]]" % t for t in unresolved[:8]))
        notes.append(
            {
                "provider": PROVIDER,
                "id": rel,
                "title": stem,
                "link": "obsidian://open?vault=%s&file=%s" % (quote(vault.name, safe=""), quote(rel, safe="")),
                "kind": "note",
                "container": folder if folder else "(vault root)",
                "created": iso_utc(created) if created else None,
                "last_edited": iso_utc(stat.st_mtime),
                "author": "other" if clipped else "me",
                "open_tasks": [one_line(t, MAX_TASK_CHARS) for t in parsed["tasks"]],
                "excerpt": parsed["body"],
                "_prefix": "\n".join(prefix),
                "_priority": stat.st_mtime,
                "_cut": "middle",
                "_tags": parsed["tags"],
                "_unresolved": unresolved,
                "_daily": daily,
            }
        )
    notes.sort(key=lambda n: n["_priority"], reverse=True)
    warnings = []
    if counts["in_window"] > MASS_CHANGE_ITEMS or (counts["in_window"] > 20 and counts["in_window"] > counts["markdown_files"] / 2):
        warnings.append(
            "%d of %d notes changed in this window. A sync, a git pull, or a copied vault can touch many notes at once. "
            "Check the table with the user, and offer a shorter window or --obsidian-exclude before running." % (counts["in_window"], counts["markdown_files"])
        )
    stats = OrderedDict([(k, counts[k]) for k in ("markdown_files", "in_window", "excluded_by_path", "excluded_by_tag", "skipped_empty", "unreadable")])
    stats["auto_skipped"] = OrderedDict([("dot_folders_not_read", dot_folders)] + sorted(auto_skipped.items()))
    return {"items": notes, "stats": stats, "warnings": warnings, "calls": {}, "names": [], "finalize": None,
            "local": {"vault": {"name": vault.name, "path": str(vault)}, "excluded": {"paths": excludes, "tags": exclude_tags}}}


# --------------------------------------------------------------------------------------------
# Adapter interface (new). all_notes.py calls detect() and check() from `providers`, and
# collect() and from_items() from `gather`.

NAME = PROVIDER
LABEL = "Obsidian"
ITEM_WORDS = ("note", "notes")
REFERENCE = ""


def detect(opts) -> dict:
    scan = bool(getattr(opts, "scan", False))
    if opts.vault:
        try:
            vault = vault_path(opts)
        except ProviderError as error:
            return {"available": False, "source": None, "detail": error.message, "fix": error.hint}
        return {"available": True, "source": "--vault", "detail": 'vault "%s"' % vault.name, "fix": "", "vault": str(vault)}
    found = find_vaults(scan=scan)
    count = len(found["vaults"])
    if count:
        names = ", ".join('"%s"' % v["name"] for v in found["vaults"][:5])
        return {"available": False, "possible": True, "source": None,
                "detail": "%d vault%s found (%s); not read until the user confirms one" % (count, "" if count == 1 else "s", names),
                "fix": "Ask the user which vault to use, then pass --vault <folder>.", "vaults": found["vaults"], "scan": found["scan"]}
    return {"available": False, "source": None,
            "detail": "no vault in Obsidian's vault list" + (" or the scan" if scan else ""),
            "fix": "Ask the user for the vault folder (the folder that holds .obsidian), then pass --vault <folder>." + ("" if scan else " `providers --scan` also searches the home folder."),
            "vaults": [], "scan": found["scan"]}


def check(opts, log=None) -> dict:
    vault = vault_path(opts)
    return {"call": "local folder check", "ok": True, "vault": vault.name}


def from_items(opts, items, since, now) -> tuple:
    excludes = [clean_rel(v).lower() for v in list(opts.obsidian_exclude) + list(opts.exclude) if clean_rel(v)]
    kept, skipped = [], 0
    for item in items:
        if path_excluded(str(item["id"]).replace("\\", "/").lower(), excludes):
            skipped += 1
            continue
        item["_cut"] = "middle"
        kept.append(item)
    return kept, {"excluded_by_path": skipped}


def section_notes(kept) -> list:
    tag_counts = Counter(t for n in kept for t in OrderedDict((x.lower(), None) for x in n.get("_tags") or []))
    link_counts = Counter(t for n in kept for t in n.get("_unresolved") or [])
    recurring = [(t, c) for t, c in tag_counts.most_common(10) if c > 1]
    lines = []
    if recurring:
        lines.append("Tags used in more than one note: %s." % ", ".join("#%s (%d)" % (t, c) for t, c in recurring))
    if link_counts:
        lines.append("Notes linked but not written yet: %s." % ", ".join("[[%s]] (%d)" % (t, c) for t, c in link_counts.most_common(8)))
    return lines
