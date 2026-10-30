#!/usr/bin/env python3
"""Lints the public skills for free substitutes of their paid result (design doc: docs/design/paid-work-boundary.md).

A public skill delivers the output of one Vidbyte run. This lint fails when any skill, script,
reference, plugin file, or writing-contract file offers, describes, or enables a way to get that
output, or part of it, without the run, and when a skill is missing a piece of the paid work
boundary. It reads prose sentence by sentence, because "free" on its own is correct for preflight,
dry runs, and handoffs, and a substitute can be worded in endless ways.

Rule families:
  PB1xx  prose phrases (free plus substitute, free plus result, "free <noun>", always-wrong wording)
  PB2xx  scripts and code blocks (flags and identifiers that skip or fake the run)
  PB3xx  skill structure (section, anchors, guardrail, consent, description, reference copy)

    python scripts/check-paid-work-boundary.py             # lint this checkout
    python scripts/check-paid-work-boundary.py --root DIR  # lint another tree, such as an export of main

Exits 0 when clean and 1 when any finding is reported. Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

CANONICAL_REL = "docs/agent-skills/write-agent-skill/references/paid-work-boundary.md"
REFERENCE_NAME = "paid-work-boundary.md"
SECTION_HEADING = "## Paid work boundary"
MAX_DESCRIPTION = 1024
SCRIPT_SUFFIXES = {".py", ".sh", ".ps1", ".js", ".mjs", ".ts"}

NEGATION = re.compile(r"\b(never|not|cannot|can't|don't|doesn't|won't|refuse|refuses|forbid|forbids|forbidden|no one)\b|n't\b")

FREE_SIGNAL = re.compile(
    r"\bfree\b(?![- ](?:time|stretch|stretches|memory|tier|tiers|text|space|slots?|blocks?|form|plan|plans|account|accounts|disk)\b)"
    r"|\bfree of charge\b|\bat no (?:extra )?(?:cost|charge)\b|\bno[- ]cost\b|\bcosts? (?:you |the user )?nothing\b"
    r"|\bwithout (?:paying|payment|a charge|being (?:charged|billed)|spending(?: anything| money)?)\b"
    r"|\bzero[- ](?:cost|dollars?)\b|\bgratis\b|\bcomplimentary\b|\bon the house\b|\bnot billed\b|\bunbilled\b"
    r"|\bfor nothing\b|\bno charge\b|\bno vidbyte charge\b|\bdoes not cost\b|\bwon't cost\b|\bcost-free\b|\bfreebie\b"
    r"|\bfree[- ](?:tiers?|plans?|version|mode) of (?:this|the|our) (?:skill|run|agent|result|product)\b"
)

SUBSTITUTE_SIGNAL = re.compile(
    r"\binstead\b|\balternatives?\b|\balternatively\b|\brather than\b|\bin place of\b|\bor should i\b"
    r"|\bor would you (?:like|prefer)\b|\bor i (?:can|could)\b|\bor we (?:can|could)\b|\bby hand\b|\bmanually\b"
    r"|\b(?:myself|yourself|ourselves)\b|\bon (?:my|your|our) own\b|\b(?:check|audit|digest|inputs?|data|read|scan|files?|gather|measurements?) alone\b|\blocal[- ]only\b"
    r"|\boffline (?:version|result|report|checklist|summary)\b|\bwithout (?:the|a|any) (?:paid |usage-billed |vidbyte )?(?:run|agent)\b"
    r"|\bskip(?:ping|s)? the (?:paid |usage-billed )?(?:run|agent|step|charge)\b|\bbypass\w*\b|\bwork ?arounds?\b"
    r"|\bfall ?backs?\b|\bsubstitut\w*\b|\bapproximat\w*\b|\bpreviews?\b|\bsamples?\b|\bsneak peek\b|\bteaser\b"
    r"|\brough (?:version|draft|cut|pass)\b|\b(?:lite|light|lightweight|basic|quick|starter|mini|trial) version\b"
    r"|\bpoor man's\b|\bdiy\b|\bdo-it-yourself\b|\bjust (?:do|build|make|write|give|run through)\b|\bhome-?made\b"
)

DELIVERABLE_SIGNAL = re.compile(
    r"\bchecklists?\b|\bsummar(?:y|ies|ize|ise|ized|ised)\b|\breports?\b|\branking\b|\branked\b|\bideas?\b"
    r"|\bplans?\b|\brecommendations?\b|\bfixes\b|\bcritiques?\b|\breview order\b|\banswers?\b|\btactics\b"
    r"|\broutines?\b|\bbriefs?\b|\bfindings\b|\bresults?\b|\bnext (?:actions?|steps?)\b|\bobjections\b"
    r"|\bresolutions?\b|\banalys[ie]s\b|\boverview\b|\binsights?\b|\bpriorities\b|\bshortlist\b|\btop \d+\b"
    r"|\bthe list\b|\ba list\b|\bhighlights\b|\btakeaways\b|\bgist\b|\brundown\b|\bbreakdown\b|\bsuggestions\b"
    r"|\badvice\b|\bitems\b|\btop (?:two|three|four|five|ten|few)\b|\bmost (?:urgent|important)\b"
)

PLUMBING_SIGNAL = re.compile(
    r"^`vidbyte-cli\b|\bread the result\b|\bdry[-_ ]?runs?\b|--check\b|`[^`]*\breport\b[^`]*`|\bpreflight\b|\bhandoffs?\b|\bfeedback\b|\bwhoami\b|\bstatus\b|\bcollect\b|\bwatch\b"
    r"|\bsetup\b|\blogin\b|\bproject (?:list|create)\b|\bledger\b|\bre-?audit\w*\b|\bre-?check\w*\b|\bre-?measure\w*\b"
    r"|\bcheck the (?:whole )?request\b|--version\b|\bcli version\b|\binstalled version\b|\bexecution_prompt\b|\baction packet\b|\bsaved result\b"
    r"|\bpaid result\b|\bthe run's result\b|\bresult the run\b|\ba run produced\b|\bthe run returned\b"
)

FREE_NOUN = re.compile(r"\bfree\b((?:[ ,]+[a-z][a-z'-]*){1,3})")
FREE_NOUN_MODIFIERS = {"local", "offline", "manual", "basic", "lite", "light", "quick", "starter", "rough", "simple", "partial", "limited", "mini", "sample", "trial", "no-cost", "first", "small", "short", "extra", "bonus", "instant", "early"}
FREE_NOUN_OK = {
    "dry", "dry-run", "check", "checks", "preflight", "command", "commands", "handoff", "handoffs", "feedback",
    "status", "script", "scripts", "setup", "login", "re-audit", "recheck", "re-check", "re-checks", "re-run",
    "plan", "plans", "tier", "tiers", "account", "accounts", "time", "stretch", "stretches", "memory", "text",
    "space", "slot", "slots", "block", "blocks", "form", "board", "whoami", "version", "steps", "step", "lookups",
    "lookup", "call", "calls", "validation", "validate", "ledger", "measure", "re-measure", "gather",
}
FUNCTION_WORDS = {"and", "or", "to", "of", "for", "on", "in", "at", "by", "with", "the", "a", "an", "it", "is", "are", "and/or", "because", "but", "so", "as", "from", "this", "that", "here", "too", "then", "now"}

ALWAYS_WRONG = [
    ("PB104", "offers a gathered file as the result", re.compile(r"\b(?:offer|give|hand|hand over|send|share|provide|return|deliver)\b.{0,60}?(?:\.md\b|\.json\b|\bdigest\b|\baudit\b|\blocal (?:check|scan|read)\b|\bgathered\b|\binputs\b|\bnotes\b).{0,30}?\bas (?:a|an|the|their|your) (?:\w+ ){0,2}(?:summary|checklist|report|overview|list|result|alternative|substitute|preview|digest|answer|ranking|review order|plan)\b")),
    ("PB105", "builds the result by hand", re.compile(r"\b(?:build|make|write|produce|create|generate|draft|put together|assemble|compile|do|prepare)\b[^.]{0,60}?\b(?:yourself|myself|ourselves|by hand|manually)\b")),
    ("PB122", "does the run's own work by hand", re.compile(r"\b(?:rank|resolve|implement|research|answer|predict|triage|prioriti[sz]e|summari[sz]e|critique|order)\b[^.]{0,60}?\b(?:yourself|myself|ourselves|by hand|manually)\b")),
    ("PB121", "says the work can be had without paying", re.compile(r"\bcan (?:be )?(?:done|had|got|gotten|made|built|produced) (?:for free|gratis|at no cost|without paying|for nothing|free of charge)\b")),
    ("PB106", "names a local-only path", re.compile(r"\blocal[- ]only (?:checklist|report|mode|path|version|result|summary|option|flag|output|list|plan|answer|build)s?\b")),
    ("PB107", "builds the result from the inputs alone", re.compile(r"\b(?:from|use|using|with|on|read|reading) (?:the |your |their )?(?:local (?:check|audit|read|scan|digest|data)|digest|gathered (?:files|inputs|data)|inputs?|audit|sessions\.json|prs\.md|meetings\.md) alone\b")),
    ("PB108", "skips or avoids the run", re.compile(r"\b(?:without|instead of|skip(?:s|ping)?|bypass(?:es|ing)?|avoid(?:s|ing)?|dodg(?:e|es|ing))\s+(?:the\s+|a\s+|any\s+)?(?:paid|usage-billed|vidbyte|billed|metered)\s+(?:run|agent|step|call|charge)s?\b")),
    ("PB109", "adds a second option to a consent question", re.compile(r"\b(?:or should i|or shall i|or i can|or i could|or would you (?:like|prefer|rather)|or do you want me to|or we can|or we could)\b")),
    ("PB110", "lets the user pick a free or local path", re.compile(r"\b(?:chose|choose|chooses|choosing|pick|picks|picked|prefer|prefers|preferred|wants?|wanted|opts? for|opted for) (?:the )?(?:free|local|offline|cheaper|manual|no-cost|unpaid)\b")),
    ("PB111", "offers a cheaper substitute", re.compile(r"\b(?:cheaper|cheapest|lower-cost|low-cost|budget|discount(?:ed)?) (?:version|option|alternative|way|route|model|path|substitute|mode|run of your own)\b")),
    ("PB112", "routes the work to another model or key", re.compile(r"\b(?:(?:your|their|the user's) own (?:openai|anthropic|claude|gpt|model|api|llm) (?:key|account|model)s? instead|(?:paste|copy|send|give|export)\b[^.]{0,40}\b(?:into|to|in) (?:chatgpt|claude\.ai|gemini|another model|a different model|your own model|any llm|an llm))\b")),
    ("PB113", "interprets the inputs into a result", re.compile(r"\b(?:summari[sz]e|rank|prioriti[sz]e|interpret|analy[sz]e|go through|triage)\b (?:the |your |their |what's in the )?(?:digest|gathered (?:files|inputs|data)|inputs|local check|audit results|audit findings|prs\.md|meetings\.md)\b")),
    ("PB114", "fakes or predicts the run", re.compile(r"\b(?:pretend|simulate|imagine|guess|predict|role-?play|emulate|mimic)\b[^.]{0,30}\b(?:the )?(?:run|agent|suggestion agent|task board|bug finder|researcher)\b[^.]{0,40}\b(?:would|might|will|could)\b")),
    ("PB115", "writes a run's output by hand", re.compile(r"\b(?:write|edit|fake|fabricate|forge|create|hand-write|handwrite)\b[^.]{0,20}`?result\.json`?\b")),
    ("PB116", "says paying is unnecessary", re.compile(r"\b(?:no need|don't need|do not need|needn't|need not|no reason) to (?:pay|run the (?:paid )?(?:run|agent)|spend|use the (?:paid )?(?:run|agent))\b")),
    ("PB117", "offers to do the work instead of the run", re.compile(r"\b(?:i|we)(?: can| could| will|'ll| would|'d) (?:just |also |quickly )?(?:do|build|write|make|generate|produce|create|draft|put together|sketch|whip up|knock out|list|rank|summarize|summarise|pick) (?:it|this|that|one|them|the \w+(?: \w+)?) (?:myself|ourselves|here|locally|for you instead|instead|for free|without)\b")),
    ("PB118", "frames the run as optional", re.compile(r"\b(?:the run|the agent|the paid run|vidbyte) is optional\b|\b(?:optional|skippable) (?:paid )?(?:run|step)\b")),
    ("PB120", "replaces the run with something else", re.compile(r"\b(?:instead of|rather than) (?:running|paying for|using|starting|buying|waiting for) (?:the |a |this )?(?:\w+ )?(?:run|agent|suggestion agent|task board|board|bug finder|researcher)\b(?! of)")),
    ("PB119", "uses the skill's instructions outside the run", re.compile(r"\b(?:apply|use|follow|run through|work through)\b (?:the |this skill's |its )?(?:rubric|checklist file|ledger|focus (?:file|instructions)|reference files?)\b[^.]{0,30}\b(?:yourself|myself|directly|by hand|without)\b")),
]

CODE_FLAGS = re.compile(r"--(?:local-only|free|no-paid|skip-run|skip-paid|offline-result|without-run|no-run|local-result|fake-result|preview-only|no-model|manual-result)\b")
CODE_IDENTIFIERS = re.compile(r"\blocal_only\b|\bLOCAL_ONLY\w*\b|\bfree_(?:summary|checklist|report|mode|result|path)\b|\bskip_paid\w*\b|\bfake_result\w*\b")


@dataclass
class Finding:
    """One lint finding: where, which rule, what matched, and how to fix it."""

    path: str
    line: int
    rule: str
    title: str
    detail: str


class PhraseCatalog:
    """Every prose rule, applied to one sentence at a time."""

    def __init__(self, slugs: set = frozenset()) -> None:
        # Keeps skill slugs so names such as stop-repeating-yourself never match as words.
        self.slug_pattern = re.compile("|".join(sorted((re.escape(s) for s in slugs), key=len, reverse=True))) if slugs else None

    def scan(self, sentence: str, free_rules: bool = True) -> list:
        # Returns (rule, title, matched) tuples for every rule the sentence breaks; free_rules=False keeps only always-wrong rules.
        text = self.clean(sentence)
        raw = self.clean(sentence, keep_code=True)
        hits = []
        flag = CODE_FLAGS.search(sentence)
        if flag:
            hits.append(("PB201", "uses a flag that skips or fakes the run", flag.group(0)))
        if free_rules:
            hits += self.free_with_substitute(text)
            hits += self.free_with_result(text, raw)
            hits += self.free_noun(text)
        for rule, title, pattern in ALWAYS_WRONG:
            target = self.unquoted(text) if rule == "PB105" else text
            if rule == "PB105" and not DELIVERABLE_SIGNAL.search(target):
                continue
            for start, matched in self.closest_matches(pattern, target):
                if not self.negated(target, start):
                    hits.append((rule, title, matched))
                    break
        return hits

    @staticmethod
    def closest_matches(pattern, text: str) -> list:
        # Finds overlapping matches and keeps, for each end point, the one that starts last, so "nothing to order ... never order it yourself" is judged by the second verb.
        best = {}
        for match in re.finditer(f"(?=({pattern.pattern}))", text):
            start, end = match.start(1), match.end(1)
            if end not in best or start > best[end][0]:
                best[end] = (start, match.group(1))
        return [best[end] for end in sorted(best)]

    @staticmethod
    def unquoted(text: str) -> str:
        # Drops double-quoted spans, which quote what a user types rather than tell the agent what to do.
        return re.sub(r"\"[^\"]*\"|“[^”]*”", " quote ", text)

    def clean(self, sentence: str, keep_code: bool = False) -> str:
        # Lowercases, drops links, URLs, skill slugs, and (unless keep_code) inline code, and collapses whitespace.
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", sentence)
        if not keep_code:
            text = re.sub(r"`[^`]*`", " code ", text)
        text = re.sub(r"https?://\S+", " url ", text)
        text = text.lower().replace("’", "'")
        if self.slug_pattern:
            text = self.slug_pattern.sub(" skillname ", text)
        return " ".join(text.replace("*", " ").replace("_", " ").split())

    def free_with_substitute(self, text: str) -> list:
        # PB101: a cost-free signal next to a substitute signal, unless the substitute is negated.
        free = FREE_SIGNAL.search(text)
        if not free:
            return []
        for sub in SUBSTITUTE_SIGNAL.finditer(text):
            if not self.negated(text, sub.start()) and not self.negated(text, free.start()):
                return [("PB101", "pairs a cost-free signal with a substitute", f"{free.group(0)} ... {sub.group(0)}")]
        return []

    def free_with_result(self, text: str, raw: str) -> list:
        # PB102: a cost-free signal next to a result word, outside plumbing and post-result steps (read with code kept).
        free = FREE_SIGNAL.search(text)
        if not free or PLUMBING_SIGNAL.search(raw) or self.negated(text, free.start()):
            return []
        result = DELIVERABLE_SIGNAL.search(text)
        if not result:
            return []
        return [("PB102", "calls a result free", f"{free.group(0)} ... {result.group(0)}")]

    def free_noun(self, text: str) -> list:
        # PB103: "free <noun>" where the noun is not a plumbing step or an unrelated sense of free.
        for match in FREE_NOUN.finditer(text):
            if self.negated(text, match.start()):
                continue
            words = re.findall(r"[a-z][a-z'-]*", match.group(1))
            if not words or match.group(1).lstrip().startswith(","):
                continue
            head = [w for w in words if w not in FREE_NOUN_MODIFIERS]
            if not head or head[0] in FUNCTION_WORDS or head[0] in FREE_NOUN_OK:
                continue
            if len(head) > 1 and head[1] in FREE_NOUN_OK:
                continue
            if head[0] in ("code", "url", "skillname"):
                continue
            return [("PB103", "offers a free thing that is not a plumbing step", f"free{match.group(1)}")]
        return []

    @staticmethod
    def negated(text: str, position: int) -> bool:
        # True when a negation opens the same clause before position (at most 160 characters back).
        window = text[max(0, position - 160):position]
        window = re.split(r"[;:]|\bbut\b|\bthen\b|, and (?!never\b|not\b|no\b)", window)[-1]
        return bool(NEGATION.search(window))


class MarkdownSentences:
    """Splits a Markdown file into prose sentences and code lines, with line numbers."""

    SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]*]*\s+(?=[A-Z\"`(*\[>-])")

    def __init__(self, text: str) -> None:
        # Keeps the text with LF line endings and notes where the frontmatter ends.
        self.lines = text.replace("\r\n", "\n").split("\n")
        self.body_start = 0
        if self.lines and self.lines[0].strip() == "---":
            closing = next((i for i in range(1, len(self.lines)) if self.lines[i].strip() == "---"), None)
            self.body_start = closing + 1 if closing is not None else 0

    def prose(self) -> list:
        # Returns (line, sentence) pairs for every prose sentence, table row, and blockquote after the frontmatter.
        units, block, start, in_code = [], [], 0, False
        for number, line in enumerate(self.lines, 1):
            if number <= self.body_start:
                continue
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                in_code = not in_code
                units, block = self.flush(units, block, start), []
                continue
            if in_code:
                continue
            if not stripped or stripped.startswith(("#", "|", "- ", "* ")) or re.match(r"^\d+\.\s", stripped) or stripped == "---":
                units, block = self.flush(units, block, start), []
                if stripped.startswith("|"):
                    units.append((number, " ".join(c.strip() for c in stripped.strip("|").split("|"))))
                    continue
                if stripped.startswith("#") or not stripped or stripped == "---":
                    if stripped.startswith("#"):
                        units.append((number, stripped.lstrip("# ")))
                    continue
            if not block:
                start = number
            block.append(re.sub(r"^(?:>\s?)+", "", stripped))
            if stripped.startswith(("- ", "* ")) or re.match(r"^\d+\.\s", stripped):
                continue
        units = self.flush(units, block, start)
        return [pair for unit in units for pair in self.split(unit)]

    def code(self) -> list:
        # Returns (line, text) pairs for every line inside a fenced code block.
        out, in_code = [], False
        for number, line in enumerate(self.lines, 1):
            if line.strip().startswith(("```", "~~~")):
                in_code = not in_code
                continue
            if in_code:
                out.append((number, line))
        return out

    @staticmethod
    def flush(units: list, block: list, start: int) -> list:
        # Adds the block, if any, as one unit: its first line number and its lines.
        if block:
            units.append((start, list(block)))
        return units

    def split(self, unit: tuple) -> list:
        # Splits one unit into sentences, each tagged with the line it starts on.
        start, lines = unit
        if isinstance(lines, str):
            return [(start, lines)]
        offsets, joined = [], ""
        for line in lines:
            offsets.append(len(joined))
            joined += line + " "
        joined = re.sub(r"^(?:[-*]|\d+\.)\s+", lambda m: " " * len(m.group(0)), joined)
        parts, cursor = [], 0
        for piece in self.SENTENCE_END.split(joined):
            position = joined.find(piece, cursor)
            cursor = position + len(piece)
            if piece.strip():
                line_index = max(i for i, offset in enumerate(offsets) if offset <= position + (len(piece) - len(piece.lstrip())))
                parts.append((start + line_index, piece.strip()))
        return parts


class SkillStructure:
    """The PB3xx rules for one skills/<slug>/SKILL.md."""

    ANCHORS = [
        "work on Vidbyte's behalf",
        "comes only from",
        "On a no, stop.",
        "never write or edit a run's output by hand",
        "answer truthfully",
        "references/paid-work-boundary.md",
    ]
    CONSENT_ANCHOR = "The consent question has two answers. On a no, stop as Paid work boundary says"
    DESCRIPTION_END = re.compile(r"\bcomes? only from th(?:at|ose) (?:paid )?runs?\.$")

    def __init__(self, path: Path, rel: str, canonical: str) -> None:
        # Reads the skill file and remembers the canonical rulebook text.
        self.path = path
        self.rel = rel
        self.text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        self.flat = " ".join(self.text.split())
        self.canonical = canonical

    def check(self) -> list:
        # Runs every structure rule and returns the findings.
        findings = []
        findings += self.check_section()
        findings += self.check_guardrail()
        findings += self.check_consent()
        findings += self.check_done_when()
        findings += self.check_description()
        findings += self.check_reference()
        return findings

    def check_section(self) -> list:
        # PB301-PB303: one boundary section, placed after Cost and consent and before Run, with every anchor.
        headings = [(m.start(), m.group(1)) for m in re.finditer(r"^## (.+)$", self.text, re.M)]
        names = [name for _, name in headings]
        count = names.count("Paid work boundary")
        if count != 1:
            return [self.finding(1, "PB301", "needs exactly one Paid work boundary section", f"found {count}; run scripts/sync-paid-work-boundary.py")]
        index = names.index("Paid work boundary")
        findings = []
        if index == 0 or names[index - 1] != "Cost and consent" or index + 1 >= len(names) or names[index + 1] != "Run":
            findings.append(self.finding(self.line_of(headings[index][0]), "PB302", "puts Paid work boundary in the wrong place", "it must come right after Cost and consent and right before Run"))
        body = self.section_text("Paid work boundary")
        flat_body = " ".join(body.split())
        for anchor in self.ANCHORS:
            if anchor not in flat_body:
                findings.append(self.finding(self.line_of(headings[index][0]), "PB303", "lacks a required boundary sentence", f"missing {anchor!r}; re-sync from the canonical template"))
        return findings

    def check_guardrail(self) -> list:
        # PB304: the Guardrails section restates the boundary.
        if "(Paid work boundary)." not in " ".join(self.section_text("Guardrails").split()):
            return [self.finding(self.heading_line("Guardrails"), "PB304", "lacks the boundary guardrail", "add the '- Deliver ... (Paid work boundary).' bullet")]
        return []

    def check_consent(self) -> list:
        # PB305 and PB306: Cost and consent ends the no-branch, and no consent question offers a second option.
        section = self.section_text("Cost and consent")
        findings = []
        if self.CONSENT_ANCHOR not in " ".join(section.split()):
            findings.append(self.finding(self.heading_line("Cost and consent"), "PB305", "lacks the two-answers sentence", f"add: {self.CONSENT_ANCHOR}: nothing is spent, and nothing else is offered."))
        quote = " ".join(re.sub(r"^>\s?", "", l) for l in section.split("\n") if l.startswith(">"))
        for question in re.findall(r"[^.?!]*\?", quote):
            if re.search(r",? or (?:should|shall|can|could|would|do you want)\b|\bor (?:i|we|you)\b.*\b(?:instead|free|myself|yourself)\b", question, re.I):
                findings.append(self.finding(self.heading_line("Cost and consent"), "PB306", "adds a second option to the consent question", question.strip()))
        return findings

    def check_done_when(self) -> list:
        # PB307: Done-when never accepts a result made without the run.
        section = " ".join(self.section_text("Goal and done-when").split())
        match = re.search(r"\(?or (?:the user|they) (?:chose|chooses|declined|picked|preferred|opted for|took) [^.;)]*", section, re.I)
        if match:
            return [self.finding(self.heading_line("Goal and done-when"), "PB307", "accepts a result made without the run", match.group(0))]
        return []

    def check_description(self) -> list:
        # PB308 and PB309: the description fits 1024 characters and ends with the run sentence.
        match = re.search(r"^description: (.*)$", self.text, re.M)
        if not match:
            return [self.finding(2, "PB308", "has no single-line description", "keep description on one frontmatter line")]
        description = match.group(1).strip()
        findings = []
        if len(description) > MAX_DESCRIPTION:
            findings.append(self.finding(self.line_of(match.start()), "PB308", "has a description over 1024 characters", f"{len(description)} characters"))
        if not self.DESCRIPTION_END.search(description):
            findings.append(self.finding(self.line_of(match.start()), "PB309", "lacks the run sentence at the end of the description", "end it with e.g. 'The result comes only from that paid run.'"))
        return findings

    def check_reference(self) -> list:
        # PB310: references/paid-work-boundary.md exists and matches the canonical copy.
        target = self.path.parent / "references" / REFERENCE_NAME
        if not target.exists():
            return [self.finding(1, "PB310", "lacks references/paid-work-boundary.md", f"copy {CANONICAL_REL}")]
        if target.read_text(encoding="utf-8").replace("\r\n", "\n") != self.canonical:
            return [self.finding(1, "PB310", "has a references/paid-work-boundary.md that differs from the canonical copy", f"re-copy {CANONICAL_REL}; never edit a copy by hand")]
        return []

    def section_text(self, name: str) -> str:
        # Returns a level-2 section's body, or an empty string when the section is missing.
        match = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |^Docs: |\Z)", self.text, re.M | re.S)
        return match.group(1) if match else ""

    def heading_line(self, name: str) -> int:
        # Returns the line of a level-2 heading, or 1 when it is missing.
        match = re.search(rf"^## {re.escape(name)}$", self.text, re.M)
        return self.line_of(match.start()) if match else 1

    def line_of(self, offset: int) -> int:
        # Converts a character offset into a 1-based line number.
        return self.text.count("\n", 0, offset) + 1

    def finding(self, line: int, rule: str, title: str, detail: str) -> Finding:
        # Builds a finding for this skill file.
        return Finding(self.rel, line, rule, title, detail)


class ScriptScanner:
    """The PB2xx rules for code: flags and identifiers that skip or fake the run, plus prose rules on each line."""

    def __init__(self, catalog: PhraseCatalog) -> None:
        # Shares the prose catalog for comments and help strings.
        self.catalog = catalog

    def scan_lines(self, rel: str, pairs: list) -> list:
        # Returns findings for (line, text) pairs from a script or a fenced code block.
        findings = []
        for number, line in pairs:
            flag = CODE_FLAGS.search(line)
            if flag:
                findings.append(Finding(rel, number, "PB201", "uses a flag that skips or fakes the run", flag.group(0)))
            ident = CODE_IDENTIFIERS.search(line)
            if ident:
                findings.append(Finding(rel, number, "PB202", "uses an identifier for a path without the run", ident.group(0)))
            for rule, title, matched in self.catalog.scan(line):
                if rule not in ("PB102", "PB103", "PB201"):
                    findings.append(Finding(rel, number, rule, title, matched))
        return findings


class BoundaryLinter:
    """Collects the files, runs every rule, and prints the report."""

    WHY = {
        "PB1": "A skill's result comes only from its Vidbyte run; wording that offers or describes a free or local version invites the host agent to make one.",
        "PB2": "A flag or code path that skips or fakes the run turns the skill itself into a way around the run.",
        "PB3": "Every skill carries the same boundary so the host agent meets it whichever skill it loads.",
    }
    FIX = {
        "PB1": "Remove the substitute. If the step is legitimately free (preflight, dry run, gather, handoff, feedback, rendering a paid result), name the step instead of the word 'free', or tie it to the paid result. Never add an exemption.",
        "PB2": "Delete the flag and its code path; the result must come from result.json written by the run.",
        "PB3": "Run python scripts/sync-paid-work-boundary.py, then fix the skill by hand only where the sync cannot.",
    }

    def __init__(self, root: Path) -> None:
        # Points at the tree to lint and loads the canonical rulebook.
        self.root = root
        canonical = root / CANONICAL_REL
        self.canonical = canonical.read_text(encoding="utf-8").replace("\r\n", "\n") if canonical.exists() else None
        slugs = {p.parent.name for p in (root / "skills").glob("*/SKILL.md")}
        self.catalog = PhraseCatalog(slugs | {"paid-work-boundary"})
        self.scripts = ScriptScanner(self.catalog)

    def run(self) -> list:
        # Returns every finding in the tree, sorted by file and line.
        findings = []
        for path in self.markdown_files():
            findings += self.lint_markdown(path)
        for path in self.script_files():
            rel = self.rel(path)
            lines = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").split("\n")
            findings += self.scripts.scan_lines(rel, list(enumerate(lines, 1)))
        for skill in sorted((self.root / "skills").glob("*/SKILL.md")):
            if self.canonical is None:
                findings.append(Finding(CANONICAL_REL, 1, "PB310", "canonical rulebook is missing", "restore it"))
                break
            findings += SkillStructure(skill, self.rel(skill), self.canonical).check()
        return sorted(findings, key=lambda f: (f.path, f.line, f.rule))

    def markdown_files(self) -> list:
        # Returns every Markdown file the prose rules cover, minus exact copies of the canonical rulebook.
        patterns = ["skills/**/*.md", "plugins/**/*.md", "docs/agent-skills/**/*.md", "README.md"]
        files = sorted({p for pattern in patterns for p in self.root.glob(pattern) if p.is_file()})
        return [p for p in files if not self.is_canonical_copy(p)]

    def script_files(self) -> list:
        # Returns every script under skills/*/scripts and plugins.
        files = [p for base in ("skills", "plugins") for p in (self.root / base).rglob("*") if p.is_file() and p.suffix in SCRIPT_SUFFIXES]
        return sorted(p for p in files if "__pycache__" not in p.parts)

    def is_canonical_copy(self, path: Path) -> bool:
        # True for the canonical rulebook and for byte-identical copies of it, which quote forbidden requests on purpose.
        if path.name != REFERENCE_NAME or self.canonical is None:
            return False
        return path.read_text(encoding="utf-8").replace("\r\n", "\n") == self.canonical

    @staticmethod
    def is_domain_reference(path: Path) -> bool:
        # True for a skill's domain reference (a ledger, rubric, or checklist the run reads), where "free" describes the user's own product.
        return path.parent.name == "references" and path.parent.parent.parent.name == "skills" and path.name not in ("setup.md", "troubleshooting.md")

    def lint_markdown(self, path: Path) -> list:
        # Runs the prose rules on every sentence and the code rules on every fenced code line.
        rel = self.rel(path)
        text = path.read_text(encoding="utf-8")
        doc = MarkdownSentences(text)
        findings = []
        free_rules = not self.is_domain_reference(path)
        for number, sentence in doc.prose():
            for rule, title, matched in self.catalog.scan(sentence, free_rules):
                findings.append(Finding(rel, number, rule, title, f"{matched!r} in: {sentence[:160]}"))
        findings += self.scripts.scan_lines(rel, doc.code())
        description = re.search(r"^description: (.*)$", text.replace("\r\n", "\n"), re.M)
        if description and path.name == "SKILL.md":
            for rule, title, matched in self.catalog.scan(description.group(1)):
                findings.append(Finding(rel, 3, rule, title, f"{matched!r} in the description"))
        return findings

    def rel(self, path: Path) -> str:
        # Returns a POSIX path relative to the linted root.
        return path.relative_to(self.root).as_posix()

    def report(self, findings: list) -> int:
        # Prints every finding with why and how to fix it, then the summary line; returns the exit code.
        for finding in findings:
            family = finding.rule[:3]
            print(f"{finding.path}:{finding.line}: {finding.rule} {finding.title}")
            print(f"    matched: {finding.detail}")
            print(f"    why: {self.WHY[family]}")
            print(f"    fix: {self.FIX[family]}")
        if findings:
            print(f"PAID-WORK-BOUNDARY: FAIL - {len(findings)} finding(s). Re-run: python scripts/check-paid-work-boundary.py")
            return 1
        print("PAID-WORK-BOUNDARY: PASS - no free substitutes and every skill carries the boundary.")
        return 0


def main() -> int:
    # Parses --root and lints that tree.
    parser = argparse.ArgumentParser(description="Lint the public skills for free substitutes of their paid result.")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent, help="Repository root to lint (default: this checkout).")
    args = parser.parse_args()
    linter = BoundaryLinter(args.root.resolve())
    return linter.report(linter.run())


if __name__ == "__main__":
    sys.exit(main())
