# Design Doc: turn-corrections-into-skills

**Status:** Draft
**Author:** Claude
**Created:** 2026-10-13
**Last Updated:** 2026-10-13

---

## 1. Overview

`turn-corrections-into-skills` is a public agent skill that finds every place a developer had to
correct their coding agent and proposes one agent skill (a `SKILL.md`) per kind of mistake. A
bundled, standard-library Python script reads the developer's most recent sessions (at most 500)
from Claude Code, Codex, OpenCode, Gemini CLI, Qwen Code, OpenClaude, Factory Droid, and Pi through
the shared `sessionkit` package. It detects corrections locally and for free: a prompt that pushes back on what the agent
just did ("no, the other file", "why did you delete that", "undo that"), an interrupted turn
followed by a redirect, and a prompt that supplies the fix after the agent's tool calls failed. For
each correction it records what the agent did (its tool actions, its last words, failed calls,
interruption) and what the user wanted instead (the correction text). It clusters the corrections
into scenarios (the same kind of mistake in the same kind of task, tool, or project), ranks them by
how often and how costly they were, and writes a redacted digest with tagged evidence: `[C#]`
scenarios, `[S#]` sessions, `[P#]` prompts. One usage-billed `vidbyte-cli agents suggest run`
sends that digest plus an explicit focus file (`references/skills-from-corrections.md`, passed with
`--instructions`) to the Vidbyte Suggestion Agent. The focus file makes the generator and every
critic return one skill specification per scenario: a slug and title, a "Use when ..." description,
what the model got wrong (quoting the corrections), the correct behavior, the rules and steps, and
where and how often it happened. A free local `report` maps every proposed skill back to its
correction evidence and flags any idea with none. For each skill the user picks, the skill offers a
`vidbyte-cli agents suggest handoff` prompt and a local `scaffold` command that drafts the
`SKILL.md` and writes it only after an explicit yes, to the location the user chooses. The skill is
listed on `/agents/skills` as a `coming-soon` entry, like every skill that needs vidbyte-cli 0.3.x.

---

## 2. Goals & Non-Goals

### Goals
- Add `skills/turn-corrections-into-skills/` to cerredz/vidbyte-cli, meeting the
  `write-agent-skill` contract (frontmatter, fixed section order, consent, exact commands, shared
  references byte-identical to blobs `8fb4e3cf` and `cb9656bc`).
- Read up to 500 recent interactive sessions from eight harnesses with the shared `sessionkit`
  package, copied byte for byte from the parent branch `skill/improve-my-prompting`.
- Detect every correction locally with four signals (pushback, interrupt-then-redirect, fix after
  failed tool calls, restated or frustrated correction), capturing `Turn.actions`,
  `before_agent_text`, `reply_text`, `failed_actions`, and `interrupted` of the corrected turn.
- Group consecutive corrections into episodes, classify each episode into a mistake kind, cluster
  episodes into scenarios, and rank scenarios by corrections, sessions, projects, prompts lost, and
  tool calls redone.
- Inventory the skills the user already has (names and descriptions only), so the Suggestion Agent
  can propose an update to an existing skill instead of a duplicate.
- Send a redacted digest and a focus file to the Suggestion Agent with `--files` and
  `--instructions`, and require tagged evidence on every idea.
- Ship a local `report` that maps each idea to its scenarios and correction prompts, checks its
  slug and description, flags ungrounded ideas and scenarios no idea covers.
- Ship a local `scaffold` that drafts a `SKILL.md` from one idea and writes it only with `--write`
  after the user's explicit yes, never overwriting a file.
- Follow the usage-billed suggestion flow of every sibling: free dry run, consent, one paid run
  with `--max-total-tokens 400000`, a `paid-run.txt` guard, a cost estimate.
- List the skill on the website with a validated `defineAgentSkill` entry placed right after
  `STOP_REPEATING_YOURSELF` in `ENTRIES`.

### Non-Goals
- Turning repeated instructions, approvals, or context into rules, hooks, or allowlist entries.
  `stop-repeating-yourself` does that (see section 3).
- Reading pull request review comments. The local branch `skill/agent-mistakes-to-guardrails`
  does that.
- Critiquing how the user writes prompts (`improve-my-prompting`, a sibling in progress), finding
  unfinished work (`find-unfinished-agent-work`), or scheduling chores
  (`automate-recurring-chores`).
- Evaluating or installing the proposed skills automatically, editing `CLAUDE.md`/`AGENTS.md`, or
  changing any agent setting. The only file the skill can write outside its output folder is a
  new `SKILL.md`, after an explicit yes.
- Non-English correction detection. Keyword signals are English; other languages under-count.
- Reading harnesses `sessionkit` does not read (Cursor, Copilot CLI, Cline, Roo Code, Kilo Code,
  Grok CLI, Amp, Windsurf); the summary names the ones it finds.
- Publishing a vidbyte-cli release or flipping the listing to `available` (draft Vidbyte#691).

---

## 3. Background & Context

- **Why now.** The user asked for a skill that "explicitly looks for places where the user had to
  correct the model, and then suggests a list of skills to create for each one of these". It is
  one of four session-reading skills built in parallel on one shared session reader (section 0 of
  the common brief).
- **The problem is broad and has a cost.** In the Stack Overflow 2025 Developer Survey, 66% of
  31,476 respondents named "AI solutions that are almost right, but not quite" as their top
  frustration with AI tools, and 45.2% said debugging AI-generated code is more time-consuming
  ([survey.stackoverflow.co/2025/ai](https://survey.stackoverflow.co/2025/ai)). Each "almost
  right" is a correction the developer types by hand. Within one conversation a wrong turn is
  expensive: Laban et al. found that "when LLMs take a wrong turn in a conversation, they get lost
  and do not recover", across 200,000+ simulated conversations
  ([arXiv:2505.06120](https://arxiv.org/abs/2505.06120)).
- **Skills are the documented fix, built from observed failures.** Anthropic's skill-authoring
  guide prescribes evaluation-driven development: "Identify gaps: Run Claude on representative
  tasks without a Skill. Document specific failures or missing context", then "Write minimal
  instructions" that address those gaps
  ([platform.claude.com best practices](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices)).
  A developer's own corrections are exactly those documented failures, already paired with the
  correct behavior. Claude Code loads skills from `.claude/skills/<name>/SKILL.md` and
  `~/.claude/skills/<name>/SKILL.md` ([code.claude.com/docs/en/skills](https://code.claude.com/docs/en/skills));
  Codex reads `.agents/skills` from the working folder up to the repository root and
  `$HOME/.agents/skills` ([learn.chatgpt.com/docs/build-skills](https://learn.chatgpt.com/docs/build-skills)).
- **What is hard without help.** Corrections are not labelled. They are spread over hundreds of
  sessions from eight harnesses in seven storage formats, and the mistake itself is in tool calls (an `Edit` of the wrong
  file, a `Bash: npm install` in a pnpm repository) that a reader of the prompts alone never sees.
  Writing a good skill also takes judgment: a description that fires at the start of the task,
  rules that would have prevented the mistake, a check before "done".
- **How it differs from the siblings.**
  - `stop-repeating-yourself` (merged) counts what the user *repeats* (instructions, requests,
    context, approvals, follow-ups) across at least two sessions and turns each into the cheapest
    durable fix: a rule, a hook, an allowlist entry, a memory entry, sometimes a skill. This skill
    starts from model *errors* the user corrected, including one costly correction (a deleted
    folder) that never repeated. It keeps what the agent did next to what the user wanted, and
    produces a full skill specification per scenario (trigger description, rules, steps, checks,
    scope), because task-specific procedure belongs in a skill that loads when the task starts,
    not in an always-loaded rule file.
  - `agent-mistakes-to-guardrails` (local branch) reads PR review comments through `gh`. This skill
    reads the live sessions where the mistake happened, before any PR exists.
  - `pick-up-where-my-sessions-left-off` and `cut-my-agent-token-usage` (merged) read the same
    transcripts for next steps and token usage.
- **Current state.** No skill detects corrections from tool actions. `stop-repeating-yourself`'s
  `repeats.py` clusters directive sentences; its stopword list and clustering approach inform this
  design, but its code is not copied.
- **Constraints.**
  - CLI surface verified against `contracts/cli-commands.json` on cerredz/vidbyte-cli-internal
    `main` (`abd31b4`, contract generated at `f1321179`): `agents suggest run` has `--goal
    --category --count --horizon --outcomes --mistakes --context --capability --success
    --constraint --avoid --files --instructions --project --max-total-tokens --dry-run`;
    `agents suggest handoff --input --idea`; `agents suggest project list`;
    `agents suggest project create --key --title --description`;
    `agents suggest feedback accept|reject --project --suggestion --reason`.
  - `--instructions`: at most 8 files and 20,000 bytes in total, appended after the fixed system
    prompt of the generator, every critic, and every extra-compute generator; never echoed.
  - Idea fields are fixed by the CLI (`SuggestionIdea`): `title` (≤256), `summary` (≤2048),
    `first_action`, `suggested_actions` (1-12), `considerations` (8-10), `why_now`,
    `expected_benefit`, `completion_criteria`, `decision_points`, `effort_estimate`. A skill
    specification must be carried inside those fields, so the focus file fixes a field mapping.
  - Traps: `--horizon` other than `any` filters after billing; `--mistakes`/`--avoid` values
    repeated word for word drop the idea (values start with tags); `--dry-run --project X` exits 2
    until X exists; Python 3.8 has no `Path.write_text(newline=)`.
  - Money safety: the OS keyring holds a live Vidbyte key. Tests never reach the real CLI; every
    `suggest`/`feedback` test uses a recording fake launcher passed with `--cli`.

---

## 4. Requirements

### Functional Requirements
1. `corrections.py gather` collects sessions with `sessionkit.collector.SessionCollector` and the
   standard `CollectOptions` flags (`--max-sessions` 1-500, default 500; `--days`, `--since`,
   `--project-dir`, `--claude-dir`, `--codex-home`, `--opencode-dir`, `--gemini-dir`, `--qwen-dir`,
   `--openclaude-dir`, `--factory-dir`, `--pi-dir`, `--no-*`, `--only` (repeatable, overrides
   `--no-*`), `--exclude-session`, `--include-current`), plus `--max-scenarios` (1-20, default 12),
   `--max-chars` (20,000-100,000, default 60,000), and `--out`.
2. For every turn with index ≥ 1, the detector decides whether the prompt corrects the agent's
   previous turn, using the signals and suppressions in section 6.2, and records the signal names,
   a strength (`strong`, `medium`, `weak`), the corrected turn's prompt, actions, failed-action
   count, interruption, and tool-call count, the agent's last words before the correction, and the
   agent's first actions after it.
3. Consecutive correction turns in one session form one episode. Each episode gets one mistake kind
   from section 6.3 and cost figures: correction prompts, tool calls in the corrected turns, failed
   calls, interruption.
4. Episodes are clustered into scenarios by kind and similarity (shared terms, tools, file types,
   project). Scenarios are ranked by the score in section 6.3. Every scenario is tagged `[C#]` in
   rank order; the top `--max-scenarios` get full blocks in the digest, the rest one line each.
5. `gather` inventories existing skills (name, description, scope) from the Claude config folders
   read, `~/.agents/skills`, the Codex home's `skills/`, and `.claude/skills`, `.agents/skills`, and
   `.opencode/skills` inside the project folders of the full scenarios. It reads only the
   frontmatter of each `SKILL.md`, at most 60 skills.
6. `gather` writes into a new output folder: `digest.md` (redacted, ≤ `--max-chars`),
   `corrections.json` (local index, never sent), `inputs.json` (pre-filled typed inputs), and
   `skills-from-corrections.md` (byte copy of the focus file, refused if over 20,000 bytes). It
   prints a JSON summary and exits 3 with `reason` `no_sessions` or `no_corrections` when there is
   nothing to send.
7. `corrections.py suggest --out DIR --dry-run|--paid [--cli PATH]` validates `inputs.json` with
   `sessionkit.runner.InputsValidator` (allowed keys, caps, lengths, tags known to
   `corrections.json`, redaction, argv under 24,000 characters), then runs
   `vidbyte-cli --json --no-input agents suggest run` from inside the output folder with
   `--category risk_prevention --category leverage --category learning --category verification
   --count <N> --horizon any ... --files digest.md --instructions skills-from-corrections.md
   [--project <key>] --max-total-tokens 400000 [--dry-run]`, where N is the number of full
   scenarios clamped to 2-12. `--paid` refuses a folder with `paid-run.txt` or `result.json` and
   writes `paid-run.txt` before it starts.
8. `corrections.py report --out DIR` reads `result.json`, maps each idea to its `[C#]`, `[P#]`, and
   `[S#]` tags, resolves them in `corrections.json`, parses the skill fields (slug, description,
   got-wrong, correct, rules, steps), checks the slug and the description, marks
   `grounded: false` for an idea with no valid scenario or prompt tag, lists unknown tags, duplicate
   slugs, slugs that match an existing skill, and full scenarios no idea cites. It writes
   `report.md` and prints JSON.
9. `corrections.py scaffold --out DIR --idea idea-NNN` renders a draft `SKILL.md` from the idea,
   saves it inside the output folder at `drafts/<slug>/SKILL.md`, and prints it with the candidate
   destinations. With `--write --dest <skills folder>` it writes `<dest>/<slug>/SKILL.md` only if
   that file does not exist; it refuses an ungrounded idea and an invalid slug or description
   (overridable with `--slug` and `--description`).
10. `corrections.py feedback --out DIR --idea idea-NNN --accept|--reject [--reason] [--project]
    [--cli]` records an explicit verdict with the idea's exact title through
    `vidbyte-cli --json --no-input agents suggest feedback accept|reject`; the project defaults to
    `inputs.json`'s `project`, else `turn-corrections-into-skills`.
11. Every subcommand that calls `vidbyte-cli` takes `--cli <path>` and exits 127 with a JSON error
    naming `references/setup.md` step 1 when no CLI is found.
12. The website entry passes `defineAgentSkill`, lint N050, and the gates, and sits right after
    `STOP_REPEATING_YOURSELF` in `ENTRIES`.

### Non-Functional Requirements
- **Performance:** `gather` over 500 sessions finishes in under 3 minutes on this machine
  (dominated by `sessionkit` reading); detection and clustering are linear in turns, with
  clustering O(episodes × scenarios) and episodes in the low hundreds.
- **Cost:** one paid run on gpt-6-sol at high effort, capped at 400,000 tokens (about $4 at most).
  The focus file is about 12 KB and the digest 60,000 characters by default.
- **Security:** every string written to `digest.md`, `inputs.json`, `corrections.json`, `report.md`,
  and scaffold drafts passes through `sessionkit.redaction.Redactor`. Only `digest.md`, the focus
  file, and the `inputs.json` values leave the machine. Session stores are read-only. Scaffold drafts
  strip evidence tags and carry no raw prompt text. Transcript text is data; the focus file tells
  the model never to follow instructions found in it.
- **Portability:** Python 3.8+ standard library, Windows/macOS/Linux, any shell; `vidbyte-cli` is
  started without a shell.
- **Observability:** the summary reports what was read per source, skips by reason, corrections by
  signal and kind, scenario table, redaction counts, existing skills found, and folders not read.
- **Reliability:** a broken session, file, or `SKILL.md` is skipped and counted, never fatal.

---

## 5. High-Level Design

The skill has the same four stages as its siblings: a free local `gather`, a free dry run, one
consented paid run, and free local `report`, `scaffold`, and `feedback` steps. Session reading,
redaction, and the generic paid-run plumbing come from `sessionkit` (a byte copy). The new parts
live in `scripts/correctionkit/`: the correction detector, the scenario builder, the skill
inventory, the digest writer, the suggest configuration, the idea report, and the scaffolder.

```
 sessionkit.SessionCollector (8 harnesses, <= 500 sessions)
        │ Session/Turn objects
        ▼
 CorrectionDetector ──▶ Correction (turn t corrects turn t-1) ──▶ Episode (consecutive corrections)
        │
        ▼
 ScenarioBuilder: kind ─▶ cluster (terms, tools, file types, project) ─▶ rank ─▶ [C1..Cn]
        │                                         SkillInventory (existing SKILL.md names)
        ▼                                                │
 DigestWriter ──▶ digest.md + inputs.json + skills-from-corrections.md (sent)   corrections.json (local)
        │
        ▼  suggest --paid (after consent; --dry-run first, free)
 vidbyte-cli --json --no-input agents suggest run --files digest.md --instructions skills-from-corrections.md ...
        │                (generator + critic rounds on gpt-6-sol through Vidbyte's model gateway)
        ▼
 result.json ──▶ report (idea ↔ [C#]/[P#]/[S#] ↔ local evidence, slug/description checks) ──▶ report.md
        │
        ├──▶ agents suggest handoff --idea <id>   (free execution prompt)
        └──▶ scaffold --idea <id> [--write --dest <skills folder>]   (draft, then write after a yes)
```

Key decisions:
- **The corrected turn is the previous turn.** `sessionkit` stores on each `Turn` what the agent did
  *after* that prompt. A correction at turn t is about turn t-1: its `actions`, `failed_actions`,
  `interrupted`, `tool_calls`, and the agent's last words (`turns[t].before_agent_text`). The
  correction text is turn t's prompt, and turn t's own `actions` show what the agent did after being
  corrected. This pairing is what lets the digest say "the agent ran `npm install`; the user said
  'use pnpm'".
- **Episodes, not prompts, are the unit.** Three corrections in a row about one wrong file are one
  mistake that cost three prompts. Counting episodes keeps one bad session from dominating, while
  `prompts_lost` keeps its cost visible.
- **Local clustering is a first pass; the model merges.** Clusters require the same kind and real
  overlap, so one mistake made in two projects with different words can land in two scenarios. The
  digest groups scenarios by kind and the focus file allows one skill to cite several `[C#]` when
  they are the same mistake, but never one scenario split into several skills.
- **The skill specification rides the fixed idea fields.** `title` carries the slug in backticks,
  `summary` carries three labelled parts (`Description:`, `Got wrong:`, `Correct:`),
  `suggested_actions` carries the rules and steps (`Rule:`/`Step:` prefixes), `why_now` carries how
  often, `considerations` carries where it applies, and `completion_criteria` carries the
  evaluation scenarios. `report` and `scaffold` parse these parts and fall back gracefully.
- **The focus file goes through `--instructions`, the evidence through `--files`.** Instructions
  shape the generator and every critic; the digest gets a `ctx-NNN` reference ideas can cite.
- **Categories:** `risk_prevention` (a skill is a guard at the start of the task, before the
  exposure; destructive and act-without-asking scenarios), `leverage` (one skill pays off in every
  later session of that kind, and the repetition is in the record), `learning` (the agent lacks a
  capability for this repository or tool, such as the right package manager or test command), and
  `verification` (claimed-done scenarios need a check with a pass condition before "done"). Other
  categories would steer toward finishing the sessions' work or toward prompting advice.
- **Writing a skill file needs an explicit yes, and never overwrites.** That makes
  `writes-files: "true"`. Drafts first land inside the output folder so the user can read them.

---

## 6. Detailed Design

### 6.1 Entry point

**File(s):** `skills/turn-corrections-into-skills/scripts/corrections.py`
**Type:** New file

#### What it does
Adds its own folder to `sys.path`, parses arguments, and dispatches to `GatherCommand`,
`SuggestCommand`, `ReportCommand`, `ScaffoldCommand`, or `FeedbackCommand`. On Windows it
reconfigures stdout and stderr with `errors="backslashreplace"`. Every command prints one JSON
document on stdout.

#### Interface / API
```text
corrections.py gather   [sessionkit CollectOptions flags] [--max-scenarios N<=20] [--max-chars N] [--out DIR]
corrections.py suggest  --out DIR (--dry-run | --paid) [--cli PATH]
corrections.py report   --out DIR
corrections.py scaffold --out DIR --idea idea-NNN [--slug SLUG] [--description TEXT] [--dest DIR --write]
corrections.py feedback --out DIR --idea idea-NNN (--accept | --reject) [--reason TEXT] [--project KEY] [--cli PATH]
```
Exit codes: 0 ok; 2 usage or validation; 3 nothing to send (gather); 127 no CLI; any other code is
vidbyte-cli's.

#### Edge Cases & Error Handling
- No subcommand, or both `--dry-run` and `--paid`: argparse usage error, exit 2.
- `--out` missing its files (`digest.md`, `corrections.json`, `inputs.json`): exit 2 with "run
  gather first".
- An unexpected exception in one command prints `{"error": {...}}` and exits 1, never a traceback
  with transcript text.

### 6.2 Correction detector

**File(s):** `scripts/correctionkit/detector.py`
**Type:** New file

#### What it does
`CorrectionDetector` walks each session's turns and returns `Correction` objects, then groups them
into `Episode` objects. `CorrectionRules` holds the phrase tables as compiled regular expressions.

#### Interface / API
```python
class CorrectionRules:                       # compiled phrase tables (class attributes)
    def signals(self, text: str, turn, previous) -> tuple: ...   # (signal names, strength) or ((), "")
class Correction:                            # one prompt that corrects the previous turn
    __slots__ = ("session", "turn", "previous", "signals", "strength", "text")
class Episode:                               # consecutive corrections in one session
    def __init__(self, session, ask_turn, corrections: list) -> None: ...
    prompts_lost: int; tool_calls_redone: int; failed_actions: int; interrupted: bool; first_ts; last_ts
class CorrectionDetector:
    def __init__(self, rules: CorrectionRules | None = None) -> None: ...
    def detect(self, sessions: list) -> list: ...            # Episodes, newest first
    def overall(self, sessions: list, episodes: list) -> dict: ...
```

#### Logic / Algorithm
1. Normalize each prompt: drop fenced code blocks, inline code, and lines that start with `>`;
   collapse whitespace; lower-case; keep the first 400 characters (`lead`).
2. Set aside templates first: a prompt of 25 or more words whose collapsed, lower-cased text appears
   in 2 or more sessions or 3 or more times is a slash-command template (OpenCode expands them
   inline as the user's text, sometimes 100+ times) or a reused paste, never a correction; the
   count goes into `overall.templates_skipped`. Goal-continuation prompts the harness writes are
   already dropped by sessionkit v2.
   Skip turn 0, slash commands (`/clear`, `/skill args`) and `$skill` invocations, prompts that open
   like a pasted template (a Markdown heading, a `<tag>`, or `1. **`/`- **`), self-fixes of the
   user's own previous prompt (`next pr*`, `516 i mean`), empty leads, go-aheads (≤ 8 words of
   `yes|ok|okay|sure|continue|go ahead|proceed|do it|lgtm|sounds good|thanks|great|perfect|nice`),
   and pure resumes after an interruption (`continue|go on|keep going|resume|carry on`).
3. Signals, matched in `lead` (the first 200 characters unless noted):
   - `pushback` (strong): `^(no|nope|nah)\b` followed by more words; `(that'?s|this is|it'?s|you'?re)
     (not right|wrong|incorrect)`; `wrong (file|folder|directory|branch|repo|place|path|one|approach|
     way|function|method|component|command|version|port|table|column|endpoint|url|model|project)`;
     `i (meant|said|asked( you)?( for| to)?|wanted|told you|didn'?t (ask|want|say|tell))`;
     `you (didn'?t|did not|forgot|missed|broke|deleted|removed|overwrote|ignored|skipped|changed|never)`;
     `why (did|would|are|is) you`; `not what i`; `not (that|this) one`; `the other (one|file)`.
   - `undo` (strong): `undo|revert|roll ?back|put (it|that|them) back|change (it|that) back|restore`.
     Counts only when the previous turn made at least one tool call; otherwise "revert X" is a new
     task.
   - `restated` (strong): `i already told you|as i said|like i said|i just said|how many times|again[,!]`.
   - `frustration` (strong): `wtf|what the hell|what are you doing|seriously\?|come on|are you kidding`.
   - `directive` (medium): `(stop|don'?t|do not|never) (add|change|touch|use|create|delete|run|commit|
     push|modify|edit|write|rewrite|remove|install|refactor|rename)\w*`, only when the previous turn made
     a tool call. A leading `stop` counts only when followed by punctuation, the end, `it|that|this|
     now|right|there|doing|you`, or a gerund ("stop using npm"), so "stop the runner and merge" is an
     instruction; the middle word may not be an article ("stop the running server"). When `directive`
     is the only signal it must sit in the first sentence: "Add the endpoint. Don't touch the
     schema." is a new task with a constraint.
   - `still_broken` (medium): `still (not|broken|failing|fails|wrong|the same)|doesn'?t work|didn'?t
     work|not working|(it|that|you) broke|not fixed`.
   - `instead` (medium): `instead of|rather than|should(n'?t)? have`.
   - `interrupt_redirect` (medium): the previous turn was interrupted and the prompt is not a pure
     resume, has at least 2 words, is not additive (`also`, `as well`, `btw`, `too`, `after you're
     done`), and is not a question (`what|which|how|does|is|explain|tell me ...`): those add to the
     task or ask about it instead of redirecting it.
   - `fix_after_failure` (medium): the previous turn has `failed_actions >= 1` and either at least 3
     failed calls or failed calls making up at least a quarter of its tool calls (one failed probe in
     a long, successful turn is normal work), the prompt has at most 40 words and does not open as a
     question, it is not a generic nudge (`try again|retry|fix it|fix (the|these|those) errors|keep
     going`), and it carries a fix cue: inline code or a command word (`npm|pnpm|yarn|pip|uv|python|
     pytest|git|gh|docker|make|cargo|go|node|npx|bun`), a path, or `use|try|run|you need to|the
     (issue|problem|error|fix) is|it'?s because|should be|it'?s in|look in|activate|install|set`.
   - `hesitation` (weak): `^(actually|wait|hmm|hold on|hang on)\b`; and a bare `no`/`nope`.
4. Strength is the strongest signal's level. Downgrade one level when the agent's previous turn
   ended with a question (`turn.after_question`): the user is redirecting a proposal, not undoing
   work. Drop the turn when its strength is `weak` and the previous turn had no tool call, no failed
   call, and no interruption, and drop a bare decline (`no`, `no thanks`, `nope`, ≤ 5 words) after a
   question. Exclude `no problem|no worries|not bad|no rush|no need to worry`. Drop a correction that
   is not strong and opens with praise (`great|perfect|thanks|lgtm|looks good ...`): "great, merge
   it" is a go-ahead, while "great, but you forgot the tests" keeps its strong pushback.
5. A kept turn becomes a `Correction(session, turn, previous=turns[t-1], signals, strength,
   text=turn.text)`.
6. Episodes: walk each session's corrections in order; a correction whose turn index is the previous
   correction's index + 1 joins that episode, otherwise it starts a new one. `ask_turn` is the turn
   before the episode's first correction (the task the agent was doing), looking back past bare
   resumes such as "continue" for up to 5 turns so the ask is the real task. `prompts_lost` = number of
   corrections; `tool_calls_redone` = sum of `previous.tool_calls`; `failed_actions` = sum of
   `previous.failed_actions`; `interrupted` = any `previous.interrupted`.
7. `overall`: sessions and prompts by harness, corrections, episodes, sessions with a correction,
   corrections per 100 prompts (0 when there are no prompts), counts by signal and by strength, and
   `templates_skipped`.

These suppressions were tuned on this machine's 500 real sessions: before them, about 30 of 105
detected corrections were praise or additive notes after an interruption, a single failed probe in a
long turn, or a typo fix; after them, 74 remained.

#### Edge Cases & Error Handling
- A pasted log containing "no," deep inside is not a correction, because only the lead is matched.
- A quoted agent reply (`> you said ...`) is stripped before matching.
- A correction at the session's last turn still counts; its "after" actions are empty.
- A session with zero turns produces nothing and no division by zero.

### 6.3 Scenario builder

**File(s):** `scripts/correctionkit/scenarios.py`
**Type:** New file

#### What it does
`MistakeKinds` classifies an episode; `ScenarioBuilder` clusters episodes into `Scenario` objects
and ranks them.

#### Interface / API
```python
class MistakeKinds:
    ORDER = ("destructive_action", "stalled", "false_done", "ask_first", "unrequested_change", "wrong_target",
             "wrong_tool", "environment_fix", "convention", "misread_intent", "rejected_approach", "off_track", "other")
    LABELS: dict                                            # kind -> plain-English label
    def classify(self, episode) -> str: ...
class EpisodeFeatures:                                       # terms, tools, file_types, project, harness
    @classmethod
    def of(cls, episode) -> "EpisodeFeatures": ...
class Scenario:
    kind; episodes; terms (Counter); tools (Counter); file_types (Counter); projects (Counter); harnesses (Counter)
    corrections; sessions; prompts_lost; tool_calls_redone; strong; score; first_ts; last_ts; label
class ScenarioBuilder:
    def __init__(self, threshold: float = 0.45) -> None: ...
    def build(self, episodes: list) -> list: ...             # Scenarios, best first
```

#### Logic / Algorithm
1. **Kind.** The first kind in `ORDER` whose rule matches the episode's correction texts (and, for
   two kinds, its corrected actions):
   - `destructive_action`: `deleted|removed|wiped|overwrote|lost (my|the|all)|force.?push|reset --hard|
     dropped|nuked`, or a corrected action matching `rm -rf|git reset --hard|git push (-f|--force)|
     git checkout --|git clean|drop table|truncate` together with a strong signal.
   - `stalled`: `stalled|hung|hangs|stuck|froze|timed out|not responding|taking too long|never
     finished`: the agent ran a command that blocked until the user stepped in (on real data the
     most common scenario: "continue, tool stalled" after an interrupted `npm run dev`).
   - `false_done`: `still_broken`, or `you said .{0,40}(done|fixed|works|passing)|tests? (fail|are
     failing|still)|did(n'?t| not) (run|test|verify|check)|(is|it'?s) not (done|working|fixed)`.
   - `ask_first`: `(don'?t|do not|never) (commit|push|merge|deploy|publish|release|open a pr)|ask (me )?
     (before|first)|without asking|i didn'?t tell you to|wait for me`.
   - `unrequested_change`: `undo`, or `didn'?t ask|don'?t (change|touch|modify|refactor|rewrite)|leave
     .{0,30}(alone|as is)|only (change|touch|edit)|why did you (change|add|remove|delete|rewrite|
     refactor|rename|create)`.
   - `wrong_target`: `wrong (file|folder|directory|branch|repo|place|path|function|method|component|
     table|column|endpoint|url|project|worktree)|not (that|this) (file|one|function|branch)|the other
     (file|one)`.
   - `wrong_tool`: `use \S+ (not|instead of) \S+|instead of (npm|yarn|pip|...)`, a known tool word next
     to `use|don'?t use|not`, or `fix_after_failure` whose prompt carries a command.
   - `environment_fix`: `fix_after_failure` with `venv|virtualenv|activate|port|env|environment
     variable|path|install|credential|login|token|permission|sudo|docker|node version|python version`.
   - `convention`: `comment|naming|style|format|lint|type hint|docstring|emoji|indent|convention|like the
     other|follow (the|our)|consistent|folder structure`.
   - `misread_intent`: `i (meant|said|asked|wanted)|not what i|misunderstood|i was asking|re-?read|
     didn'?t answer|my (actual|main) question`.
   - `rejected_approach`: `approach|design|interface|abstraction|architecture|don'?t like|cleaner|
     simpler|over-engineered|too complex|hacky|duplicate (logic|code)`.
   - `off_track`: an `interrupt_redirect` episode with none of the above.
   - `other`: everything else.
2. **Features.** Terms are stemmed content words of the correction texts (stopwords and generic
   correction words such as `wrong`, `meant`, `undo` removed, length 3 to 24; email addresses, URLs,
   numbers, and key-like words are dropped so a secret can never become a scenario term). Tools are known tool words
   in the correction texts plus the first word of each `Bash:` action. File types are extensions in
   the corrected actions and in paths inside the correction text. Project is the session's
   `project_name()`.
3. **Cluster.** Episodes are taken newest first. For each, compute against every scenario of the same
   kind: `sim = jaccard(terms, scenario.top_terms(25)) + 0.30·[tools overlap] + 0.15·[file types overlap]
   + 0.10·[same project]`. Join the best scenario when `sim >= 0.45`, else start a new one. Merge the
   episode's counters into the scenario.
4. **Rank.** `score = 2·sessions + episodes + corrections + projects + min(tool_calls_redone, 50)/10 +
   0.5·strong + (3 if kind == destructive_action)`. Sort by score, then by the newest correction.
5. **Label.** `"<kind label>: <top 3 tools or file types> <top 3 terms>"`, redacted later, e.g.
   `"Used the wrong tool or command: pnpm, npm - install, lockfile, workspace"`.

#### Edge Cases & Error Handling
- Zero episodes: an empty list.
- Every episode in its own scenario (nothing similar): still ranked; the digest shows the top ones in
  full.
- Ties: newest first, so the output is deterministic for a fixed input.

### 6.4 Skill inventory

**File(s):** `scripts/correctionkit/inventory.py`
**Type:** New file

#### What it does
`SkillInventory` lists existing skills so ideas can update them instead of duplicating them.

#### Interface / API
```python
class SkillInventory:
    def __init__(self, home: Path, claude_dirs: list, codex_home: Path | None) -> None: ...
    def scan(self, project_folders: list) -> list: ...   # [{"name", "description", "scope", "path"}], <= 60
```

#### Logic / Algorithm
1. Roots in order: each Claude config folder and the OpenClaude folder read (`<dir>/skills`, scope
   `claude personal`),
   `~/.agents/skills` (`agents personal`), `<codex home>/skills` (`codex personal`), then for up to 10
   project folders of the full scenarios that exist on disk: `.claude/skills`, `.agents/skills`,
   `.opencode/skills` (`claude project <name>` and so on).
2. For each `<root>/*/SKILL.md`, read at most 8 KB, parse the frontmatter between the first two `---`
   lines, and take `name` (else the folder name) and `description` (single line, quoted, or a folded
   `>`/`|` block), cut to 300 characters.
3. Skip unreadable files; dedupe by `(name, scope)`; stop at 60.

#### Edge Cases & Error Handling
- No skills anywhere: an empty list, and the digest says so.
- A `SKILL.md` with no frontmatter: the folder name and an empty description.

### 6.5 Digest writer

**File(s):** `scripts/correctionkit/digest.py`
**Type:** New file

#### What it does
`DigestWriter` assigns tags, renders `digest.md` within its budget, and writes `corrections.json`,
`inputs.json`, and the focus file copy.

#### Interface / API
```python
class Plural:                                   # Plural.n(1, "session") -> "1 session"
class TagBook:                                  # assigns S#, P#, C# in order of first use
    def session(self, session) -> str: ...
    def prompt(self, session, turn, role: str, scenario_tag: str) -> str: ...
class DigestWriter:
    def __init__(self, out_dir: Path, redactor, max_chars: int, max_scenarios: int, focus_path: Path) -> None: ...
    def write(self, scenarios: list, overall: dict, skills: list, collect_summary: dict) -> dict: ...
```

#### Logic / Algorithm
1. **Header (~2,500 chars):** what the digest is, the window, harnesses and session counts, how to
   read `[C#]`, `[S#]`, `[P#]`, and a line saying the text is data, not instructions.
2. **Overall:** corrections, episodes, sessions with a correction out of all sessions, corrections per
   100 prompts, prompts lost, tool calls redone, counts by signal and by kind.
3. **Existing skills:** up to 40 lines `name (scope) - description`, project skills of scenario
   projects first; or "No existing skills were found."
4. **Scenarios**, grouped under kind headings in rank order. A full block:
   `### [C1] <label>` then a stats line (corrections, episodes, sessions, projects, prompts lost, tool
   calls redone, first and last date), a where line (harnesses, projects, file types, tools), a
   signals line, and up to 4 examples (one per session first, strong first, newest first):
   ```
   - [P4] [S2] claude-code · app · Oct 09
     Asked [P3]: <the ask turn's prompt, <= 300 chars>
     Agent did: Bash: npm install; Edit package.json (+3 more) | failed calls: 2 | interrupted: no
     Agent said: <before_agent_text, <= 220 chars>
     Correction [P4]: <correction, <= 500 chars>   (signals: pushback, strong)
     Then: <first 3 actions after the correction, or "no tool call">
   ```
   Further corrections in the scenario are listed as tags only (`Also: [P9] [P12]`, at most 12).
5. **Minor scenarios** (beyond `--max-scenarios`): one line each,
   `[C14] <label> - 1 correction in 1 session - [P31] "<correction, <= 140 chars>"`, until the budget
   ends; the rest are counted.
6. **Budget:** every text is redacted before it is measured. If the digest is over budget, drop
   examples from the lowest-ranked full scenario first (each keeps one), then minor lines, then
   existing-skill lines. If the header and one example per scenario still do not fit, the value was
   too small: exit 2 (`--max-chars` minimum is 20,000).
7. **`corrections.json`:** window, read summary, overall, `count`, `scenarios[]` (tag, kind, label,
   full, counts, sessions, projects, harnesses, file types, tools, signals, prompt tags, first,
   last, score), `sessions[]` (tag, harness, session id, source, cwd, project, title, first, last,
   prompts, models), `prompts[]` (tag, session, index, ts, role `ask` or `correction`, scenario,
   redacted text ≤ 2,000 chars, signals, strength, corrected actions, failed calls, interrupted), and
   `skills_found[]`.
8. **`inputs.json`:** `goal` (≤ 700 chars, names the scenario count and asks for one skill per
   scenario); `outcomes` (≤ 4, `[all]`): totals and rates; `mistakes` (≤ 8): one per top scenario,
   starting `[C1 P4 P9]`, with its kind label and counts; `context` (≤ 4): harnesses and models
   `[all]`, existing skills `[setup]`; `capability` (≤ 2): where skills can live and that they load
   by description; `success`, `constraint`, `avoid`: empty; `project`: `null`.
9. **Focus file copy:** byte copy of `references/skills-from-corrections.md` to the output folder;
   exit 2 if it is over 20,000 bytes.
10. Every count phrase goes through `Plural.n(count, noun)` ("1 correction", "2 corrections"), which
    the report and the scaffolder share.

#### Edge Cases & Error Handling
- One scenario only: `count` is 2 (the CLI minimum); the focus file says the count is a maximum.
- A prompt over 2,000 characters is cut in `corrections.json`; a huge paste shows its first 300
  characters and its length in the digest.

### 6.6 Suggest configuration

**File(s):** `scripts/correctionkit/suggest.py`
**Type:** New file

#### What it does
`CorrectionSuggestConfig` holds this skill's flag kinds, caps, tag pattern, categories, count rule,
and file names, and builds the validator and the argv from `sessionkit.runner`.

#### Interface / API
```python
class CorrectionSuggestConfig:
    FLAG_KINDS = OrderedDict([("outcomes", ("--outcomes", 4)), ("mistakes", ("--mistakes", 8)),
        ("context", ("--context", 4)), ("capability", ("--capability", 2)), ("success", ("--success", 3)),
        ("constraint", ("--constraint", 3)), ("avoid", ("--avoid", 3))])
    TAGGED = {"outcomes", "mistakes", "context"}
    CATEGORIES = ("risk_prevention", "leverage", "learning", "verification")
    DIGEST = "digest.md"; FOCUS = "skills-from-corrections.md"; PROJECT = "turn-corrections-into-skills"
    def known_tags(self, index: dict) -> set: ...          # C#, S#, P#, plus "all" and "setup"
    def count(self, index: dict) -> int: ...               # full scenarios clamped to 2..12
    def validator(self, index: dict, redactor) -> InputsValidator: ...
    def argv(self, exe: str, inputs: dict, index: dict, dry_run: bool) -> list: ...
```

#### Logic / Algorithm
1. The tag pattern accepts a leading `[` + one or more of `C\d+`, `P\d+`, `S\d+`, `all`, `setup`
   separated by spaces + `]`.
2. `argv` calls `SuggestArgv.build(exe, goal, CATEGORIES, count, FLAG_KINDS, values, [DIGEST],
   [FOCUS], project, dry_run, horizon="any")`, which yields
   `<exe> --json --no-input agents suggest run --goal ... --category ... --count N --horizon any
   <typed flags> --files digest.md --instructions skills-from-corrections.md [--project KEY]
   --max-total-tokens 400000 [--dry-run]`.
3. `SuggestCommand` checks the argv is under 24,000 characters, then uses `PaidRunGuard` for
   `--paid`, `CliInvoker.run(argv, out_dir, "result.json" | "dry-run.json")`, and
   `RunReporter.summarize` (which adds `estimated_cost_usd` via `CostEstimator`).

#### Edge Cases & Error Handling
- An untagged `mistakes` value, an unknown tag, a 9th `mistakes` value, a value over 500 characters,
  an unknown key: listed problems, exit 2, nothing run.
- A value with a secret: redacted before it reaches argv.

### 6.7 Idea report

**File(s):** `scripts/correctionkit/report.py`
**Type:** New file

#### What it does
`SkillIdeaParser` extracts the skill specification from one idea; `IdeaReporter` maps every idea to
its evidence, checks it, and renders `report.md`.

#### Interface / API
```python
class SkillSpec:      # slug, slug_source, title, description, got_wrong, correct, rules, steps, where, evals, problems
class SkillIdeaParser:
    SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    def parse(self, idea: dict) -> SkillSpec: ...
class IdeaReporter:
    def __init__(self, out_dir: Path, redactor) -> None: ...   # re-redacts every quoted string
    def build(self) -> dict: ...      # writes report.md, returns the JSON summary
```

#### Logic / Algorithm
1. Text pool per idea: `title`, `summary`, `first_action`, `why_now`, `expected_benefit`,
   `completion_criteria`, `suggested_actions`, `considerations`, `decision_points`, and
   `handoff.evidence[].content`.
2. Tags: every `[...]` group, split on spaces, keep `C\d+`, `P\d+`, `S\d+`. Resolve against
   `corrections.json`; list unknown ones. Scenarios cited = `C` tags plus the scenarios of cited
   prompts. `grounded` = at least one valid `C` or `P` tag.
3. Slug: the first backticked token in `title` that matches `SLUG_RE`; else a kebab form of the
   title (`slug_source: "derived"`). Problems: longer than 64, contains `claude` or `anthropic`,
   duplicate across ideas, same name as an existing skill (`updates_existing`).
4. Description: the text after `Description:` up to `Got wrong:` (else the first sentence starting
   `Use when`). Problems: missing, not starting with `Use when`, over 1,024 characters, containing
   `<` or `>`.
5. `got_wrong` and `correct`: the labelled parts of `summary`. Rules: `suggested_actions` items that
   start `Rule:`; steps: the rest, prefix removed.
6. Evidence per idea: each cited correction prompt with its redacted text (≤ 300 chars), corrected
   actions, date, harness, project. How often: corrections, episodes, sessions, projects, prompts
   lost, tool calls redone, first and last date, summed over cited scenarios (each counted once).
7. `uncovered`: full scenarios no grounded idea cites.
8. `report.md` under `# Proposed skills from your corrections`: per grounded idea in rank order
   (`## 1. `slug` - title (idea-001)`): description, how often, what the model got wrong with the
   quoted corrections, correct behavior, rules, steps, where it applies (keeping `Trigger:` and
   `Do not fire:` labels), evaluation (`completion_criteria`), and problems. Ungrounded ideas follow
   under `# Without evidence from your sessions` with a warning that they are not scaffolded, then
   `# Scenarios with no proposed skill`. The JSON summary lists `ideas`, `ungrounded`,
   `uncovered_scenarios`, and `warnings`.

#### Edge Cases & Error Handling
- `result.json` missing or not `suggestions.result`: exit 2.
- An idea with no labels in `summary`: `description` falls back to the first `Use when` sentence,
  `got_wrong` to the summary, and a problem is listed.
- Zero ideas: a report that says so with `status`, `stop_reason`, and `warnings`.

### 6.8 Scaffolder

**File(s):** `scripts/correctionkit/scaffold.py`
**Type:** New file

#### What it does
`SkillScaffolder` renders a draft `SKILL.md` for one idea and, only when asked to, writes it.

#### Interface / API
```python
class SkillScaffolder:
    def __init__(self, out_dir: Path, redactor) -> None: ...
    def render(self, idea_id: str, slug: str | None, description: str | None) -> dict: ...   # {slug, text, problems, draft_path}
    def write(self, rendered: dict, dest_root: Path) -> Path: ...                              # raises ScaffoldRefusal
```

#### Logic / Algorithm
1. Load the idea and its `SkillSpec` (6.7). Overrides from `--slug` and `--description` win.
2. Refuse (problems) when the idea is ungrounded, the slug is invalid, or the description is
   missing, over 1,024 characters, does not start with `Use when`, or contains `<`/`>`.
3. Render, stripping every `[C#]`/`[P#]`/`[S#]` tag (a tag that is the object of `in`, `from`, `like`,
   `see`, `as in` becomes "an earlier session", so "the request in [P2]" reads "the request in an
   earlier session"), capitalizing the first letter, and redacting again:
   ```markdown
   ---
   name: <slug>
   description: <description, quoted for YAML when it contains ": " or starts with a quote>
   ---

   # <plain title>

   <got wrong, as "Why this skill exists: ...">

   ## Do this instead
   <correct>

   ## Rules
   - <Rule: items>

   ## Steps
   1. <Step: items>

   ## Done when
   <completion_criteria>

   ## Where it applies
   - <considerations that name a harness, project, file type, folder, or tool; at most 6>

   <!-- Drafted by turn-corrections-into-skills on <date> from <n> corrections in <m> sessions (<idea id>). Review it before you commit it. -->
   ```
4. Save the draft to `<out_dir>/drafts/<slug>/SKILL.md` (always allowed: it is the skill's own
   folder).
5. `write` (only with `--write --dest`): expand `~`, resolve the path, target
   `<dest>/<slug>/SKILL.md`; refuse when that file exists, when `dest` is inside a session store
   (`.claude/projects`, `.codex/sessions`, `.codex/archived_sessions`, `opencode/storage`,
   `.gemini/tmp`, `.qwen/tmp`, `.openclaude/projects`, `.factory/sessions`, `.pi/agent`), or when the
   render had problems; create the folder and
   write with LF line endings.

#### Edge Cases & Error Handling
- `--write` without `--dest`, or `--dest` without `--write`: usage error, exit 2.
- An existing target: exit 2 with the path; nothing is changed.
- A description with a colon: written as a double-quoted YAML string with `"` and `\` escaped.

### 6.9 Feedback command

**File(s):** `scripts/corrections.py` (`FeedbackCommand`)
**Type:** New

Copies the idea's exact `title` from `result.json` into
`vidbyte-cli --json --no-input agents suggest feedback accept|reject --project <key> --suggestion
<title> [--reason <redacted>]`, with the project from `--project`, else `inputs.json`, else
`turn-corrections-into-skills`. An unknown idea id is exit 2.

### 6.10 Focus file

**File(s):** `skills/turn-corrections-into-skills/references/skills-from-corrections.md`
**Type:** New file (about 12 KB, under the 20,000-byte limit)

#### What it does
Developer instructions appended to the generator's and every critic's system prompt. Sections:
1. The situation: a developer's own corrections, clustered into `[C#]` scenarios; they asked for
   one proposed agent skill per scenario.
2. One idea = one skill, and the exact field mapping (title with the slug in backticks; summary with
   `Description:`, `Got wrong:`, `Correct:`; `Rule:`/`Step:` suggested actions; how often in
   `why_now`; where in `considerations`; evaluation scenarios in `completion_criteria`; install
   location in `first_action`).
3. How to write the skill: slug rules (lowercase, hyphens, ≤ 64, no `claude`/`anthropic`, named
   after the task), a description that fires when the task starts ("Use when" + concrete triggers:
   phrasings, file types, tools, folders; ≤ 1,024, aim for ≤ 400; no angle brackets), only what the
   agent does not already know, a default with an escape hatch, low freedom for destructive or
   fragile operations, a check before "done", evaluation scenarios replaying the cited tasks.
4. Evidence rules: cite the scenario `[C#]` and at least two correction `[P#]` (one if the scenario
   has one); quote at most 25 words verbatim; copy counts from the scenario header; ideas without
   tagged evidence are not allowed.
5. Existing skills: update an existing skill instead of duplicating it, and say why it did not fire.
6. What is not a model error (a change of mind, information the agent could not have had) and what
   not to propose (generic advice, prompting critique, tone, anything that weakens permission
   prompts, sandboxing, or confirmation before destructive actions).
7. Ranking and merging rules, and the critic checklist.
8. One worked example idea with fictional tags.

### 6.11 Public SKILL.md and references

**File(s):** `skills/turn-corrections-into-skills/SKILL.md`, `references/setup.md`,
`references/troubleshooting.md`
**Type:** New files

`SKILL.md` follows the `write-agent-skill` section order: Title, The problem, How vidbyte-cli solves
it, Goal and done-when, Preflight, Gather inputs, Steps, CLI commands, Cost and consent, Run, Read the
result, Deliver, Troubleshooting, Guardrails. Frontmatter:

```yaml
name: turn-corrections-into-skills
description: Use when the user wants their coding agents to stop making the mistakes they had to correct - ... Drives the Vidbyte Suggestion Agent through vidbyte-cli.
metadata:
  agent: "suggestion-agent"
  commands: "agents suggest project list, agents suggest project create, agents suggest run, agents suggest handoff, agents suggest feedback accept, agents suggest feedback reject"
  cost: "Model usage from the Vidbyte balance: gpt-6-sol at $2 / $10 per million input / output tokens + 2%, capped at 400,000 tokens per run"
  writes-files: "true"
  min-cli-version: "0.3.0"
  status: stable
```

Only skills published on `main` are named in the body (`stop-repeating-yourself`,
`pick-up-where-my-sessions-left-off`), per the field guide. `references/setup.md` and
`references/troubleshooting.md` are byte copies of blobs `8fb4e3cf` and `cb9656bc`.

### 6.12 Shared session reader

**File(s):** `skills/turn-corrections-into-skills/scripts/sessionkit/*`
**Type:** New files (byte copy; never edited here)

Copied from `skills/improve-my-prompting/scripts/sessionkit/` at commit
`b72fe1873b58cee63de27fad952c35bbfb0cb88c` (sessionkit v4) of branch `skill/improve-my-prompting` (ten
files: `__init__`, `model`, `redaction`, `claude_code`, `codex`, `opencode`, `gemini`,
`message_logs`, `collector`, `runner`), and every file is checked with `git hash-object` against that commit. Used: `model` (`Session`, `Turn`, `Clock`, `TextTools`),
`redaction.Redactor`, `collector` (`CollectOptions`, `SessionCollector`), `runner`
(`InputsValidator`, `SuggestArgv`, `PaidRunGuard`, `CliInvoker`, `RunReporter`). v3 reports a store
that cannot be opened as a `read[]` entry with `"status": "error"` and a redacted `error`; v4 adds
OpenClaude (`~/.openclaude`, Claude Code format), Factory Droid (`~/.factory/sessions`), and Pi
(`~/.pi/agent/sessions`), and `--only <harness>`.

### 6.13 Website listing

**File(s):** `next-app/constants/agent-skills/turn-corrections-into-skills/catalog.js` (new) and
`next-app/constants/agent-skills/catalog.js` (modified) in cerredz/Vidbyte
**Type:** New + Modified

A `defineAgentSkill` entry: `status: "coming-soon"`, `minCliVersion: "0.3.0"`, `writesFiles: true`,
`addedOn: "2026-10-13"`, `agents: ["suggestion-agent"]`, `topics: ["documentation", "debugging"]`,
`tags: ["Agent skills", "SKILL.md", "Claude Code", "Codex", "OpenCode"]`, the six
`vidbyte-cli agents suggest ...` commands, and the frontmatter description verbatim. One import line
in the alphabetical block and one `ENTRIES` line right after `STOP_REPEATING_YOURSELF`.

---

## 7. Data Model Changes

### 7.1 `corrections.json` (local, never sent)
**Change type:** New
```json
{
  "schema": 1,
  "skill": "turn-corrections-into-skills",
  "window": {"since": "ISO|null", "until": "ISO", "max_sessions": 500},
  "read": [{"harness": "opencode", "root": "~/.local/share/opencode", "status": "read|missing|error", "candidates": 0, "kept": 0}],
  "overall": {"sessions": 0, "prompts": 0, "corrections": 0, "episodes": 0, "sessions_with_corrections": 0,
              "per_100_prompts": 0.0, "by_signal": {}, "by_strength": {}, "by_kind": {}, "by_harness": {},
              "templates_skipped": 0},
  "count": 8,
  "scenarios": [{"tag": "C1", "kind": "wrong_tool", "label": "...", "full": true, "corrections": 5, "episodes": 4,
                 "sessions": ["S1", "S3"], "projects": {"app": 3}, "harnesses": {"claude-code": 3}, "file_types": {".ts": 2},
                 "tools": {"pnpm": 3}, "signals": {"pushback": 4}, "prompts_lost": 6, "tool_calls_redone": 41,
                 "strong": 3, "prompts": ["P3", "P4"], "first": "ISO", "last": "ISO", "score": 17.5}],
  "sessions": [{"tag": "S1", "harness": "claude-code", "session_id": "...", "source": "...", "cwd": "...",
                "project": "app", "title": "...", "first": "ISO", "last": "ISO", "prompts": 12, "models": ["..."]}],
  "prompts": [{"tag": "P4", "session": "S1", "index": 4, "ts": "ISO", "role": "correction", "scenario": "C1",
               "text": "redacted", "signals": ["pushback"], "strength": "strong", "corrected_actions": ["Bash: npm install"],
               "failed_actions": 2, "interrupted": false}],
  "skills_found": [{"name": "...", "scope": "claude personal", "path": "~/.claude/skills/x/SKILL.md", "description": "..."}]
}
```

### 7.2 `inputs.json` (values sent)
**Change type:** New
```json
{"goal": "string <= 700", "outcomes": ["[all] ..."], "mistakes": ["[C1 P4 P9] ..."], "context": ["[all] ...", "[setup] ..."],
 "capability": ["..."], "success": [], "constraint": [], "avoid": [], "project": null}
```

### 7.3 Website entry
**Change type:** New. A frozen `defineAgentSkill` object (6.13). Migration: N/A (static data built at
deploy). Rollback: revert the commit.

---

## 8. API Changes

N/A. No HTTP endpoint changes. The skill calls existing `vidbyte-cli agents suggest run`, `handoff`,
`project list`, `project create`, and `feedback accept|reject` with options present in
`contracts/cli-commands.json`. The script's own command line is specified in 6.1.

---

## 9. File Change Manifest

| Action | File Path | Reason |
|--------|-----------|--------|
| CREATE | `docs/design/turn-corrections-into-skills.md` | This design doc |
| CREATE | `skills/turn-corrections-into-skills/SKILL.md` | The public skill |
| CREATE | `skills/turn-corrections-into-skills/references/setup.md` | Canonical copy (blob 8fb4e3cf) |
| CREATE | `skills/turn-corrections-into-skills/references/troubleshooting.md` | Canonical copy (blob cb9656bc) |
| CREATE | `skills/turn-corrections-into-skills/references/skills-from-corrections.md` | Focus file sent with `--instructions` |
| CREATE | `skills/turn-corrections-into-skills/scripts/corrections.py` | Entry point and command classes |
| CREATE | `skills/turn-corrections-into-skills/scripts/correctionkit/__init__.py` | Package marker |
| CREATE | `skills/turn-corrections-into-skills/scripts/correctionkit/detector.py` | Correction signals and episodes |
| CREATE | `skills/turn-corrections-into-skills/scripts/correctionkit/scenarios.py` | Mistake kinds, clustering, ranking |
| CREATE | `skills/turn-corrections-into-skills/scripts/correctionkit/inventory.py` | Existing skill inventory |
| CREATE | `skills/turn-corrections-into-skills/scripts/correctionkit/digest.py` | digest.md, corrections.json, inputs.json |
| CREATE | `skills/turn-corrections-into-skills/scripts/correctionkit/suggest.py` | Flag kinds, categories, count, argv |
| CREATE | `skills/turn-corrections-into-skills/scripts/correctionkit/report.py` | Idea → evidence mapping and checks |
| CREATE | `skills/turn-corrections-into-skills/scripts/correctionkit/scaffold.py` | SKILL.md drafts and guarded writes |
| CREATE | `skills/turn-corrections-into-skills/scripts/sessionkit/*` | Byte copy of the shared session reader |
| CREATE | `scripts/test-turn-corrections-into-skills.py` (repo root) | Phase 5 verification script |
| CREATE | `next-app/constants/agent-skills/turn-corrections-into-skills/catalog.js` (Vidbyte) | Website listing |
| MODIFY | `next-app/constants/agent-skills/catalog.js` (Vidbyte) | Import and `ENTRIES` line |

---

## 10. Testing Plan

All tests live in `scripts/test-turn-corrections-into-skills.py`. Unit tests build `Session`/`Turn`
objects directly with `sessionkit.model`. Integration tests write fixture stores into a temporary
home and run `corrections.py` in a subprocess with `HOME`/`USERPROFILE`, `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, `XDG_DATA_HOME`, and `WIN_PD_OVERRIDE_LOCAL_APPDATA` pointed into it. Every `suggest`
and `feedback` test passes `--cli <fake>`, a recording `vidbyte-cli` launcher made with pip's
vendored distlib `ScriptMaker` (a real `.exe` on Windows), and the subprocess `PATH` holds no
`vidbyte-cli`, so an ignored `--cli` exits 127. The script runs under the current Python and under
the local Python 3.8.20.

### Unit Tests
- `CorrectionRules` → "no, use the other file" after a turn with an Edit is `pushback`/strong — [Edge Case]
- `CorrectionRules` → a 5,000-char pasted log with "no," at char 3,000 is not a correction — [Silent Failure]
- `CorrectionRules` → "no" after an agent question (≤ 5 words) is dropped; "no, I meant the API route" after a question is kept and downgraded to medium — [Hidden Assumption]
- `CorrectionRules` → "revert the last commit" after a turn with 0 tool calls is not `undo`; after a turn with an Edit it is — [Hidden Assumption]
- `CorrectionRules` → "continue" after an interrupted turn is a resume, not a redirect; "stop, edit api.py instead" is `interrupt_redirect` + `directive` — [Edge Case]
- `CorrectionRules` → "use `pnpm install`" after a turn with 2 failed calls is `fix_after_failure`; "try again" is not — [Silent Failure]
- `CorrectionRules` → "what's wrong with this test?" is not a correction; "that's wrong" is — [Silent Failure]
- `CorrectionRules` → "no problem, thanks" is not a correction — [Edge Case]
- `CorrectionRules` → "> you said X" quoted text with "you didn't" inside the quote is not matched — [Hidden Failure]
- `CorrectionRules` → turn 0, slash commands, and `$skill` invocations are never corrections (`rules_commands_and_turn0`) — [Hidden Assumption]
- `CorrectionRules` → "stop the runner and merge" and "Add the endpoint. Don't touch the schema." are instructions; "don't touch the schema, just add a column" is a `directive` (`rules_not_corrections`) — [Silent Failure]
- `CorrectionRules` → after an interruption, "great, merge into main", an additive "... after you are done too", "next pr*", "516 i mean", and "and which ones do we already have" are not corrections, while "great, but you forgot the tests" stays strong pushback (`rules_praise_additive_selffix`) — [Silent Failure]
- `CorrectionDetector` → a 25+ word prompt seen in two sessions is a template, never a correction, and `templates_skipped` counts it (`detector_templates`) — [Silent Failure]
- `CorrectionDetector` → the ask turn skips a bare "continue" back to the real task (`detector_ask_lookback`) — [Hidden Assumption]
- `CorrectionDetector` → corrections at turns 3, 4, 7 form two episodes (3-4, 7) with `prompts_lost` 2 and 1, and the ask turn of the first is turn 2 — [Silent Failure]
- `CorrectionDetector` → the corrected actions come from the previous turn, not the correcting turn — [Silent Failure]
- `CorrectionDetector` → a session with no turns, and a session with one turn, yield nothing; `overall` has 0 per 100 prompts with 0 prompts — [Edge Case]
- `MistakeKinds` → "you deleted my migrations" is `destructive_action`; a strong pushback after `Bash: git reset --hard` is too — [Edge Case]
- `MistakeKinds` → "it's still failing" is `false_done`; "don't commit yet" is `ask_first`; "wrong branch" is `wrong_target`; "use pnpm not npm" is `wrong_tool`; "I meant the docs page" is `misread_intent` — [Silent Failure]
- `MistakeKinds` → an interrupt-redirect with no other cue is `off_track`; "continue, tool stalled" after an interrupted `npm run dev` is `stalled`; "I don't like this approach" is `rejected_approach` (`kinds_table`) — [Edge Case]
- `ScenarioBuilder` → three pnpm corrections in two projects cluster into one scenario; a "wrong branch" correction stays separate — [Silent Failure]
- `ScenarioBuilder` → two episodes of different kinds never share a scenario even with identical words — [Hidden Assumption]
- `ScenarioBuilder` → ranking: a scenario in 3 sessions outranks one with 3 corrections in 1 session; a destructive singleton gets its bonus — [Silent Failure]
- `ScenarioBuilder` → zero episodes give an empty list — [Edge Case]
- `SkillInventory` → reads name/description from block-scalar and quoted frontmatter, uses the folder name when there is none, skips an unreadable file, and stops at 60 — [Hidden Failure]
- `DigestWriter` → the digest never exceeds `max_chars`, and every full scenario keeps at least one example — [Silent Failure]
- `DigestWriter` → no raw secret, email, or home path from fixtures appears in `digest.md`, `inputs.json`, or `corrections.json` — [Silent Failure]
- `DigestWriter` → every tag in `digest.md` and `inputs.json` exists in `corrections.json`, and tags are unique — [Hidden Failure]
- `DigestWriter` → `max_chars` below 20,000 is a usage error; the focus copy is byte-identical and ≤ 20,000 bytes — [Edge Case]
- `DigestWriter` → `inputs.json` `mistakes` values all start with a tag and number ≤ 8 — [Hidden Assumption]
- `CorrectionSuggestConfig` → `count` is 2 for 1 scenario and 12 for 15; argv has root options first, the 4 categories, `--horizon any`, `--files digest.md`, `--instructions skills-from-corrections.md`, `--max-total-tokens 400000`, `--dry-run` only when asked, `--project` only when set — [Silent Failure]
- `CorrectionSuggestConfig` → a `[C99]` tag and an untagged `mistakes` value are problems; `[setup]` and `[all]` are accepted — [Edge Case]
- `SkillIdeaParser` → parses slug, description, got-wrong, correct, rules, steps from a well-formed idea; derives a slug and lists a problem when the title has none — [Silent Failure]
- `SkillIdeaParser` → flags a slug with `claude`, over 64 chars, and a description over 1,024 chars or with `<` — [Edge Case]
- `IdeaReporter` → an idea citing `[C1 P4]` is grounded with its quoted correction; an idea with no tags is `grounded: false`; `[P99]` is listed as unknown; an uncited full scenario is in `uncovered`; two ideas with one slug are flagged — [Silent Failure]
- `SkillScaffolder` → the draft has valid frontmatter, no `[P#]` tags, "in an earlier session" where a tag was cited, a dated draft comment with singular/plural counts, and the description quoted when it has a colon; `write` refuses an existing file, an ungrounded idea, and a destination inside Pi's session store — [Hidden Failure]

### Integration Tests
- `gather_end_to_end`: over Claude Code and Codex fixtures, exit 0; 5 corrections, 4 episodes, scenarios `C1 wrong_tool` (2 sessions), `C2 false_done`, `C3 off_track`, count 3; the four files; both existing skills found.
- `gather_no_secrets`: the secret and email reach no file; the home path appears only in `corrections.json`'s local fields (`sessions[].cwd`/`source`, `skills_found` paths, `read` roots); a secret never becomes a scenario term.
- `gather_tags`: every tag in `digest.md` and `inputs.json` resolves; the C1 block keeps its correction and actions; counts use the singular for 1.
- `gather_nothing`: no corrections exits 3 `no_corrections`; all eight `--no-*` flags, or `--only pi` on a home without Pi, exit 3 `no_sessions`.
- `gather_more_harnesses`: OpenClaude, Factory Droid, and Pi fixtures are all read; the OpenClaude and Pi corrections are found, the Factory session has none; the OpenClaude skill is listed; the goal names the three harnesses; `--only pi --no-pi` keeps only Pi.
- `gather_bad_opencode`: a non-SQLite `opencode.db` is a `read[]` entry with `"status": "error"` and an error text, and the Claude Code correction is still found.
- `gather_bad_out_and_flags`: a non-empty `--out` and `--max-chars 1000` exit 2.
- `suggest_bad_inputs`: an untagged `mistakes` value exits 2 with `problems`, and the fake is never called.
- `suggest --dry-run` with the fake: exit 0, the recorded argv equals the expected argv, the cwd is the output folder, paths are relative, no `paid-run.txt`.
- `suggest --paid` with the fake: writes `paid-run.txt` and `result.json`; a second `--paid` exits 2 and the fake records no second call (no silent double charge).
- `suggest --paid` where the fake exits 5 with an error JSON on stderr: the summary carries `error.code` and exits 5.
- `suggest` with no `--cli` and no `vidbyte-cli` on `PATH` exits 127 and runs nothing.
- `report` after a fake `result.json` writes `report.md` with each idea's quoted correction and lists uncovered scenarios.
- `scaffold` prints a draft and saves it under `drafts/`; `scaffold --write --dest <tmp>/.claude/skills` writes `<slug>/SKILL.md`; repeating it exits 2 and leaves the file unchanged.
- `feedback --reject --reason` sends the exact idea title, the project from `inputs.json`, and a redacted reason.
- The whole flow runs under Python 3.8.20.

### Manual / QA Test Cases
1. Given this machine's real stores, when `gather` runs with defaults, then it finishes in under 3 minutes and reports counts only (sessions, corrections, scenarios); the out folder is deleted afterwards — [Hidden Failure]
2. Given the real digest, when `suggest --dry-run` runs against the scratch-venv CLI with a blank `VIDBYTE_API_KEY`, then exit 0, `stop_reason: dry_run`, `digest.md` in the manifest, and `--instructions skills-from-corrections.md` in the argv (the dry-run manifest does not list the instructions file) — [Hidden Assumption]
5. Given `--only pi --only factory --only openclaude` on the real stores, when `gather` runs, then only those three harnesses are kept and their corrections are found — [Hidden Assumption]
3. Given `--max-sessions 1`, when `gather` runs, then exactly one session is read and the exit is 0 or 3 — [Edge Case]
4. Given the website worktree, when `npm run build` runs, then the only `[agent-skills]` warning is `fetch-failed` for this slug — [Hidden Assumption]

---

## 11. Dependencies & External Services

| Dependency | Version / Endpoint | Purpose | Risk |
|------------|--------------------|---------|------|
| `vidbyte-cli` | 0.3.0+ (PyPI has 0.2.0) | `agents suggest run`, `handoff`, `project`, `feedback` | Listed `coming-soon` until 0.3.x ships |
| Vidbyte model gateway / OpenAI gpt-6-sol | via `agents suggest run` | Generator and critics | Usage-billed; capped at 400,000 tokens |
| `sessionkit` (byte copy) | `b72fe18` (v4) on `skill/improve-my-prompting` | Session reading, redaction, paid-run plumbing | A bug there is fixed in the parent and re-copied |
| Python | 3.8+ standard library | The helper script | `sqlite3` missing disables OpenCode's database |
| cerredz/Vidbyte `defineAgentSkill`, lint N050 | `main` | Website listing | N050 checks the commands against the vendored contract |

---

## 12. Rollout & Deployment

- No feature flags; a new skill folder and a new catalog entry, not a breaking change.
- Order: the public PR first, then the website PR. The entry is `coming-soon`, so before the public
  file merges the detail page says the skill is being written.
- The public PR depends on the `sessionkit` copy matching the parent's merged version; if the parent
  changes `sessionkit` before merging, re-copy and re-run the tests.
- When vidbyte-cli 0.3.x reaches PyPI, flip `status` to `available` with the other suggestion skills.
- Rollback: revert the website entry first, then the public folder.

---

## 13. Open Questions

- [x] `sessionkit` source commit: `b72fe1873b58cee63de27fad952c35bbfb0cb88c` (v4; parent branch
  `skill/improve-my-prompting`). If the parent changes `sessionkit` before it merges, re-copy.
- [ ] `min-cli-version: "0.3.0"` matches every sibling suggestion skill; usage billing landed after
  0.3.0 and the first PyPI release will be 0.3.2. Move all suggestion skills together (Vidbyte#691)?
- [ ] Should a later version re-run weekly with a state file (like `stop-repeating-yourself`) to show
  which scenarios stopped after their skill was added?
- [ ] English-only signals: add other languages later?
- [ ] Should the website add an `agent-skills` topic instead of reusing `documentation`?

---

## 14. Alternatives Considered

### Alternative 1: Let the model find the corrections in raw transcripts
- What: send whole session slices and ask the model to spot corrections.
- Why rejected: 500 sessions are millions of tokens, far over the 400,000-token cap; secrets would
  leave the machine; local detection is free, repeatable, and gives exact counts the report can
  check.

### Alternative 2: Extend `stop-repeating-yourself`
- What: add a "corrections" pattern kind to `repeats.py`.
- Why rejected: that skill needs repetition across sessions and picks the cheapest fix kind; this one
  needs single costly errors, the agent's actions, eight harnesses, and a skill specification per
  scenario. Different inputs, outputs, and consent steps.

### Alternative 3: Send the focus file with `--files`
- What: attach the skill-writing guidance as citable context.
- Why rejected: ideas could cite the guidance instead of the user's corrections. `--instructions`
  shapes generator and critics while evidence stays the user's own prompts.

### Alternative 4: Write every proposed SKILL.md automatically
- What: create all skill folders after the run.
- Why rejected: violates consent (write-agent-skill section 5) and would add untested skills that
  fire in every session. The user picks skills one by one and approves each path.

### Alternative 5: Use the MCP server
- What: `vidbyte_plan`/`vidbyte_run`.
- Why rejected: it fixes the flags (no `--instructions`, no categories) and adds a paid routing call.
