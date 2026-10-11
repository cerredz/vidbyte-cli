#!/usr/bin/env python3
"""Build a redacted digest of this project's recent coding-agent sessions.

Part of the next-from-my-sessions skill. Standard library only; Python 3.8+; Windows, macOS, Linux.
It reads local files and runs only read-only commands, then writes one Markdown digest that the
skill passes to `vidbyte-cli agents suggest run --files`. Raw transcripts never leave the machine.

Sources (newest first, at most --limit sessions in total):
  Claude Code  <config dir>/projects/*/*.jsonl whose recorded cwd is inside a --root. The config
               dir is $CLAUDE_CONFIG_DIR, else ~/.claude, plus every --claude-dir. Subagent files
               under <session>/subagents/ are not sessions and are skipped.
  Codex        <$CODEX_HOME or ~/.codex>/sessions/**/rollout-*.jsonl whose first record
               (session_meta) has a cwd inside a --root. Subagent sessions and programmatic SDK
               sessions (originator codex_python_sdk) are skipped.
  Commands     vidbyte-cli --json runs list --all --limit 200, git log --oneline -30,
               git branch --show-current, git status --porcelain, and, when gh is installed,
               gh pr list --state open --limit 10 --json number,title,headRefName.

Everything written is redacted first: API keys and tokens, NAME=value assignments, emails,
URL credentials, home-directory paths, and the login name. Paths inside a root become
repo-relative, and edits to files outside every root are dropped.

Prints one JSON summary to stdout. Exit 0: digest written. Exit 2: bad arguments.
Exit 3: no sessions found for these roots (nothing written).
"""

from __future__ import annotations

import argparse
import collections
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

CAPS = {"prompt": 400, "last": 300, "reply": 500, "error": 160, "title": 120, "files": 10, "errors": 5}
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
GENERIC_USERS = {"user", "users", "admin", "root", "test", "guest", "owner", "home", "runner", "ubuntu",
                 "vagrant", "ec2-user", "developer", "default", "public"}
PATCH_FILE = re.compile(r"\*\*\* (?:Add|Update|Delete) File: (.+?)(?:\\n|\n|[\"'`]|$)")
FULL_BLOCKS = 20  # newest sessions get a full block; older ones a compact one so ~100 fit the cap
UNFINISHED_HINTS = ("next step", "todo", "not yet", "remaining", "blocked", "follow-up", "follow up",
                    "still fail", "couldn't", "could not", "unable to", "left to do", "pending")


class Redactor:
    """Replaces secrets, emails, and home paths; counts every replacement."""

    def __init__(self, roots):
        self.count = 0
        home = os.path.expanduser("~")
        self.rules = []
        # Roots first (longest first) so in-project paths become repo-relative, then the home dir.
        for root in sorted(set(roots), key=len, reverse=True):
            self._path_rule(root, "", "<repo>")
        self._path_rule(home, "~/", "~")
        # Claude Code names project folders after the path with every non-alphanumeric as "-".
        encoded_home = re.sub(r"[^A-Za-z0-9]", "-", home)
        self.rules.append((re.compile(re.escape(encoded_home), re.IGNORECASE), "~"))
        # Login names also show up outside paths (ls -l owners, prompts); short or generic ones are kept.
        names = {os.path.basename(os.path.normpath(home)), os.environ.get("USER", ""), os.environ.get("USERNAME", "")}
        for user in sorted(n for n in names if len(n) >= 4 and n.lower() not in GENERIC_USERS):
            self.rules.append((re.compile(r"(?<![\w.-])%s(?![\w-])" % re.escape(user), re.IGNORECASE), "<user>"))
        secret = [
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)",
            r"\b(sk|rk|pk)-[A-Za-z0-9_-]{16,}", r"\bvb_(live|test)_[A-Za-z0-9_-]{8,}",
            r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}", r"\bgithub_pat_[A-Za-z0-9_]{20,}",
            r"\bxox[abprs]-[A-Za-z0-9-]{10,}", r"\bAKIA[0-9A-Z]{16}\b", r"\bAIza[0-9A-Za-z_-]{30,}",
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", r"\b0x[0-9a-fA-F]{64}\b",
            r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{12,}",
            r"\b(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{40,}\b",
        ]
        self.rules += [(re.compile(p), "[REDACTED]") for p in secret]
        self.rules += [
            (re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
            (re.compile(r"\b([A-Z][A-Z0-9_]{2,})=(\"[^\"]*\"|'[^']*'|\S+)"), r"\1=[REDACTED]"),
            (re.compile(r"(?i)(\$env:[A-Za-z0-9_]+\s*=\s*)(\"[^\"]*\"|'[^']*'|\S+)"), r"\1[REDACTED]"),
            (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd|credentials?)(\"?\s*[:=]\s*)"
                        r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"), r"\1\2[REDACTED]"),
            (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
            # Any other user's home: C:\Users\x, /c/Users/x and /mnt/c/Users/x, /Users/x, /home/x.
            (re.compile(r"(?i)\b[a-z]:(?:\\\\|\\|/)users(?:\\\\|\\|/)[^\\/\s\"']+"), "~"),
            (re.compile(r"(?<![\w.])/(?:mnt/)?[a-z]/(?:Users|users|home)/[^/\s\"']+"), "~"),
            (re.compile(r"(?<![\w./-])/(?:Users|home)/[^/\s\"']+"), "~"),
        ]

    def _path_rule(self, path, with_tail, bare):
        # Matches the path with either slash style (and JSON-escaped backslashes), any case.
        parts = [re.escape(p) for p in re.split(r"[\\/]+", os.path.normpath(path)) if p]
        if not parts:
            return
        sep = r"(?:\\\\|\\|/)"
        body = (re.escape("/") if path.startswith("/") else "") + sep.join(parts)
        self.rules.append((re.compile(body + sep + "+", re.IGNORECASE), with_tail))
        self.rules.append((re.compile(body + r"(?![\w.-])", re.IGNORECASE), bare))

    def __call__(self, text, cap=None):
        text = str(text or "")
        if cap and len(text) > 4 * cap + 2000:
            # Bound the work, and cut back to whitespace so no half secret survives at the edge.
            text = text[: 4 * cap + 2000].rsplit(None, 1)[0]
        for pattern, replacement in self.rules:
            text, hits = pattern.subn(replacement, text)
            self.count += hits
        text = re.sub(r"\s+", " ", text).strip()
        if cap and len(text) > cap:
            text = text[: cap - 3].rstrip() + "..."
        return text


def norm(path):
    return os.path.normcase(os.path.realpath(os.path.expanduser(path)))


def inside(path, roots):
    if not isinstance(path, str) or not path:
        return False
    p = norm(path)
    for root in roots:
        try:
            if os.path.commonpath([p, root]) == root:
                return True
        except ValueError:  # different drives on Windows
            continue
    return False


def read_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        yield record
    except OSError:
        return


def first_cwd(path, limit=200):
    for index, record in enumerate(read_jsonl(path)):
        if isinstance(record.get("cwd"), str):
            return record["cwd"]
        if index >= limit:
            break
    return None


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # Claude "text" blocks; Codex "input_text"/"output_text" items
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and isinstance(b.get("text"), str))
    return ""


def new_session(source, path):
    return {"source": source, "path": path, "mtime": os.path.getmtime(path), "start": None, "end": None,
            "branch": None, "title": None, "prompts": [], "reply": None, "interrupts": 0,
            "files": collections.Counter(), "errors": [], "error_count": 0, "prs": [], "compactions": 0}


def human_prompt(record):
    # A real typed prompt: not injected (isMeta), not a sidechain or compaction summary, not a
    # task notification or other automatic turn, and not a local-command or reminder wrapper.
    if record.get("isMeta") or record.get("isSidechain") or record.get("isCompactSummary"):
        return None
    if record.get("turnOrigin") not in (None, "human"):
        return None
    text = text_of((record.get("message") or {}).get("content")).strip()
    if not text or text.startswith("[Request interrupted"):
        return None
    command = re.match(r"<command-name>\s*(/?[\w:.-]+)\s*</command-name>", text)
    if command:
        # A slash command with arguments states a goal ("/design-doc build X"); a bare one (/model) does not.
        args = re.search(r"<command-args>([\s\S]*?)</command-args>", text)
        args = args.group(1).strip() if args else ""
        return (command.group(1) + " " + args) if args else None
    if text.startswith("<"):
        return None
    return text


def parse_claude(path):
    s = new_session("claude", path)
    tool_names = {}
    for r in read_jsonl(path):
        kind, stamp = r.get("type"), r.get("timestamp")
        if isinstance(stamp, str):
            s["start"] = s["start"] or stamp
            s["end"] = stamp
        if r.get("gitBranch"):
            s["branch"] = r["gitBranch"]
        if kind in ("ai-title", "custom-title"):
            s["title"] = r.get("customTitle") or r.get("aiTitle") or s["title"]
        elif kind == "pr-link" and r.get("prUrl"):
            s["prs"].append(r["prUrl"])
        elif kind == "system" and r.get("subtype") == "compact_boundary":
            s["compactions"] += 1
        elif kind == "assistant" and not r.get("isSidechain"):
            for block in (r.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    s["reply"] = block["text"]
                elif block.get("type") == "tool_use":
                    data = block.get("input") or {}
                    command = data.get("command") if isinstance(data.get("command"), str) else ""
                    tool_names[block.get("id")] = (block.get("name") or "tool") + (
                        " `%s`" % command[:80] if command else "")
                    target = data.get("file_path") or data.get("notebook_path")
                    if block.get("name") in EDIT_TOOLS and target:
                        s["files"][target] += 1
        elif kind == "user":
            content = (r.get("message") or {}).get("content")
            if text_of(content).startswith("[Request interrupted"):
                s["interrupts"] += 1
            prompt = human_prompt(r)
            if prompt:
                s["prompts"].append(prompt)
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                        add_error(s, tool_names.get(block.get("tool_use_id"), "tool"), text_of(block.get("content")))
    return s


def add_error(s, label, output):
    # Keeps one line per failure: "Exit code N" headers point at the last line, where errors land.
    lines = [x.strip() for x in re.sub(r"</?tool_use_error>", "", str(output or "")).splitlines() if x.strip()]
    first = lines[0] if lines else "failed"
    code = re.match(r"(?i)^exit code:? (-?\d+)$", first)
    detail = ("exit %s: %s" % (code.group(1), lines[-1] if len(lines) > 1 else "")) if code else first
    s["error_count"] += 1
    s["errors"].append("%s %s" % (label, detail))


def parse_codex(path, meta):
    # Codex logs a failure in one of several shapes depending on its version; each call id counts once.
    s = new_session("codex", path)
    s["start"] = meta.get("timestamp")
    s["branch"] = (meta.get("git") or {}).get("branch")
    calls, failed, patched, typed = {}, set(), set(), []
    for r in read_jsonl(path):
        if isinstance(r.get("timestamp"), str):
            s["end"] = r["timestamp"]
        p = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        kind, call = p.get("type"), p.get("call_id")
        if kind == "user_message" and str(p.get("message") or "").strip():
            s["prompts"].append(str(p["message"]))
        elif r.get("type") == "response_item" and kind == "message" and p.get("role") == "user":
            # Newer codex-tui logs typed prompts only here, beside <environment_context>-style blocks.
            for block in p.get("content") or []:
                text = str(block.get("text") or "").strip() if isinstance(block, dict) else ""
                if text and not text.startswith("<") and "AGENTS.md instructions" not in text[:200]:
                    typed.append(text)
        elif kind == "task_complete" and p.get("last_agent_message"):
            s["reply"] = p["last_agent_message"]
        elif kind == "agent_message" and p.get("message"):
            s["reply"] = p["message"]
        elif kind == "turn_aborted":
            s["interrupts"] += 1
        elif kind == "context_compacted":
            s["compactions"] += 1
        elif kind in ("function_call", "custom_tool_call"):
            raw = p.get("arguments") if kind == "function_call" else p.get("input")
            try:
                command = json.loads(raw).get("command") if kind == "function_call" else None
            except (ValueError, TypeError, AttributeError):
                command = None
            command = " ".join(command) if isinstance(command, list) else str(command or "")
            calls[call] = (p.get("name") or "tool") + (" `%s`" % command[-80:] if command else "")
            # apply_patch input, or a code-mode exec script that embeds a patch in a string literal.
            targets = PATCH_FILE.findall(raw) if isinstance(raw, str) else []
            if targets and call not in patched:
                patched.add(call)
                for target in dict.fromkeys(t.strip() for t in targets):
                    s["files"][target] += 1
        elif kind in ("function_call_output", "custom_tool_call_output") and call not in failed:
            out = p.get("output")
            text = text_of(out) if isinstance(out, list) else str(out or "")
            try:
                meta_code = (json.loads(text).get("metadata") or {}).get("exit_code") if text.startswith("{") else None
            except (ValueError, AttributeError):
                meta_code = None
            code = re.match(r"Exit code: (-?\d+)", text)
            if (code and code.group(1) != "0") or (isinstance(meta_code, int) and meta_code != 0) or \
                    re.match(r"Script (failed|error)", text.lstrip()):
                failed.add(call)
                add_error(s, calls.get(call, "tool"), text)
        elif kind == "patch_apply_end":
            if call not in patched:
                patched.add(call)
                for target in (p.get("changes") or {}):
                    s["files"][target] += 1
            if p.get("success") is False and call not in failed:
                failed.add(call)
                add_error(s, "apply_patch", p.get("stderr") or "failed")
        elif kind == "exec_command_end" and isinstance(p.get("exit_code"), int) and p["exit_code"] != 0 \
                and call not in failed:
            failed.add(call)
            command = p.get("command")
            command = " ".join(command) if isinstance(command, list) else str(command or "")
            add_error(s, "exec `%s`" % command[-80:], "Exit code %s\n%s" % (
                p["exit_code"], p.get("stderr") or p.get("aggregated_output") or ""))
    s["prompts"] = s["prompts"] or typed
    return s


def find_claude(config_dirs, roots):
    found = []
    for config in config_dirs:
        for folder in glob.glob(os.path.join(config, "projects", "*")):
            files = sorted(glob.glob(os.path.join(folder, "*.jsonl")), key=os.path.getmtime, reverse=True)
            # Every session in a folder shares its launch directory, so one cwd decides the folder.
            if files and any(inside(first_cwd(f), roots) for f in files[:3]):
                found.extend(files)
    return sorted(set(found), key=os.path.getmtime, reverse=True)


def find_codex(codex_home, roots, limit):
    found = []
    files = glob.glob(os.path.join(codex_home, "sessions", "**", "rollout-*.jsonl"), recursive=True)
    for path in sorted(files, key=os.path.getmtime, reverse=True):
        meta = next(read_jsonl(path), {})
        payload = meta.get("payload") if meta.get("type") == "session_meta" else None
        if not isinstance(payload, dict) or not isinstance(payload.get("source"), str):
            continue  # not a session_meta record, or a subagent session (source is an object)
        if payload.get("originator") == "codex_python_sdk" or not inside(payload.get("cwd"), roots):
            continue
        found.append((path, payload))
        if len(found) >= limit:
            break
    return found


def run(args, cwd, timeout=30):
    try:
        done = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        return done.stdout if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def vidbyte_runs(roots, limit, cwd, exe=None):
    # Free and offline. vidbyte-cli before 0.3.0 has no `runs`; that failure just means no runs.
    exe = exe or shutil.which("vidbyte-cli")
    output = exe and run([exe, "--json", "runs", "list", "--all", "--limit", "200"], cwd, timeout=60)
    try:
        runs = json.loads(output)["data"]["runs"] if output else []
    except (ValueError, KeyError, TypeError):
        return []
    kept = [r for r in runs if inside((r.get("request") or {}).get("working_directory"), roots)]
    return kept[:limit]


def day(stamp):
    return (stamp or "")[:10] or "unknown"


def session_block(tag, s, red, full):
    # Older sessions (full=False) keep the goal, files, and errors but drop the reply and last prompt.
    p, k = s["prompts"], 1 if full else 2
    ended = "interrupted" if s["interrupts"] else "ok"
    if s["reply"] and any(h in s["reply"].lower() for h in UNFINISHED_HINTS):
        ended += "; final reply mentions unfinished work"
    lines = ["### %s · %s · %s · branch %s · %d prompts · %s"
             % (tag, day(s["end"] or s["start"]), s["source"], red(s["branch"] or "?", 60), len(p), ended)]
    if s["title"]:
        lines.append("Title: " + red(s["title"], CAPS["title"]))
    if p:
        lines.append("First prompt: " + red(p[0], CAPS["prompt"] // k))
    if len(p) > 1 and full:
        lines.append("Last prompt: " + red(p[-1], CAPS["last"]))
    if s["reply"] and full:
        lines.append("Final reply: " + red(s["reply"], CAPS["reply"]))
    if s["files"]:
        top = [red(f, 100) for f, _ in s["files"].most_common(CAPS["files"] // k)]
        more = len(s["files"]) - len(top)
        lines.append("Files edited: " + ", ".join(top) + (" (+%d more)" % more if more > 0 else ""))
    if s["errors"]:
        distinct = list(dict.fromkeys(red(e, CAPS["error"]) for e in s["errors"]))[: CAPS["errors"] // k]
        lines.append("Errors (%d): %s" % (s["error_count"], " | ".join(distinct)))
    if s["prs"]:
        lines.append("PRs: " + ", ".join(dict.fromkeys(red(u, 120) for u in s["prs"])))
    return "\n".join(lines)


def error_key(text):
    # Groups the same failure across sessions: drops numbers, quoted values, and path-like tokens.
    text = re.sub(r"\S*[\\/]\S*", "<path>", text.lower())
    return re.sub(r"\s+", " ", re.sub(r"\d+|'[^']*'|\"[^\"]*\"", "#", text)).strip()[:120]


def build(args):
    roots_raw = args.root or [run(["git", "rev-parse", "--show-toplevel"], os.getcwd()) or os.getcwd()]
    roots_raw = [r.strip() for r in roots_raw if r and r.strip()]
    roots = [norm(r) for r in roots_raw]
    red = Redactor(roots_raw + [os.path.realpath(r) for r in roots_raw])
    main_root = roots_raw[0]
    name = red(os.path.basename(os.path.normpath(main_root)), 80) or "project"
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-_") or "project"
    digest_id = hashlib.sha256(roots[0].encode("utf-8")).hexdigest()[:6]

    sessions = []
    if not args.no_claude:
        env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        dirs = [env_dir or "~/.claude"] + list(args.claude_dir or [])
        dirs = list(dict.fromkeys(os.path.expanduser(d) for d in dirs))
        sessions += [parse_claude(f) for f in find_claude(dirs, roots)[: args.limit * 2]]
    if not args.no_codex:
        home = os.path.expanduser(args.codex_home or os.environ.get("CODEX_HOME") or "~/.codex")
        sessions += [parse_codex(f, meta) for f, meta in find_codex(home, roots, args.limit * 2)]
    sessions = [s for s in sessions if s["prompts"]]
    for s in sessions:
        # Edits outside the project (agent memory, global config) say nothing about this project's work.
        s["files"] = collections.Counter({f: n for f, n in s["files"].items() if not os.path.isabs(f) or inside(f, roots)})
    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    sessions = sessions[: args.limit]
    if not sessions:
        return None, {"error": "no sessions found", "roots_checked": len(roots)}

    width = 3 if len(sessions) >= 100 else 2
    tags = {id(s): "S%0*d" % (width, i + 1) for i, s in enumerate(sessions)}
    runs = [] if args.no_vidbyte_runs else vidbyte_runs(roots, args.limit, main_root, args.vidbyte_cli)
    counts = collections.Counter(s["source"] for s in sessions)
    span = sorted(day(s["start"] or s["end"]) for s in sessions)

    errors, files = collections.defaultdict(list), collections.defaultdict(set)
    for s in sessions:
        for e in s["errors"]:
            errors[error_key(red(e))].append((tags[id(s)], red(e, CAPS["error"])))
        for f in s["files"]:
            files[red(f, 100)].add(tags[id(s)])

    head = ["# Session digest: %s" % red(name, 80), "",
            "Built %s by next-from-my-sessions/scripts/digest_sessions.py. Redacted: API keys, tokens, "
            "environment-variable assignments, emails, URL credentials, home-directory paths, the login "
            "name. Paths are relative to the repository root. %s is the newest session; sessions after "
            "%s are abbreviated." % (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                     "S%0*d" % (width, 1), "S%0*d" % (width, FULL_BLOCKS)),
            "", "## Overview",
            "- Sessions: %d (Claude Code %d, Codex %d), %s to %s."
            % (len(sessions), counts["claude"], counts["codex"], span[0], span[-1]),
            "- Interrupted sessions: %s." % (", ".join(tags[id(s)] for s in sessions if s["interrupts"]) or "none"),
            "- Vidbyte runs in this project: %d." % len(runs)]
    repeated = sorted(((k, v) for k, v in errors.items() if len({t for t, _ in v}) > 1),
                      key=lambda kv: -len({t for t, _ in kv[1]}))[:8]
    if repeated:
        head += ["", "## Repeated errors (across sessions)"]
        head += ["- %s: %s" % (" ".join(sorted({t for t, _ in v})), v[0][1]) for _, v in repeated]
    hot = sorted(((f, t) for f, t in files.items() if len(t) > 1), key=lambda kv: -len(kv[1]))[:15]
    if hot:
        head += ["", "## Files edited in several sessions"] + ["- %s (%d sessions)" % (f, len(t)) for f, t in hot]
    if runs:
        head += ["", "## Vidbyte runs"]
        for r in runs[:20]:
            params = (r.get("request") or {}).get("params") or {}
            what = params.get("goal") or params.get("task") or params.get("tasks") or params.get("github_repo") or ""
            head.append("- %s %s %s: %s" % (day(r.get("started_at")), r.get("agent"), r.get("state"),
                                           red(" ".join(what) if isinstance(what, list) else what, 160)))
    repo = []
    if not args.no_git:
        git = [("Branch", ["git", "branch", "--show-current"]), ("Recent commits", ["git", "log", "--oneline", "-30"])]
        for label, command in git:
            out = run(command, main_root)
            if out and out.strip():
                repo.append("%s:\n%s" % (label, "\n".join("- " + red(x, 120) for x in out.strip().splitlines())))
        status = run(["git", "status", "--porcelain"], main_root)
        if status is not None:
            repo.append("Uncommitted files: %d" % len([x for x in status.splitlines() if x.strip()]))
        gh = shutil.which("gh") and run(["gh", "pr", "list", "--state", "open", "--limit", "10", "--json",
                                         "number,title,headRefName"], main_root)
        try:
            prs = json.loads(gh) if gh else []
        except ValueError:
            prs = []
        if prs:
            repo.append("Open PRs:\n" + "\n".join("- #%s %s (%s)" % (p.get("number"), red(p.get("title"), 100),
                                                                    red(p.get("headRefName"), 60)) for p in prs))
    if repo:
        head += ["", "## Repository"] + repo

    text = "\n".join(head) + "\n\n## Sessions (newest first)\n"
    omitted = []
    for index, s in enumerate(sessions):
        block = session_block(tags[id(s)], s, red, index < FULL_BLOCKS) + "\n\n"
        if len(text) + len(block) <= args.max_chars:
            text += block
        else:
            omitted.append(s)
    if omitted:
        tail = "Omitted for size: %s (%s to %s).\n" % (
            ", ".join(tags[id(s)] for s in omitted), day(omitted[-1]["start"]), day(omitted[0]["end"]))
        text = text[: args.max_chars - len(tail)] + tail

    out_dir = args.out or os.path.join(tempfile.gettempdir(), "next-from-my-sessions", "%s-%s" % (slug, digest_id))
    os.makedirs(out_dir, exist_ok=True)
    digest_path = os.path.join(out_dir, "digest.md")
    with open(digest_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return digest_path, {
        "digest_path": digest_path, "out_dir": out_dir, "chars": len(text),
        "sessions": {"claude": counts["claude"], "codex": counts["codex"], "total": len(sessions)},
        "first_day": span[0], "last_day": span[-1],
        "omitted_for_size": len(omitted), "vidbyte_runs": len(runs), "redactions": red.count,
        "project_key": ("sessions-%s" % slug)[:57].rstrip("-_") + "-" + digest_id, "project_title": name[:200],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--root", action="append", help="project root (repeatable; default: git toplevel)")
    parser.add_argument("--limit", type=int, default=100, help="newest sessions to keep, 1-500 (default 100)")
    parser.add_argument("--claude-dir", action="append", help="extra Claude Code config dir (repeatable)")
    parser.add_argument("--codex-home", help="Codex home (default: $CODEX_HOME or ~/.codex)")
    parser.add_argument("--vidbyte-cli", help="vidbyte-cli executable (default: the one on PATH)")
    parser.add_argument("--max-chars", type=int, default=80000, help="digest size cap (default 80000)")
    parser.add_argument("--out", help="output dir (default: <temp>/next-from-my-sessions/<repo>-<hash>)")
    for flag in ("--no-claude", "--no-codex", "--no-vidbyte-runs", "--no-git"):
        parser.add_argument(flag, action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 500 or not 2000 <= args.max_chars <= 1000000:
        parser.error("--limit must be 1-500 and --max-chars 2000-1000000")
    digest_path, summary = build(args)
    print(json.dumps(summary))
    return 0 if digest_path else 3


if __name__ == "__main__":
    sys.exit(main())
