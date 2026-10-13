# Design Doc: automate-recurring-chores

**Status:** Draft
**Author:** Claude
**Created:** 2026-10-13
**Last Updated:** 2026-10-13

---

## 1. Overview

`automate-recurring-chores` is a public agent skill that finds the tasks a person redoes by hand,
in a fresh coding-agent session, on a cadence (a monthly budget cleanup, invoice generation,
expense categorization, quarterly tax questions, a weekly report, a dependency bump, release notes,
log triage) and turns each one into a scheduled routine. A bundled, standard-library Python script
reads the user's most recent sessions (at most 500) from Claude Code, Codex, OpenCode, Gemini CLI,
and Qwen Code through the shared `sessionkit` package. It groups session openers by wording, folder,
files, data sources, and commands, keeps groups with at least three runs in distinct sessions,
measures the intervals between runs, and names the cadence the dates show (daily, weekdays, weekly,
every two weeks, monthly with "first business day" and "end of month" patterns, or quarterly), with
its regularity and the next expected date. It records what each run needed: files and folders read,
data sources, commands, credentials by name only, and outputs. It writes a redacted digest with
tagged evidence (`[C#]` chores, `[S#]` sessions, `[P#]` prompts, and the run dates) and sends it to
the Vidbyte Suggestion Agent in one usage-billed `vidbyte-cli agents suggest run`, together with a
focus file passed with `--instructions` that tells the generator and every critic to return one
scheduled routine per chore. A local `report` step maps each routine back to its runs and flags
ungrounded ones. On the user's explicit yes for one routine, a local `routine` step writes the
routine files (a prompt, a small runner, and a schedule snippet) into a folder the user picks and
prints the exact command that registers the job; it never registers anything itself. The skill is
listed on `/agents/skills` as a `coming-soon` entry, like every other skill that needs vidbyte-cli
0.3.x.

---

## 2. Goals & Non-Goals

### Goals
- Add `skills/automate-recurring-chores/` to cerredz/vidbyte-cli, meeting the `write-agent-skill`
  contract: frontmatter, fixed section order, consent, exact commands, shared references.
- Read sessions only through a byte-for-byte copy of the shared `sessionkit` package (Claude Code,
  Codex, OpenCode, Gemini CLI, Qwen Code; the most recent 500 interactive sessions by default).
- Detect recurring chores locally and for free: cluster openers and early prompts by stemmed-term
  similarity weighted by rarity, boosted by the same folder and the same files, data sources, and
  commands from `Turn.actions`; require at least 3 runs in distinct sessions; classify the cadence
  with a regularity measure (median interval, spread, share of on-schedule intervals) and the next
  expected date.
- Record each chore's inputs (files and folders read, data sources and exports, commands,
  credentials named only), outputs (files written), and external actions (sending, paying,
  posting, filing) that must stay human steps.
- Build a redacted digest with tagged evidence and send it with an explicit focus file
  (`references/chore-routines.md`, passed with `--instructions`) so the generator and every critic
  return ONE scheduled routine per chore, each with the cadence and its dates, inputs and their
  sources, outputs and their destinations, a ready-to-use routine prompt, scheduling options for the
  user's platform, the steps that still need a human, and a dry-run-first plan.
- Never let a routine spend money, send messages, or file anything externally without a human
  confirmation step: say it in the focus file, pass it as `--forbidden`, check it in `report`, and
  bake it into the standing rules of every routine file the skill writes.
- A local `report` step maps each routine to its chore, run dates, session tags, and resume
  commands from the local index, and flags ungrounded routines, cadence mismatches, missing
  elements, and external actions with no human step.
- A local `routine` step that, after the user's explicit yes for one routine, writes the routine
  files into a folder the user picks and prints the exact register, test, and remove commands.
- Follow the usage-billed suggestion flow every sibling uses: free dry run, consent, one paid run
  with `--max-total-tokens 400000`, a `paid-run.txt` guard, and a cost estimate.
- List the skill on `/agents/skills` with a validated `defineAgentSkill` entry placed right after
  `TOO_MANY_MEETINGS` in `ENTRIES`.

### Non-Goals
- Registering, enabling, or running a scheduled job. The skill shows the exact command; the user
  runs it.
- Running a routine (headless agent) during the skill. The first dry run of a routine is the user's
  command, printed by `routine`.
- Generating GitHub Actions workflow files or harness cloud schedules with the `routine` writer.
  The focus file teaches the Suggestion Agent the verified facts for those options so ideas can
  name them, but the writer covers the local schedulers (cron, launchd, Windows Task Scheduler),
  where a routine can read the same local files the manual runs read.
- Finding instructions repeated inside interactive work and turning them into rules, hooks, and
  skills. That is `stop-repeating-yourself`. This skill finds whole tasks that recur on a cadence,
  each started fresh, and turns each into a scheduled routine.
- Token usage (`cut-my-agent-token-usage`), next steps for recent work
  (`pick-up-where-my-sessions-left-off`), prompting critique (`improve-my-prompting`), corrections
  (`turn-corrections-into-skills`), and unfinished work (`find-unfinished-agent-work`).
- Chores repeated inside one long session, and tasks that repeat with no steady cadence. Irregular
  repeats are listed locally for the user, never sent, and never given a schedule.
- Non-English cadence words. Clustering is language-neutral (terms), but the period-word stripping
  (month names, "weekly") is English.

---

## 3. Background & Context

- **Why now.** The user asked for a skill that detects chores repeated on a cadence ("monthly budget
  cleanup, invoice generation, expense categorization, or quarterly tax questions. Each is being
  done by hand in a fresh session every time. The suggestion is a scheduled routine with the inputs
  it needs. It names the cadence the sessions already show."). It is one of four session-reading
  skills built in parallel on the shared `sessionkit` package (section 0 of the common brief).
- **What it solves.** Coding agents are increasingly used for personal and business chores, not
  only code. A chore done by hand in a fresh session each time costs the same setup every run: find
  the new export, re-explain the steps, re-add the same follow-up requirements, check the same
  output. Nobody notices the cadence from inside one session, and the evidence is spread across
  hundreds of transcripts in incompatible formats. Outside evidence that recurring manual work is a
  broad, costly problem is cited in section 11.
- **Current state.** Three merged skills read local transcripts and drive `agents suggest run` with
  a redacted digest (`pick-up-where-my-sessions-left-off`, `cut-my-agent-token-usage`,
  `stop-repeating-yourself`), Claude Code and Codex only. `stop-repeating-yourself` is the nearest
  neighbour: it clusters repeated sentences, requests, and approvals inside interactive work and
  proposes durable rules, hooks, skills, and allowlist entries, and it explicitly treats "the same
  request in new words" as something to turn into a skill or slash command. It does not look at
  when a task recurs, does not treat the session as the unit of a run, and proposes no schedule.
  This skill's unit is a whole session that starts a task fresh, its evidence is the run dates, and
  its output is a scheduled routine.
- **Constraints and dependencies.**
  - The CLI surface comes from `contracts/cli-commands.json` on cerredz/vidbyte-cli-internal `main`
    (generated at `f1321179`; `main` is at `abd31b4`, 0.3.2). `agents suggest run` has `--goal`,
    `--category`, `--count`, `--horizon`, `--outcomes`, `--context`, `--capability`, `--risks`,
    `--forbidden`, `--constraint`, `--avoid`, `--success`, `--files`, `--instructions`, `--project`,
    `--max-total-tokens`, and `--dry-run`. PyPI still has only 0.2.0, so the skill declares
    `min-cli-version: "0.3.0"` like every suggestion sibling and is listed `coming-soon`.
  - `--instructions`: at most 8 files and 20,000 bytes in total, appended after the fixed system
    prompt of the generator, every critic, and every extra-compute generator; paths and contents are
    never echoed (`commands/agents/prompts/instructions.md`).
  - Exact-repeat suppression: `selection.suppress_rejected` drops any idea whose title, summary,
    first action, or suggested actions contain, case-insensitively, the full text of a
    `completed`, `in_progress`, `avoid`, `mistakes`, or `forbidden` value. So every `forbidden`
    value starts with `[all]`, which no idea repeats by accident.
  - Idea fields have hard limits (`types/suggestions.py`): `summary` 2,048 characters,
    `first_action`/`why_now`/`expected_benefit`/`completion_criteria` 1,024, 1-12
    `suggested_actions`, 8-10 `considerations`, up to 12 `decision_points`. The focus file assigns
    each required routine element to a field that fits.
  - Known traps: `--horizon` other than `any` filters after billing; Python 3.8 has no
    `Path.write_text(newline=)`; tests must never reach the real `vidbyte-cli` (the OS keyring holds
    a live key).
  - The sessions window is the newest 500 sessions. For a heavy user that can be a few days, too
    short to show a monthly cadence. `gather` reports the span and which cadences it can show, and
    the skill suggests `--project-dir` (500 sessions of one folder reach further back).
  - Shared references must be byte-identical to the canonical copies: `setup.md` blob `8fb4e3cf`,
    `troubleshooting.md` blob `cb9656bc`.

---

## 4. Requirements

### Functional Requirements
1. `recurring_chores.py gather` reads sessions through `sessionkit` (`CollectOptions` flags:
   `--max-sessions` 1-500 default 500, `--days`, `--since`, `--project-dir`, `--claude-dir`,
   `--codex-home`, `--opencode-dir`, `--gemini-dir`, `--qwen-dir`, `--no-*`, `--exclude-session`,
   `--include-current`) plus its own `--min-runs` (3-10, default 3), `--max-chores` (1-10, default
   10), `--max-chars` (20,000-100,000, default 60,000), and `--out`.
2. For each session the detector picks the opener: the first of the first three prompts with at
   least 3 content terms, else the first prompt. Sessions whose opener is a continuation ("continue",
   "pick up where", "resume", "keep going", "where we left off", "next step") or a bare go-ahead are
   not chore candidates, because they continue earlier work instead of starting a run fresh.
3. Terms: lower-cased words, stop words removed, light stemming (`-ing`, `-ies`, `-ed`, `-s`), with
   period values removed (month names and abbreviations, years, dates, quarter labels, day numbers)
   so "the September invoices" and "the October invoices" match. The opener's first 600 characters
   give the terms; when it has fewer than 5 terms, the next prompt's terms are added.
4. Resources from `Turn.actions` of every turn: files read and written (paths made relative to the
   session folder, lower-cased, with dates, month names, quarter labels, and numbers replaced by
   placeholders, so `statements/2026-09.csv` and `statements/2026-10.csv` match), their folders,
   command signatures (program plus subcommand or script, after stripping `cd ... &&` and
   environment assignments; generic commands such as `ls`, `cat`, `grep` are dropped), web domains,
   and MCP server/tool names. Resources present in more than 40% of all sessions are ignored.
5. Similarity: `text + 0.15 * same_folder + 0.35 * resources`, where `text` and `resources` are
   Jaccard indexes weighted by inverse document frequency. Two sessions are linked only when
   `text >= 0.2`. Clusters are built by average-linkage agglomeration over linked pairs with a
   threshold of 0.5, so a cluster cannot chain unrelated sessions.
6. Runs: a cluster's sessions on the same local day are one run; when the median gap between runs
   is at least 5 days, runs closer than a quarter of that median are merged (a chore finished the
   next day is one run). A chore needs at least `--min-runs` runs (3 by default) in distinct
   sessions; daily and weekday chores need at least 5 runs over at least 7 days.
7. Cadence: from the median gap, classify daily, weekdays, weekly (with the usual weekday),
   biweekly, monthly (patterns `first_business_day`, `last_business_day`, `end_of_month`, or
   `day_of_month` with the usual day), or quarterly (month within the quarter and usual day).
   Regularity is the share of intervals on schedule (one skipped period counts half). Below 0.6 the
   cluster is `irregular`: listed locally, never sent. Each chore reports the median interval, the
   spread (median absolute deviation, in days), the usual start time, and the next expected date
   (marked overdue when it is already past).
8. Each chore records, with the number of runs each appeared in: files and folders read, data
   sources (web domains, MCP servers, data-file types such as `.csv`, `.xlsx`, `.pdf`, `.ofx`, data
   CLIs such as `gh`, `stripe`, `aws`, `psql`), commands, credentials named (environment-variable
   names such as `STRIPE_API_KEY`, `.env`, keychain or password-manager mentions), files written,
   external actions (sending email or messages, posting, uploading, paying, filing, `git push`,
   `gh pr create`, MCP tools whose names send, post, or pay), recurring follow-up prompts, session
   time per run, and the last run's closing agent text. Credential values are never captured: only
   the matched name is kept.
9. `gather` writes into the output folder: `digest.md` (redacted, at most `--max-chars`),
   `chores.json` (the local index, never sent), `inputs.json` (typed inputs pre-filled from the
   numbers), and `chore-routines.md` (a byte copy of the focus file, refused above 20,000 bytes). It
   prints a JSON summary with the window, span, detectable cadences, chore table, irregular repeats,
   redaction counts, and folders not read. It exits 3 with `reason: no_sessions` or `no_chores`.
10. `recurring_chores.py suggest --dry-run|--paid` validates `inputs.json` through
    `sessionkit.runner.InputsValidator` (allowed keys, counts, lengths, tags known to `chores.json`,
    redaction, at least one `outcomes` value) and runs, from inside the output folder,
    `vidbyte-cli --json --no-input agents suggest run --goal ... --category delegation --category
    leverage --category risk_prevention --count <chores, 2-10> --horizon any <typed flags> --files
    digest.md --instructions chore-routines.md [--project automate-recurring-chores]
    --max-total-tokens 400000 [--dry-run]`. `--paid` refuses an output folder that already has
    `paid-run.txt` or `result.json`, writes `paid-run.txt` first, and reports usage and
    `estimated_cost_usd`.
11. `recurring_chores.py report` maps each idea in `result.json` to the chores it cites (`[C#]`
    directly, or through `[S#]`/`[P#]` tags of a chore's runs), with each chore's cadence, run
    dates, session tags, harness, and resume command, and flags: `grounded: false` (no valid tag),
    `cadence_mismatch` (the idea names another cadence), `missing` elements (routine prompt,
    schedule, human step, dry run), `unguarded_external_action` (the chore has an external action
    and the idea never names a confirmation), and `duplicate_chore`. It lists chores no idea covers,
    writes `report.md`, and prints JSON.
12. `recurring_chores.py routine --out DIR --idea idea-NNN --dest DIR --harness
    claude-code|codex --scheduler cron|launchd|task-scheduler|none [--chore C#] [--time HH:MM]
    [--force]` writes into `--dest` (which must be empty or absent unless `--force`):
    `routine-prompt.md` (standing safety rules plus the idea's routine prompt), `routine.json`
    (cadence, harness and its absolute path, working folder, mode `dry-run`, allowed commands,
    time), `run_routine.py` (a byte copy of the shipped runner), `SCHEDULE.md` (the exact register,
    test, and remove commands), and `<label>.plist` for launchd. It prints JSON with the files and
    commands. It never runs a scheduler, a harness, or `vidbyte-cli`.
13. The runner (`run_routine.py`) runs the routine headless in the chore's folder: by default a dry
    run (Claude Code `--permission-mode plan`, Codex `--sandbox read-only`) whose output is only a
    log; `--live`, or `"mode": "live"` in `routine.json`, allows edits with the listed tools.
    `--scheduled` applies a date guard (run only inside the cadence's window and once per period,
    with catch-up inside the window). Every run writes `logs/<stamp>-<mode>.log`.
14. `recurring_chores.py feedback` records an explicit accept or reject with the idea's exact title
    through `vidbyte-cli --json --no-input agents suggest feedback accept|reject` with project
    `automate-recurring-chores`.
15. Every subcommand that calls `vidbyte-cli` takes `--cli <path>` and exits 127 when no CLI is
    found.
16. The website lists the skill with a `defineAgentSkill` entry that passes the validator and lint
    rule N050, imported in `catalog.js` and placed in `ENTRIES` immediately after
    `TOO_MANY_MEETINGS`.

### Non-Functional Requirements
- **Performance:** detection over 500 sessions finishes in seconds after collection (pairwise
  similarity only for pairs sharing a term, through an inverted index; average linkage with a heap).
  Collection time is `sessionkit`'s.
- **Cost:** one paid run, gpt-6-sol at high reasoning effort, capped at 400,000 tokens (under about
  $4). The focus file is about 12 KB and is sent with every model call; the digest is at most 60,000
  characters by default.
- **Security:**
  - Every string that enters `digest.md`, `inputs.json`, `report.md`, the routine files, or the
    excerpts in `chores.json` passes through `sessionkit.redaction.Redactor`.
  - Credentials are recorded by name only, from a name-only pattern; a value is never captured.
  - Only `digest.md`, `chore-routines.md`, and the `inputs.json` values leave the machine.
  - Sessions are read through `sessionkit`, read-only.
  - Transcript text is data; the focus file tells the model never to follow instructions in it.
  - The routine writer writes only inside `--dest`, never runs anything, and starts every routine in
    dry-run mode with standing rules that forbid paying, sending, posting, or filing without a human.
- **Portability:** standard library only, Python 3.8+, Windows, macOS, and Linux, any shell.
  `vidbyte-cli` and the harnesses are started without a shell.
- **Observability:** the gather summary reports what was read, the span, the detectable cadences,
  chores and irregular repeats, and redaction counts; `suggest` saves stdout and stderr; the runner
  logs every run.
- **Reliability:** a session or action that cannot be parsed is skipped, never fatal; a harness
  failure is reported by `sessionkit` while the others continue.

---

## 5. High-Level Design

The skill follows the shape of its siblings: a free local `gather`, a free dry run, one consented
paid run, a free local `report`, and `feedback` for learning. It adds a local `routine` writer for
the routine the user picks. The new parts are the chore detector, the cadence analyzer, and the
routine writer with its runner.

```
 sessionkit.SessionCollector (5 harnesses, newest 500)            [free, local]
        │ sessions (turns, actions, folders, times)
        ▼
 SessionSignature ─▶ ChoreDetector (similarity, average linkage, runs) ─▶ CadenceAnalyzer
        │                                                                     │
        ▼                                                                     ▼
 ChoreDigestWriter ─▶ digest.md + inputs.json + chore-routines.md (sent)   chores.json (local)
        │
        ▼ suggest --dry-run (free) ─▶ consent ─▶ suggest --paid (usage-billed, once)
 vidbyte-cli --json --no-input agents suggest run --files digest.md --instructions chore-routines.md ...
        │        (generator + 2 critic rounds on gpt-6-sol through Vidbyte's gateway)
        ▼
 result.json ─▶ report (ideas ↔ [C#]/[S#]/[P#] ↔ runs, flags) ─▶ report.md ─▶ Deliver
        │
        ▼ on an explicit yes for one routine
 routine ─▶ <dest>/routine-prompt.md, routine.json, run_routine.py, SCHEDULE.md (+ .plist)
        │       prints the register / dry-run / remove commands; registers nothing
        ▼
 the user runs: python run_routine.py (dry run) ... then registers the schedule themselves
```

Key decisions:
- **The focus goes through `--instructions`, the evidence through `--files`.** The focus file
  shapes both the generator and every critic, so every routine has the same required elements and
  the critic grades them. The digest is citable evidence (`ctx-NNN`), so ideas cite the chores' own
  runs, not the focus file.
- **Chores are measured locally; the model writes the routines.** Clustering, intervals, cadence,
  regularity, next date, and inputs are counted by code, so the cadence an idea names is the one
  the dates show. The model writes what code cannot: the routine prompt, the human checkpoints, and
  the platform-specific plan.
- **The schedule snippet in the written files is computed, not copied from the model.** The
  `routine` writer derives the cron line, the launchd calendar, and the Task Scheduler trigger from
  the detected cadence through the same window logic the runner uses for its date guard, so a
  "first business day" routine cannot fire on the wrong day. The model's schedule text is advice;
  the written files are exact.
- **A runner with a date guard instead of a bare scheduler line.** Cron, launchd, and Task
  Scheduler cannot express "first business day", "last business day", or "every two weeks"
  directly, and cron ORs day-of-month with day-of-week. The runner checks the window, runs once per
  period with catch-up inside the window, starts in dry-run mode, resolves the harness path at write
  time (schedulers run with a minimal `PATH`), and logs every run.
- **Categories:** `delegation` (hand the recurring execution to an unattended agent while the
  user keeps the decisions), `leverage` (one routine removes a cost paid on every run), and
  `risk_prevention` (guards for routines that touch money, messages, or filings). The focus file
  says categories are labels, not quotas: one idea per chore.
- **Packages.** `scripts/sessionkit/` is the shared copy (never edited here). `scripts/chores/`
  holds this skill's logic, one concern per module. `scripts/recurring_chores.py` adds its folder to
  `sys.path` and dispatches. The runner `chores/run_routine.py` is both a module (the detector and
  writer import its window logic, so the guard and the schedule share one implementation) and a
  standalone script copied into the routine folder; it imports nothing outside the standard library.

---

## 6. Detailed Design

### 6.1 Entry point

**File(s):** `skills/automate-recurring-chores/scripts/recurring_chores.py`
**Type:** New file

#### What it does
Parses arguments and dispatches to `GatherCommand`, `SuggestCommand`, `ReportCommand`,
`RoutineCommand`, and `FeedbackCommand`. On Windows it reconfigures stdout and stderr with
`errors="backslashreplace"`. Every command prints one JSON document to stdout.

#### Interface / API
```text
recurring_chores.py gather   [sessionkit CollectOptions flags] [--min-runs N<=10] [--max-chores N<=10]
                             [--max-chars N<=100000] [--out DIR]
recurring_chores.py suggest  --out DIR (--dry-run | --paid) [--cli PATH]
recurring_chores.py report   --out DIR
recurring_chores.py routine  --out DIR --idea idea-NNN --dest DIR --harness {claude-code,codex}
                             --scheduler {cron,launchd,task-scheduler,none} [--chore C#] [--time HH:MM] [--force]
recurring_chores.py feedback --out DIR --idea idea-NNN (--accept | --reject) [--reason TEXT] [--project KEY] [--cli PATH]
```
Exit codes: 0 ok; 2 usage or validation; 3 nothing to send (`no_sessions` or `no_chores`, gather);
127 no CLI; any other code is vidbyte-cli's.

### 6.2 Session signatures

**File(s):** `scripts/chores/signatures.py`
**Type:** New file

#### What it does
Turns one `Session` into the features the detector compares: opener turn, terms, period mentions,
resources, folder key, and whether the opener continues earlier work.

#### Interface / API
```python
class TermExtractor:
    def terms(self, text: str) -> frozenset: ...            # stemmed content terms, period values removed
    def period_mentions(self, text: str) -> list: ...       # ["month", "quarter", "year", "date", "cadence"]
class PathNormalizer:
    def normalize(self, path: str, cwd: str) -> str: ...    # relative, lower-case, <date>/<month>/<quarter>/<n>
class CommandSignature:
    def of(self, command: str) -> CommandInfo | None: ...   # signature, program, file args, external action
class ActionParser:
    def parse(self, action: str, cwd: str) -> ParsedAction: ...  # kind: read|write|command|web|mcp|search|agent|other
class SessionSignature:
    def __init__(self, session, extractor, parser) -> None: ...
    # opener (Turn), opener_index, terms, periods, resources (frozenset of "file:", "dir:", "cmd:", "url:", "mcp:"),
    # reads, writes, commands, sources, credentials, external (lists with evidence), project_key, continuation
```

#### Logic / Algorithm
1. Opener: the first of turns 0-2 with at least 3 terms, else turn 0. A session with no turns has no
   signature.
2. `continuation` is true when the opener's first 80 characters match the continuation or go-ahead
   phrases (FR 2).
3. Terms: the opener's first 600 characters; plus the next prompt's terms when fewer than 5.
4. Actions (`Turn.actions`, formats from `sessionkit.model.ActionText`):
   - `<Shell>: <command>` (label in `ActionText.SHELL_TOOLS`) → `CommandSignature.of`: strip
     `cd X &&`/`;` prefixes and `NAME=value` assignments, split on `|`, `&&`, `;`, take the program
     basename without extension. For package managers, VCS, interpreters, and cloud/data CLIs, add
     the subcommand or the script path (`git pull`, `npm outdated`, `python scripts/x.py`,
     `gh pr list`). Generic programs (`ls`, `cat`, `grep`, `rg`, `find`, `head`, `tail`, `sed`,
     `awk`, `wc`, `echo`, `pwd`, `cd`, `dir`, `type`, `Get-ChildItem`, `Select-String`) give no
     command signature, but their file arguments with a data extension count as reads.
   - `Edit <path>` → write; `Read <path>` → read; `WebFetch <url>` → web domain; a label starting
     with `mcp__` → `mcp:<server>:<tool>`; search tools and `Agent:` add nothing.
5. External actions: command signatures `git push`, `gh pr create|merge`, `gh release create`,
   `gh issue create`, `npm publish`, `twine upload`, `curl`/`wget` with `-X POST|PUT|DELETE` or
   `--data`, `sendmail`, `mail`; MCP tools whose name contains `send`, `post`, `reply`, `publish`,
   `upload`, `submit`, `pay`, `transfer`, `create_invoice`, `create_payment`; prompt phrases
   ("send it to", "email the", "post it", "submit", "file the", "pay the", "transfer"). Each keeps
   the prompt index or action that showed it.
6. Credentials: names matching `\b[A-Z][A-Z0-9_]*(?:API_KEY|KEY|TOKEN|SECRET|PASSWORD|PASS|CREDENTIALS?)\b`
   in prompts and commands, plus `.env`, `credentials.json`, `*.pem`, `keychain`, `1password`/`op
   read`, `bitwarden`, `gh auth`, `aws configure`, `gcloud auth` mentions. Only the name is stored.

#### Edge Cases & Error Handling
- An action string with an unexpected shape is `other` and adds nothing.
- Paths outside the session folder keep their normalized absolute form (the redactor later replaces
  the home folder).
- A Windows drive path and a POSIX path normalize to the same lower-case, forward-slash form.

### 6.3 Chore detector

**File(s):** `scripts/chores/detector.py`
**Type:** New file

#### What it does
Builds signatures, computes weighted similarities, clusters with average linkage, groups runs,
asks the cadence analyzer for each cluster, aggregates inputs and outputs, and ranks chores.

#### Interface / API
```python
class Occurrence:   # one run: sessions (list of SessionSignature), day (date), start (datetime)
class Chore:        # signatures, occurrences, cadence (Cadence), inputs/outputs aggregates, label, terms, score
class ChoreDetector:
    def __init__(self, min_runs: int = 3, today=None) -> None: ...
    def detect(self, sessions: list) -> DetectionResult: ...   # chores (ranked), irregular, span, stats
```

#### Logic / Algorithm
1. Signatures for every session with a non-continuation opener and a timestamp.
2. IDF for terms and resources over those signatures; resources in more than 40% of signatures are
   dropped.
3. Candidate pairs through an inverted index on terms; score pairs per FR 5; keep edges with
   `text >= 0.2` and score > 0.
4. Average linkage: every signature starts alone; a heap orders cluster pairs by mean pairwise score
   (missing edges count 0); merge while the best mean is at least 0.5; stale heap entries are
   skipped.
5. For each cluster of at least `min_runs` signatures, group runs (FR 6), skip clusters with fewer
   than `min_runs` runs, and call `CadenceAnalyzer.analyze`.
6. Aggregate per chore: each resource counted once per run; keep items seen in at least 2 runs or
   half the runs, top 8 per kind; external actions and credentials keep every distinct name with
   its run count and evidence tags.
7. Recurring follow-ups: the cluster's non-opener prompts from the first 6 turns, grouped by term
   Jaccard >= 0.4, kept when a group spans at least 2 runs.
8. Label: the opener with the highest mean similarity to the rest (redacted later, cut to 90
   characters). Key terms: the top 6 terms by in-cluster frequency × IDF, shown in their most common
   surface form.
9. Score: `runs × regularity × log(2 + median session minutes)`; chores sorted by score, then most
   recent run. Irregular clusters go to `irregular`.

#### Edge Cases & Error Handling
- Zero or one signature: no chores, no division by zero.
- All sessions on one day: one run per cluster, so no chore.
- A session with no timestamp is not a candidate.
- Ties are broken by first-run date and then session id, so results are deterministic.

### 6.4 Cadence analyzer and run windows

**File(s):** `scripts/chores/cadence.py` (analysis) and `scripts/chores/run_routine.py`
(`RunWindow`, shared with the runner)
**Type:** New files

#### What it does
`CadenceAnalyzer` classifies run dates into a `Cadence`. `RunWindow` answers, for a cadence and a
date: the period key, whether the date is inside the run window, the scheduler triggers, and the
next run date. The detector, the writer, and the runner all use `RunWindow`.

#### Interface / API
```python
class Cadence:      # kind, pattern, weekday, day, month_in_quarter, anchor, time, label,
                    # median_gap, spread, regularity, runs, intervals, next_expected, overdue
class CadenceAnalyzer:
    def analyze(self, days: list, starts: list, today) -> Cadence: ...
class RunWindow:    # in run_routine.py; config dict {kind, pattern, weekday, day, month_in_quarter, anchor}
    def __init__(self, cadence: dict) -> None: ...
    def period_key(self, day) -> str: ...
    def in_window(self, day) -> bool: ...
    def next_run(self, after) -> date: ...
    def triggers(self) -> dict: ...     # {"type": "daily"|"weekly"|"monthly", "weekdays": [...], "days": [...], "months": [...]}
```

#### Logic / Algorithm
1. Gaps between run days; median gap `m`.
2. Classification:
   - `m <= 1.5`: `weekdays` when every run is Monday to Friday and every gap is the next business
     day; else `daily`.
   - `5 <= m <= 9`: `weekly`, nominal 7, tolerance ±1; `weekday` is the most common weekday.
   - `11 <= m <= 17`: `biweekly`, nominal 14, tolerance ±2, anchor the first run.
   - `24 <= m <= 38`: `monthly`, judged on month steps (1 on schedule, 2 one skip). Pattern:
     `first_business_day` when every run is the first business day of its month (within 1 business
     day for at most one run); `last_business_day` when every run is the last business day;
     `end_of_month` when every run is within the last 3 calendar days; else `day_of_month` with the
     median day.
   - `70 <= m <= 110`: `quarterly`, month steps 2-4 on schedule; `month_in_quarter` and `day` from
     the medians.
   - Otherwise `irregular`.
3. Regularity: share of intervals on schedule, a one-period skip counting half; monthly patterns also
   require the day pattern to hold for the run to count. Below 0.6, or too few runs for the kind
   (FR 6), the cadence is `irregular`.
4. Spread: median absolute deviation of the gaps (days); monthly adds the day-of-month spread.
5. Usual time: the median local start time, rounded to 5 minutes.
6. Next expected: `RunWindow.next_run(last run)`; `overdue` when it is before today.
7. Windows (catch-up inside each):

   | Kind / pattern | Period | In window | Triggers |
   |---|---|---|---|
   | daily | day | every day | daily |
   | weekdays | day | Monday-Friday | weekly Mon-Fri |
   | weekly (weekday D) | ISO week | weekday D to D+2 (not past Sunday) | weekly D..D+2 |
   | biweekly (D, anchor) | 14-day block from the anchor's Monday | "on" week and weekday D to D+2 | weekly D..D+2 |
   | monthly first_business_day | month | business day 1-3 of the month | monthly days 1-7 |
   | monthly last_business_day | month | on or after the month's last business day | monthly days 24-31 |
   | monthly end_of_month | month | the last 3 calendar days | monthly days 26-31 |
   | monthly day_of_month (N) | month | day N (clamped to the month's length) to N+3 | monthly days N..N+3 (clamped) |
   | quarterly (M, N) | quarter | month M of the quarter, day N to N+6 | monthly days N..N+6 in the 4 months |

#### Edge Cases & Error Handling
- Fewer than 3 runs: `irregular`.
- February and 30-day months: windows clamp to the month's last day.
- A run that lands on a weekend for a business-day pattern makes the pattern fail, so the cadence
  falls back to `day_of_month` or `irregular` rather than claiming a pattern the dates deny.

### 6.5 Digest writer

**File(s):** `scripts/chores/digest.py`
**Type:** New file

#### What it does
`ChoreDigestWriter` assigns tags, renders `digest.md` within its budget, writes `chores.json`,
`inputs.json`, and the focus-file copy, and returns the gather summary.

#### Interface / API
```python
class ChoreDigestWriter:
    def __init__(self, out_dir: Path, redactor, max_chars: int, focus_path: Path, home: Path) -> None: ...
    def write(self, result, collect_summary: dict, environment: dict) -> dict: ...
```

#### Logic / Algorithm
1. Tags: chores `C1..` in rank order; sessions `S#` and prompts `P#` in order of first appearance.
2. Header (about 2,500 characters): generation date, sessions read by harness, window and span,
   platform, headless CLIs found on `PATH` (`claude`, `codex`, `opencode`, `gemini`, `qwen`),
   detectable cadences for this span (daily/weekdays need 7 days, weekly 14, biweekly 28, monthly
   60, quarterly 180), and how to read tags. One line on how chores are found and what is measured.
3. One block per chore, in rank order:
   `### [C1] <label> - <cadence label>, <n> runs, regularity <r>`; the project label (last folder
   name) and harnesses; the runs with dates and `[S#]` tags; intervals, median, spread, usual time,
   next expected date (and overdue); session time and prompts per run; up to 4 openers `[P#] [S#]
   <date>: "<text up to 300 chars>"`; recurring follow-ups with `[P#]` tags; inputs, data sources,
   commands, credentials named, outputs written, each with run counts; external actions with their
   tags and "needs a human"; the last run's closing agent text (up to 300 characters); and whether
   the folder is a git repository with a GitHub remote.
4. Budget: chores are rendered in rank order; when the budget runs short, openers, follow-ups, and
   closing text are trimmed from the lowest-ranked chore first; a chore that still does not fit is
   dropped and listed in the summary as `not_in_digest`.
5. `chores.json` (local): window, read summary, environment, every chore with real session ids,
   sources, working folders, resume commands (`claude --resume <id>` and `codex resume <id>` with
   `CLAUDE_CONFIG_DIR`/`CODEX_HOME` when the session came from a non-default folder; other harnesses
   give the session id and file), the redacted prompt texts by tag, inputs and outputs, and the
   irregular repeats.
6. `inputs.json`:
   - `goal` (at most 700 characters): the user's framing, the chore count, the harnesses, and the
     ask: one scheduled routine per chore on the cadence its runs show.
   - `outcomes`: one per chore, `[C1 S3 S7 S12] <label>: <cadence label>, <n> runs <first>-<last>,
     median <m> days ±<spread>, next expected <date>, about <min> min of session time per run.`
   - `context`: `[all]` platform and harnesses on `PATH`; `[all]` window and span; per chore with a
     GitHub remote, `[C#] the folder is a git repository with a GitHub remote`.
   - `capability`: what the routines can use (the local scheduler of the platform; headless
     `claude -p` / `codex exec` when found; GitHub Actions for chores with a GitHub remote).
   - `risks`: per chore with external actions or credentials, `[C# P#] ...` naming the action or the
     credential name.
   - `forbidden`: three fixed `[all]` values: no paying, sending, posting, uploading, or filing
     without a human confirming that exact step; no registering or enabling a scheduled job for the
     user; no credential value in a routine file, prompt, command line, or log.
   - `constraint`, `avoid`, `success`: empty (the user's own words only); `project`: `null`.
7. Focus copy: `references/chore-routines.md` copied byte for byte; refused (exit 2) above 20,000
   bytes.

#### Edge Cases & Error Handling
- `--max-chars` below 20,000: usage error (exit 2).
- A chore whose label, prompts, or closing text contain secrets: redacted before measuring.
- Tags in `inputs.json` always exist in `chores.json`.

### 6.6 Suggest wiring

**File(s):** `scripts/chores/suggest.py` (uses `sessionkit/runner.py`)
**Type:** New file

#### What it does
Declares this skill's flag kinds, caps, categories, count rule, and project key, and builds the
validator and argv through `sessionkit.runner` (`InputsValidator`, `SuggestArgv`, `PaidRunGuard`,
`CliInvoker`, `CostEstimator`, `RunReporter`).

#### Interface / API
```python
FLAG_KINDS = OrderedDict([("outcomes", ("--outcomes", 10)), ("context", ("--context", 6)),
    ("capability", ("--capability", 3)), ("risks", ("--risks", 8)), ("forbidden", ("--forbidden", 4)),
    ("success", ("--success", 3)), ("constraint", ("--constraint", 3)), ("avoid", ("--avoid", 3))])
TAGGED_KINDS = {"outcomes", "context", "risks", "forbidden"}
TAG_PATTERN = r"(?:C|S|P)\d+"
CATEGORIES = ("delegation", "leverage", "risk_prevention")
PROJECT_KEY = "automate-recurring-chores"
class SuggestPlan:
    def count(self, chores_in_digest: int) -> int: ...      # max(2, min(10, chores))
```

#### Logic / Algorithm
1. `InputsValidator(FLAG_KINDS, TAGGED_KINDS, TAG_PATTERN, known_tags, redactor, required_any=("outcomes",))`.
2. `SuggestArgv.build(exe, goal, CATEGORIES, count, FLAG_KINDS, values, ["digest.md"], ["chore-routines.md"], project, dry_run, horizon="any")`.
3. Argv under 24,000 characters; else exit 2 naming the longest values.
4. `--paid`: `PaidRunGuard(out_dir).problem()` refuses; `mark()` writes `paid-run.txt` first.

#### Edge Cases & Error Handling
- `--dry-run` and `--paid` together or neither: exit 2.
- `digest.md` or `chore-routines.md` missing: exit 2, run `gather` first.
- A non-zero CLI exit: the summary carries the CLI's `error` from stderr.

### 6.7 Routine report

**File(s):** `scripts/chores/report.py`
**Type:** New file

#### What it does
`RoutineReporter` reads `result.json` and `chores.json`, maps each idea to its chores and runs, and
flags problems.

#### Interface / API
```python
class RoutineReporter:
    def __init__(self, out_dir: Path, redactor) -> None: ...
    def build(self) -> dict: ...    # {"ideas": [...], "uncovered_chores": [...], "report": "report.md"}
```

#### Logic / Algorithm
1. Text per idea: title, summary, first action, why now, expected benefit, completion criteria,
   suggested actions, decision points, considerations, and each `handoff.evidence[].content`.
2. Tags `[C#]`, `[S#]`, `[P#]` (several per bracket allowed) resolved in `chores.json`; a session or
   prompt tag maps to the chore whose runs contain it.
3. Flags (FR 11): cadence words (`daily`, `every day`, `weekday`, `weekly`, `every week`,
   `biweekly`, `every two weeks`, `fortnight`, `monthly`, `every month`, `quarterly`, `every
   quarter`) mapped to kinds, mismatch when the idea names kinds and none is the chore's; elements by
   keyword (routine prompt, schedule, human step, dry run); external-action guard by confirmation
   words (`confirm`, `approve`, `approval`, `human`, `review before`, `ask`).
4. `report.md`: per idea in rank order, the title, its chores with cadence label, run dates and
   session tags with resume commands, the routine prompt excerpt, the flags; then uncovered chores.

#### Edge Cases & Error Handling
- `result.json` missing or not `suggestions.result`: exit 2.
- An idea with no tags: `grounded: false`, presented last with a caveat.

### 6.8 Routine writer

**File(s):** `scripts/chores/routine.py`, `scripts/chores/schedule.py`
**Type:** New files

#### What it does
`RoutineWriter` writes one routine folder for one idea and chore. `ScheduleBuilder` turns a
`RunWindow`'s triggers into the cron line, the launchd plist, and the Windows commands.

#### Interface / API
```python
class ScheduleBuilder:
    def __init__(self, window: RunWindow, time: str, python: str, runner: Path, label: str) -> None: ...
    def cron_line(self) -> str: ...
    def launchd_plist(self) -> str: ...
    def windows_commands(self) -> dict: ...   # {"powershell": "...", "schtasks": "...", "remove": "..."}
class RoutineWriter:
    def __init__(self, out_dir: Path, redactor) -> None: ...
    def write(self, idea_id: str, dest: Path, harness: str, scheduler: str, chore_tag: str | None, time: str | None, force: bool) -> dict: ...
```

#### Logic / Algorithm
1. Load the idea and chore; the chore is `--chore`, else the single chore the idea cites; several or
   none is a usage error naming the choices.
2. Routine prompt: the suggested action that starts with `ROUTINE PROMPT:` (the focus file asks for
   it), else the idea's summary and actions assembled with `prompt_source: "assembled"`. Redacted.
   Prefixed with fixed standing rules: dry run unless the runner says live; never pay, send, post,
   upload, or file anything outside this machine, and stop with a draft for the user instead; never
   print or write a credential value; stop and report when an expected input is missing; write a
   short run summary at the end.
3. `routine.json`: chore tag and label, cadence config, usual time (or `--time`), harness, the
   harness's absolute path from `shutil.which` (exit 2 if not found), working folder (the chore's
   most common folder), `mode: "dry-run"`, allowed commands (the chore's command signatures minus
   external and destructive ones), timeout 45 minutes, created date, source idea id and title.
4. `run_routine.py`: a byte copy of `chores/run_routine.py`.
5. Scheduler files and commands (with the Python that ran `routine`, `sys.executable`):
   - `cron`: the crontab line and `crontab -e` plus the one-line append command; remove by deleting
     the line.
   - `launchd`: `com.vidbyte.routine.<slug>.plist` with `StartCalendarInterval`, plus the copy and
     `launchctl bootstrap gui/$(id -u) ...` command; remove with `launchctl bootout`.
   - `task-scheduler`: a PowerShell `Register-ScheduledTask` command with
     `New-ScheduledTaskSettingsSet -StartWhenAvailable` (daily or weekly trigger; monthly and
     quarterly cadences use a daily trigger plus the date guard), the `schtasks /Create` equivalent,
     and `Unregister-ScheduledTask` / `schtasks /Delete` to remove.
   - `none`: no schedule; the routine runs on demand.
6. `SCHEDULE.md`: what the routine does, its cadence and window, the dry-run command to run first,
   the register command, how to go live (set `"mode": "live"` after the dry runs look right), how
   to check logs, and how to remove it.
7. Refuse a non-empty `--dest` without `--force`; write only inside `--dest`; print JSON with every
   file and command.

#### Edge Cases & Error Handling
- `--scheduler launchd` on Windows or `task-scheduler` on macOS: allowed (the user may copy the
  folder), with a warning.
- `--time` not `HH:MM`: exit 2.
- The chore's folder no longer exists: warning; the runner reports it at run time.

### 6.9 Runner

**File(s):** `scripts/chores/run_routine.py`
**Type:** New file (standalone script and module)

#### What it does
Runs one routine headless with the chosen harness, in dry-run or live mode, with a date guard,
logging, and a per-period state file.

#### Interface / API
```text
python run_routine.py                 # dry run now (ignores the date guard)
python run_routine.py --live          # live run now
python run_routine.py --scheduled     # what the scheduler calls: date guard, then routine.json's mode
python run_routine.py --print-command # prints the harness command and exits
python run_routine.py --check-date    # prints whether today is a run day and exits 0
```

#### Logic / Algorithm
1. Load `routine.json` and `routine-prompt.md` from the runner's folder.
2. `--scheduled`: `RunWindow.in_window(today)` and the period not yet done (from `state.json`);
   otherwise log "not a run day" and exit 0.
3. Build the harness argv (verified flags, section 11):
   - Claude Code: `claude -p --output-format text` with `--permission-mode plan` (dry) or
     `--permission-mode acceptEdits --allowedTools <list>` (live); the prompt on stdin.
   - Codex: `codex exec --skip-git-repo-check --cd <folder>` with `--sandbox read-only` (dry) or
     `--sandbox workspace-write` (live); the prompt on stdin (`-`).
4. Prefix the prompt with the run's mode and date; run in the working folder with the timeout;
   write stdout and stderr to `logs/<YYYYMMDD-HHMMSS>-<mode>.log`.
5. On exit 0, record the period in `state.json` (`dry_run_periods` or `live_periods`).
6. Exit with the harness's code (124 on timeout).

#### Edge Cases & Error Handling
- Harness path missing: exit 127 with a log line.
- Working folder missing: exit 2 with a log line.
- Malformed `routine.json`: exit 2.

### 6.10 Feedback command

**File(s):** `scripts/recurring_chores.py` (`FeedbackCommand`)
**Type:** New

#### What it does
Copies the exact idea title from `result.json` into
`vidbyte-cli --json --no-input agents suggest feedback accept|reject --project <key> --suggestion <title> [--reason <redacted>]`.
The project comes from `--project`, else `inputs.json`.

### 6.11 Focus file

**File(s):** `skills/automate-recurring-chores/references/chore-routines.md`
**Type:** New file

#### What it does
The developer instructions appended to the generator's and every critic's system prompt. Parts:
1. The situation: the digest's chores are measured from the user's own sessions; the user wants one
   scheduled routine per chore.
2. One idea per `[C#]`, title `[C#] <chore name>: <cadence> routine`, and the field map: `summary`
   (chore, cadence with run dates and tags, inputs and their sources, outputs and their
   destinations); `first_action` (the first dry run); `suggested_actions` in order: `ROUTINE
   PROMPT:` (ready to paste, at most about 1,500 characters), `SCHEDULE:` (the platform option and
   its exact command), `HUMAN STEPS:`, `DRY RUN:`, `GO LIVE:`, `TURN OFF:`; `decision_points`
   (what the user must decide); `considerations` (8-10: inputs, credentials by name, outputs,
   failure handling, logs, permissions, scheduler caveats, the user's own harness usage, how to
   pause); `completion_criteria` (observable dry-run and first-live-run checks).
3. Verified scheduling facts (section 11): headless commands, local schedulers, GitHub Actions, and
   harness-native schedules, with their limits.
4. Safety rules: no paying, sending, posting, uploading, or filing without a human confirming that
   exact step; drafts instead of sends; never register a job; no credential values; dry run first;
   least privilege.
5. What not to do: no invented cadence, no idea without tags, no merged chores, no irregular chores,
   no generic automation advice.
6. How the critic grades a routine.

It stays under 20,000 bytes (target about 12 KB).

### 6.12 Public SKILL.md and references

**File(s):** `skills/automate-recurring-chores/SKILL.md`, `references/setup.md`,
`references/troubleshooting.md`
**Type:** New files

`SKILL.md` follows the `write-agent-skill` section order. Frontmatter:

```yaml
name: automate-recurring-chores
description: Use when the user keeps redoing the same chore by hand in a fresh coding-agent session every week, month, or quarter and wants it to run on a schedule - ... Drives the Vidbyte Suggestion Agent through vidbyte-cli.
metadata:
  agent: "suggestion-agent"
  commands: "agents suggest project list, agents suggest run, agents suggest handoff, agents suggest project create, agents suggest feedback accept, agents suggest feedback reject"
  cost: "Model usage from the Vidbyte balance: gpt-6-sol at $2 / $10 per million input / output tokens + 2%, capped at 400,000 tokens per run"
  writes-files: "true"
  min-cli-version: "0.3.0"
  status: stable
```

`writes-files` is `"true"` because Deliver writes routine files after the user's explicit yes.
`references/setup.md` and `references/troubleshooting.md` are byte copies of blobs `8fb4e3cf` and
`cb9656bc`.

### 6.13 Website listing

**File(s):** `next-app/constants/agent-skills/automate-recurring-chores/catalog.js` (new),
`next-app/constants/agent-skills/catalog.js` (modified) in cerredz/Vidbyte
**Type:** New + Modified

A `defineAgentSkill` entry: `status: "coming-soon"`, `minCliVersion: "0.3.0"`,
`writesFiles: true`, `addedOn: "2026-10-13"`, `agents: ["suggestion-agent"]`,
`topics: ["planning", "project-management"]`, tags such as `"Recurring tasks"`, `"Scheduling"`,
`"Automation"`, `"Claude Code"`, `"Codex"`, the six `vidbyte-cli agents suggest ...` commands, and
the frontmatter `description` verbatim. One import line (alphabetical) and one `ENTRIES` line
immediately after `TOO_MANY_MEETINGS`.

---

## 7. Data Model Changes

### 7.1 `chores.json` (local, never sent)
**Change type:** New
```json
{
  "schema": 1,
  "window": {"since": "ISO|null", "until": "ISO", "span_days": 0, "max_sessions": 500},
  "read": [], "environment": {"platform": "Windows", "headless_clis": {"claude": true, "codex": true}},
  "detectable_cadences": ["daily", "weekdays", "weekly"],
  "chores": [{"tag": "C1", "label": "...", "terms": ["budget"], "project": "finances", "cwd": "real path",
    "github_remote": false, "cadence": {"kind": "monthly", "pattern": "first_business_day", "label": "...",
    "weekday": null, "day": null, "month_in_quarter": null, "anchor": "YYYY-MM-DD", "time": "09:10",
    "median_gap": 30.0, "spread": 1.0, "regularity": 0.9, "intervals": [30, 31], "next_expected": "YYYY-MM-DD", "overdue": false},
    "runs": [{"day": "YYYY-MM-DD", "sessions": ["S3"]}],
    "inputs": {"files_read": [["statements/<date>.csv", 4]], "folders": [], "commands": [], "data_sources": [], "credentials": []},
    "outputs": {"files_written": [], "external_actions": [{"action": "...", "runs": 3, "tags": ["P11"]}]},
    "follow_ups": [{"text": "...", "tags": ["P12", "P20"]}], "minutes_per_run": 25, "prompts": ["P4"]}],
  "sessions": [{"tag": "S3", "harness": "claude-code", "session_id": "...", "source": "...", "cwd": "...",
    "first": "ISO", "prompts": 9, "resume": {"command": "claude --resume ...", "env": {}}}],
  "prompts": [{"tag": "P4", "session": "S3", "index": 0, "ts": "ISO", "text": "redacted"}],
  "irregular": [{"label": "...", "runs": 4, "intervals": [2, 19, 6]}]
}
```

### 7.2 `inputs.json` (values sent)
**Change type:** New
```json
{"goal": "string <= 700", "outcomes": ["[C1 S3 S7 S12] ..."], "context": ["[all] ..."], "capability": ["..."],
 "risks": ["[C1 P11] ..."], "forbidden": ["[all] ..."], "success": [], "constraint": [], "avoid": [], "project": null}
```

### 7.3 Routine folder (written only on an explicit yes)
**Change type:** New. `routine-prompt.md`, `routine.json` (`{"schema": 1, "chore": "C1", "label",
"cadence": {...}, "time": "HH:MM", "harness": "claude-code", "harness_path", "workdir", "mode":
"dry-run", "allowed_commands": [], "timeout_minutes": 45, "created", "idea": {"id", "title"}}`),
`run_routine.py`, `SCHEDULE.md`, optional `.plist`, and later `logs/` and `state.json`.

### 7.4 Website entry
**Change type:** New. A frozen `defineAgentSkill` object (section 6.13). Migration: N/A, static data
built at deploy. Rollback: revert the commit.

---

## 8. API Changes

N/A. No HTTP endpoint changes. The skill calls the existing `vidbyte-cli agents suggest run`,
`handoff`, `project list`, `project create`, and `feedback accept|reject` commands with options that
exist in `contracts/cli-commands.json`. The script's own command-line interface is in 6.1 and the
runner's in 6.9.

---

## 9. File Change Manifest

| Action | File Path | Reason |
|--------|-----------|--------|
| CREATE | `docs/design/automate-recurring-chores.md` (public repo) | This design doc |
| CREATE | `skills/automate-recurring-chores/SKILL.md` | The public skill |
| CREATE | `skills/automate-recurring-chores/references/setup.md` | Canonical copy (blob 8fb4e3cf) |
| CREATE | `skills/automate-recurring-chores/references/troubleshooting.md` | Canonical copy (blob cb9656bc) |
| CREATE | `skills/automate-recurring-chores/references/chore-routines.md` | Focus file sent with `--instructions` |
| CREATE | `skills/automate-recurring-chores/scripts/recurring_chores.py` | Entry point and command classes |
| CREATE | `skills/automate-recurring-chores/scripts/sessionkit/*` (9 files) | Byte copy of the shared package from parent commit `c6be510` |
| CREATE | `skills/automate-recurring-chores/scripts/chores/__init__.py` | Package marker |
| CREATE | `skills/automate-recurring-chores/scripts/chores/signatures.py` | Openers, terms, resources, credentials, external actions |
| CREATE | `skills/automate-recurring-chores/scripts/chores/detector.py` | Similarity, clustering, runs, aggregation, ranking |
| CREATE | `skills/automate-recurring-chores/scripts/chores/cadence.py` | Cadence classification, regularity, next date |
| CREATE | `skills/automate-recurring-chores/scripts/chores/digest.py` | digest.md, chores.json, inputs.json, focus copy |
| CREATE | `skills/automate-recurring-chores/scripts/chores/suggest.py` | Flag kinds, categories, count, argv wiring |
| CREATE | `skills/automate-recurring-chores/scripts/chores/report.py` | Idea → chore/run mapping and flags |
| CREATE | `skills/automate-recurring-chores/scripts/chores/schedule.py` | cron, launchd, Windows schedule snippets |
| CREATE | `skills/automate-recurring-chores/scripts/chores/routine.py` | Routine folder writer |
| CREATE | `skills/automate-recurring-chores/scripts/chores/run_routine.py` | Runner and shared `RunWindow` |
| CREATE | `scripts/test-automate-recurring-chores.py` (public repo root) | Phase 5 verification script; not installed by `npx skills add` |
| CREATE | `next-app/constants/agent-skills/automate-recurring-chores/catalog.js` (Vidbyte) | Website listing |
| MODIFY | `next-app/constants/agent-skills/catalog.js` (Vidbyte) | Import and place after `TOO_MANY_MEETINGS` |

---

## 10. Testing Plan

All tests live in `scripts/test-automate-recurring-chores.py`. Unit tests build `sessionkit`
`Session`/`Turn` objects directly. Integration tests write Claude Code and Codex fixtures (and one
OpenCode legacy-JSON session) into a temporary home and run `recurring_chores.py` in a subprocess
with `HOME`/`USERPROFILE`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, and `XDG_DATA_HOME` pointing into it,
and `WIN_PD_OVERRIDE_LOCAL_APPDATA` set to a temporary folder. Every `suggest` and `feedback` test
passes `--cli <fake>`, a recording `vidbyte-cli` launcher made with pip's vendored distlib
`ScriptMaker` (a real `.exe` on Windows), and the subprocess `PATH` holds no `vidbyte-cli`, so an
ignored `--cli` fails with 127. Runner tests use fake `claude` and `codex` launchers made the same
way. The script runs under the current Python and Python 3.8.20.

### Unit Tests
- `TermExtractor` → "the September invoices" and "the October invoices" give the same terms; month names, years, dates, and `Q3` are removed and reported as period mentions — [Silent Failure]
- `TermExtractor` → empty and whitespace text give an empty set — [Edge Case]
- `PathNormalizer` → `C:\Users\me\fin\statements\2026-09.csv` with cwd `C:\Users\me\fin` and `/home/me/fin/statements/2026-10.csv` with cwd `/home/me/fin` both give `statements/<date>.csv` — [Silent Failure]
- `CommandSignature` → `cd app && npm outdated --json` gives `npm outdated`; `FOO=1 python scripts/cat.py` gives `python scripts/cat.py`; `ls -la` gives none; `cat data/2026-09.csv` gives a read of `data/<date>.csv` — [Edge Case]
- `CommandSignature` → `git push`, `gh pr create`, and `curl -X POST ...` are external actions; `git pull` and `curl https://x` are not — [Silent Failure]
- `ActionParser` → `Bash: ...`, `Edit x`, `Read y`, `WebFetch https://bank.example/x`, and `mcp__gmail__send_message a@b.c` map to command, write, read, web (domain only), and mcp with an external action; an unknown shape is `other` — [Hidden Assumption]
- `SessionSignature` → credential names (`STRIPE_API_KEY`, `.env`) are captured without values: the value in `export STRIPE_API_KEY=sk_live_...` never appears in any field — [Silent Failure]
- `SessionSignature` → an opener `continue where we left off` is a continuation; `hi` followed by a real request uses the second prompt as opener — [Edge Case]
- `CadenceAnalyzer` → Jun 2, Jul 1, Aug 1, Sep 1, Oct 1 2026 (each the first business day) is `monthly`/`first_business_day` with next expected Nov 2 2026 — [Silent Failure]
- `CadenceAnalyzer` → four Mondays in a row is `weekly`, weekday Monday, regularity 1.0; one skipped week still `weekly` with regularity below 1 — [Edge Case]
- `CadenceAnalyzer` → ten consecutive weekdays over two weeks is `weekdays`, not `daily` — [Silent Failure]
- `CadenceAnalyzer` → days 1, 3, 20, 21, 40 is `irregular` — [Silent Failure]
- `CadenceAnalyzer` → last business days (Jul 31, Aug 29, Sep 30 2025) is `last_business_day`; Jan 15, Apr 15, Jul 15, Oct 15 is `quarterly`, month 1 of the quarter, day 15 — [Edge Case]
- `CadenceAnalyzer` → two runs is `irregular` (too few); daily with 4 runs is `irregular` (needs 5) — [Edge Case]
- `RunWindow` → first_business_day: Nov 2 2026 (Monday) is in window, Nov 1 (Sunday) is not, Nov 4 is in window (catch-up), Nov 6 is not; period keys differ across months — [Hidden Failure]
- `RunWindow` → day_of_month 31 in February clamps to Feb 28; biweekly alternates weeks from the anchor — [Edge Case]
- `RunWindow.triggers` → monthly patterns never put a day-of-week restriction in the trigger (cron ORs the two fields) — [Hidden Assumption]
- `ChoreDetector` → four monthly "do the <month> budget cleanup" sessions in one folder reading `statements/<date>.csv` form one chore; an unrelated daily coding session stream does not join it — [Silent Failure]
- `ChoreDetector` → three similar sessions on the same day are one run, so no chore — [Hidden Failure]
- `ChoreDetector` → a chore finished the next day in a second session is one run, not two (monthly gaps stay ~30) — [Hidden Failure]
- `ChoreDetector` → average linkage: A~B and B~C with A≁C do not chain into one cluster of three — [Silent Failure]
- `ChoreDetector` → zero sessions and one session give no chores without errors — [Edge Case]
- `ChoreDetector` → a chore's inputs list `statements/<date>.csv (4/4)` and external action "send" with its prompt — [Silent Failure]
- `ChoreDigestWriter` → the digest never exceeds `max_chars`; every tag in `digest.md` and `inputs.json` exists in `chores.json`; tags are unique — [Hidden Failure]
- `ChoreDigestWriter` → no raw secret, email, or home path from the fixtures appears in `digest.md`, `inputs.json`, or `report.md` — [Silent Failure]
- `ChoreDigestWriter` → `max_chars` below 20,000 is a usage error; the focus copy is byte-identical and ≤ 20,000 bytes — [Edge Case]
- `ChoreDigestWriter` → every `forbidden` value starts with `[all]` — [Hidden Assumption]
- `SuggestPlan` → count is 2 for 1 chore, 10 for 14 chores — [Edge Case]
- Suggest argv → root options first; the three categories, `--horizon any`, `--files digest.md`, `--instructions chore-routines.md`, `--max-total-tokens 400000`; `--dry-run` only when asked; `--project` only when set — [Silent Failure]
- Validator → an unknown key, an 11th `outcomes`, a value over 500 characters, an untagged `risks` value, and an unknown `[C9]` each give a problem; `avoid` is accepted without tags — [Edge Case]
- `RoutineReporter` → resolves `[C1]` and `[S3]`; flags an idea with no tags; flags "weekly" on a monthly chore; flags a send-chore idea with no confirmation; lists an uncovered chore — [Silent Failure]
- `ScheduleBuilder` → first_business_day at 09:10 gives `10 9 1-7 * *`, a launchd array with Day 1-7, and a daily Windows trigger; weekly Monday gives `10 9 * * 1,2,3` — [Silent Failure]
- `RoutineWriter` → writes the five files, `mode` is `dry-run`, the standing rules precede the prompt, `run_routine.py` is byte-identical to the shipped runner, and nothing is written outside `--dest` — [Hidden Failure]
- `RoutineWriter` → a non-empty `--dest` without `--force` exits 2; an idea citing two chores without `--chore` exits 2; a missing harness exits 2 — [Edge Case]
- `RoutineWriter` → `allowed_commands` excludes `git push` and other external commands — [Silent Failure]
- Runner → dry run builds `claude -p ... --permission-mode plan` (and `codex exec --sandbox read-only ...`), sends the prompt on stdin, writes a log, records the dry-run period; `--scheduled` outside the window exits 0 without calling the harness; a second `--scheduled` in the same period does not run again — [Hidden Failure]

### Integration Tests
- `gather` end to end over fixtures (a monthly first-business-day chore in Claude Code, a weekly Monday chore in Codex, an irregular repeat, and unrelated sessions): exit 0; two chores with the right cadences and next dates; the irregular repeat listed locally and absent from `digest.md`.
- `gather` with every harness disabled exits 3 with `reason: no_sessions`; with sessions but no chores, exit 3 with `reason: no_chores`.
- `suggest --dry-run` with the recording fake: exit 0; the recorded argv equals the expected argv; cwd is the output folder; `--files` and `--instructions` are relative; no `paid-run.txt`.
- `suggest --paid` with the fake: writes `paid-run.txt`, then `result.json`; a second `--paid` exits 2 and the fake records no second call.
- `suggest --paid` where the fake exits 5 with an error JSON: the summary carries `error.code`, exit 5.
- `suggest` with no `--cli` and no `vidbyte-cli` on `PATH` exits 127 and runs nothing.
- `report` over a fake `result.json` writes `report.md` with run dates and resume commands.
- `routine` over the fake result writes the folder; the runner then runs a dry run against a fake `claude`.
- `feedback --accept` sends the exact title and project; `--reason` is redacted.
- The whole flow runs under Python 3.8.20.

### Manual / QA Test Cases
1. Given this machine's real stores, when `gather` runs with defaults, then it finishes, reports the span and detectable cadences, and `digest.md` shows no secret on a skim — [Hidden Failure]
2. Given the real `digest.md`, when `suggest --dry-run` runs against the scratch-venv CLI with a blank key, then exit 0, `stop_reason: dry_run`, and `digest.md` in the manifest — [Hidden Assumption]
3. Given `--project-dir` on one folder, when `gather` runs, then the span grows and only that folder's sessions are read — [Edge Case]
4. Given the website worktree, when `npm run build` runs, then the only `[agent-skills]` warning is `fetch-failed` for this slug — [Hidden Assumption]

---

## 11. Dependencies & External Services

| Dependency | Version / Endpoint | Purpose | Risk |
|------------|--------------------|---------|------|
| `vidbyte-cli` | 0.3.0+ (PyPI has 0.2.0) | `agents suggest run`, `handoff`, `project`, `feedback` | Listed `coming-soon` until 0.3.x is on PyPI |
| Vidbyte model gateway / gpt-6-sol | via `agents suggest run` | Generator + critics | Usage-billed, capped at 400,000 tokens |
| `sessionkit` | parent branch `skill/improve-my-prompting`, commit `c6be5101575f15249de63216e6e6684dee759937` (`sessionkit` version 1, 9 files) | Session reading, redaction, suggest plumbing | Copied byte for byte and verified with `git hash-object`; bugs reported to the parent, never patched here |
| Python | 3.8+ standard library | Scripts and runner | none beyond `sessionkit`'s optional `sqlite3` |
| Claude Code / Codex CLIs | user-installed | Headless routine runs | Flags verified against the official docs below; usage is the user's own plan or API |
| cron / launchd / Task Scheduler | OS | Schedules the runner | Registered only by the user |
| cerredz/Vidbyte `defineAgentSkill`, lint N050 | `main` | Website listing | N050 checks commands against the vendored contract |

Verified scheduling and headless facts, and the outside evidence, are listed with their sources in
the appendix at the end of this document.

---

## 12. Rollout & Deployment

- No feature flags; a new skill folder and a new catalog entry, not a breaking change.
- Order (per `add-agent-skill`): public PR first (cerredz/vidbyte-cli), then the website PR. The
  website entry is `coming-soon`, so the detail page says the skill is being written until the
  public file merges.
- When vidbyte-cli 0.3.x reaches PyPI, flip `status` to `available` with the skills in Vidbyte#691.
- The `sessionkit` copy is pinned to a parent commit; a later `sessionkit` change is re-copied into
  every sibling in one PR.
- Rollback: revert the website entry first, then the public folder.

---

## 13. Open Questions

- [ ] `min-cli-version` says `0.3.0` to match every suggestion sibling; usage billing landed in
  0.3.1 and the first PyPI release will be 0.3.2. Move all suggestion skills together (Vidbyte#691)?
- [ ] The newest-500 window can be too short for monthly chores for heavy users. Should `sessionkit`
  offer an opener-only mode that reads thousands of sessions cheaply for cadence detection?
- [ ] Should the `routine` writer also emit GitHub Actions workflows for chores with a GitHub remote?
- [ ] Should a later version re-run `gather` on the next expected date and report whether a written
  routine replaced the manual runs (as `stop-repeating-yourself` does with its state file)?

---

## 14. Alternatives Considered

### Alternative 1: Let the model find the chores in raw openers
- What: send the openers of all 500 sessions with dates and ask the model to find recurring tasks.
- Why rejected: 500 openers with actions is far larger than a digest, carries more private text,
  and a model counting intervals over hundreds of dates is less reliable than code. Measured
  cadences make the routines checkable.

### Alternative 2: Connected components instead of average linkage
- What: link similar sessions and take connected components.
- Why rejected: chaining merges unrelated sessions through intermediates (A~B~C), which in a busy
  repository produces one giant "chore". Average linkage keeps clusters tight.

### Alternative 3: Copy the model's cron line into the written files
- What: write the schedule exactly as the idea states it.
- Why rejected: a model-written cron line can be wrong for "first business day" (cron ORs
  day-of-month and day-of-week), and the written files are what actually runs. The writer computes
  the trigger from the measured cadence and guards it in the runner.

### Alternative 4: Register the schedule for the user
- What: run `crontab`, `launchctl`, or `Register-ScheduledTask` after a yes.
- Why rejected: the brief forbids it, and an unattended job that runs an agent with write access is
  a standing risk the user should create deliberately. The skill prints the exact command.

### Alternative 5: Use the MCP server
- What: drive the Suggestion Agent with `vidbyte_plan`/`vidbyte_run`.
- Why rejected: `write-agent-skill` section 1. The MCP server fixes the flags (no `--instructions`,
  no categories) and adds a paid routing call.

---

## Appendix: verified facts and sources

Filled in from official documentation fetched on 2026-10-13 (see the next commit).
