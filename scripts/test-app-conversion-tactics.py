#!/usr/bin/env python3
"""Verification script for skills/app-conversion-tactics (design doc section 10).

Static checks read the skill folder, the ledger, and the rubric against the write-agent-skill
contract and the CLI's exported command tree. Quote checks compare every tagged quote in the
ledger with text snapshots of the article and of Apple's pages, which stay out of this repository.
Cross-repo checks import the website listing from a Vidbyte checkout with Node. Integration checks
run SKILL.md's own bash and PowerShell Run blocks with --dry-run through a real vidbyte-cli, which
reads no key and calls no model, and assert that the request and the ledger reach the agent's
context unchanged. Nothing is billed and nothing leaves the machine.

    python scripts/test-app-conversion-tactics.py --contract <cli-commands.json> --cli "vidbyte-cli" \
        --article <article.txt> --apple <guidelines.txt> --apple <retention.txt> --website <vidbyte checkout>

Every input is required: a missing one fails the checks that need it, by name, so a full pass
proves every check ran.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLUG = "app-conversion-tactics"
SKILL = ROOT / "skills" / SLUG
SKILL_MD = SKILL / "SKILL.md"
LEDGER = SKILL / "references" / "conversion-tactics-ledger.md"
RUBRIC = SKILL / "references" / "conversion-rubric.md"
MIRROR = ROOT / "docs" / "agent-skills" / "write-agent-skill" / "references"
README = ROOT / "README.md"
CONTRACT_SECTIONS = ["The problem", "How vidbyte-cli solves it", "Goal and done-when", "Preflight", "Gather inputs", "Steps", "CLI commands",
                     "Cost and consent", "Run", "Read the result", "Deliver", "Troubleshooting", "Guardrails"]
METADATA_FIELDS = ["agent", "commands", "cost", "writes-files", "min-cli-version", "status", "docs"]
ROOT_ONLY_OPTIONS = {"--json", "--no-input", "--color", "--debug", "--format", "--profile"}
CATEGORIES = ["business_growth", "product_experience", "quick_wins", "experiment", "risk_prevention"]
TOKEN_CAP = 250000
TIER_COUNTS = {"A": 8, "G": 3, "B": 7}
SUPPORT_COUNTS = {"P": 5, "F": 6, "N": 11, "X": 7, "V": 11}
ID_PATTERN = re.compile(r"\b([AGBPFNXV])-(\d{2})\b")
QUOTE_PATTERN = re.compile(r'"([^"\n]+)"\s*\[(article|post \d+|apple[^\]]*)\]')
NASTY_GOAL = ("How do I go from $5k to $20k MRR? My \"paywall\" isn't converting! 50% off & `rm -rf` <test> | ^caret %PATH% "
              "\\\"slash\\ \U0001F4B8 café\nSecond line: 'single' quotes and a trailing backslash \\")
APP_FACTS = ("- Platform: iOS, SwiftUI (Habit.xcodeproj).\n- Purchases: RevenueCat (Podfile.lock), monthly and yearly plans, 7-day trial.\n"
             "- Owner says: 3% of trials convert; most users come from TikTok.\n")


class Results:
    """Collects one PASS or FAIL line per test case and the final exit code."""

    def __init__(self) -> None:
        # Starts with no recorded outcomes.
        self.rows: list[tuple[str, str, bool, str]] = []

    def run(self, category: str, name: str, check) -> None:
        # Runs one check, recording an AssertionError or any crash as a failure with its message.
        try:
            check()
            self.rows.append((category, name, True, ""))
        except AssertionError as error:
            self.rows.append((category, name, False, str(error)))
        except Exception:  # noqa: BLE001 - a crashing check is a failing check, never a pass
            self.rows.append((category, name, False, traceback.format_exc(limit=3)))

    def finish(self) -> int:
        # Prints every outcome and the summary line, and returns 0 only when everything passed.
        for category, name, passed, detail in self.rows:
            print(f"{'PASS' if passed else 'FAIL'} {category} {name}" + ("" if passed else f"\n    {detail}"))
        passed = sum(1 for row in self.rows if row[2])
        print(f"{passed}/{len(self.rows)} tests passed")
        return 0 if passed == len(self.rows) else 1


class Check:
    """Assertion helpers whose messages name what differed."""

    @staticmethod
    def true(condition: object, message: str) -> None:
        # Fails with the message when the condition is false.
        if not condition:
            raise AssertionError(message)

    @staticmethod
    def equal(actual: object, expected: object, label: str) -> None:
        # Fails when two values differ, showing both.
        if actual != expected:
            raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")

    @staticmethod
    def given(value: object, flag: str) -> None:
        # Fails a check whose required command-line input was not passed.
        if not value:
            raise AssertionError(f"not run: pass {flag}")


class SkillFile:
    """Parses SKILL.md once: frontmatter, sections, fenced blocks, and every vidbyte-cli invocation."""

    def __init__(self, path: Path) -> None:
        # Reads the file and splits it into frontmatter and body.
        self.text = path.read_text(encoding="utf-8")
        head, self.body = self.text.split("\n---\n", 1)
        self.frontmatter = head.split("---\n", 1)[1]

    def field(self, name: str) -> str:
        # Returns one top-level frontmatter value as written.
        match = re.search(rf"^{re.escape(name)}: (.*)$", self.frontmatter, re.M)
        Check.true(match, f"frontmatter has no {name}")
        return match.group(1)

    def metadata(self) -> dict[str, str]:
        # Returns the metadata block as raw values, quotes included.
        block = self.frontmatter.split("metadata:\n", 1)[1]
        return dict(re.findall(r"^  ([a-z-]+): (.*)$", block, re.M))

    def headings(self) -> list[str]:
        # Returns every level-two heading in body order.
        return re.findall(r"^## (.+)$", self.body, re.M)

    def section(self, name: str) -> str:
        # Returns the text of one level-two section, without its heading.
        match = re.search(rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)", self.body, re.M | re.S)
        Check.true(match, f"no section {name}")
        return match.group(1)

    def fenced(self, section: str, language: str) -> str:
        # Returns the first fenced block of one language inside one section.
        match = re.search(rf"```{language}\n(.*?)```", self.section(section), re.S)
        Check.true(match, f"no {language} block in {section}")
        return match.group(1)

    def invocations(self, text: str) -> list[list[str]]:
        # Returns the tokens of every vidbyte-cli invocation in code blocks and inline code.
        joined = re.sub(r"\\\n\s*", " ", text)
        spans = re.findall(r"```[a-z]*\n(.*?)```", joined, re.S) + re.findall(r"`([^`\n]*vidbyte-cli [^`\n]*)`", joined)
        found = []
        for span in spans:
            for line in span.splitlines():
                if "vidbyte-cli " in line or line.strip().endswith("vidbyte-cli"):
                    found.append(self._tokens(line[line.index("vidbyte-cli"):]))
        return found

    @staticmethod
    def _tokens(command: str) -> list[str]:
        # Splits one command line the way a shell does, dropping a trailing comment, and stops at a redirect, a pipe, or a chain.
        tokens = shlex.split(command.replace("$(cat goal.txt)", "GOAL"), comments=True, posix=True)
        stop = next((i for i, token in enumerate(tokens) if token in {">", "2>", "|", "&&"} or token.startswith(("2>", ">"))), len(tokens))
        return tokens[1:stop]


class Contract:
    """The CLI's exported command tree: every command path and its long options."""

    def __init__(self, path: Path) -> None:
        # Loads cli-commands.json into a path -> options map.
        data = json.loads(path.read_text(encoding="utf-8"))
        self.options = {entry["path"]: set(entry.get("options", [])) for entry in data["commands"]}

    def resolve(self, tokens: list[str]) -> tuple[str, list[str], list[str]]:
        # Splits tokens into root options, the longest known command path, and the options after it.
        index = 0
        while index < len(tokens) and tokens[index].startswith("--"):
            index += 1
        root, rest = tokens[:index], tokens[index:]
        words = [token for token in rest if not token.startswith("-")]
        path = ""
        for length in range(len(words), 0, -1):
            candidate = " ".join(words[:length])
            if candidate in self.options and rest[:length] == words[:length]:
                path = candidate
                break
        consumed = len(path.split()) if path else 0
        after = [token.split("=", 1)[0] for token in rest[consumed:] if token.startswith("--")]
        return path, root, after


class SkillContractTests:
    """Static checks of SKILL.md and the shared references against the write-agent-skill contract."""

    def __init__(self, results: Results, contract_path: str | None) -> None:
        # Holds the parsed skill and the optional CLI contract path.
        self.results = results
        self.skill = SkillFile(SKILL_MD)
        self.contract_path = contract_path

    def run(self) -> None:
        # Registers every static contract case with its failure category.
        r = self.results
        r.run("[Edge Case]", "frontmatter name equals the folder name and the slug", self.name_is_slug)
        r.run("[Hidden Assumption]", "description starts 'Use when', is at most 1024 chars, has no '$' price, names vidbyte-cli and the Suggestion Agent", self.description_rules)
        r.run("[Silent Failure]", "every metadata field is present and quoted, and status is stable", self.metadata_fields)
        r.run("[Silent Failure]", "sections appear in contract order and no '## When' heading exists", self.section_order)
        r.run("[Silent Failure]", "every step names an existing section", self.steps_name_sections)
        r.run("[Hidden Assumption]", "every vidbyte-cli line puts root options before the command path", self.root_options_first)
        r.run("[Hidden Failure]", "every vidbyte-cli command path and long option exists in contracts/cli-commands.json", self.commands_in_contract)
        r.run("[Silent Failure]", "the CLI commands table lists every command the body runs, and metadata.commands matches it", self.table_covers_body)
        r.run("[Silent Failure]", "both Run blocks pass --goal, --files app-facts.md, --files <ledger>, and --instructions <rubric>", self.run_passes_facts)
        r.run("[Hidden Assumption]", "every --files and --instructions path in Run is relative", self.paths_are_relative)
        r.run("[Silent Failure]", "the metadata cost cap equals --max-total-tokens in both Run blocks and the five categories are passed", self.cap_and_categories)
        r.run("[Silent Failure]", "setup.md and troubleshooting.md are byte-identical to the docs/agent-skills mirror every sibling ships", self.references_identical)
        r.run("[Edge Case]", "the docs metadata URL and the closing Docs line point at /agents/skills/app-conversion-tactics", self.docs_links)

    def name_is_slug(self) -> None:
        # The website refuses a SKILL.md whose name differs from its slug.
        Check.equal(self.skill.field("name"), SLUG, "name")
        Check.equal(SKILL.name, SLUG, "folder")

    def description_rules(self) -> None:
        # The description is the host's trigger text and the website copies it verbatim.
        description = self.skill.field("description")
        Check.true(description.startswith("Use when"), "description must start with 'Use when'")
        Check.true(len(description) <= 1024, f"description is {len(description)} chars")
        Check.true("$" not in description, "description carries a price")
        Check.true("vidbyte-cli" in description and "Suggestion Agent" in description, "description must name vidbyte-cli and the Suggestion Agent")

    def metadata_fields(self) -> None:
        # Every metadata value except status is a quoted string, and the values match this skill.
        metadata = self.skill.metadata()
        for name in METADATA_FIELDS:
            Check.true(name in metadata, f"metadata.{name} missing")
            if name != "status":
                Check.true(metadata[name].startswith('"') and metadata[name].endswith('"'), f"metadata.{name} is not quoted")
        Check.equal(metadata["status"], "stable", "status")
        Check.equal(metadata["agent"], '"suggestion-agent"', "agent")
        Check.equal(metadata["writes-files"], '"false"', "writes-files")
        Check.equal(metadata["min-cli-version"], '"0.3.2"', "min-cli-version")

    def section_order(self) -> None:
        # The public contract fixes the section order and bans when-to-use lists.
        headings = self.skill.headings()
        Check.equal(headings, CONTRACT_SECTIONS, "sections")
        Check.true(not re.search(r"^## When", self.skill.body, re.M), "a '## When' section exists")

    def steps_name_sections(self) -> None:
        # Each numbered step ends by naming the section that holds its details.
        steps = re.split(r"\n(?=\d+\. )", self.skill.section("Steps").strip())
        Check.equal(len(steps), 10, "step count")
        for step in steps:
            named = re.findall(r"\(([A-Z][a-z][^()]*)\)", step)
            Check.true(named and named[-1] in CONTRACT_SECTIONS, f"step names no section: {step[:60]!r}")

    def root_options_first(self) -> None:
        # vidbyte-cli rejects --json and --no-input after the command path.
        for tokens in self.skill.invocations(self.skill.body):
            path_started = False
            for token in tokens:
                if not token.startswith("-"):
                    path_started = True
                elif path_started and token in ROOT_ONLY_OPTIONS:
                    raise AssertionError(f"root option {token} after the command path in: {' '.join(tokens)}")

    def commands_in_contract(self) -> None:
        # A command or flag the released CLI lacks fails for every user who installs the skill.
        Check.given(self.contract_path, "--contract")
        contract = Contract(Path(self.contract_path))
        for tokens in self.skill.invocations(self.skill.body):
            path, root, after = contract.resolve(tokens)
            Check.true(path in contract.options, f"unknown command path in: {' '.join(tokens)}")
            for option in root:
                Check.true(option in contract.options[""], f"unknown root option {option}")
            for option in after:
                Check.true(option in contract.options[path], f"{path} has no {option}")

    def table_covers_body(self) -> None:
        # The CLI commands table is the agent's whole plan, so every command the body runs is in it.
        table = self.skill.section("CLI commands")
        table_paths = {self._path(tokens) for tokens in self.skill.invocations(table)}
        prose = "\n".join(self.skill.section(name) for name in CONTRACT_SECTIONS if name not in ("CLI commands", "Guardrails"))
        body_paths = {self._path(tokens) for tokens in self.skill.invocations(prose)}
        Check.true(body_paths <= table_paths, f"not in the table: {sorted(body_paths - table_paths)}")
        listed = {part.strip() for part in self.skill.metadata()["commands"].strip('"').split(",")}
        Check.equal(listed, table_paths - {"", "whoami"}, "metadata.commands")

    @staticmethod
    def _path(tokens: list[str]) -> str:
        # Returns the command words of one invocation, without options or values.
        words = []
        for token in tokens:
            if token.startswith("-") or token in {"...", "GOAL"}:
                if words:
                    break
                continue
            words.append(token)
        return " ".join(words)

    def run_passes_facts(self) -> None:
        # The facts reach the agent only if both Run blocks pass the goal, the app facts, the ledger, and the rubric.
        for language in ("bash", "powershell"):
            block = re.sub(r"\\\n\s*", " ", self.skill.fenced("Run", language))
            for needle in ("--goal ", "--files app-facts.md", "--files conversion-tactics-ledger.md", "--instructions conversion-rubric.md"):
                Check.true(needle in block, f"{language} Run block lacks {needle!r}")
        Check.true('--goal "$(cat goal.txt)"' in self.skill.fenced("Run", "bash"), "bash Run block must read the goal from goal.txt")

    def paths_are_relative(self) -> None:
        # A --files path reaches the model, so an absolute path would name the user's home folder.
        for language in ("bash", "powershell"):
            block = self.skill.fenced("Run", language)
            for value in re.findall(r"--(?:files|instructions) (\S+)", block):
                Check.true(not re.search(r"[/\\:<]", value), f"{language} passes a non-relative path {value!r}")

    def cap_and_categories(self) -> None:
        # The quoted cost assumes this cap and these five lenses.
        cost = self.skill.metadata()["cost"]
        Check.true(f"{TOKEN_CAP:,}" in cost, f"metadata.cost does not name {TOKEN_CAP:,}")
        for language in ("bash", "powershell"):
            block = self.skill.fenced("Run", language)
            Check.true(f"--max-total-tokens {TOKEN_CAP}" in block, f"{language} Run block cap")
            Check.equal(re.findall(r"--category (\w+)", block), CATEGORIES, f"{language} categories")

    def references_identical(self) -> None:
        # Each skill installs alone, so it carries the same shared references as its siblings.
        for name in ("setup.md", "troubleshooting.md"):
            ours = (SKILL / "references" / name).read_bytes().replace(b"\r\n", b"\n")
            mirror = (MIRROR / name).read_bytes().replace(b"\r\n", b"\n")
            Check.true(ours == mirror, f"references/{name} differs from {MIRROR / name}")

    def docs_links(self) -> None:
        # The docs link is how a reader gets from the installed file to the listing.
        url = f"https://vidbyte.pro/agents/skills/{SLUG}"
        Check.equal(self.skill.metadata()["docs"], f'"{url}"', "metadata.docs")
        Check.true(self.skill.text.rstrip().endswith(f"Docs: <{url}>"), "the closing Docs line")


class LedgerTests:
    """Integrity of the fact ledger and the rubric: IDs, Apple anchors, arithmetic, and size."""

    def __init__(self, results: Results) -> None:
        # Reads the ledger, the rubric, and SKILL.md once.
        self.results = results
        self.ledger = LEDGER.read_text(encoding="utf-8")
        self.rubric = RUBRIC.read_text(encoding="utf-8")
        self.skill = SKILL_MD.read_text(encoding="utf-8")

    def run(self) -> None:
        # Registers every ledger case with its failure category.
        r = self.results
        r.run("[Edge Case]", "8 allowed, 3 gray, and 7 banned tactic records with unique, gap-free IDs", self.tactic_ids)
        r.run("[Edge Case]", "supporting facts P, F, N, X, and V are unique and gap-free", self.support_ids)
        r.run("[Hidden Assumption]", "every gray and banned record carries at least one [apple §] anchor", self.anchors)
        r.run("[Silent Failure]", "every ID the rubric, SKILL.md, and the ledger cite is defined in the ledger", self.citations_resolve)
        r.run("[Silent Failure]", "every derived number recomputes to what the ledger states", self.arithmetic)
        r.run("[Edge Case]", "ledger under 40,000 chars and rubric under 8,000 bytes (inside the 20,000-byte --instructions cap)", self.sizes)
        r.run("[Hidden Assumption]", "ledger and rubric are UTF-8 .md files without a byte-order mark", self.encoding)

    def tactic_ids(self) -> None:
        # The agent and the rubric address tactics by these IDs.
        for prefix, count in TIER_COUNTS.items():
            found = re.findall(rf"^### ({prefix}-\d\d) ", self.ledger, re.M)
            Check.equal(found, [f"{prefix}-{n:02d}" for n in range(1, count + 1)], f"{prefix} records")

    def support_ids(self) -> None:
        # Supporting facts are defined once each, as bold bullet labels.
        for prefix, count in SUPPORT_COUNTS.items():
            found = re.findall(rf"^- \*\*({prefix}-\d\d)\b", self.ledger, re.M)
            Check.equal(found, [f"{prefix}-{n:02d}" for n in range(1, count + 1)], f"{prefix} facts")

    def anchors(self) -> None:
        # A gray or banned tier is only as good as the Apple rule behind it. A record ends at the next
        # heading of either level, so the last record of a tier cannot borrow a quote from the next section.
        records = re.split(r"^(?=#{2,3} )", self.ledger, flags=re.M)
        checked = 0
        for record in records:
            heading = re.match(r"### ([GB]-\d\d)", record)
            if heading:
                checked += 1
                Check.true(re.search(r'"[^"\n]+"\s*\[apple § [0-9]', record), f"{heading.group(1)} has no quoted Apple section")
        Check.equal(checked, TIER_COUNTS["G"] + TIER_COUNTS["B"], "gray and banned records checked")

    def defined_ids(self) -> set[str]:
        # Collects every ID the ledger defines as a heading or a bold label.
        headings = re.findall(r"^### ([AGB]-\d\d) ", self.ledger, re.M)
        labels = re.findall(r"^- \*\*([PFNXV]-\d\d)\b", self.ledger, re.M)
        return set(headings) | set(labels)

    def citations_resolve(self) -> None:
        # A citation to an ID that does not exist is a claim the agent cannot check.
        defined = self.defined_ids()
        for name, text in (("rubric", self.rubric), ("SKILL.md", self.skill), ("ledger", self.ledger)):
            cited = {f"{a}-{b}" for a, b in ID_PATTERN.findall(text)}
            Check.true(cited <= defined, f"{name} cites undefined IDs {sorted(cited - defined)}")

    def arithmetic(self) -> None:
        # Recomputes every [derived] figure from the article's own numbers.
        weekly = round(3.99 * 52, 2)
        default_weekly = round(7.99 * 52, 2)
        mix = [210, 132, 110, 74, 1, 1]
        cases = [
            (f"$3.99 × 52 = ${weekly:.2f}", weekly == 207.48),
            (f"$79.99 / $207.48 = {79.99 / weekly * 100:.1f}%", round(79.99 / weekly * 100, 1) == 38.6),
            (f"a {round(100 - 79.99 / weekly * 100)}% saving", round(100 - 79.99 / weekly * 100) == 61),
            (f"$7.99 × 52 = ${default_weekly:.2f}", default_weekly == 415.48),
            (f"$49.99 / $415.48 = {49.99 / default_weekly * 100:.1f}%", round(49.99 / default_weekly * 100, 1) == 12.0),
            (f"an {round(100 - 49.99 / default_weekly * 100)}%", round(100 - 49.99 / default_weekly * 100) == 88),
            (f"210 + 132 + 110 + 74 + 1 + 1 = {sum(mix)}", sum(mix) == 528),
            (f"110 / (210 + 110) = {round(110 / 320 * 100)}%", round(110 / 320 * 100) == 34),
            (f"24,176 / 28,555 = {round(24176 / 28555 * 100)}%", round(24176 / 28555 * 100) == 85),
            (f"(10 - 4) / 10 = {round((10 - 4) / 10 * 100)}%", round((10 - 4) / 10 * 100) == 60),
        ]
        flat = re.sub(r"\s+", " ", self.ledger)
        for text, holds in cases:
            Check.true(holds, f"arithmetic does not hold: {text}")
            Check.true(text in flat, f"ledger does not state {text!r}")
        for count, share in zip(mix[:4], (40, 25, 21, 14)):
            Check.equal(round(count / 528 * 100), share, f"share of {count}")
            Check.true(f"{count} ({share}%)" in flat, f"ledger does not state {count} ({share}%)")

    def sizes(self) -> None:
        # Keeps the ledger cheap to carry on every call and the rubric inside the CLI's cap.
        Check.true(len(self.ledger) < 40000, f"ledger is {len(self.ledger)} chars")
        Check.true(len(self.rubric.encode("utf-8")) < 8000, "rubric is too large")

    def encoding(self) -> None:
        # The CLI accepts .txt, .md, and .json files; a BOM would count as content in other readers.
        for path in (LEDGER, RUBRIC):
            Check.equal(path.suffix, ".md", path.name)
            raw = path.read_bytes()
            Check.true(not raw.startswith(b"\xef\xbb\xbf"), f"{path.name} starts with a byte-order mark")
            raw.decode("utf-8")


class QuoteTests:
    """Every quote tagged [article], [post], or [apple] appears verbatim in its source snapshot."""

    def __init__(self, results: Results, article: str | None, apple: list[str]) -> None:
        # Holds the snapshot paths given on the command line.
        self.results = results
        self.article = article
        self.apple = apple
        self.quotes = QUOTE_PATTERN.findall(LEDGER.read_text(encoding="utf-8"))

    def run(self) -> None:
        # Registers the two quote cases.
        self.results.run("[Silent Failure]", "every [apple] quote appears verbatim in the Apple snapshots", self.apple_quotes)
        self.results.run("[Silent Failure]", "every [article] and [post] quote appears verbatim in the article snapshot", self.article_quotes)

    @staticmethod
    def _flat(text: str) -> str:
        # Collapses whitespace so a quote matches across the snapshot's line breaks.
        return re.sub(r"\s+", " ", text)

    def apple_quotes(self) -> None:
        # A misquoted rule is worse than none, because the agent will repeat it.
        Check.given(self.apple, "--apple")
        source = self._flat(" ".join(Path(path).read_text(encoding="utf-8") for path in self.apple))
        quotes = [quote for quote, tag in self.quotes if tag.startswith("apple")]
        Check.true(len(quotes) >= 18, f"only {len(quotes)} Apple quotes found")
        missing = [quote[:70] for quote in quotes if self._flat(quote) not in source]
        Check.true(not missing, f"not in the Apple snapshots: {missing}")

    def article_quotes(self) -> None:
        # The article is the source of truth, so its quotes must be its own words.
        Check.given(self.article, "--article")
        source = self._flat(Path(self.article).read_text(encoding="utf-8"))
        quotes = [quote for quote, tag in self.quotes if not tag.startswith("apple")]
        Check.true(len(quotes) >= 15, f"only {len(quotes)} article quotes found")
        missing = [quote[:70] for quote in quotes if self._flat(quote) not in source]
        Check.true(not missing, f"not in the article snapshot: {missing}")


class ReadmeTests:
    """The public README lists the skill once with the CLI version it needs."""

    def __init__(self, results: Results) -> None:
        # Reads the README once.
        self.results = results
        self.text = README.read_text(encoding="utf-8")

    def run(self) -> None:
        # Registers the README case.
        self.results.run("[Edge Case]", "README has one row for the slug naming 0.3.2", self.row)

    def row(self) -> None:
        # The README table is where a GitHub visitor picks a skill.
        rows = [line for line in self.text.splitlines() if line.startswith(f"| [`{SLUG}`]")]
        Check.equal(len(rows), 1, "README rows")
        Check.true("0.3.2" in rows[0], "README row does not name 0.3.2")


class WebsiteTests:
    """Cross-repo equality between the public SKILL.md and the Vidbyte listing."""

    def __init__(self, results: Results, website: str | None) -> None:
        # Holds the Vidbyte checkout path and the parsed skill.
        self.results = results
        self.website = Path(website) if website else None
        self.skill = SkillFile(SKILL_MD)

    def run(self) -> None:
        # Registers the cross-repo cases.
        r = self.results
        r.run("[Hidden Failure]", "the Vidbyte entry imports through defineAgentSkill and is in AGENT_SKILLS", self.entry_loads)
        r.run("[Silent Failure]", "the entry's description equals the public frontmatter verbatim", self.description_matches)
        r.run("[Silent Failure]", "minCliVersion, writesFiles, and commands match the public metadata", self.metadata_matches)
        r.run("[Silent Failure]", "SKILL_SPECS, PUBLIC_MARKDOWN_PATHS, and public/llms.txt name the skill", self.agent_layer)

    def entry(self) -> dict:
        # Imports the real catalog with Node and returns the entry as JSON.
        Check.given(self.website, "--website")
        catalog = (self.website / "next-app" / "constants" / "agent-skills" / "catalog.js").resolve().as_uri()
        script = (f"const m = await import({json.dumps(catalog)}); const e = m.AGENT_SKILLS_BY_SLUG[{json.dumps(SLUG)}];"
                  f"console.log(JSON.stringify({{entry: e ?? null, listed: m.AGENT_SKILLS.some((s) => s.slug === {json.dumps(SLUG)})}}));")
        done = subprocess.run(["node", "--no-warnings", "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=120)
        Check.equal(done.returncode, 0, f"node import failed: {done.stderr[-400:]}")
        return json.loads(done.stdout.strip().splitlines()[-1])

    def entry_loads(self) -> None:
        # defineAgentSkill validates at import time, so a clean import is a validated entry.
        loaded = self.entry()
        Check.true(loaded["entry"], "no entry for the slug")
        Check.true(loaded["listed"], "the entry is not in AGENT_SKILLS")
        Check.equal(loaded["entry"]["status"], "coming-soon", "status until vidbyte-cli 0.3.2 is on PyPI")

    def description_matches(self) -> None:
        # The website copies the description; drift would show one text and match another.
        Check.equal(self.entry()["entry"]["description"], self.skill.field("description"), "description")

    def metadata_matches(self) -> None:
        # The listing's install facts must be the file's own.
        entry = self.entry()["entry"]
        metadata = self.skill.metadata()
        Check.equal(entry["minCliVersion"], metadata["min-cli-version"].strip('"'), "minCliVersion")
        Check.equal(entry["writesFiles"], metadata["writes-files"].strip('"') == "true", "writesFiles")
        expected = [f"vidbyte-cli {part.strip()}" for part in metadata["commands"].strip('"').split(",")]
        Check.equal(entry["commands"], expected, "commands")

    def agent_layer(self) -> None:
        # A listing without its brief breaks the build; without its mirror path agents cannot read it.
        Check.given(self.website, "--website")
        app = self.website / "next-app"
        Check.true(f'"{SLUG}": {{' in (app / "lib" / "llms" / "briefs" / "agents.js").read_text(encoding="utf-8"), "no SKILL_SPECS entry")
        Check.true(f'"/agents/skills/{SLUG}",' in (app / "lib" / "llms" / "publicPaths.js").read_text(encoding="utf-8"), "no public mirror path")
        Check.true(f"/agents/skills/{SLUG}.md" in (app / "public" / "llms.txt").read_text(encoding="utf-8"), "llms.txt does not list the skill")


class DryRunTests:
    """Runs SKILL.md's own Run blocks with --dry-run through a real vidbyte-cli: free, no key, no model."""

    def __init__(self, results: Results, cli: str | None, base: Path) -> None:
        # Holds the CLI command, the scratch folder, and the parsed skill.
        self.results = results
        self.cli = shlex.split(cli) if cli else None
        self.base = base
        self.skill = SkillFile(SKILL_MD)
        self.ledger_text = LEDGER.read_text(encoding="utf-8-sig")

    def run(self) -> None:
        # Registers every integration case with its failure category.
        r = self.results
        r.run("[Hidden Failure]", "the bash Run block with --dry-run exits 0 with a suggestions.result dry run", self.bash_dry_run)
        r.run("[Silent Failure]", "the ledger reaches the context whole: included, its length, and its sha256", self.ledger_included)
        r.run("[Hidden Assumption]", "settings echo the five categories, horizon any, the token cap, and gpt-6-sol", self.settings_echo)
        r.run("[Hidden Failure]", "a goal with $, quotes, backticks, !, %, &, |, <, >, ^, a newline, emoji, and a trailing backslash arrives verbatim through bash", self.bash_goal_verbatim)
        r.run("[Hidden Failure]", "the same goal arrives verbatim through the PowerShell Run block", self.powershell_goal_verbatim)
        r.run("[Hidden Failure]", "without the escape line, Windows PowerShell's legacy argument passing breaks the goal (and the escape is skipped otherwise)", self.powershell_naive_form)
        r.run("[Edge Case]", "a 4,096-character goal passes and a 4,097-character goal exits 2", self.goal_boundary)
        r.run("[Hidden Failure]", "a missing ledger file exits 2 before any model call", self.missing_ledger)
        r.run("[Hidden Assumption]", "a long request adds request.md in command-line order: app-facts.md, request.md, then the ledger", self.long_request_order)

    def prepare(self, name: str, goal: str = NASTY_GOAL) -> Path:
        # Builds an output folder the way Gather inputs does: references copied in, goal.txt, app-facts.md.
        out = self.base / name
        out.mkdir(parents=True)
        shutil.copy(LEDGER, out / LEDGER.name)
        shutil.copy(RUBRIC, out / RUBRIC.name)
        (out / "goal.txt").write_text(goal, encoding="utf-8", newline="")
        (out / "app-facts.md").write_text(APP_FACTS, encoding="utf-8")
        return out

    def dry_block(self, language: str, out: Path, extra_files: str = "") -> str:
        # Turns one Run block into its Gather-inputs dry run: --dry-run after the cap, dry-run file names.
        Check.given(self.cli, "--cli")
        block = self.skill.fenced("Run", language)
        block = block.replace(f"--max-total-tokens {TOKEN_CAP}", f"--max-total-tokens {TOKEN_CAP} --dry-run")
        block = block.replace("result.json", "dry-run.json").replace("result.stderr.txt", "dry-run.stderr.txt")
        if extra_files:
            block = block.replace("--files app-facts.md", f"--files app-facts.md {extra_files}")
        if language == "bash":
            block = block.replace('"<out_dir>"', shlex.quote(out.as_posix()))
            return block.replace("vidbyte-cli ", " ".join(shlex.quote(token) for token in self.cli) + " ", 1)
        block = block.replace('"<out_dir>"', f"'{out}'")
        return block.replace("& vidbyte-cli ", "& " + " ".join(self.cli) + " ", 1)

    def run_bash(self, out: Path, extra_files: str = "") -> dict:
        # Runs the bash Run block as a dry run and returns the parsed result.
        bash = shutil.which("bash")
        Check.true(bash, "no bash on PATH")
        done = subprocess.run([bash, "-c", self.dry_block("bash", out, extra_files)], capture_output=True, text=True, timeout=300)
        Check.equal(done.returncode, 0, f"bash dry run exit ({(out / 'dry-run.stderr.txt').read_text(encoding='utf-8', errors='replace')[-400:]})")
        return json.loads((out / "dry-run.json").read_text(encoding="utf-8"))

    def run_powershell(self, out: Path, block: str) -> tuple[int, dict | None]:
        # Runs one PowerShell block from a script file and returns its exit code and parsed result.
        shell = shutil.which("powershell") or shutil.which("pwsh")
        Check.true(shell, "no PowerShell on PATH")
        script = out / "run.ps1"
        script.write_text(block + "\nexit $LASTEXITCODE\n", encoding="utf-8-sig")
        done = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], capture_output=True, text=True, timeout=300)
        try:
            return done.returncode, json.loads((out / "dry-run.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return done.returncode, None

    def bash_dry_run(self) -> None:
        # The dry run is the skill's free proof that the request is valid before consent.
        data = self.run_bash(self.prepare("bash"))
        Check.equal(data["kind"], "suggestions.result", "kind")
        Check.equal(data["data"]["stop_reason"], "dry_run", "stop_reason")

    def ledger_included(self) -> None:
        # Compares the manifest's hash with the ledger, proving the agent receives the file byte for byte.
        data = self.run_bash(self.prepare("ledger"))["data"]
        entries = [entry for entry in data["context_manifest"] if entry["source"] == LEDGER.name]
        Check.equal(len(entries), 1, "ledger manifest entries")
        entry = entries[0]
        Check.equal((entry["kind"], entry["status"]), ("file", "included"), "ledger entry")
        Check.equal(entry["chars"], len(self.ledger_text), "ledger chars")
        Check.equal(entry["sha256"], hashlib.sha256(self.ledger_text.encode("utf-8")).hexdigest()[:16], "ledger sha256")

    def settings_echo(self) -> None:
        # The cost statement assumes these settings.
        settings = self.run_bash(self.prepare("settings"))["data"]["settings"]
        Check.equal(settings["categories"], CATEGORIES, "categories")
        Check.equal(settings["horizon"], "any", "horizon")
        Check.equal(settings["max_total_tokens"], TOKEN_CAP, "max_total_tokens")
        Check.equal((settings["model"], settings["critic_model"]), ("gpt-6-sol", "gpt-6-sol"), "models")

    def bash_goal_verbatim(self) -> None:
        # The user's own words are the goal, so the shell must not expand or drop anything in them.
        data = self.run_bash(self.prepare("goal-bash"))["data"]
        Check.equal(data["goal"], NASTY_GOAL.strip(), "goal")

    def powershell_goal_verbatim(self) -> None:
        # Windows users run the PowerShell block; it must deliver the same words.
        out = self.prepare("goal-ps")
        code, result = self.run_powershell(out, self.dry_block("powershell", out))
        Check.equal(code, 0, f"PowerShell dry run exit ({(out / 'dry-run.stderr.txt').read_text(encoding='utf-8', errors='replace')[-300:] if (out / 'dry-run.stderr.txt').exists() else ''})")
        Check.equal(result["data"]["goal"], NASTY_GOAL.strip(), "goal")

    def powershell_naive_form(self) -> None:
        # Shows the escape line is load-bearing where it applies and harmless where it does not.
        out = self.prepare("goal-ps-naive")
        block = "\n".join(line for line in self.dry_block("powershell", out).splitlines() if not line.startswith("if (-not $PSNativeCommandArgumentPassing"))
        shell = shutil.which("powershell") or shutil.which("pwsh")
        Check.true(shell, "no PowerShell on PATH")
        probe = "if (-not $PSNativeCommandArgumentPassing -or $PSNativeCommandArgumentPassing -eq 'Legacy') { 'legacy' } else { 'standard' }"
        mode = subprocess.run([shell, "-NoProfile", "-Command", probe], capture_output=True, text=True, timeout=60).stdout.strip()
        code, result = self.run_powershell(out, block)
        verbatim = code == 0 and result is not None and result["data"]["goal"] == NASTY_GOAL.strip()
        Check.equal(verbatim, mode != "legacy", f"unescaped goal arrived verbatim under {mode} argument passing")

    def direct(self, out: Path, *args: str) -> subprocess.CompletedProcess:
        # Runs the CLI with an argument list, so no shell touches the values.
        Check.given(self.cli, "--cli")
        return subprocess.run([*self.cli, "--json", "--no-input", "agents", "suggest", "run", *args], cwd=out, capture_output=True, timeout=300)

    def goal_boundary(self) -> None:
        # Gather inputs moves a request over 4,096 characters into request.md because the CLI refuses it.
        out = self.prepare("boundary")
        ok = self.direct(out, "--goal", "x" * 4096, "--files", LEDGER.name, "--dry-run")
        Check.equal(ok.returncode, 0, f"4,096 chars exit ({ok.stderr.decode('utf-8', 'replace')[-200:]})")
        over = self.direct(out, "--goal", "x" * 4097, "--files", LEDGER.name, "--dry-run")
        Check.equal(over.returncode, 2, "4,097 chars exit")

    def missing_ledger(self) -> None:
        # A damaged install must fail for free, before anything is billed.
        out = self.prepare("missing")
        (out / LEDGER.name).unlink()
        done = self.direct(out, "--goal", "Get more users to subscribe", "--files", LEDGER.name, "--dry-run")
        Check.equal(done.returncode, 2, "missing ledger exit")

    def long_request_order(self) -> None:
        # The request file joins the context next to the app facts, and the ledger stays included.
        out = self.prepare("long", goal="Help me get more of my app's users to pay.")
        (out / "request.md").write_text("Help me get more of my app's users to pay. " + "Details. " * 600, encoding="utf-8")
        manifest = self.run_bash(out, extra_files="--files request.md")["data"]["context_manifest"]
        files = [entry["source"] for entry in manifest if entry["kind"] == "file"]
        Check.equal(files, ["app-facts.md", "request.md", LEDGER.name], "file order")
        Check.true(all(entry["status"] == "included" for entry in manifest), "every file included")


def parse_args() -> argparse.Namespace:
    # Reads the inputs every check group needs.
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--contract", help="path to the CLI's contracts/cli-commands.json")
    parser.add_argument("--cli", help='the vidbyte-cli command to dry-run, such as "vidbyte-cli"')
    parser.add_argument("--article", help="text snapshot of the article and the posts it embeds")
    parser.add_argument("--apple", action="append", default=[], help="text snapshot of an Apple page; repeat for each")
    parser.add_argument("--website", help="path to a Vidbyte checkout with the listing")
    return parser.parse_args()


def main() -> int:
    # Runs every check group in a fresh scratch folder and returns the exit code.
    args = parse_args()
    results = Results()
    base = Path(tempfile.mkdtemp(prefix="test-app-conversion-tactics-"))
    try:
        SkillContractTests(results, args.contract).run()
        LedgerTests(results).run()
        QuoteTests(results, args.article, args.apple).run()
        ReadmeTests(results).run()
        WebsiteTests(results, args.website).run()
        DryRunTests(results, args.cli, base).run()
    finally:
        shutil.rmtree(str(base), ignore_errors=True)
    return results.finish()


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
