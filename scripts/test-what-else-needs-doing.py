#!/usr/bin/env python3
"""Verification script for skills/what-else-needs-doing (design doc section 10).

Checks the public SKILL.md against the write-agent-skill contract, every vidbyte-cli command in it
against the CLI's exported command contract on the internal repository's origin/main, and,
optionally, the Vidbyte website catalog entry against the frontmatter. Negative tests feed mutated
copies of the skill through the same checks to prove each check can fail. Standard library only;
needs Python 3.10 or newer, git, and (for --website-repo) node:

    python scripts/test-what-else-needs-doing.py
    python scripts/test-what-else-needs-doing.py --cli-repo ../vidbyte-cli --website-repo ../vidbyte

--cli-repo defaults to the first sibling folder named vidbyte-cli found above this repository.
--website-repo has no default, because the listing merges after this skill; without it the website
test prints SKIP and does not count.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLUG = "what-else-needs-doing"
SKILL_FILE = ROOT / "skills" / SLUG / "SKILL.md"
CANONICAL_REFERENCES = ROOT / "docs" / "agent-skills" / "write-agent-skill" / "references"
SKILL_REFERENCES = ROOT / "skills" / SLUG / "references"

SECTION_ORDER = (
    "The problem", "How vidbyte-cli solves it", "Goal and done-when", "Preflight", "Gather inputs", "Steps",
    "CLI commands", "Cost and consent", "Paid work boundary", "Run", "Read the result", "Deliver", "Troubleshooting", "Guardrails",
)
METADATA_FIELDS = ("agent", "commands", "cost", "writes-files", "min-cli-version", "status")
AGENT_IDS = ("suggestion-agent", "persistent-researcher", "persistent-agent", "ensemble-agent", "task-board", "stages-agent", "bug-finder")
ROOT_ONLY_OPTIONS = ("--json", "--no-input", "--format", "--profile", "--debug", "--color")
FORBIDDEN_RUN_FLAGS = ("--idempotency-key", "--completed", "--in-progress", "--horizon", "--attach", "--avoid", "--all-categories")
NON_SKILL_KEBAB_TOKENS = {"min-cli-version", "writes-files", "suggestion-agent", SLUG}
GOAL_CHARSET = re.compile(r"^[A-Za-z0-9 ,.\-]+$")


@dataclass(frozen=True, slots=True)
class Invocation:
    """One vidbyte-cli command found in the skill, split into root options, path, and options."""

    text: str
    root_options: tuple[str, ...]
    path: str
    options: tuple[tuple[str, str], ...]
    fenced: bool


@dataclass(slots=True)
class SkillDocument:
    """A parsed SKILL.md: frontmatter values (raw and unquoted), ## sections, and code spans."""

    text: str
    frontmatter_raw: dict[str, str] = field(default_factory=dict)
    sections: list[tuple[str, str]] = field(default_factory=list)
    fences: list[tuple[str, str]] = field(default_factory=list)
    inline_spans: list[str] = field(default_factory=list)

    @classmethod
    def from_text(cls, text: str) -> SkillDocument:
        # Splits the file into frontmatter, sections, fenced blocks, and inline code spans.
        text = text.replace("\r\n", "\n")
        document = cls(text=text)
        frontmatter, body = document._split_frontmatter(text)
        document.frontmatter_raw = document._parse_frontmatter(frontmatter)
        document.sections = document._parse_sections(body)
        document.fences = re.findall(r"^\s*```(\w*)\n(.*?)^\s*```", body, flags=re.M | re.S)
        document.inline_spans = document._parse_inline_spans(body)
        return document

    def value(self, key: str) -> str:
        # Returns a frontmatter value with one layer of surrounding double quotes removed.
        raw = self.frontmatter_raw.get(key, "")
        return raw[1:-1] if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"') else raw

    def section(self, title: str) -> str:
        # Returns the body of the ## section with this exact title, or raises when it is missing.
        for name, body in self.sections:
            if name == title:
                return body
        raise AssertionError(f"section '## {title}' is missing")

    def _split_frontmatter(self, text: str) -> tuple[str, str]:
        # Separates the leading --- block from the markdown body.
        match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, flags=re.S)
        if not match:
            raise AssertionError("SKILL.md has no leading --- frontmatter block")
        return match.group(1), match.group(2)

    def _parse_frontmatter(self, frontmatter: str) -> dict[str, str]:
        # Reads top-level keys and the one nested metadata map; values keep their quotes.
        values: dict[str, str] = {}
        for line in frontmatter.split("\n"):
            nested = re.match(r"^  ([a-z-]+):\s*(.*)$", line)
            top = re.match(r"^([a-z-]+):\s*(.*)$", line)
            if nested:
                values[f"metadata.{nested.group(1)}"] = nested.group(2)
            elif top:
                values[top.group(1)] = top.group(2)
        return values

    def _parse_sections(self, body: str) -> list[tuple[str, str]]:
        # Lists ## sections in order, ignoring headings inside fenced code blocks.
        # Fences are blanked to the same length, so heading offsets still index the real body.
        unfenced = re.sub(r"^\s*```.*?^\s*```", lambda m: re.sub(r"[^\n]", " ", m.group(0)), body, flags=re.M | re.S)
        starts = [(m.start(), m.group(1).strip()) for m in re.finditer(r"^## (.+)$", unfenced, flags=re.M)]
        ends = [start for start, _ in starts[1:]] + [len(body)]
        return [(title, body[start:end]) for (start, title), end in zip(starts, ends, strict=True)]

    def _parse_inline_spans(self, body: str) -> list[str]:
        # Collects inline code spans outside fences; a span may wrap across one line break.
        unfenced = re.sub(r"^\s*```.*?^\s*```", "", body, flags=re.M | re.S)
        return [" ".join(span.split()) for span in re.findall(r"(?<!`)`([^`\n]+(?:\n[^`\n]+)?)`(?!`)", unfenced)]


class CliContract:
    """The CLI's exported command tree and suggestion category ids, read from the internal repo."""

    def __init__(self, cli_repo: Path, ref: str = "origin/main") -> None:
        # Loads contracts/cli-commands.json and categories.py from one git ref of the internal repo.
        self.ref = ref
        self.sha = self._git(cli_repo, "rev-parse", "--short", ref).strip()
        commands = json.loads(self._git(cli_repo, "show", f"{ref}:contracts/cli-commands.json"))["commands"]
        self.options = {entry["path"]: set(entry["options"]) for entry in commands}
        registry = self._git(cli_repo, "show", f"{ref}:src/vidbyte_cli/services/suggestions/categories.py")
        self.category_ids = set(re.findall(r'CategoryDefinition\(\s*"([a-z_]+)"', registry))

    def has_path(self, path: str) -> bool:
        # True when the command path exists in the contract ("" is the root command).
        return path in self.options

    def options_for(self, path: str) -> set[str]:
        # The long options the contract declares for one command path.
        return self.options[path]

    def _git(self, repo: Path, *arguments: str) -> str:
        # Runs one read-only git command in the internal repository and returns its stdout.
        return subprocess.run(["git", "-C", str(repo), *arguments], check=True, capture_output=True, text=True, encoding="utf-8").stdout


class CommandExtractor:
    """Finds every vidbyte-cli invocation in fenced blocks and inline code and splits it into parts."""

    def __init__(self, contract: CliContract) -> None:
        # Keeps the contract so a path is matched as the longest known prefix of the command words.
        self.contract = contract

    def extract(self, document: SkillDocument) -> list[Invocation]:
        # Returns every invocation, fenced ones first, each parsed against the contract's paths.
        found = [(line, True) for _, block in document.fences for line in self._fenced_lines(block)]
        found += [(span, False) for span in document.inline_spans]
        return [self._parse(text, fenced) for text, fenced in found if "vidbyte-cli" in text]

    def _fenced_lines(self, block: str) -> list[str]:
        # Joins backslash continuations, then keeps only lines that call vidbyte-cli.
        joined = re.sub(r"\\\n\s*", " ", block)
        return [line for line in joined.split("\n") if "vidbyte-cli" in line]

    def _parse(self, text: str, fenced: bool) -> Invocation:
        # Cuts the command out of its shell wrapping and splits root options, path, and options.
        command = text[text.index("vidbyte-cli") + len("vidbyte-cli"):]
        command = re.split(r"\s+#|\s2?>\s|'\s*$", command)[0]
        tokens = [token for token in re.sub(r'"[^"]*"', "QUOTED", command).split() if token != "..."]
        root = tuple(token for token in self._leading_options(tokens))
        rest = tokens[len(root):]
        words = [token for token in rest[: next((i for i, t in enumerate(rest) if t.startswith("-")), len(rest))]]
        path = next((" ".join(words[:n]) for n in range(len(words), -1, -1) if self.contract.has_path(" ".join(words[:n]))), " ".join(words))
        tail = rest[len(words):]
        options = tuple((token, tail[i + 1] if i + 1 < len(tail) and not tail[i + 1].startswith("-") else "") for i, token in enumerate(tail) if token.startswith("--"))
        return Invocation(text=text.strip(), root_options=root, path=path, options=options, fenced=fenced)

    def _leading_options(self, tokens: list[str]) -> list[str]:
        # The run of --options before the first command word; they are the root options.
        leading = []
        for token in tokens:
            if not token.startswith("-"):
                break
            leading.append(token)
        return leading


class SkillChecks:
    """Every contract check, each raising AssertionError with the reason when the skill breaks it."""

    def __init__(self, document: SkillDocument, contract: CliContract, invocations: list[Invocation]) -> None:
        # Holds the parsed skill, the CLI contract, and the extracted invocations every check reads.
        self.doc = document
        self.contract = contract
        self.invocations = invocations

    def name_equals_folder(self) -> None:
        # The website refuses a SKILL.md whose name differs from its slug.
        assert self.doc.value("name") == SLUG, f"name is {self.doc.value('name')!r}, folder is {SLUG!r}"

    def description_shape(self) -> None:
        # "Use when", at most 1024 characters, two or more quoted phrasings, no price, names the CLI.
        description = self.doc.value("description")
        assert description.startswith("Use when"), "description must start with 'Use when'"
        assert len(description) <= 1024, f"description is {len(description)} characters"
        assert len(re.findall(r'"[^"]+"', description)) >= 2, "description must quote at least two user phrasings"
        assert not re.search(r"\$\s?\d|\d+\s?cents?\b", description), "description must not hold a price"
        assert "vidbyte-cli" in description and "Suggestion Agent" in description, "description must name the agent and vidbyte-cli"

    def metadata_fields(self) -> None:
        # Every metadata field is present, quoted except status, and drawn from its vocabulary.
        for name in METADATA_FIELDS:
            assert f"metadata.{name}" in self.doc.frontmatter_raw, f"metadata.{name} is missing"
        for name in METADATA_FIELDS[:-1]:
            raw = self.doc.frontmatter_raw[f"metadata.{name}"]
            assert raw.startswith('"') and raw.endswith('"'), f"metadata.{name} must be quoted, got {raw}"
        assert "metadata.internal" not in self.doc.frontmatter_raw, "metadata.internal is reserved"
        assert self.doc.value("metadata.agent") in AGENT_IDS, "metadata.agent is not a known agent id"
        assert self.doc.value("metadata.status") in ("stable", "preview"), "metadata.status must be stable or preview"
        assert self.doc.value("metadata.writes-files") in ("true", "false"), "metadata.writes-files must be true or false"

    def version_agrees(self) -> None:
        # min-cli-version is 0.3.2 (first release with usage-billed suggestions) everywhere it appears.
        version = self.doc.value("metadata.min-cli-version")
        assert version == "0.3.2", f"min-cli-version is {version}, expected 0.3.2"
        assert f"# must be {version} or newer" in self.doc.section("Preflight"), "Preflight --version comment disagrees"
        assert f"needs {version} or newer" in self.doc.section("CLI commands"), "CLI commands table version disagrees"

    def section_order(self) -> None:
        # The ## headings equal the contract's order exactly, with no When-to-use sections.
        titles = tuple(title for title, _ in self.doc.sections)
        assert not any(title.startswith("When") for title in titles), "When to use / When not to use sections are not allowed"
        assert titles == SECTION_ORDER, f"sections are {titles}"

    def root_options_first(self) -> None:
        # No root-only option such as --json appears after the command path.
        for call in self.invocations:
            late = [name for name, _ in call.options if name in ROOT_ONLY_OPTIONS]
            assert not late, f"root option {late} after the command path in: {call.text}"

    def commands_exist_in_contract(self) -> None:
        # Every command path and long option exists in contracts/cli-commands.json on origin/main.
        for call in self.invocations:
            assert self.contract.has_path(call.path), f"unknown command path {call.path!r} in: {call.text}"
            unknown_root = set(call.root_options) - self.contract.options_for("")
            unknown = {name for name, _ in call.options} - self.contract.options_for(call.path)
            assert not unknown_root and not unknown, f"unknown options {sorted(unknown_root | unknown)} in: {call.text}"

    def table_covers_commands(self) -> None:
        # The CLI commands table lists every path run in a code block, and metadata.commands matches it.
        table = self.doc.section("CLI commands")
        table_paths = {call.path for call in self.invocations if not call.fenced and call.text in table}
        run_paths = {call.path for call in self.invocations if call.fenced}
        assert run_paths <= table_paths, f"run but not in the table: {sorted(run_paths - table_paths)}"
        declared = {part.strip() for part in self.doc.value("metadata.commands").split(",")}
        assert declared == {path for path in table_paths if path.startswith("agents ")}, f"metadata.commands {sorted(declared)} disagrees with the table"
        for path in declared:
            assert self.contract.has_path(path), f"metadata.commands names unknown path {path!r}"

    def categories_are_registry_ids(self) -> None:
        # Every --category is a registered id, and Preflight checks exactly the lenses Run passes.
        values = {value for call in self.invocations for name, value in call.options if name == "--category"}
        assert values, "no --category found"
        unknown = values - self.contract.category_ids
        assert not unknown, f"unknown categories {sorted(unknown)}"
        checked = set(re.findall(r"`([a-z_]+)`", self.doc.section("Preflight"))) & self.contract.category_ids
        assert checked == values, f"Preflight checks {sorted(checked)} but Run passes {sorted(values)}"

    def token_cap_agrees(self) -> None:
        # The --max-total-tokens the run passes equals the cap metadata.cost and Cost and consent state.
        caps = {value for call in self.invocations for name, value in call.options if name == "--max-total-tokens"}
        assert len(caps) == 1, f"expected one token cap across commands, got {sorted(caps)}"
        cap = int(next(iter(caps)))
        assert f"capped at {cap:,} tokens" in self.doc.value("metadata.cost"), "metadata.cost names another cap"
        assert f"--max-total-tokens {cap}" in self.doc.section("Cost and consent"), "Cost and consent names another cap"
        assert f"{cap:,} tokens" in self.doc.section("Cost and consent"), "Cost and consent prose names another cap"

    def dry_run_matches_paid_run(self) -> None:
        # Each dry run validates exactly the request the paid run sends, minus --dry-run.
        runs = [call for call in self.invocations if call.fenced and call.path == "agents suggest run"]
        dry = [sorted(o for o in call.options if o[0] != "--dry-run") for call in runs if ("--dry-run", "") in call.options]
        paid = [sorted(call.options) for call in runs if ("--dry-run", "") not in call.options]
        assert dry and paid, "expected both dry-run and paid-run commands in code blocks"
        assert all(options == paid[0] for options in dry + paid), "a dry run or paid run differs from the others"

    def no_forbidden_run_flags(self) -> None:
        # The run never passes flags this design rules out (substring suppression, post-review filters).
        for call in self.invocations:
            used = [name for name, _ in call.options if name in FORBIDDEN_RUN_FLAGS]
            assert not used, f"forbidden flag {used} in: {call.text}"

    def goal_is_shell_inert(self) -> None:
        # The goal template and its example use only letters, digits, spaces, commas, periods, hyphens.
        templates = [body.strip() for lang, body in self.doc.fences if lang == "text" and body.strip().startswith("Leave the change for")]
        examples = [span for span in self.doc.inline_spans if span.startswith("Leave the change for")]
        assert templates and examples, "goal template or example not found"
        for goal in templates + examples:
            plain = re.sub(r"<[^>]+>", "x", goal)
            assert GOAL_CHARSET.match(plain), f"goal holds a shell-active character: {goal}"
            assert len(goal) <= 600, "goal is over 600 characters"

    def steps_name_sections(self) -> None:
        # Every numbered step ends by naming the section that holds its details.
        steps = re.findall(r"^\d+\. (.*?)(?=^\d+\. |\Z)", self.doc.section("Steps"), flags=re.M | re.S)
        assert len(steps) >= 6, "Steps lists too few steps"
        titles = set(SECTION_ORDER)
        for step in steps:
            reference = re.search(r"\(([^()]*)\)\s*$", step.strip())
            assert reference and reference.group(1).split(",")[0] in titles, f"step does not end with its section: {step.strip()[:60]}"

    def troubleshooting_and_guardrails(self) -> None:
        # The exit table has rows 2, 4, 5, Other, and Guardrails restate the standing rules.
        table = self.doc.section("Troubleshooting")
        for row in ("| 2 |", "| 4 |", "| 5 |", "| Other |"):
            assert row in table, f"Troubleshooting lacks the {row} row"
        rails = self.doc.section("Guardrails")
        for rule in ("paste an API key", "billing top-up --confirm", "one file-writing Vidbyte agent"):
            assert rule in rails, f"Guardrails lack the rule about {rule!r}"

    def references_identical(self) -> None:
        # setup.md and troubleshooting.md equal the canonical copies, ignoring checkout line endings.
        for name in ("setup.md", "troubleshooting.md"):
            ours = (SKILL_REFERENCES / name).read_bytes().replace(b"\r\n", b"\n")
            canon = (CANONICAL_REFERENCES / name).read_bytes().replace(b"\r\n", b"\n")
            assert ours == canon, f"references/{name} differs from the canonical copy"

    def named_siblings_exist(self) -> None:
        # Every skill slug the body names in backticks is published under skills/ on this branch.
        tokens = {span for span in self.doc.inline_spans if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+){2,}", span)}
        missing = [token for token in sorted(tokens - NON_SKILL_KEBAB_TOKENS) if not (ROOT / "skills" / token / "SKILL.md").is_file()]
        assert not missing, f"names skills that are not under skills/: {missing}"


class WebsiteEntryCheck:
    """Compares the Vidbyte catalog entry with the frontmatter, through Node's ESM loader."""

    def __init__(self, website_repo: Path) -> None:
        # Points at next-app/constants/agent-skills/<slug>/catalog.js in a Vidbyte checkout.
        self.entry = website_repo / "next-app" / "constants" / "agent-skills" / SLUG / "catalog.js"

    def load(self) -> dict:
        # Imports the entry with Node, so defineAgentSkill's validator runs exactly as in the build.
        script = "import(process.argv[1]).then(m => process.stdout.write(JSON.stringify(m.default)))"
        result = subprocess.run(["node", "-e", script, self.entry.resolve().as_uri()], capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, f"node could not import the entry: {result.stderr.strip()[-400:]}"
        return json.loads(result.stdout)

    def compare(self, document: SkillDocument, entry: dict) -> None:
        # Description verbatim, version, writes-files, and commands must all agree.
        assert entry["description"] == document.value("description"), "website description differs from the frontmatter"
        assert entry["minCliVersion"] == document.value("metadata.min-cli-version"), "minCliVersion disagrees"
        assert entry["writesFiles"] == (document.value("metadata.writes-files") == "true"), "writesFiles disagrees"
        declared = {f"vidbyte-cli {part.strip()}" for part in document.value("metadata.commands").split(",")}
        assert set(entry["commands"]) == declared, f"commands {entry['commands']} disagree with metadata.commands"
        assert entry["slug"] == SLUG and entry["agents"] == [document.value("metadata.agent")], "slug or agent disagrees"


class TestRunner:
    """Collects PASS/FAIL/SKIP per test and prints the summary."""

    def __init__(self) -> None:
        # Starts with no results.
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def run(self, category: str, name: str, test) -> None:
        # Runs one test callable; an AssertionError or any exception is a FAIL with its reason.
        try:
            test()
        except AssertionError as error:
            self.failed += 1
            print(f"FAIL [{category}] {name} -- {error}")
            return
        except Exception:  # noqa: BLE001 - a crash in a check is a failure, never a pass
            self.failed += 1
            print(f"FAIL [{category}] {name} -- crashed:\n{traceback.format_exc()}")
            return
        self.passed += 1
        print(f"PASS [{category}] {name}")

    def skip(self, category: str, name: str, reason: str) -> None:
        # Records a test that could not run, without counting it as passed.
        self.skipped += 1
        print(f"SKIP [{category}] {name} -- {reason}")

    def finish(self) -> int:
        # Prints "X/Y tests passed" and returns the process exit code.
        total = self.passed + self.failed
        skipped = f", {self.skipped} skipped" if self.skipped else ""
        print(f"\n{self.passed}/{total} tests passed{skipped} (Python {sys.version.split()[0]})")
        return 1 if self.failed else 0


class MutationSuite:
    """Proves each check can fail: one mutated copy of the skill per check must be rejected."""

    MUTATIONS = (
        ("section order check rejects swapped sections", "section_order", ("## Steps\n", "## Steps-moved\n")),
        ("forbidden-flag check rejects --horizon on the run", "no_forbidden_run_flags", ("--count 8 \\\n", "--count 8 --horizon now \\\n")),
        ("contract check rejects an option the CLI lacks", "commands_exist_in_contract",
         ("--max-total-tokens 300000 --dry-run", "--max-total-tokens 300000 --dry-run --bogus-flag")),
        ("dry-run check rejects a dry run that differs from the paid run", "dry_run_matches_paid_run",
         ("--count 8 --files work.md --max-total-tokens 300000 --dry-run", "--count 9 --files work.md --max-total-tokens 300000 --dry-run")),
        ("category check rejects an unknown category", "categories_are_registry_ids", ("--category adjacent_opportunity \\\n", "--category adjacent_work \\\n")),
        ("goal check rejects a goal holding a $", "goal_is_shell_inert", ("Leave the change for <the request", "Leave the change for $HOME <the request")),
        ("token-cap check rejects a cost line naming another cap", "token_cap_agrees", ("capped at 300,000 tokens per run", "capped at 400,000 tokens per run")),
        ("category check rejects a Preflight lens Run does not pass", "categories_are_registry_ids", ("`adjacent_opportunity`, the five lenses", "`leverage`, the five lenses")),
        ("root-option check rejects --json after the path", "root_options_first", ("vidbyte-cli --json agents suggest handoff --input", "vidbyte-cli agents suggest handoff --json --input")),
    )

    def __init__(self, text: str, contract: CliContract) -> None:
        # Keeps the real skill text and the contract each mutation is checked against.
        self.text = text.replace("\r\n", "\n")
        self.contract = contract

    def cases(self) -> list[tuple[str, callable]]:
        # One test per mutation: the mutation must apply, and its check must then raise.
        return [(name, lambda check=check, pair=pair: self._expect_rejection(check, pair)) for name, check, pair in self.MUTATIONS]

    def _expect_rejection(self, check: str, pair: tuple[str, str]) -> None:
        # Applies one text replacement and asserts the named check fails on the result.
        old, new = pair
        assert old in self.text, f"mutation anchor not found: {old!r}"
        mutated = SkillDocument.from_text(self.text.replace(old, new, 1))
        checks = SkillChecks(mutated, self.contract, CommandExtractor(self.contract).extract(mutated))
        try:
            getattr(checks, check)()
        except AssertionError:
            return
        raise AssertionError(f"{check} accepted a mutated skill")


def find_cli_repo(explicit: str | None) -> Path:
    # Uses --cli-repo, else the first folder named vidbyte-cli beside any parent of this repository.
    if explicit:
        return Path(explicit)
    for parent in ROOT.parents:
        candidate = parent / "vidbyte-cli"
        if (candidate / ".git").exists():
            return candidate
    raise SystemExit("vidbyte-cli internal repository not found; pass --cli-repo")


def main() -> int:
    # Runs every contract check, the website comparison when asked, and the mutation suite.
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cli-repo", help="path to the cerredz/vidbyte-cli-internal checkout")
    parser.add_argument("--cli-ref", default="origin/main", help="git ref of the CLI contract, such as a release commit")
    parser.add_argument("--website-repo", help="path to a Vidbyte checkout holding the catalog entry")
    arguments = parser.parse_args()

    # Load the skill and the CLI contract every check reads.
    text = SKILL_FILE.read_text(encoding="utf-8")
    document = SkillDocument.from_text(text)
    contract = CliContract(find_cli_repo(arguments.cli_repo), arguments.cli_ref)
    invocations = CommandExtractor(contract).extract(document)
    checks = SkillChecks(document, contract, invocations)
    print(f"CLI contract: {contract.ref} @ {contract.sha}; {len(invocations)} vidbyte-cli invocations found\n")

    # Run the contract checks, labelled with the failure category each one guards against.
    runner = TestRunner()
    runner.run("Hidden Assumption", "frontmatter name equals the folder name", checks.name_equals_folder)
    runner.run("Edge Case", "description starts with Use when, fits 1024 chars, quotes phrasings, has no price", checks.description_shape)
    runner.run("Silent Failure", "every metadata field is present and quoted except status", checks.metadata_fields)
    runner.run("Silent Failure", "min-cli-version 0.3.2 agrees with Preflight and the commands table", checks.version_agrees)
    runner.run("Hidden Assumption", "sections follow the contract order with no When sections", checks.section_order)
    runner.run("Silent Failure", "root options come before the command path", checks.root_options_first)
    runner.run("Hidden Failure", "every command path and option exists in the CLI contract", checks.commands_exist_in_contract)
    runner.run("Silent Failure", "the commands table covers every command run, and metadata.commands matches", checks.table_covers_commands)
    runner.run("Hidden Failure", "every --category is a registered category id", checks.categories_are_registry_ids)
    runner.run("Silent Failure", "the token cap agrees across the run, metadata.cost, and Cost and consent", checks.token_cap_agrees)
    runner.run("Silent Failure", "every dry run validates exactly the paid run's request", checks.dry_run_matches_paid_run)
    runner.run("Hidden Assumption", "no command passes a flag the design rules out", checks.no_forbidden_run_flags)
    runner.run("Edge Case", "the goal template and example are shell-inert and at most 600 chars", checks.goal_is_shell_inert)
    runner.run("Hidden Assumption", "every step names the section that holds its details", checks.steps_name_sections)
    runner.run("Hidden Assumption", "troubleshooting rows and standing guardrails are present", checks.troubleshooting_and_guardrails)
    runner.run("Silent Failure", "references are identical to the canonical copies", checks.references_identical)
    runner.run("Hidden Assumption", "every sibling skill named is published under skills/", checks.named_siblings_exist)

    # Compare the website listing only when a Vidbyte checkout was named.
    if arguments.website_repo:
        website = WebsiteEntryCheck(Path(arguments.website_repo))
        runner.run("Silent Failure", "the website entry matches the frontmatter", lambda: website.compare(document, website.load()))
    else:
        runner.skip("Silent Failure", "the website entry matches the frontmatter", "pass --website-repo to run it")

    # Prove the checks can fail by feeding each one a mutated copy of the skill.
    for name, test in MutationSuite(text, contract).cases():
        runner.run("Hidden Failure", name, test)
    return runner.finish()


if __name__ == "__main__":
    sys.exit(main())
