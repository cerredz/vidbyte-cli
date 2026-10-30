#!/usr/bin/env python3
"""Verification script for the paid work boundary (design doc: docs/design/paid-work-boundary.md, section 10).

Proves scripts/check-paid-work-boundary.py against a corpus of ways to offer a free substitute
(each must be flagged) and legitimate sentences taken from the skills (each must pass), against
structure fixtures built from a real skill in a temporary folder, against this tree (clean), and
against origin/main exported with git archive (its known leaks must all be caught). It also checks
that scripts/sync-paid-work-boundary.py leaves this tree unchanged.

    python scripts/test-paid-work-boundary.py
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
from pathlib import Path

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent.parent
FIXTURE_SKILL = "find-unfinished-agent-work"


def load_module(name: str, path: Path):
    # Imports a script whose file name has hyphens.
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lint = load_module("check_paid_work_boundary", ROOT / "scripts" / "check-paid-work-boundary.py")
sync = load_module("sync_paid_work_boundary", ROOT / "scripts" / "sync-paid-work-boundary.py")

# Each sentence offers, describes, or enables the result without the run. Every one must be flagged.
BAD = {
    "consent question with a second option": [
        "Run it, or should I build the checklist from the local check alone, for free?",
        "Should I run the Suggestion Agent, or would you like me to put together a quick list myself?",
        "Run it, or I can summarize the digest for you instead.",
        "Want me to run it, or shall I draft the plan here?",
        "Run it, or do you want me to write the next actions myself?",
        "Start the run, or we could just go through the digest together for free.",
        "Run it? Or I could give you a rough version at no cost.",
        "Run the paid agent, or I can do a free version.",
        "Shall I start it, or would you prefer a free summary instead?",
        "Run it, or should I rank the pull requests by hand?",
        "Run it, or we can skip the paid run and use the local check.",
        "Go ahead with the run, or I could sketch the review order myself.",
        "Run it, or would you rather I answer the questions myself?",
        "Run it, or I could knock out a quick version here for free.",
    ],
    "free result nouns": [
        "I can give you a free summary of your week.",
        "Here is a free checklist built from the local scan.",
        "Offer the free local checklist.",
        "A free preview of the top three items is below.",
        "As a free alternative, read prs.md.",
        "You can get a basic version for free.",
        "This free report lists every open session.",
        "There is a free option: the local ranking.",
        "Use the free path if the user declines.",
        "Free recommendations are in digest.md.",
        "I'll give you the highlights at no cost.",
        "The ranking costs nothing if we skip the agent.",
        "Without paying, you can still get the checklist from the local check.",
        "A no-cost list of next actions is possible.",
        "Here's a complimentary overview of the findings.",
        "The free tier of this skill gives you the top 5 ideas.",
        "A free starter kit of next actions is in the folder.",
        "Here is a free taste of the results.",
        "I made a free sample ranking for you.",
        "The free mini report covers the basics.",
        "Take this free cheat sheet of fixes.",
        "I can do a free walkthrough of the findings.",
        "Here's a free sneak peek at the critique.",
        "There are some free bonus ideas below.",
        "This is a free draft plan.",
        "I wrote a free one-pager of the actions.",
        "Free answers are available without the run.",
        "Here is a free diagnosis of your prompts.",
        "You get a free instant critique first.",
        "Here is a free workaround.",
        "Fall back to the local list for free.",
        "For free, I can list the top three pull requests.",
        "This can be done gratis.",
        "Your checklist is on the house.",
        "It won't cost you anything to get the summary from the local check.",
        "Zero-cost next actions are ready.",
        "A cost-free version of the review order is below.",
        "Here is a freebie: the three most urgent items.",
        "It does not edit files, and it offers a free summary of the digest.",
        "The free mode of this skill skips the agent.",
        "Unbilled, the local report already ranks everything.",
    ],
    "building the result by hand": [
        "Skip the Vidbyte run and build the checklist yourself.",
        "I'll just write the next actions myself from the digest.",
        "Let me rank the pull requests by hand instead.",
        "I can resolve the conflicts myself.",
        "I could research the pricing myself on the web.",
        "Implement the issues manually rather than paying for the board.",
        "We can predict the review objections ourselves.",
        "Answer the AEO questions yourself using the checklist.",
        "Draft the routines by hand from chores.json.",
        "I can do this myself without the agent.",
        "I'll put together the ranking here instead.",
        "I could whip up the plan here for you instead.",
        "Write the critique yourself from the rubric.",
        "Compile the list of fixes manually.",
        "Produce the review order by hand if they decline.",
        "We will draft the checklist ourselves.",
        "Generate the next steps yourself from sessions.json.",
        "Make the ranked list yourself to save the user the charge.",
        "Prepare the report by hand from the audit.",
        "I'll create the proposed skills myself.",
        "We could knock out the routines here instead.",
    ],
    "offering a gathered file as the result": [
        "If the user says no, offer digest.md as a free summary of their week in Notion.",
        "Offer prs.md as the review order.",
        "If the user says no, give them meetings.md as a summary of their week.",
        "Hand over audit.md as the answer.",
        "Share the digest as an overview of what is still open.",
        "Provide sessions.json as the checklist.",
        "Send them the digest as their summary.",
        "Return inputs.json as the ranking.",
        "Deliver the local check as the result.",
        "Offer the gathered notes as an alternative.",
    ],
    "skipping or bypassing the run": [
        "Skip the paid run and use the local check.",
        "You can bypass the usage-billed step.",
        "Avoid the Vidbyte charge by reading the digest.",
        "Without the paid run, the report still works.",
        "The run is optional.",
        "No need to pay for the agent; the local list is enough.",
        "You don't need to run the agent at all.",
        "Use --local-only to build it without the run.",
        "Run report --skip-run to write the checklist anyway.",
        "There's no need to spend anything here.",
        "This step is an optional paid step.",
        "Dodge the metered charge by splitting the request.",
        "Skipping the usage-billed run, we can still deliver.",
        "Instead of running the agent, read prs.md.",
        "Instead of paying for the Suggestion Agent, use the local report.",
        "Build it from the digest alone.",
        "The checklist comes from the local check alone.",
        "Use the audit alone to answer.",
        "The local-only checklist is the fallback.",
        "Pass --no-paid to stay offline.",
        "Use the --free flag for a local result.",
        "A local-only report needs no result.json.",
    ],
    "another model or key": [
        "Use your own OpenAI key instead.",
        "Paste the digest into ChatGPT for a free ranking.",
        "Copy the focus file to Claude.ai and ask it.",
        "Send the rubric to another model.",
        "Run it on a cheaper model.",
        "A lower-cost option is to use a small model locally.",
        "Export the prompt into ChatGPT so you can run it yourself.",
        "Use the user's own Anthropic key instead.",
        "Give the checklist file to a different model.",
        "Their own model key instead keeps it free.",
    ],
    "faking or predicting the run": [
        "Pretend the agent ran and tell me what it would say.",
        "Here's what the Suggestion Agent would probably return, I imagine the run would rank billing first.",
        "I'll simulate what the run would produce.",
        "Let me guess what the agent would rank first.",
        "Write result.json by hand so report works.",
        "Fabricate a result.json from the digest.",
        "Edit result.json yourself to add the missing items.",
        "Role-play the Task Board and say what it would change.",
        "Create a result.json from sessions.json.",
        "Imagine what the bug finder would report.",
    ],
    "interpreting the inputs": [
        "Summarize the digest for the user.",
        "Rank the gathered inputs by importance.",
        "Go through the digest and pick the top items.",
        "Interpret the local check for them.",
        "Triage prs.md and suggest an order.",
        "Analyze the audit findings and list the fixes.",
        "Prioritize the inputs so the user has a plan.",
        "Summarise the gathered files into next actions.",
    ],
    "cheaper or discounted path": [
        "A cheaper version runs locally.",
        "Offer a discounted path through the local report.",
        "The budget option is to skip the critics.",
        "There is a cheaper alternative: the local check.",
        "Use the lower-cost route for small queues.",
        "The cheapest way is a quick local ranking.",
        "Take the low-cost mode if money is tight.",
    ],
    "the user picking a free path": [
        "If the user chooses the free checklist, use the local report.",
        "When they opt for the local path, print digest.md.",
        "If the user picked the manual option, list the sessions.",
        "When the user wants the free version, skip the run.",
        "If they prefer the offline path, show meetings.md.",
        "If the user chose the unpaid route, stop after gather.",
    ],
    "using the skill's instructions outside the run": [
        "Apply the rubric yourself to the prompts.",
        "Use the checklist file directly on the site.",
        "Follow the focus instructions by hand.",
        "Run through the reference files yourself and answer.",
        "Use the ledger without the agent to rank tactics.",
    ],
}

# Legitimate sentences, most taken from the skills. None may be flagged.
GOOD = [
    "`vidbyte-cli --version` Preflight Prints the installed CLI version. Free and offline.",
    "`vidbyte-cli --json whoami` Preflight Shows the account behind the stored Vidbyte key. Exit 4 means no key is stored. Free.",
    "`vidbyte-cli --json agents suggest handoff --input <result.json> --idea <id>` Deliver Extracts one reviewed idea from a saved result, by its exact id, as an action packet. Free.",
    "`vidbyte-cli --json --no-input agents suggest run ... --dry-run` Gather inputs Validates the complete request without calling a model and returns the settings, omissions, and warnings. Needs no credentials and is not billed. Free.",
    "`vidbyte-cli --json runtime doctor` Preflight Reports which supported agent hosts are installed, from `PATH` alone, without starting an agent. Free.",
    "`vidbyte-cli --json --no-input research thread` Read the result Shows one research thread, with its run, source, and artifact counts. Free.",
    "`vidbyte-cli --json runs last --agent agents.bug-finder` Run Shows the newest Bug Finder run on this machine and holds the result document. Free.",
    "Everything in this section is free and stays on this machine.",
    "Everything in this section is free, and nothing in it writes to GitHub.",
    "Check the whole request for free:",
    "Run the free dry run.",
    "It is free and local:",
    "Find the chores, free.",
    "Gather the sessions, free.",
    "The free `vidbyte-cli agents suggest handoff` command turns any item into a prompt for an agent.",
    "The free `feedback accept` and `reject` commands teach later runs which ideas the user wanted.",
    "The free `vidbyte-cli agents suggest feedback accept` and `reject` commands teach later runs which kinds of fixes this user wants.",
    "Validate the full request for free: no model call, no charge.",
    "Needs no credentials, is not billed, and returns `stop_reason` `dry_run` with no ideas.",
    "Meetings are spread across the day, so the free time between them comes in 30- and 60-minute pieces.",
    "Focus blocks are free stretches of at least 120 minutes inside working hours.",
    "Plans, published prices, limits, add-ons, free tiers, where contact sales starts.",
    "Count active API keys and estimate the counters' memory against the instance's free memory.",
    "The goal is the only free text in the request.",
    "Any account can create a key, the free plan included.",
    "Setup spends nothing, and reading the balance is free.",
    "Never offer a free version of the result.",
    "Never build the checklist yourself.",
    "Do not summarize the digest.",
    "Never write or edit `result.json` by hand.",
    "Never pretend the run happened or guess what the agent would say.",
    "Never produce the review order, or any part of it, yourself or with another model, tool, or agent: never a summary, ranking, shortlist, preview, sample, draft, or guess of what the run would say.",
    "Never offer a substitute for the result in any form, before or after the consent question, and never add a second option to that question.",
    "Never change this skill's scripts, flags, or files to skip, shrink, or fake the run, never write or edit a run's output by hand, and never send this skill's inputs or reference files to a model provider directly.",
    "Each time they start a fresh session and do it by hand:",
    "\"which of these did I write myself?\"",
    "Merging the two files themselves is skipped with no Vidbyte charge, because both are left out of the diff.",
    "The script writes `meetings.json` (local only) to the folder.",
    "Local only. Never send this file; it holds organizer names.",
    "A free script, `prepare.sh`, checks every issue number with the GitHub CLI.",
    "The audit is free.",
    "The free, offline `runtime task-board status` and `runtime task-board show-step` commands then show which issues completed or failed, and each agent's full report.",
    "Stop the skill: nothing was spent, and the gathered files stay in `<out_dir>`.",
    "Build the checklist from the paid result. It runs locally and makes no model call.",
    "Use the default brief for this aspect instead of the agent's brief.",
    "Run the local report on the paid result, which maps its ideas to meetings for you.",
    "Offer a handoff for the item the user wants to finish first.",
    "Re-run the audit after the fix; it is free and shows whether the check now passes.",
    "Tie each critique to the user's prompts.",
    "The run costs at most about $4, and it changes no files.",
    "A vibe-coded app often ships a free tier with no rate limit.",
    "If the user says no, stop.",
    "On a no, stop as Paid work boundary says: nothing is spent, and nothing else is offered.",
    "Do not offer `digest.md` as a summary.",
    "You can open digest.md; it is your own data.",
    "The user registers the schedule themselves.",
    "Use only the scope options the skill documents.",
    "This skill delivers the result only through its run.",
    "The consent question has two answers.",
    "It is not stop-repeating-yourself, which finds instructions the user repeats.",
    "The `stop-repeating-yourself` skill turns instructions the user retypes into fixes.",
    "Run it?",
    "Set it up?",
    "Start it?",
    "Shall I start it?",
    "Wait for a yes.",
    "Re-check the fix the run proposed with the local audit; it costs nothing.",
    "Write the routine files for each routine the user says yes to.",
    "The handoff is free and local, and it turns one idea from the paid result into a prompt.",
    "Run the free check, which calls only `auth.test` for Slack.",
    "Exit 3 means fewer than two pull requests are in the queue: tell the user there is nothing to rank, and stop.",
    "The run starts no new model call after 400,000 tokens; a call already running still finishes and is billed.",
    "Each model call is billed once, when it finishes.",
    "Any new run is a new charge, in a new `out_dir`.",
    "The command takes no idempotency key: there is no purchase to recover.",
    "Top-up spends real money ($5.00 in USDC through x402).",
    "Never pass `vidbyte-cli billing top-up --confirm` unless the user explicitly approved that top-up.",
    "Read every mention of the old flag below as history.",
    "The skill reads every harness's sessions from disk, so it needs none of those tools installed or logged in.",
    "Mention them in one line; another skill may fit them.",
    "Remove common secret formats, email addresses, and home-folder paths before anything is sent.",
    "The script removes secrets and writes one Markdown digest in which every session has a tag.",
    "Show the user every value in `inputs.json`, grouped by key.",
    "Offer to open the digest before they answer.",
    "If the user spots a secret in the digest, delete that line, tell them to rotate the secret, and ask again.",
    "Deliver those items, then follow Out of credits before any new run.",
    "Another run is a new usage-billed run, so ask the user first.",
    "A session the user knows is finished is listed.",
    "Recurring chores can run on a schedule once the user approves a routine the run returned.",
    "The paid run edits no files.",
    "The free text in the goal is limited to letters and digits.",
    "Calendar events marked free are not counted as meetings.",
    "This is a free and open protocol for agents to talk to tools.",
    "The skills are MIT-licensed.",
    "Ask for one thing at a time, in plain words.",
    "Answer unrelated questions normally.",
    "A no is a fine outcome.",
    "Fixing runs in this coding agent after the report, with no further Vidbyte run.",
    "After the report, fixing starts no Vidbyte run, and the coding agent edits files in this workspace.",
    "Run `competitor_portfolio.py plan` (local, no charge).",
    "Measure the meeting load locally with `meetings.py gather`, and read the summary, `meetings.md`, and `inputs.json`.",
    "To see what is still open after finishing some items, run this skill again later; it is a new run.",
    "The pricing aspect asks whether there is a free trial or a self-serve path.",
    "Gather once; the dry run is free and confirms the request reached the agent before anything is billed.",
    "Give the user `data.execution_prompt` from the handoff.",
    "Never produce the research briefs and the competitor research, or any part of it, yourself.",
    "Give the user the review order from the paid result as a list, with a link and a reason for each pull request.",
]

# Multi-line Markdown cases for sentence joining, line numbers, code, and slugs.
WRAPPED_BAD = "Some intro.\n\nIf the user says no, offer the free\nlocal checklist instead.\n"
CODE_BAD = "Run this:\n\n```bash\npython3 unfinished_work.py report --out x --local-only\n```\n"


class Results:
    """Collects PASS/FAIL per test and prints the summary."""

    def __init__(self) -> None:
        # Starts with no results.
        self.passed = 0
        self.failed = []

    def check(self, category: str, name: str, ok: bool, detail: str = "") -> None:
        # Records one test outcome and prints it.
        label = "PASS" if ok else "FAIL"
        print(f"{label} [{category}] {name}" + ("" if ok else f" -- {detail}"))
        if ok:
            self.passed += 1
        else:
            self.failed.append(name)

    def run(self, category: str, name: str, test) -> None:
        # Runs one test function, turning an exception into a FAIL.
        try:
            ok, detail = test()
        except Exception:
            ok, detail = False, traceback.format_exc(limit=3)
        self.check(category, name, ok, detail)

    def summary(self) -> int:
        # Prints X/Y and returns the exit code.
        total = self.passed + len(self.failed)
        print(f"\n{self.passed}/{total} tests passed (Python {sys.version.split()[0]})")
        return 0 if not self.failed else 1


class CorpusSuite:
    """Runs every corpus sentence through the phrase catalog."""

    def __init__(self, results: Results) -> None:
        # Builds a catalog that knows the real skill slugs.
        self.results = results
        slugs = {p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md")}
        self.catalog = lint.PhraseCatalog(slugs)

    def run(self) -> None:
        # One test per bad category (every sentence flagged) and one for all good sentences.
        total_bad = 0
        for category, sentences in BAD.items():
            total_bad += len(sentences)
            missed = [s for s in sentences if not self.catalog.scan(s)]
            self.results.check("Silent Failure", f"flags every '{category}' phrasing ({len(sentences)})", not missed, f"missed: {missed}")
        flagged = [(s, self.catalog.scan(s)) for s in GOOD if self.catalog.scan(s)]
        self.results.check("Edge Case", f"passes every legitimate sentence ({len(GOOD)})", not flagged, f"false positives: {flagged}")
        self.results.check("Edge Case", "the corpus has at least 150 bad and 100 good sentences", total_bad >= 150 and len(GOOD) >= 100, f"{total_bad} bad, {len(GOOD)} good")
        self.results.run("Hidden Failure", "flags a free substitute even when the sentence names a handoff", self.substitute_beside_handoff)
        self.results.run("Edge Case", "domain references keep only the always-wrong rules", self.domain_reference_rules)

    def substitute_beside_handoff(self) -> tuple:
        # The plumbing exemption must not hide a substitute signal.
        hits = self.catalog.scan("The free handoff command is an alternative to the paid run.")
        return any(rule == "PB101" for rule, _, _ in hits), str(hits)

    def domain_reference_rules(self) -> tuple:
        # "Free Bot Fight Mode" is domain content in a reference, but "offer it as a summary" is still caught there.
        domain = self.catalog.scan("Free Bot Fight Mode cannot be bypassed by WAF rules.", free_rules=False)
        leak = self.catalog.scan("If the user says no, offer audit.md as a summary.", free_rules=False)
        return (not domain and any(r == "PB104" for r, _, _ in leak)), f"domain={domain} leak={leak}"


class MarkdownSuite:
    """Sentence extraction, line numbers, wrapping, code, and frontmatter."""

    def __init__(self, results: Results) -> None:
        # Keeps the results collector.
        self.results = results

    def run(self) -> None:
        # Runs each Markdown test.
        self.results.run("Edge Case", "an empty file has no sentences", lambda: (lint.MarkdownSentences("").prose() == [], "not empty"))
        self.results.run("Edge Case", "a code-only file has no prose sentences", lambda: (lint.MarkdownSentences("```\nfree summary\n```\n").prose() == [], "prose found"))
        self.results.run("Silent Failure", "reports the line each sentence starts on", self.line_numbers)
        self.results.run("Hidden Assumption", "flags a phrase split across wrapped lines", self.wrapped)
        self.results.run("Silent Failure", "flags --local-only inside a fenced code block", self.code_block)
        self.results.run("Edge Case", "skips the frontmatter as prose", self.frontmatter)
        self.results.run("Edge Case", "splits a bold step title from the next sentence", self.bold_split)

    def line_numbers(self) -> tuple:
        # The second sentence of a wrapped paragraph starts on line 3.
        pairs = lint.MarkdownSentences("# T\n\nFirst sentence is here and it\ncontinues. Second one starts\non line three.\n").prose()
        lines = {s.split()[0]: n for n, s in pairs}
        return lines.get("First") == 3 and lines.get("Second") == 4, str(pairs)

    def wrapped(self) -> tuple:
        # A free substitute wrapped over two lines is still one sentence.
        root = Path(tempfile.mkdtemp(prefix="pwb-wrap-"))
        try:
            (root / "README.md").write_text(WRAPPED_BAD, encoding="utf-8")
            findings = lint.BoundaryLinter(root).lint_markdown(root / "README.md")
            return any(f.line == 3 for f in findings), str([(f.line, f.rule) for f in findings])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def code_block(self) -> tuple:
        # The code scanner sees the flag on its own line.
        root = Path(tempfile.mkdtemp(prefix="pwb-code-"))
        try:
            (root / "README.md").write_text(CODE_BAD, encoding="utf-8")
            findings = lint.BoundaryLinter(root).lint_markdown(root / "README.md")
            return any(f.rule == "PB201" and f.line == 4 for f in findings), str([(f.line, f.rule) for f in findings])
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def frontmatter(self) -> tuple:
        # metadata lines such as cost: "No Vidbyte charge" are not prose.
        pairs = lint.MarkdownSentences("---\nname: x\ncost: \"No Vidbyte charge + preview\"\n---\n\nBody.\n").prose()
        return [s for _, s in pairs] == ["Body."], str(pairs)

    def bold_split(self) -> tuple:
        # "**Tie each routine.** It is free and local:" is two sentences, so the routine is not called free.
        pairs = lint.MarkdownSentences("1. **Tie each routine to its runs.** It is free and local:\n").prose()
        return len(pairs) == 2, str(pairs)


class StructureSuite:
    """PB3xx rules against a real skill copied into a temporary tree and broken one way at a time."""

    def __init__(self, results: Results) -> None:
        # Keeps the results collector and the canonical rulebook.
        self.results = results
        self.canonical = (ROOT / lint.CANONICAL_REL).read_text(encoding="utf-8").replace("\r\n", "\n")

    def run(self) -> None:
        # One fixture per structure rule, plus the clean baseline.
        self.results.run("Edge Case", "the unbroken fixture skill passes every structure rule", lambda: self.expect(lambda t: t, set()))
        cases = [
            ("Hidden Assumption", "a missing section is PB301", "PB301", self.drop_section),
            ("Hidden Assumption", "a duplicated section is PB301", "PB301", lambda t: t.replace("## Run\n", self.section(t) + "\n## Run\n", 1)),
            ("Hidden Assumption", "a section after Run is PB302", "PB302", self.move_section_after_run),
            ("Hidden Assumption", "a missing anchor sentence is PB303", "PB303", lambda t: t.replace("answer truthfully", "answer", 1)),
            ("Hidden Assumption", "a missing guardrail is PB304", "PB304", lambda t: t.replace("(Paid work boundary).", "(boundary).", 1)),
            ("Hidden Assumption", "a missing two-answers sentence is PB305", "PB305", lambda t: t.replace("The consent question has two answers. On a no, stop as Paid work boundary says", "On a no, stop", 1)),
            ("Silent Failure", "a consent question with 'or should I' is PB306", "PB306", lambda t: t.replace("> tokens, and it changes no files. Run it?", "> tokens, and it changes no files. Run it, or should I list them myself?", 1)),
            ("Silent Failure", "a Done-when that accepts a free result is PB307", "PB307", lambda t: t.replace("estimated cost have been reported;", "estimated cost have been reported (or the user chose the local list);", 1)),
            ("Edge Case", "a description over 1024 characters is PB308", "PB308", lambda t: t.replace("Use when the user", "Use when " + "very " * 200 + "the user", 1)),
            ("Silent Failure", "a description without the run sentence is PB309", "PB309", lambda t: t.replace(" The result comes only from that paid run.", "", 1)),
        ]
        for category, name, rule, mutate in cases:
            self.results.run(category, name, lambda mutate=mutate, rule=rule: self.expect(mutate, {rule}))
        self.results.run("Hidden Assumption", "a missing reference copy is PB310", lambda: self.expect(lambda t: t, {"PB310"}, reference=None))
        self.results.run("Hidden Failure", "a reference copy one byte off is PB310 and loses its exemption", self.one_byte_off)

    def tree(self, text: str, reference) -> Path:
        # Builds a temporary repository with the canonical rulebook and one skill.
        root = Path(tempfile.mkdtemp(prefix="pwb-struct-"))
        canonical = root / lint.CANONICAL_REL
        canonical.parent.mkdir(parents=True)
        canonical.write_text(self.canonical, encoding="utf-8")
        skill = root / "skills" / FIXTURE_SKILL
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text(text, encoding="utf-8")
        if reference is not None:
            (skill / "references" / lint.REFERENCE_NAME).write_text(reference, encoding="utf-8")
        return root

    def expect(self, mutate, rules: set, reference: str = "canonical") -> tuple:
        # Applies mutate to the real skill and checks the structure findings are exactly rules.
        original = (ROOT / "skills" / FIXTURE_SKILL / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        text = mutate(original)
        if rules and text == original and reference == "canonical":
            return False, "mutation did not change the file"
        root = self.tree(text, self.canonical if reference == "canonical" else reference)
        try:
            findings = lint.SkillStructure(root / "skills" / FIXTURE_SKILL / "SKILL.md", "SKILL.md", self.canonical).check()
            got = {f.rule for f in findings}
            return got == rules, f"expected {sorted(rules)}, got {sorted(got)}: {[f.detail for f in findings][:3]}"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def one_byte_off(self) -> tuple:
        # A copy with one changed byte is a structure finding and is prose-linted like any other file.
        altered = self.canonical.replace("Never.", "Never!", 1)
        root = self.tree((ROOT / "skills" / FIXTURE_SKILL / "SKILL.md").read_text(encoding="utf-8"), altered)
        try:
            findings = lint.BoundaryLinter(root).run()
            copy = f"skills/{FIXTURE_SKILL}/references/{lint.REFERENCE_NAME}"
            structure = any(f.rule == "PB310" for f in findings)
            prose = any(f.path == copy and f.rule.startswith("PB1") for f in findings)
            return structure and prose, str({(f.path, f.rule) for f in findings})
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @staticmethod
    def section(text: str) -> str:
        # Returns the boundary section, heading included.
        start = text.index("## Paid work boundary\n")
        return text[start:text.index("## Run\n", start)]

    def drop_section(self, text: str) -> str:
        # Removes the boundary section.
        return text.replace(self.section(text), "", 1)

    def move_section_after_run(self, text: str) -> str:
        # Moves the boundary section to just after the Run section.
        section = self.section(text)
        text = text.replace(section, "", 1)
        return text.replace("## Read the result\n", section + "## Read the result\n", 1)


class TreeSuite:
    """The real tree, origin/main, the sync script, and the scripts scanner."""

    KNOWN_MAIN_LEAKS = [
        ("skills/find-unfinished-agent-work/SKILL.md", 89),
        ("skills/find-unfinished-agent-work/SKILL.md", 280),
        ("skills/find-unfinished-agent-work/SKILL.md", 351),
        ("skills/find-unfinished-agent-work/SKILL.md", 355),
        ("skills/find-unfinished-agent-work/SKILL.md", 478),
        ("skills/find-unfinished-agent-work/SKILL.md", 572),
        ("skills/next-actions-from-notion/SKILL.md", 389),
        ("skills/which-pr-to-review-first/SKILL.md", 183),
        ("skills/which-pr-to-review-first/SKILL.md", 330),
        ("skills/find-unfinished-agent-work/scripts/unfinished_work.py", 236),
        ("skills/find-unfinished-agent-work/scripts/unfinished/report.py", 28),
    ]

    def __init__(self, results: Results) -> None:
        # Keeps the results collector.
        self.results = results

    def run(self) -> None:
        # Runs each whole-tree test.
        self.results.run("Silent Failure", "this tree lints clean", self.tree_clean)
        self.results.run("Hidden Failure", "every known leak on origin/main is caught", self.main_leaks)
        self.results.run("Silent Failure", "the sync script leaves this tree unchanged", self.sync_clean)
        self.results.run("Silent Failure", "the scripts scanner flags --local-only and local_only", self.scripts_scanner)
        self.results.run("Hidden Assumption", "the sync table names exactly the skill folders", self.sync_table)

    def tree_clean(self) -> tuple:
        # No finding anywhere in the checkout.
        findings = lint.BoundaryLinter(ROOT).run()
        return not findings, str([(f.path, f.line, f.rule) for f in findings][:10])

    def main_leaks(self) -> tuple:
        # Exports origin/main and checks each known leak has a finding within two lines.
        temp = Path(tempfile.mkdtemp(prefix="pwb-main-"))
        try:
            archive = temp / "main.tar"
            done = subprocess.run(["git", "archive", "--format=tar", "-o", str(archive), "origin/main"], cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if done.returncode != 0:
                return False, done.stderr.decode("utf-8", "replace")
            with tarfile.open(str(archive)) as tar:
                tar.extractall(str(temp / "tree"))
            findings = lint.BoundaryLinter(temp / "tree").run()
            leaks = [f for f in findings if f.rule.startswith(("PB1", "PB2"))]
            missed = [(path, line) for path, line in self.KNOWN_MAIN_LEAKS if not any(f.path == path and abs(f.line - line) <= 2 for f in leaks)]
            return not missed, f"missed {missed}; found {sorted({(f.path, f.line) for f in leaks})[:40]}"
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def sync_clean(self) -> tuple:
        # The sync in --check mode reports nothing out of sync.
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync-paid-work-boundary.py"), "--check"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return done.returncode == 0, done.stdout.decode("utf-8", "replace")[:500]

    def scripts_scanner(self) -> tuple:
        # An argparse flag and an identifier are both found.
        scanner = lint.ScriptScanner(lint.PhraseCatalog())
        findings = scanner.scan_lines("x.py", [(1, 'report.add_argument("--local-only", action="store_true")'), (2, "    if self.local_only:"), (3, 'parser.add_argument("--dry-run")')])
        rules = sorted((f.line, f.rule) for f in findings)
        return rules == [(1, "PB201"), (2, "PB202")], str(rules)

    def sync_table(self) -> tuple:
        # Every skill folder has a row and every row has a folder.
        folders = {p.parent.name for p in (ROOT / "skills").glob("*/SKILL.md")}
        return folders == set(sync.SKILLS), f"diff {folders ^ set(sync.SKILLS)}"


def main() -> int:
    # Runs every suite and prints the summary.
    results = Results()
    CorpusSuite(results).run()
    MarkdownSuite(results).run()
    StructureSuite(results).run()
    TreeSuite(results).run()
    return results.summary()


if __name__ == "__main__":
    sys.exit(main())
