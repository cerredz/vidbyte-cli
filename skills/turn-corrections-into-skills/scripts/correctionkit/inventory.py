"""Lists the agent skills the user already has, so a proposal can update one instead of duplicating it.

Only each SKILL.md's frontmatter is read (its name and description); skill bodies are never read or
sent. Personal folders come first, then the skill folders inside the projects where corrections
happened.
"""

from __future__ import annotations

import re
from pathlib import Path

from sessionkit.model import TextTools


class SkillInventory:
    """Finds SKILL.md files in the Claude Code, Codex, OpenCode, and .agents skill folders."""

    MAX_SKILLS = 60
    MAX_PROJECTS = 10
    MAX_BYTES = 8192
    PROJECT_FOLDERS = ((".claude", "skills", "claude project"), (".agents", "skills", "agents project"), (".opencode", "skills", "opencode project"))
    KEY_RE = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")

    def __init__(self, home: Path, claude_dirs: list, codex_home=None) -> None:
        # Remembers the personal roots: each Claude config folder, ~/.agents, and the Codex home.
        self.home = Path(home)
        self.claude_dirs = [Path(d).expanduser() for d in claude_dirs]
        self.codex_home = Path(codex_home).expanduser() if codex_home else self.home / ".codex"

    def scan(self, project_folders: list) -> list:
        # Returns [{"name", "description", "scope", "path"}] for at most 60 skills, personal folders first.
        found: list = []
        seen: set = set()
        for root, scope in self.roots(project_folders):
            for entry in self.read_root(root, scope):
                key = (entry["name"], entry["scope"])
                if key in seen:
                    continue
                seen.add(key)
                found.append(entry)
                if len(found) >= self.MAX_SKILLS:
                    return found
        return found

    def roots(self, project_folders: list) -> list:
        # The (folder, scope label) pairs to look in, in priority order.
        roots = [(d / "skills", "claude personal") for d in self.claude_dirs]
        roots.append((self.home / ".agents" / "skills", "agents personal"))
        roots.append((self.codex_home / "skills", "codex personal"))
        projects = 0
        for folder in project_folders:
            path = Path(folder)
            try:
                exists = path.is_dir()
            except OSError:
                exists = False
            if not exists:
                continue
            projects += 1
            for parent, child, scope in self.PROJECT_FOLDERS:
                roots.append((path / parent / child, "%s %s" % (scope, path.name)))
            if projects >= self.MAX_PROJECTS:
                break
        return roots

    def read_root(self, root: Path, scope: str) -> list:
        # Reads <root>/*/SKILL.md frontmatter; a missing or unreadable folder yields nothing.
        entries = []
        try:
            paths = sorted(root.glob("*/SKILL.md"))
        except OSError:
            return entries
        for path in paths:
            name, description = self.frontmatter(path)
            if name is None:
                continue
            entries.append({"name": name or path.parent.name, "description": TextTools.one_line(description, 300), "scope": scope, "path": str(path)})
        return entries

    def frontmatter(self, path: Path) -> tuple:
        # Returns (name, description) from a SKILL.md's YAML frontmatter, ("", "") without one, or (None, None) when unreadable.
        try:
            with open(str(path), encoding="utf-8", errors="replace") as handle:
                text = handle.read(self.MAX_BYTES)
        except OSError:
            return None, None
        lines = text.lstrip("﻿").splitlines()
        if not lines or lines[0].strip() != "---":
            return "", ""
        fields: dict = {}
        key, block = None, []
        for line in lines[1:]:
            if line.strip() == "---":
                break
            match = self.KEY_RE.match(line)
            if match and not line.startswith((" ", "\t")):
                if key:
                    fields[key] = self.join_block(fields.get(key, ""), block)
                key, block = match.group(1), []
                fields[key] = match.group(2).strip()
            elif key:
                block.append(line.strip())
        if key:
            fields[key] = self.join_block(fields.get(key, ""), block)
        return self.unquote(fields.get("name", "")), self.unquote(fields.get("description", ""))

    @staticmethod
    def join_block(head: str, block: list) -> str:
        # Joins a folded (>) or literal (|) block, or continuation lines, onto one line.
        head = "" if head in (">", "|", ">-", "|-", ">+", "|+") else head
        return " ".join([head] + [b for b in block if b]).strip()

    @staticmethod
    def unquote(value: str) -> str:
        # Strips one pair of matching YAML quotes.
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            return value[1:-1]
        return value
