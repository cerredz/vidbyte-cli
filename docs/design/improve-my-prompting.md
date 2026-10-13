# Design Doc: improve-my-prompting

**Status:** Draft
**Author:** Claude
**Created:** 2026-10-13
**Last Updated:** 2026-10-13

---

## 1. Overview

`improve-my-prompting` is a public agent skill that gives a developer detailed, evidence-backed
critique of how they prompt their coding agents. A bundled, standard-library Python script reads
the developer's most recent sessions (at most 500) from the local session stores of Claude Code,
Codex, OpenCode, Gemini CLI, and Qwen Code. It measures prompting habits that have a visible cost in
those sessions, such as corrections, interrupted turns, go-ahead round trips, openers with no
context, requirements drip-fed over many turns, oversized pastes, and tricks that research has
debunked. It redacts secrets, emails, and home paths, and writes a digest of the developer's own
prompts, each tagged `[P#]` inside a session tagged `[S#]`. One usage-billed
`vidbyte-cli agents suggest run` then sends that digest, plus an evidence-based prompting rubric
passed with `--instructions`, to the Vidbyte Suggestion Agent. The rubric reaches the generator and
every critic. The result is a ranked list of critiques: each names one habit, quotes the
developer's own prompts as evidence, cites the research finding behind the critique, and gives a
before-and-after rewrite of one of those prompts. The skill is then listed on `/agents/skills` in
the Vidbyte website as a `coming-soon` entry, the same status as every other skill that needs
vidbyte-cli 0.3.x.

---

## 2. Goals & Non-Goals

### Goals
- Add `skills/improve-my-prompting/` to cerredz/vidbyte-cli, meeting the `write-agent-skill`
  contract (frontmatter, fixed section order, consent, exact commands, shared references).
- Read local session data from five harnesses: Claude Code (every config folder the user names),
  Codex, OpenCode (its SQLite database and its older JSON store), Gemini CLI, and Qwen Code. By
  default, read the 500 most recent interactive sessions across all of them.
- Measure prompting signals locally and for free. Map each signal to rubric items, and put
  counts, rates, and tagged examples in a redacted digest of at most 100,000 characters (70,000 by
  default).
- Ship `references/prompting-rubric.md`, a condensed and evidence-tagged version of the prompting
  checklist researched in this session (vendor guides from Anthropic, OpenAI, and Google; the
  Wharton Prompting Science Reports; Laban et al.; IFScale; Chroma's context-rot study; Leviathan
  et al.; FormatSpread). Send it to the Suggestion Agent with `--instructions`, so the generator
  and every critic judge against it.
- Make every critique checkable: a `report` step maps each idea back to the prompts it cites and
  the rubric items it names, and flags an idea that cites no prompt.
- Follow the usage-billed suggestion flow every sibling skill uses: a free dry run, consent, one
  paid run with `--max-total-tokens 400000`, a `paid-run.txt` guard, and a cost estimate.
- List the skill on `/agents/skills` with a validated `defineAgentSkill` entry in the Vidbyte repo.

### Non-Goals
- Reading Cursor, GitHub Copilot CLI, Aider, Cline, Roo Code, Kilo Code, Windsurf, Amp, or
  Antigravity sessions. Their local formats are undocumented, binary, cloud-only, or not on this
  machine to verify against. The script reports the folders it found and did not read.
- Critiquing the agents' work, code quality, or token usage. `cut-my-agent-token-usage` covers token
  usage, and `stop-repeating-yourself` covers turning repeated instructions into durable rules.
- Changing any agent settings, instruction files, or transcripts. The skill only reads.
- Non-English signal detection. The keyword signals are English. Prompts in other languages still
  reach the model through the session samples, but the measured rates under-count.
- Publishing a `vidbyte-cli` release, or flipping the listing to `available`. That waits for 0.3.x
  on PyPI, the same as draft Vidbyte#691.

---

## 3. Background & Context

- **Why now.** The user asked for a skill that routes their coding-agent conversations to the
  Suggestion Agent and returns critique of their prompting, built on the research checklist
  compiled earlier in this session. Prompting quality is a problem every coding-agent user has, and
  one they can measure from their own transcripts. Outside evidence shows the pain is broad and has
  a real cost: drip-feeding requirements over several turns cost a 39% average drop across 200,000
  simulated conversations (Laban et al., ICLR 2026). Instruction compliance falls as rules pile up
  (IFScale, NeurIPS 2025). And popular tricks such as threats, tips, and expert personas show no
  reliable gain (Wharton Prompting Science Reports 3 and 4, 2025).
- **What it solves.** Developers do not see their own prompting patterns. The evidence, such as a
  "no, I meant…" correction, an interrupted run, or a "yes, go ahead" round trip, is spread across
  hundreds of transcripts in four incompatible formats. These are too large for a new agent session
  to read, and pasting them would leak secrets.
- **Current state.** Three merged skills already read local transcripts and drive
  `agents suggest run` with a redacted digest: `pick-up-where-my-sessions-left-off`
  (`sessions.py`), `cut-my-agent-token-usage` (`token_usage.py`), and `stop-repeating-yourself`
  (`repeats.py`). They read Claude Code and Codex only. This skill reuses their redaction rules,
  their Claude/Codex parsing logic, and their paid-run flow, and adds OpenCode, Gemini CLI, and Qwen
  Code.
- **Constraints and dependencies.**
  - The CLI surface comes from `contracts/cli-commands.json` on cerredz/vidbyte-cli-internal
    `main` (generated at `f1321179`). `agents suggest run` has `--instructions`, `--files`,
    `--max-total-tokens`, `--horizon`, and `--category`. `--instructions` landed in #156, before the
    0.3.0 cut (#159). Usage billing landed in #162. PyPI still has only 0.2.0, and every suggestion
    skill declares `min-cli-version: "0.3.0"` and is listed `coming-soon`.
  - `--instructions`: at most 8 files and 20,000 bytes in total. The text is appended after the
    fixed system prompt of the generator, every critic, and every extra-compute generator. Paths and
    contents are never echoed.
  - Known traps (memory `vidbyte-skill-cli-constraints`):
    - `--horizon` other than `any` filters silently after the run is billed.
    - `--mistakes`/`--avoid` values that an idea repeats word for word drop that idea, so values
      start with tags.
    - Helper scripts must run on Python 3.8, so never use `Path.write_text(newline=)`.
  - Tests must never reach the real `vidbyte-cli`: the OS keyring holds a live key (memory
    `vidbyte-cli-tests-can-charge`). Every paid-path test passes a recording fake with `--cli`.
  - Shared references must be byte-identical to the canonical copies: `setup.md` blob `8fb4e3cf`,
    `troubleshooting.md` blob `cb9656bc`.

---

## 4. Requirements

### Functional Requirements
1. `prompt_review.py gather` reads sessions from the five harnesses. By default it keeps the 500
   most recent interactive sessions by last activity, across harnesses. `--max-sessions` (1-500)
   lowers the cap, and `--days`/`--since` narrow the window.
2. Claude Code is read from `$CLAUDE_CONFIG_DIR` (else `~/.claude`) plus every `--claude-dir`.
   Other `~/.claude*` folders that hold `projects/` are reported as `other_claude_dirs_not_read`.
3. Codex is read from `--codex-home`, else `$CODEX_HOME`, else `~/.codex`, covering `sessions/**`
   and `archived_sessions/`.
4. OpenCode is read from `--opencode-dir`, else `$XDG_DATA_HOME/opencode`, else
   `~/.local/share/opencode`. Three backends are read, each session once:
   - the v2 tables `session_v2` and `session_message`;
   - the v1 tables `session`, `message`, and `part`;
   - the legacy `storage/` JSON tree.

   The database is opened read-only. If that fails, the script copies the database and its WAL
   files into the output folder, reads the copy, and deletes it.
5. Gemini CLI is read from `--gemini-dir`, else `~/.gemini`, and Qwen Code from `--qwen-dir`, else
   `~/.qwen`. Both use `tmp/*/chats/session-*.json`.
6. Only sessions a person typed into are kept. The script skips SDK sessions, Claude sidechains,
   Codex `exec`/automation/subagent threads, OpenCode child sessions (`parent_id` set), and the
   session it runs in. The current session is detected through `CLAUDE_CODE_SESSION_ID`,
   `CODEX_SESSION_ID`, `CODEX_THREAD_ID`, or `OPENCODE_SESSION_ID`, and `--include-current` keeps
   it. Every skip is counted by reason.
7. For every human prompt, the script records:
   - its text, its time, and its position in the session;
   - the agent's last text before the prompt, and whether the agent had finished or ended with a
     question;
   - how many tool calls the agent made in response, the agent's reply text, and whether the user
     interrupted that response.
8. Injected text is never treated as a human prompt. This covers system reminders, command
   caveats, skill bodies, tool results, Codex environment and AGENTS.md blocks, OpenCode synthetic
   parts, and built-in slash commands without arguments.
9. The signal detector computes the signals in section 6.6 over every kept prompt. Each signal
   carries its rubric item IDs, its count of prompts and sessions, and ranked examples.
10. `digest.md` holds four parts:
    - a header with the window, harnesses, models, and how to read the tags;
    - the overall numbers;
    - one section per signal with tagged examples;
    - session samples that show whole prompt sequences.

    It is redacted and at most `--max-chars` (default 70,000, at most 100,000).
11. `gather` also writes three more files into the output folder:
    - `sessions.json`, the local index that is never sent;
    - `inputs.json`, typed inputs pre-filled from the numbers;
    - `prompting-rubric.md`, a byte-for-byte copy of `references/prompting-rubric.md`, checked to
      be 20,000 bytes or fewer.

    It prints a JSON summary and exits 3 when no prompt was found.
12. `prompt_review.py suggest --dry-run|--paid` validates `inputs.json`: allowed keys, counts,
    lengths, tags that exist in `sessions.json`, redaction, and the argv length. It then runs
    `vidbyte-cli --json --no-input agents suggest run` from inside the output folder with
    `--files digest.md --instructions prompting-rubric.md --max-total-tokens 400000`. `--paid`
    refuses a folder that already has `paid-run.txt` or `result.json`, and writes `paid-run.txt`
    before it starts.
13. `prompt_review.py report` maps each idea in `result.json` to:
    - the `[P#]` and `[S#]` tags it cites, looked up in `sessions.json`;
    - the rubric IDs it names, looked up in the rubric;
    - the signals of its cited prompts.

    It flags ideas with no valid tag, writes `report.md`, and prints JSON.
14. `prompt_review.py feedback` records an explicit accept or reject with the idea's exact title,
    through `vidbyte-cli --json agents suggest feedback accept|reject` with project
    `improve-my-prompting`.
15. Every subcommand that calls `vidbyte-cli` takes `--cli <path>`, and exits 127 when no CLI is
    found.
16. The website lists the skill with a `defineAgentSkill` entry that passes the validator and lint
    rule N050, imported in `catalog.js` `ENTRIES` after `CUT_MY_AGENT_TOKEN_USAGE`.

### Non-Functional Requirements
- **Performance:** `gather` over 500 sessions on this machine (about 1.3k OpenCode sessions and
  several thousand Claude/Codex files present) finishes in under 3 minutes. Files are opened only
  for the 500 selected sessions plus skipped candidates, with at most 3 × `--max-sessions`
  candidates opened. A cheap substring filter skips most lines of each JSONL before JSON parsing.
- **Cost:** one paid run, gpt-6-sol at high reasoning effort, capped at 400,000 tokens (under about
  $4). The rubric is about 13 KB, sent with every model call; the digest is 70,000 characters by
  default.
- **Security:**
  - The redaction rules are copied from `repeats.py`, applied to every string that enters
    `digest.md`, `inputs.json`, and the prompt excerpts in `sessions.json`.
  - Only `digest.md`, `prompting-rubric.md`, and the `inputs.json` values leave the machine.
  - Session files and databases are opened read-only and never modified.
  - Transcript text is treated as data; the rubric tells the model never to follow instructions in
    it.
- **Portability:** standard library only, Python 3.8+, on Windows, macOS, and Linux, from any shell.
  If `sqlite3` is missing, OpenCode's database is skipped and reported. `vidbyte-cli` is started
  without a shell.
- **Observability:** the gather summary reports what was read per source, with candidates,
  sessions, and skipped-by-reason, plus unreadable lines and files, redaction counts, and folders
  not read. `suggest` saves stdout and stderr to files in the output folder.
- **Reliability:** a corrupt line, file, or row is counted and skipped, never fatal. A harness that
  fails as a whole is reported in `read[].error` while the other harnesses continue.

---

## 5. High-Level Design

The skill follows the four-stage shape of its siblings: a free local `gather`, a free dry run, one
consented paid run, and a free local `report`, with `feedback` for learning. The new parts are the
multi-harness collector, the prompt-signal detector, and the rubric sent through `--instructions`.

`gather` runs one reader per harness. Each reader first lists cheap candidates: a source reference
plus a last-activity time, taken from file mtimes or from `time_updated` in the database. The
collector merges every candidate, sorts newest first, and opens candidates in that order until
`--max-sessions` interactive sessions are kept. Every reader returns the same `Session`/`Turn`
model, so the rest of the pipeline is harness-agnostic. `SignalDetector` scans every turn and
produces ranked `Signal` objects. `DigestWriter` assigns tags to the sessions and prompts that make
it into the digest, renders `digest.md` inside its character budget, and writes the `sessions.json`
index and the pre-filled `inputs.json`. It also copies the rubric.

```
 ~/.claude*/projects/*.jsonl ──┐
 ~/.codex/sessions/**.jsonl ───┤        ┌──────────────┐   ┌────────────────┐   ┌──────────────┐
 opencode.db / storage/*.json ─┼──────▶ │SessionCollector│─▶│ SignalDetector │─▶ │ DigestWriter │
 ~/.gemini/tmp/*/chats/*.json ─┤ readers└──────────────┘   └────────────────┘   └──────┬───────┘
 ~/.qwen/tmp/*/chats/*.json ───┘                                                        │
         digest.md + inputs.json + prompting-rubric.md (sent)   sessions.json (local) ◀─┘
                       │
                       ▼  suggest --paid (after consent)
 vidbyte-cli --json --no-input agents suggest run --files digest.md --instructions prompting-rubric.md ...
                       │              (generator + 2 critic rounds on gpt-6-sol through Vidbyte's gateway)
                       ▼
                 result.json ──▶ report (ideas ↔ [P#]/[S#] ↔ rubric IDs) ──▶ report.md ──▶ Deliver / handoff / feedback
```

Key decisions:
- **The rubric goes through `--instructions`, not `--files`.** `--instructions` reaches the
  generator and every critic as developer guidance, which is exactly "the Suggestion Agent gets the
  context of the checklist". A `--files` reference would make the rubric citable evidence, and
  ideas could then cite the rubric instead of the user's prompts. Evidence should be the user's own
  prompts, so the rubric stays in instructions and the digest stays in files.
- **Signals are measured locally, and the model writes the critique.** Heuristics count what can
  be counted, such as corrections, interruptions, and caps. The model sees tagged examples plus
  whole session samples, so it can also catch what heuristics cannot, such as contradictions and a
  missing "why".
- **Categories:** `learning`, `goal_clarification`, `quick_wins`, `leverage`, `simplification`, and
  `risk_prevention`. These cover a skill to build, a missing outcome or done-check, a habit change, a
  reusable template or instruction-file rule, removing noise (personas, caps, threats), and guarding
  agentic runs (autonomy and stop rules).
- **Two packages, one of them shared.** One script would pass 2,000 lines, so the helper is split.
  `scripts/sessionkit/` holds everything that is not about prompting: the session model,
  redaction, the five harness readers, the collector, and the generic suggest-run plumbing (CLI
  invocation, input validation, the paid-run guard, cost estimate). Three sibling skills are being
  built in parallel from the same session data: `turn-corrections-into-skills`,
  `find-unfinished-agent-work`, and `automate-recurring-chores`. Each of them copies `sessionkit/`
  byte for byte from a pinned commit of this branch, because `npx skills add` installs one skill
  folder and a skill cannot import a sibling's files. `scripts/prompting/` holds this skill's own
  logic: signals, digest, and report. `scripts/prompt_review.py` adds its own folder to `sys.path`
  and imports both. Modules are class-based, with one-line signatures and a 1-2 line comment under
  each.

---

## 6. Detailed Design

### 6.1 Entry point

**File(s):** `skills/improve-my-prompting/scripts/prompt_review.py`
**Type:** New file

#### What it does
Parses arguments and dispatches to one command class per subcommand: `GatherCommand`,
`SuggestCommand`, `ReportCommand`, and `FeedbackCommand`. On Windows it reconfigures stdout and
stderr with `errors="backslashreplace"`.

#### Interface / API
```text
prompt_review.py gather   [--max-sessions N<=500] [--days D | --since DATE] [--project-dir DIR]...
                          [--claude-dir DIR]... [--codex-home DIR] [--opencode-dir DIR] [--gemini-dir DIR] [--qwen-dir DIR]
                          [--no-claude] [--no-codex] [--no-opencode] [--no-gemini] [--no-qwen]
                          [--exclude-session ID]... [--include-current] [--max-chars N<=100000] [--out DIR]
prompt_review.py suggest  --out DIR (--dry-run | --paid) [--cli PATH]
prompt_review.py report   --out DIR
prompt_review.py feedback --out DIR --idea idea-NNN (--accept | --reject) [--reason TEXT] [--project KEY] [--cli PATH]
```
Exit codes: 0 ok; 2 usage or validation; 3 no prompts (gather); 127 no CLI; any other code is
vidbyte-cli's.

### 6.2 Session model and helpers

**File(s):** `scripts/sessionkit/model.py`
**Type:** New file (shared with the sibling session skills)

#### What it does
Holds the harness-agnostic `Turn` and `Session` classes plus small shared helpers: timestamp
parsing, flattening content parts, LF file writing, JSON output, and window computation. Fields
that this skill does not need, such as `actions`, `todos`, and `end_state`, are there for the
siblings: corrections need what the agent did, unfinished work needs the final state and the todo
list, and chores need the times and folders.

#### Interface / API
```python
class Turn:  # one human prompt and what the agent did with it
    __slots__ = ("index", "text", "ts", "before_agent_text", "after_finished", "after_question",
                 "reply_text", "tool_calls", "actions", "failed_actions", "interrupted")
    # actions: up to 25 short tool-call descriptions (<= 160 chars) such as "Edit src/app.py", "Bash: npm test"
class Session:
    # harness, session_id, source, cwd, title, first_ts, last_ts, models (Counter), turns,
    # todos [(content, status)], end_state ("finished"|"question"|"interrupted"|"working"|"error"|"unknown"),
    # final_agent_text (tail, <= 1,200 chars), skip_reason
    def __init__(self, harness: str, session_id: str, source: str) -> None: ...
    def add_turn(self, text: str, ts) -> Turn | None: ...      # dedupes Codex double-recording
    def note_agent_text(self, text: str, ts=None) -> None: ...  # reply of the current turn
    def note_action(self, description: str, failed: bool = False) -> None: ...
    def note_interrupt(self) -> None: ...
    def note_turn_end(self, state: str) -> None: ...
    def note_todos(self, todos: list) -> None: ...
class Clock:  # parse_ts, iso, local_day, local_stamp
class TextTools:  # part_text, one_line, word_count, write_lf, read_json_file
```

#### Logic / Algorithm
1. `add_turn` sets `before_agent_text` from the session's running "last agent text" (kept to the
   last 400 characters), and `after_finished`/`after_question` from the last turn-end state. Then it
   appends a new `Turn` and makes it current.
2. `note_agent_text` sets the current turn's `reply_text`, keeping the first 400 and last 400
   characters, and updates the running last agent text.
3. `note_interrupt` marks the current turn as interrupted and resets the turn-end state to
   `interrupted`.

#### Edge Cases & Error Handling
- A prompt before any agent text has `before_agent_text = ""` and `after_finished = None`.
- An identical prompt recorded twice within 10 seconds (Codex records it as both an event and an
  item) is kept once.
- An empty or whitespace-only prompt is ignored.

### 6.3 Redaction

**File(s):** `scripts/sessionkit/redaction.py`
**Type:** New file

#### What it does
`Redactor`, copied from `repeats.py`. It removes private keys, JWTs, provider keys (`sk-`,
`sk_live_`, `ghp_`, `github_pat_`, `xox*`, `AKIA`, `AIza`, `vb_live_`), bearer and basic
credentials, URL credentials, webhook links, `.env` lines, secret-named assignments, emails, home
paths in every spelling (including Claude's `C--Users-me` form), and the login name. It counts
redactions by kind.

#### Interface / API
```python
class Redactor:
    def __init__(self, home: Path) -> None: ...
    def __call__(self, text: object) -> str: ...
    counts: Counter
```

#### Edge Cases & Error Handling
- `None` becomes `""`.
- `git@github.com` is not an email.
- Generic login names (`user`, `admin`, …) are not replaced.

### 6.4 Harness readers

**File(s):** `scripts/sessionkit/claude_code.py`, `codex.py`, `opencode.py`, `gemini.py`
**Type:** New files

#### What it does
Each reader is a class with `name`, `roots`, `candidates(since) -> list[Candidate]`, and
`read(candidate, since, stats) -> Session | None`. A `Candidate` holds
`(harness, ref, last_activity, root)`.

#### Interface / API
```python
class Candidate:  # (harness, ref, last_activity, root, extra)
class ClaudeCodeReader:
    def __init__(self, config_dirs: list) -> None: ...
    def candidates(self, since) -> list: ...       # <dir>/projects/*/*.jsonl by mtime
    def read(self, candidate, since, stats: Counter): ...
class CodexReader:          # <home>/sessions/**/rollout-*.jsonl + <home>/archived_sessions/*.jsonl
class OpenCodeReader:       # opencode.db (v2 then v1) + storage/ legacy JSON
class GeminiReader:         # <root>/tmp/*/chats/session-*.json; harness "gemini-cli" or "qwen-code"
```

#### Logic / Algorithm
- **Claude Code:**
  1. Read lines whose text contains `"type":"user"` or `"type":"assistant"`.
  2. Stop with skip reason `sdk` when `entrypoint` starts with `sdk`, and skip `isSidechain` records.
  3. A user record with `tool_result` parts closes pending tool uses.
  4. User text that starts with `[Request interrupted` marks an interrupt.
  5. Otherwise, `claude_prompt` (from `repeats.py`) decides whether the text is a human prompt.
     Slash commands with arguments become `/cmd args`.
  6. An assistant record adds its `text` parts as agent text, counts its `tool_use` parts, and
     records `message.model`.
  7. When the stop reason is `end_turn` and there is no tool use, the turn has finished.
- **Codex:**
  1. Parse `session_meta`. Skip when the originator contains `sdk`, when the source is `exec` or
     the originator is `codex_exec`, or when the thread is a subagent or automation thread.
  2. `turn_context.model` gives the model.
  3. These events are read: `event_msg` with `user_message` (prompt), `agent_message` with phase
     `final_answer` or `None` (agent text), `task_complete` (finished), and `turn_aborted`
     (interrupt).
  4. Each `response_item` with `function_call` or `custom_tool_call` counts as a tool call.
  5. The `item_completed` variants `UserMessage`/`AgentMessage` are deduped by item id.
  6. Text that starts with `<environment_context>`, `<user_instructions>`, `# AGENTS.md
     instructions`, or `<turn_aborted>` is dropped.
- **OpenCode:**
  1. Candidates: for `session_v2`, select rows where `parent_id IS NULL`, ordered by `time_updated`
     DESC, with a limit. Add v1 `session` rows whose id is absent from v2. Add legacy
     `storage/session/*/*.json` files whose id is in neither table.
  2. v2 read: `session_message` rows ordered by `seq`.
     - `user` gives the prompt, from `data.text`.
     - `assistant` reads its `content[]` items: `text` is agent text, and `tool` counts as a tool
       call.
     - `data.error` naming an abort, or `finish == "aborted"`, marks an interrupt. `finish == "stop"`
       ends the turn.
     - `synthetic`, `system`, `idle`, and the switch types are ignored.
  3. v1 read: `message` rows ordered by `time_created`, with `part` rows per message.
     - User text parts without `synthetic: true` give the prompt.
     - Assistant `text` parts are agent text, and `tool` parts count as tool calls.
     - `error.name == "MessageAbortedError"` marks an interrupt.
  4. The legacy JSON read uses the same shapes from `storage/message/<ses>/*.json` and
     `storage/part/<msg>/*.json`.
  5. The model comes from `modelID` or `model.modelID`, and the folder from `directory`.
- **Gemini CLI / Qwen Code:**
  1. Load one JSON file: `{sessionId, projectHash, startTime, lastUpdated, messages[]}`.
  2. `user` gives the prompt, from a string or a list of parts. A built-in slash command without
     arguments is dropped.
  3. `gemini` gives agent text from `content`, counts `toolCalls[]`, and records `model`.
  4. An `info` message reading `Request cancelled.` marks an interrupt.
  5. The project folder is resolved from `<root>/projects.json` when it maps a folder to this
     `projectHash` (sha256 of the folder path); otherwise it is shown as `gemini project <hash8>`.

#### Edge Cases & Error Handling
- An unreadable line, file, or row increments `stats["unreadable_lines"]` or
  `stats["unreadable_files"]` and is skipped.
- A database that is locked or opens with an error: retry on a copy (FR 4). If that fails too, the
  error is recorded in `read[]` and the other harnesses continue.
- `sqlite3` not importable: OpenCode's database is reported as `skipped: sqlite3 unavailable`. The
  legacy JSON store is still read.
- Schema drift, such as a missing table or column: that backend is skipped and its absence noted.
  The reader never crashes.
- A session whose prompts all fall before `--since` is dropped, and is not counted against the cap.

### 6.5 Session collector

**File(s):** `scripts/sessionkit/collector.py`
**Type:** New file

#### What it does
`SessionCollector` builds the readers from the arguments and environment, merges candidates, and
opens them newest first until it has kept the cap. It applies `--project-dir`, the exclusions, and
the window, and reports per-source statistics plus folders it found but did not read.

#### Interface / API
```python
class SessionCollector:
    def __init__(self, options: CollectOptions, home: Path) -> None: ...
    def collect(self) -> CollectResult: ...    # sessions newest-first + read summary
class CollectOptions:  # max_sessions, since, project_dirs, claude_dirs, codex_home, opencode_dir, gemini_dir, qwen_dir, disabled harnesses, exclude ids
class CollectResult:   # sessions, read (per source), skipped (Counter by reason), unreadable, other_claude_dirs_not_read, not_read
```

#### Logic / Algorithm
1. Build the enabled readers and their roots: flags first, then environment, then defaults.
2. Gather all candidates and sort them by `last_activity` descending.
3. Open candidates in order. Count a skip reason for each rejected session (`sdk`, `subagent`,
   `non_interactive`, `excluded`, `outside_project`, `no_prompts`, `outside_window`). Stop when
   `max_sessions` are kept, or after opening `3 × max_sessions` candidates.
4. Detect `other_claude_dirs_not_read`: `~/.claude*` folders with `projects/` that are not read.
   Detect `not_read`: `~/.cursor/chats`, `~/.copilot/session-state`, and the VS Code globalStorage
   folders of Cline, Roo Code, and Kilo Code.

#### Edge Cases & Error Handling
- No harness folder exists: return no sessions, so `gather` exits 3 with the `read` list.
- A `--max-sessions` value outside 1-500 is a usage error (exit 2).

### 6.6 Prompt signals

**File(s):** `scripts/prompting/signals.py`
**Type:** New file

#### What it does
`SignalDetector` checks every turn against a table of signal definitions. Each definition has
`id`, `title`, `rubric` IDs, `measure` text, and `matches(turn, session) -> bool`. The detector also
computes the overall rates.

| Signal id | Detected when | Rubric |
|---|---|---|
| `correction` | A non-first prompt whose first 100 characters match a correction phrase: `no,`, `not that`, `that's not`, `wrong`, `undo`, `revert`, `I meant`, `I said`, `why did you`, `you didn't`, `stop`, `actually,`, `instead` + verb | RT1, RC1, RX3 |
| `interrupted` | The agent's response to this prompt was interrupted by the user | RT3, RA2, RA4 |
| `go_ahead` | The agent finished with a question, and the prompt is ≤ 8 words of confirmation (`yes`, `ok`, `go ahead`, `do it`, `proceed`, `continue`, `sounds good`, `lgtm`, …) | RA1, RT5 |
| `vague_opener` | First prompt of a session with < 12 words, no path, file name, code, or URL, and either (a) the session later has a correction, interruption, or go-ahead, or (b) it starts with a reference that has no referent (`fix this`, `why is it failing`, `continue`) | RC1, RT1, RT3 |
| `drip_feed` | A session with ≥ 3 non-first prompts that start by adding a requirement (`also`, `and also`, `one more`, `oh and`, `additionally`, `btw`, `forgot to mention`, `make sure it also`) | RC3 |
| `restated` | A prompt that restates something already said (`I already told you`, `as I said`, `like I said`, `again,`, `how many times`, `I just said`, `for the second time`) | RX2, RC2 |
| `frustration` | Exasperation (`wtf`, `what the hell`, `are you serious`, `seriously?`, `useless`, `come on`) | RC4, RX3 |
| `long_session` | A session with ≥ 30 prompts | RC4, RX2 |
| `huge_paste` | A prompt over 8,000 characters | RC2, RS2 |
| `emphasis` | ≥ 3 all-caps words of 4+ letters that are not known acronyms, or `!!!`, or capitalized `MUST`/`NEVER`/`ALWAYS`/`CRITICAL`/`IMPORTANT`/`DO NOT` | RP3 |
| `emotional_stakes` | Tips, payment, threats, or pleading (`tip you`, `$200 tip`, `you'll be fired`, `my job depends`, `or else`, `please please`, `I'm begging`) | RP4 |
| `persona` | `you are / act as / pretend to be` + (`world-class`, `expert`, `senior`, `10x`, `genius`, `elite`, `principal`, `staff`, `top`) | RR1 |
| `step_by_step` | `think step by step`, `step-by-step reasoning`, `let's think`, `take a deep breath` | RQ1, RQ2 |
| `negative_only` | ≥ 3 negative directives (`don't`, `do not`, `never`, `no more`, `stop`) with no `instead` | RP1 |

Overall rates, computed over all kept prompts:
- prompts and sessions by harness, and the models seen;
- the median words per prompt and per opener;
- the share of openers under 12 words, and the share that name a file or path;
- the share of openers that give a reason (`because`, `so that`, `so I can`, `the goal is`,
  `in order to`), and the share that state a check (`test`, `verify`, `make sure … pass`,
  `done when`, `until`, `confirm`);
- prompts per session (median and p90);
- corrections, interruptions, and go-aheads per 100 prompts.

#### Interface / API
```python
class SignalDefinition:  # id, title, rubric, measure, scope ("turn" | "session"), matcher
class SignalHit:         # session, turn (or None for session scope), context dict
class Signal:            # definition, hits, prompt_count, session_count
class SignalDetector:
    def __init__(self, definitions: list | None = None) -> None: ...
    def detect(self, sessions: list) -> list: ...      # Signals ranked by sessions affected, then hits
    def overall(self, sessions: list) -> dict: ...
```

#### Logic / Algorithm
1. For each session and turn, run each turn-scope matcher, then each session-scope matcher. A hit
   stores the turn, the previous turn's text (for corrections, restatements, and frustration, so
   the digest can show what was asked first), and `before_agent_text`.
2. Rank signals by distinct sessions affected, then by hit count. Drop signals with zero hits.
3. Rank each signal's examples:
   - prefer one example per session, the most recent first;
   - for `correction` and `restated`, prefer hits whose previous prompt is under 25 words, because
     those are the clearest cases of an underspecified ask.

#### Edge Cases & Error Handling
- Matchers lower-case the text and look only at the leading 100 or 400 characters, where it
  matters, so a pasted log that contains "no," does not count as a correction.
- Code fences and quoted text are stripped before the emphasis and negative checks, so an error
  message in capitals does not count as shouting.
- Acronyms (`API`, `JSON`, `HTTP`, `TODO`, `README`, `CLI`, `PR`, `URL`, `SQL`, `CSS`, `HTML`,
  `AWS`, `GPU`, …) and tokens with digits or underscores are not shouting.

### 6.7 Digest writer

**File(s):** `scripts/prompting/digest.py`
**Type:** New file

#### What it does
`DigestWriter` assigns tags, renders `digest.md` within its budget, and writes `sessions.json`,
`inputs.json`, and `prompting-rubric.md`.

#### Interface / API
```python
class DigestWriter:
    def __init__(self, out_dir: Path, redactor: Redactor, max_chars: int, rubric_path: Path) -> None: ...
    def write(self, sessions: list, signals: list, overall: dict, window: dict, read_summary: dict) -> dict: ...  # summary for stdout
```

#### Logic / Algorithm
1. **Budget.** The header and overall numbers take about 4,000 characters. Signal sections get
   60% of the rest and session samples 40%. Every example is redacted before it is measured.
2. **Signal sections, in rank order.** Each section has a heading
   `### [sig-<id>] <title> - <n> prompts in <m> sessions (rubric <ids>)`, one line on how it was
   measured, and up to 4 examples. An example has:
   - its tags `[P#] [S#]`, harness, project label, and local date;
   - `Asked:` the earlier prompt, where relevant, up to 400 characters;
   - `Agent:` the agent's last text before the prompt, up to 220 characters;
   - `Prompt:` the prompt itself, up to 500 characters (a huge paste shows its length and its
     first 200 characters).

   When the budget runs short, examples are dropped from the lowest-ranked signal first, and every
   signal keeps at least 1 example.
3. **Session samples.** Up to 10 sessions: the 6 with the most signal hits per prompt, plus up to 4
   typical sessions (median prompt count, most recent) for balance. Each sample shows a header line
   (tag, harness, project, date, prompt count, model, and its signal counts), then up to 12 prompts
   of up to 350 characters, each with markers (`[corrected next]`, `[interrupted]`,
   `[go-ahead]`), and the agent's final reply excerpt.
4. **Tags.** `[S#]` is assigned in order of first appearance, and `[P#]` the same way. Only tagged
   items appear in `sessions.json`.
5. **`sessions.json`**, local only:
   - window, read summary, and overall numbers;
   - `sessions[]` entries: `tag`, `harness`, `session_id`, `source`, `cwd`, `project`, `title`,
     `first`, `last`, `prompts`, `models`;
   - `prompts[]` entries: `tag`, `session`, `index`, `ts`, `text` (redacted, up to 4,000
     characters), `signals`.
6. **`inputs.json`:**
   - `goal`: a template of at most 700 characters, naming the session count and harnesses.
   - `outcomes`: up to 5 `[all]` values with the overall rates.
   - `mistakes`: one value per top signal, at most 8, each starting with up to 3 example `[P#]`
     tags, with its counts and rubric IDs.
   - `context`: up to 3 values on harnesses, models, and generation.
   - `capability`: what the user can change: their prompts, reusable prompt templates and slash
     commands, `CLAUDE.md`/`AGENTS.md`, session hygiene, and model/effort settings.
   - `success`, `constraint`, and `avoid` start empty, and `project` is `null`.
7. **Rubric copy.** Copy the rubric byte for byte, and refuse (exit 2) if it is over 20,000 bytes.

#### Edge Cases & Error Handling
- Sessions with zero signals still appear as typical samples, so a user with clean habits gets a
  "what you already do well" picture, and the rubric tells the model to say so.
- If even the header does not fit `--max-chars`, the value was too small, so it is a usage error
  (exit 2, with a minimum of 20,000).

### 6.8 Suggest runner

**File(s):** `scripts/sessionkit/runner.py` (generic, shared) and `scripts/prompting/suggest.py`
(this skill's flag kinds, categories, and argv)
**Type:** New files

#### What it does
`InputsValidator` validates `inputs.json` against a configuration of flag kinds and caps that each
skill passes in. `CliInvoker` resolves `--cli`, runs vidbyte-cli in the
output folder, and captures its output. `RunReporter` summarizes the result, including
`estimated_cost_usd` at gpt-6-sol rates ($2 / $0.20 cached / $10 per million) ×1.02.

#### Interface / API
```python
class InputsValidator:
    def __init__(self, known_tags: set, redactor: Redactor) -> None: ...
    def validate(self, data: object) -> tuple: ...           # (inputs | None, problems: list)
class CliInvoker:
    def __init__(self, cli: str | None) -> None: ...
    def resolve(self) -> str | None: ...
    def run(self, argv: list, out_dir: Path, stdout_name: str) -> tuple: ...  # (code, stdout_path, stderr_path, error)
class SuggestArgvBuilder:
    def build(self, exe: str, inputs: dict, dry_run: bool) -> list: ...
class RunReporter:
    def summarize(self, code: int, stdout_path: Path, stderr_path: Path, error) -> dict: ...
```

#### Logic / Algorithm
1. Allowed keys are `goal`, `project`, and these flag kinds, each with a cap:
   `outcomes` (5), `mistakes` (8), `context` (4), `capability` (3), `success` (3),
   `constraint` (3), `avoid` (3).
2. `goal` is non-empty and at most 700 characters. Each value is at most 500 characters.
3. Tagged kinds (`outcomes`, `mistakes`, `context`) must start with `[P# …]`, `[S# …]`, or
   `[all]`, and every tag must be in `sessions.json`. `success`, `constraint`, and `avoid` are
   untagged, because they hold the user's own words.
4. Every value is redacted again, and at least one `mistakes` or `outcomes` value is required.
5. The argv:
   ```
   <cli> --json --no-input agents suggest run --goal <goal>
     --category learning --category goal_clarification --category quick_wins
     --category leverage --category simplification --category risk_prevention
     --count 8 --horizon any <repeated typed flags> --files digest.md
     --instructions prompting-rubric.md [--project <key>] --max-total-tokens 400000 [--dry-run]
   ```
6. The argv must be under 24,000 characters.
7. For `--paid`, refuse if `paid-run.txt` or `result.json` exists. Otherwise write
   `paid-run.txt`, then run.

#### Edge Cases & Error Handling
- `--dry-run` and `--paid` together, or neither: exit 2.
- `digest.md` or `prompting-rubric.md` missing from the output folder: exit 2, saying to run
  `gather`.
- CLI not found: exit 127, with JSON naming `references/setup.md` step 1.
- A non-zero CLI exit: the summary carries the CLI's `error` (`code`, `message`, `hint`,
  `retryable`) from the last stderr JSON document.

### 6.9 Idea report

**File(s):** `scripts/prompting/report.py`
**Type:** New file

#### What it does
`IdeaReporter` reads `result.json`, `sessions.json`, and `prompting-rubric.md`, and maps each idea
to its cited prompts, sessions, and rubric items. It writes `report.md` and returns JSON.

#### Interface / API
```python
class RubricIndex:
    def __init__(self, text: str) -> None: ...      # parses "- **RT1** ALL - ..." lines
    def lookup(self, rubric_id: str) -> dict | None: ...
class IdeaReporter:
    def __init__(self, out_dir: Path) -> None: ...
    def build(self) -> dict: ...
```

#### Logic / Algorithm
1. Collect text from the idea's `title`, `summary`, `first_action`, `why_now`,
   `expected_benefit`, `completion_criteria`, and each `handoff.evidence[].content`.
2. Find `P\d+` and `S\d+` tags inside square brackets, and rubric IDs matching
   `\bR[TCSPREQOAX]\d{1,2}\b`.
3. Resolve the prompt tags to their redacted text (shown up to 300 characters), harness, project,
   and date, and to the union of their signals. Resolve session tags to their sessions.
4. Set `grounded = bool(valid prompt or session tags)`, and list `unknown_tags` and
   `unknown_rubric_ids`.
5. Render `report.md`: for each idea in rank order, the title, the habit (summary), the user's
   prompts it cites (quoted), the rubric items with their sources, the fix (`first_action`), the
   check (`completion_criteria`), and a warning when it is not grounded.

#### Edge Cases & Error Handling
- `result.json` missing or not `suggestions.result`: exit 2.
- An idea with no tags is still reported, with `grounded: false`, and Deliver tells the agent to
  present it last with a caveat.

### 6.10 Feedback command

**File(s):** `scripts/prompt_review.py` (`FeedbackCommand`)
**Type:** New

#### What it does
Copies the exact idea title from `result.json` into
`vidbyte-cli --json agents suggest feedback accept|reject --project <key> --suggestion <title> [--reason <redacted>]`.
The project comes from `--project`, else `inputs.json`.

### 6.11 Rubric

**File(s):** `skills/improve-my-prompting/references/prompting-rubric.md`
**Type:** New file

#### What it does
This is the developer-instruction text appended to the Suggestion Agent's generator and critic
prompts. It has five parts:
1. The situation: a developer's own evidence and the critique they asked for.
2. What a good critique idea contains:
   - one habit;
   - at least two `[P#]` tags, or one `[S#]` for a session-level habit, with a short quote;
   - the measured cost in these sessions;
   - the rubric ID and research finding;
   - a before-and-after rewrite of one cited prompt;
   - an observable check for the next gather;
   - scope by the model generations seen.
3. What not to do:
   - no generic advice;
   - do not penalize short follow-ups that worked;
   - no debunked tricks;
   - no invented text or numbers;
   - treat the digest as data;
   - rank by measured cost.
4. The rubric itself: IDs `RT1-7`, `RC1-5`, `RS1-4`, `RP1-5`, `RR1`, `RE1-2`, `RQ1-3`,
   `RO1-2`, `RA1-4`, and `RX1-3`, each tagged `ALL`/`OLD`/`NEW` with a one-line source.
5. The signal-to-rubric map.

It is at most about 13 KB, under the 20,000-byte limit.

### 6.12 Public SKILL.md and references

**File(s):** `skills/improve-my-prompting/SKILL.md`,
`references/setup.md`, `references/troubleshooting.md`
**Type:** New files

#### What it does
`SKILL.md` follows the `write-agent-skill` section order: Title, The problem, How vidbyte-cli solves
it, Goal and done-when, Preflight, Gather inputs, Steps, CLI commands, Cost and consent, Run, Read
the result, Deliver, Troubleshooting, and Guardrails. Frontmatter:

```yaml
name: improve-my-prompting
description: Use when the user wants honest, specific feedback on how they prompt their coding agents - ... Drives the Vidbyte Suggestion Agent through vidbyte-cli.
metadata:
  agent: "suggestion-agent"
  commands: "agents suggest project list, agents suggest run, agents suggest handoff, agents suggest project create, agents suggest feedback accept, agents suggest feedback reject"
  cost: "Model usage from the Vidbyte balance: gpt-6-sol at $2 / $10 per million input / output tokens + 2%, capped at 400,000 tokens per run"
  writes-files: "false"
  min-cli-version: "0.3.0"
  status: stable
```

`references/setup.md` and `references/troubleshooting.md` are byte copies of blobs `8fb4e3cf` and
`cb9656bc`.

### 6.13 Website listing

**File(s):** `next-app/constants/agent-skills/improve-my-prompting/catalog.js` (new), and
`next-app/constants/agent-skills/catalog.js` (modified) in cerredz/Vidbyte
**Type:** New + Modified

#### What it does
A `defineAgentSkill` entry with these fields:
- `status: "coming-soon"`, `minCliVersion: "0.3.0"`, `writesFiles: false`,
  `addedOn: "2026-10-13"`;
- `agents: ["suggestion-agent"]`, `topics: ["planning"]`;
- `tags: ["Prompting", "Claude Code", "Codex", "OpenCode", "Gemini CLI"]`;
- the six `vidbyte-cli agents suggest …` commands;
- a `description` that is the frontmatter description, verbatim.

It is imported and placed after `CUT_MY_AGENT_TOKEN_USAGE` in `ENTRIES`. The sibling skills take
other places: unfinished work after `PICK_UP_WHERE_MY_SESSIONS_LEFT_OFF`, corrections after
`STOP_REPEATING_YOURSELF`, and chores after `TOO_MANY_MEETINGS`. That way the four website PRs do
not edit the same lines.

---

## 7. Data Model Changes

### 7.1 `sessions.json` (local, never sent)
**Change type:** New
```json
{
  "schema": 1,
  "window": {"since": "ISO|null", "until": "ISO", "max_sessions": 500},
  "read": [{"harness": "opencode", "root": "~/.local/share/opencode", "backend": "sqlite-v2", "candidates": 0, "sessions": 0, "error": null}],
  "overall": {"sessions": 0, "prompts": 0, "by_harness": {}, "models": {}, "rates": {}},
  "sessions": [{"tag": "S1", "harness": "claude-code", "session_id": "...", "source": "path or db#id", "cwd": "...", "project": "...", "title": "...", "first": "ISO", "last": "ISO", "prompts": 12, "models": ["..."]}],
  "prompts": [{"tag": "P1", "session": "S1", "index": 0, "ts": "ISO", "text": "redacted", "signals": ["vague_opener"]}]
}
```

### 7.2 `inputs.json` (values sent)
**Change type:** New
```json
{"goal": "string <= 700", "outcomes": ["[all] ..."], "mistakes": ["[P3 P9] ..."], "context": ["[all] ..."],
 "capability": ["..."], "success": [], "constraint": [], "avoid": [], "project": null}
```

### 7.3 Website entry
**Change type:** New. This is a frozen `defineAgentSkill` object (section 6.13). Migration: N/A,
because static data is built at deploy. Rollback: revert the commit.

---

## 8. API Changes

N/A. No HTTP endpoint changes. The skill calls the existing `vidbyte-cli agents suggest run`,
`handoff`, `project list`, `project create`, and `feedback accept|reject` commands with flags that
exist in `contracts/cli-commands.json`. The script's own command-line interface is specified in 6.1.

---

## 9. File Change Manifest

| Action | File Path | Reason |
|--------|-----------|--------|
| CREATE | `docs/design/improve-my-prompting.md` (public repo) | This design doc |
| CREATE | `skills/improve-my-prompting/SKILL.md` | The public skill |
| CREATE | `skills/improve-my-prompting/references/setup.md` | Canonical copy (blob 8fb4e3cf) |
| CREATE | `skills/improve-my-prompting/references/troubleshooting.md` | Canonical copy (blob cb9656bc) |
| CREATE | `skills/improve-my-prompting/references/prompting-rubric.md` | Rubric sent with `--instructions` |
| CREATE | `skills/improve-my-prompting/scripts/prompt_review.py` | Entry point and command classes |
| CREATE | `skills/improve-my-prompting/scripts/sessionkit/__init__.py` | Shared package marker and version note |
| CREATE | `skills/improve-my-prompting/scripts/sessionkit/model.py` | Turn/Session and shared helpers |
| CREATE | `skills/improve-my-prompting/scripts/sessionkit/redaction.py` | Redactor |
| CREATE | `skills/improve-my-prompting/scripts/sessionkit/claude_code.py` | Claude Code reader |
| CREATE | `skills/improve-my-prompting/scripts/sessionkit/codex.py` | Codex reader |
| CREATE | `skills/improve-my-prompting/scripts/sessionkit/opencode.py` | OpenCode reader (SQLite v2/v1 + JSON) |
| CREATE | `skills/improve-my-prompting/scripts/sessionkit/gemini.py` | Gemini CLI / Qwen Code reader |
| CREATE | `skills/improve-my-prompting/scripts/sessionkit/collector.py` | Merge, cap, filter, report sources; shared argparse flags |
| CREATE | `skills/improve-my-prompting/scripts/sessionkit/runner.py` | Generic inputs validation, CLI invocation, paid-run guard, cost |
| CREATE | `skills/improve-my-prompting/scripts/prompting/__init__.py` | Package marker |
| CREATE | `skills/improve-my-prompting/scripts/prompting/signals.py` | Signal detection and overall rates |
| CREATE | `skills/improve-my-prompting/scripts/prompting/digest.py` | digest.md, sessions.json, inputs.json |
| CREATE | `skills/improve-my-prompting/scripts/prompting/suggest.py` | This skill's flag kinds, categories, argv |
| CREATE | `skills/improve-my-prompting/scripts/prompting/report.py` | Idea → prompt/rubric mapping |
| CREATE | `scripts/test-improve-my-prompting.py` (public repo root) | Phase 5 verification script; not installed by `npx skills add`, which reads only `skills/` |
| CREATE | `next-app/constants/agent-skills/improve-my-prompting/catalog.js` (Vidbyte) | Website listing |
| MODIFY | `next-app/constants/agent-skills/catalog.js` (Vidbyte) | Import and place in `ENTRIES` |

---

## 10. Testing Plan

All tests live in `scripts/test-improve-my-prompting.py`. It builds synthetic fixtures for every
harness in a temporary home directory, imports the modules directly for unit tests, and runs
`prompt_review.py` in a subprocess. For the subprocess runs, `HOME`/`USERPROFILE`, `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, and `XDG_DATA_HOME` all point into that temporary home, so the real stores are never
read. Every `suggest` and `feedback` test passes `--cli <fake>`. The fake is a recording
`vidbyte-cli` launcher made with pip's vendored distlib `ScriptMaker` (a real `.exe` on Windows),
and the subprocess's `PATH` contains no `vidbyte-cli` at all, so a `--cli` the script ignored fails
with exit 127 instead of reaching the real CLI. The script is run with the current Python and with
the local Python 3.8.20.

### Unit Tests
- `Redactor` → redacts `sk-…`, `ghp_…`, a JWT, `Authorization: Bearer …`, an email, `C:\Users\<login>\…`, and `C--Users-<login>-repo`; keeps `git@github.com` — [Silent Failure]
- `Redactor` → `None` input returns `""` — [Hidden Assumption]
- `Session.add_turn` → the same text twice within 10 seconds is kept once; after 10 seconds, twice — [Edge Case]
- `Session.add_turn` → a whitespace-only prompt is ignored — [Edge Case]
- `ClaudeCodeReader.read` → a system reminder, a command caveat, a tool result, and a skill body are not prompts; `/design-doc build X` is a prompt; `/clear` with no arguments is not — [Silent Failure]
- `ClaudeCodeReader.read` → `[Request interrupted by user]` marks the previous turn interrupted — [Hidden Failure]
- `ClaudeCodeReader.read` → a session whose first record has `entrypoint: sdk-py` returns skip reason `sdk` — [Hidden Assumption]
- `ClaudeCodeReader.read` → a corrupt JSON line is counted in `unreadable_lines` and parsing continues — [Hidden Failure]
- `CodexReader.read` → an `exec` session is skipped as `non_interactive`, and a subagent thread as `subagent` — [Hidden Assumption]
- `CodexReader.read` → `<environment_context>` user text is not a prompt; `turn_aborted` marks an interrupt; `function_call` counts as a tool call — [Silent Failure]
- `CodexReader.read` → the same prompt as `user_message` and as a `UserMessage` item is kept once — [Edge Case]
- `OpenCodeReader` (v2) → user and assistant rows map to turns, synthetic rows are ignored, an aborted assistant marks an interrupt, and child sessions (`parent_id`) are not candidates — [Silent Failure]
- `OpenCodeReader` (v1) → a session only in v1 tables is read; a synthetic text part is not a prompt — [Hidden Failure]
- `OpenCodeReader` (legacy JSON) → a session only in `storage/` is read; one also in the DB is read once — [Silent Failure]
- `OpenCodeReader` → a missing `session_v2` table (an older DB) falls back to v1 without error — [Hidden Assumption]
- `OpenCodeReader` → a non-SQLite file named `opencode.db` is reported in `read[].error` and other harnesses still run — [Hidden Failure]
- `GeminiReader` → a list-of-parts user content is flattened; `Request cancelled.` marks an interrupt; `/help` is not a prompt; a Qwen root yields harness `qwen-code` — [Edge Case]
- `GeminiReader` → a truncated JSON file is counted as unreadable and skipped — [Hidden Failure]
- `SessionCollector` → with 7 sessions and `max_sessions=5`, keeps the 5 newest across harnesses, in newest-first order — [Silent Failure]
- `SessionCollector` → skipped sessions do not count toward the cap — [Silent Failure]
- `SessionCollector` → `max_sessions=0` or `501` is a usage error — [Edge Case]
- `SessionCollector` → `--project-dir` keeps sessions in that folder and below it, and drops a sibling folder with a shared prefix (`/repo` vs `/repo2`) — [Silent Failure]
- `SessionCollector` → no harness folders gives 0 sessions and does not crash — [Edge Case]
- `SignalDetector` → `no, I meant the other file` after a prompt is a correction; a pasted log containing "no," deep inside is not — [Silent Failure]
- `SignalDetector` → `yes` after an agent question is `go_ahead`; `yes` after an agent that did not ask is not — [Hidden Assumption]
- `SignalDetector` → `fix this` as an opener is `vague_opener`; a 5-word opener naming `src/app.py` is not — [Edge Case]
- `SignalDetector` → three `also …` follow-ups make one `drip_feed` session hit; two do not — [Edge Case]
- `SignalDetector` → `JSON API README TODO` is not emphasis; `YOU MUST NEVER DELETE THIS` is — [Silent Failure]
- `SignalDetector` → a `$200 tip` is `emotional_stakes`; `you are a world-class engineer` is `persona`; `think step by step` is `step_by_step` — [Edge Case]
- `SignalDetector` → a session with 0 prompts produces no hits and no division by zero in the rates — [Edge Case]
- `SignalDetector` → signals rank by distinct sessions, then hits — [Silent Failure]
- `DigestWriter` → the digest never exceeds `max_chars`, and every kept signal has at least 1 example — [Silent Failure]
- `DigestWriter` → no raw secret, email, or home path from the fixtures appears in `digest.md` or `inputs.json` — [Silent Failure]
- `DigestWriter` → every `[P#]`/`[S#]` in `digest.md` and `inputs.json` exists in `sessions.json`, and tags are unique — [Hidden Failure]
- `DigestWriter` → `max_chars` below 20,000 is a usage error — [Edge Case]
- `DigestWriter` → `prompting-rubric.md` is copied byte for byte and is ≤ 20,000 bytes — [Hidden Assumption]
- `InputsValidator` → an unknown key, a 9th `mistakes` value, a value over 500 characters, an untagged `mistakes` value, and an unknown tag each give a problem — [Edge Case]
- `InputsValidator` → `avoid`/`constraint` values are accepted without tags — [Hidden Assumption]
- `InputsValidator` → a value containing an `sk-` key is redacted before it reaches argv — [Silent Failure]
- `SuggestArgvBuilder` → root options come before the command; it has all 6 categories, `--count 8`, `--horizon any`, `--files digest.md`, `--instructions prompting-rubric.md`, and `--max-total-tokens 400000`; `--dry-run` only when asked; `--project` only when set — [Silent Failure]
- `RunReporter` → `estimated_cost_usd` is computed at gpt-6-sol rates ×1.02, and is `None` when another model ran or there were 0 calls — [Silent Failure]
- `RubricIndex` → parses every rubric ID in the shipped rubric, and all signal rubric IDs exist in it — [Hidden Assumption]
- `IdeaReporter` → resolves `[P2]` and `RC3` in a summary; flags an idea with no tags as `grounded: false`; lists an unknown `[P99]` — [Silent Failure]

### Integration Tests
- `gather` end to end over fixtures from all five harnesses (Claude in two config folders, one passed with `--claude-dir`): exit 0; the summary counts match the fixtures; `other_claude_dirs_not_read` lists an unpassed `~/.claude-work`; `not_read` lists a fake `~/.cursor/chats`.
- `gather` with every harness disabled exits 3, and its summary names the folders read.
- `suggest --dry-run` with the recording fake: exit 0; the recorded argv equals the expected argv; the cwd is the output folder; `--instructions` and `--files` are relative; no `paid-run.txt` is written.
- `suggest --paid` with the fake: writes `paid-run.txt`, then `result.json`; a second `--paid` in the same folder exits 2, and the fake records no second call. This catches a silent double charge.
- `suggest --paid` where the fake exits 5 with an error JSON on stderr: the summary carries `error.code`, and the exit code is 5.
- `suggest` with no `--cli` and no `vidbyte-cli` on `PATH` exits 127 and never runs anything.
- `report` after a fake `result.json` writes `report.md` with the quoted redacted prompt text of each cited tag.
- `feedback --accept` sends the exact title and the project from `inputs.json`; `--reason` is redacted.
- The whole flow runs under Python 3.8.20 as well.

### Manual / QA Test Cases
1. Given this machine's real stores, when `gather` runs with defaults, then it finishes in under 3 minutes, reads Claude, Codex, OpenCode, and Gemini, lists `~/.claude-account*` and `~/.claude-work` as not read, and `digest.md` shows no secret on a manual skim — [Hidden Failure]
2. Given the real `digest.md`, when `suggest --dry-run` runs against the real `vidbyte-cli` (a dry run is free and calls no model), then exit 0, `stop_reason: dry_run`, and `digest.md` is in the context manifest — [Hidden Assumption]
3. Given `--max-sessions 1`, when `gather` runs, then exactly 1 session and its prompts appear — [Edge Case]
4. Given the website worktree, when `npm run build` runs, then the build logs the expected `fetch-failed` warning for this coming-soon slug until the public PR merges, and no other `[agent-skills]` warning — [Hidden Assumption]

---

## 11. Dependencies & External Services

| Dependency | Version / Endpoint | Purpose | Risk |
|------------|--------------------|---------|------|
| `vidbyte-cli` | 0.3.0+ (PyPI has only 0.2.0) | `agents suggest run`, `handoff`, `project`, `feedback` | The skill stays `coming-soon` until 0.3.x is on PyPI |
| Vidbyte model gateway / OpenAI gpt-6-sol | via `agents suggest run` | Generator + critics | Usage-billed; capped at 400,000 tokens |
| Python | 3.8+ standard library (`sqlite3` optional) | The helper script | `sqlite3` missing on minimal builds disables OpenCode DB reading |
| Harness storage formats | Claude Code JSONL, Codex rollouts, OpenCode drizzle schema (v1/v2), Gemini chat JSON | Session reading | Formats change without notice; readers skip unknown shapes and report them |
| cerredz/Vidbyte `defineAgentSkill`, lint N050 | `main` | Website listing | N050 resolves command paths against the vendored contract |

---

## 12. Rollout & Deployment

- No feature flags. This is a new skill folder and a new catalog entry, not a breaking change.
- Order (per `add-agent-skill`): public PR first (cerredz/vidbyte-cli), then the website PR
  (cerredz/Vidbyte). The website entry is `coming-soon`, so before the public file merges, the
  detail page says the skill is being written.
- When vidbyte-cli 0.3.x reaches PyPI, flip `status` to `available` together with the nine skills
  in draft Vidbyte#691.
- Rollback: revert the website entry first, then the public folder (the reverse of the deploy
  order).

---

## 13. Open Questions

- [ ] `min-cli-version` says `0.3.0` to match every sibling suggestion skill. `--instructions` is in
  0.3.0's tree, but the usage-billed flow landed in 0.3.1, and the first PyPI release will be 0.3.2.
  Should all suggestion skills move to `0.3.2` together (Vidbyte#691's scope)?
- [ ] Should a follow-up add readers for Cline, Roo Code, Kilo Code, and Copilot CLI once fixtures
  from real installs are available?
- [ ] English-only keyword signals: add other languages later, or rely on the session samples?
- [ ] Should the website add a `prompting` topic, instead of reusing `planning`?

---

## 14. Alternatives Considered

### Alternative 1: Send the rubric with `--files`
- What: attach the rubric as a context file the ideas can cite.
- Why rejected: ideas would cite the rubric as their evidence instead of the user's prompts, and
  the critic would accept that. `--instructions` makes the rubric shape both the generator and the
  critic, while the evidence stays the user's own prompts.

### Alternative 2: Send raw transcripts (or a large slice of them)
- What: pass full prompt histories and let the model find the patterns.
- Why rejected: 500 sessions are millions of tokens, far over the 400,000-token cap and the
  context window. They would also carry secrets, and a model scanning raw data is less reliable
  than local counting.

### Alternative 3: One self-contained script like the siblings
- What: a single 2,500-line `prompt_review.py`.
- Why rejected: the five harness readers alone are about 1,200 lines. A package with one module
  per concern keeps files under 1,000 lines and lets the tests import each reader directly.

### Alternative 4: Use the MCP server
- What: drive the Suggestion Agent with `vidbyte_plan`/`vidbyte_run`.
- Why rejected: `write-agent-skill` section 1. The MCP server fixes the flags (no `--instructions`,
  no categories), and it adds a paid routing call first.

### Alternative 5: `--extra-compute` or more critic rounds
- What: more model calls for deeper review.
- Why rejected: each one sends the 70,000-character digest and the rubric again. The default two
  rounds already give critic review within the 400,000-token cap.
