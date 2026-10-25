"""robots.txt parsing and matching per RFC 9309: groups, longest match, wildcards, and status rules."""

from __future__ import annotations

import re
import urllib.parse


class RobotsGroup:
    """One group: the user-agent tokens it names and its allow/disallow rules in file order."""

    def __init__(self) -> None:
        # Agents are lowercased product tokens; rules are (allow: bool, pattern: str).
        self.agents: list = []
        self.rules: list = []


class RobotsTxt:
    """A parsed robots.txt that answers "may this product token fetch this path?"."""

    def __init__(self, text: str = "", status: int = 200, content_type: str = "text/plain") -> None:
        # status 4xx means allow all, 5xx or 0 means disallow all (RFC 9309 2.3.1.3 and 2.3.1.4).
        self.status = status
        self.served_as_html = "html" in (content_type or "") or bool(re.match(r"\s*<(!doctype|html)", text or "", re.I))
        self.groups: list = []
        self.sitemaps: list = []
        self.content_signals: list = []
        self.lines = 0
        self.unknown_lines: list = []
        if 200 <= status < 300 and not self.served_as_html:
            self._parse(text or "")

    @property
    def unreachable(self) -> bool:
        # True when the status makes crawlers treat the whole site as disallowed.
        return self.status == 0 or self.status >= 500

    def allowed(self, agent: str, path: str = "/") -> bool:
        # Whether agent may fetch path: the most specific matching group, then the longest matching rule.
        if self.unreachable:
            return False
        if not (200 <= self.status < 300) or self.served_as_html:
            return True
        rules = self.rules_for(agent)
        best = None
        for allow, pattern in rules:
            if pattern == "":
                continue
            if self._matches(pattern, path):
                length = len(pattern)
                if best is None or length > best[0] or (length == best[0] and allow):
                    best = (length, allow)
        return True if best is None else best[1]

    def rules_for(self, agent: str) -> list:
        # The merged rules of every group naming agent, else of the * groups, else none.
        token = agent.lower()
        named = [g for g in self.groups if token in g.agents]
        if not named and token == "applebot":
            # Apple: "If robots instructions don't mention Applebot but mention Googlebot, the Apple robot will follow Googlebot instructions."
            named = [g for g in self.groups if "googlebot" in g.agents]
        chosen = named or [g for g in self.groups if "*" in g.agents]
        return [rule for group in chosen for rule in group.rules]

    def names(self, agent: str) -> bool:
        # True when a group names this product token explicitly.
        return any(agent.lower() in g.agents for g in self.groups)

    def _parse(self, text: str) -> None:
        # Builds groups: consecutive user-agent lines open one group, rules attach to the open group.
        current = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            self.lines += 1
            key, sep, value = line.partition(":")
            if not sep:
                self.unknown_lines.append(raw.strip()[:120])
                continue
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = RobotsGroup()
                    self.groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if key in ("allow", "disallow"):
                if current is not None:
                    current.rules.append((key == "allow", value))
            elif key == "sitemap":
                self.sitemaps.append(value)
            elif key == "content-signal":
                self.content_signals.append(value)
            elif key not in ("crawl-delay", "host", "clean-param", "request-rate", "visit-time", "noindex"):
                self.unknown_lines.append(raw.strip()[:120])

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        # RFC 9309 matching: prefix match, "*" matches any run of characters, a trailing "$" anchors the end.
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        target = urllib.parse.unquote(path) if "%" in path and "%" not in pattern else path
        return re.match(regex + ("$" if anchored else ""), target) is not None
