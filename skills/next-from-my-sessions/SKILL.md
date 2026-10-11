---
name: next-from-my-sessions
description: Use when the user wants their recent coding-agent history turned into a plan for what to do next - "look at what I've been doing and tell me what to run next", "what did I leave unfinished?", "plan my next sessions". Reads up to the last 100 Claude Code and Codex sessions for the current project on the user's machine, builds a redacted digest, and returns a ranked list of next sessions, each with the sessions it came from and a ready-to-paste prompt. Drives the Vidbyte Suggestion Agent through vidbyte-cli.
metadata:
  agent: suggestion-agent
  commands: "agents suggest run, agents suggest handoff, agents suggest project list, agents suggest project create, agents suggest feedback accept, agents suggest feedback reject, runs list"
  cost: "2 cents per run at --count 8 (2 cents per block of up to 10 suggestions) + model usage on the user's own Codex login"
  writes-files: "false"
  min-cli-version: "0.3.0"
  status: stable
---

# Plan your next sessions from your session history

After days of agent sessions in one project, it is hard to remember what got finished, what was
left halfway, and what kept failing. This skill reads the newest sessions for the current project
on the user's machine. It sends only a redacted digest to the Vidbyte Suggestion Agent. The user
gets back a ranked list of next sessions, each with a ready-to-paste prompt.

## When to use

- "Look at what I've been doing and tell me what to run next."
- "What did I leave unfinished in this repo?"
- "Plan my next few sessions."
- The user comes back after a break and cannot remember where each thread stopped.
- The same errors keep coming up across sessions, and the user wants the work that fixes the cause.

## When not to use

- The user wants next steps from the repository itself (TODOs, issues, git log), not from their
  session history. Use the `whats-next` skill if it is installed. Otherwise, ask what they want.
- The user wants to continue one specific session. Reopen it with `claude --resume <session-id>`
  or `codex resume`. That is free.
- The project has fewer than three sessions. The digest has too little signal. Read the session
  and answer directly.
- The user wants a status report of past work, not suggestions. Run the digest script in Gather
  inputs and summarize `digest.md` yourself. Skip the paid run.
- Sending a summary of their sessions to a model provider is against the user's policy. Do not
  run this skill.

## Goal and done-when

The goal: turn the user's recent session history for this project into a ranked set of next
sessions. Each session must be traceable to the sessions it came from and ready to paste into a
new agent session. Raw transcripts stay on the machine, and money is spent only after the user
says yes.

Done when: the user has seen the ranked list, with the source sessions and a ready-to-paste
prompt for every idea. They have been told the charge from `data.charged_cents`. Every accept or
reject they stated is recorded with `vidbyte-cli agents suggest feedback`.

## Preflight

Run these in order. If any fails, read `references/setup.md`, fix only what failed, and run the
preflight again.

```bash
vidbyte-cli --version                  # must be 0.3.0 or newer
vidbyte-cli --json whoami              # exit 4 means no key is stored
vidbyte-cli --json runtime doctor      # data.hosts must list "codex" with "available": true
codex login status                     # must report a login; the suggestion agent runs in Codex
python3 --version                      # 3.8 or newer, for the digest script (Windows: python --version)
```

## Gather inputs

### 1. Ask before reading any history

Session transcripts can hold private code, pasted secrets, and personal details. Read nothing
until the user agrees. Send this message, with the real paths filled in:

> To plan your next sessions, I'll read your recent agent history for this project. It stays on
> this machine:
> - Claude Code: `<config dir>/projects/<this project's folder>/*.jsonl`. The config dir is
>   `$CLAUDE_CONFIG_DIR` if set, else `~/.claude`.
> - Codex: `<$CODEX_HOME or ~/.codex>/sessions/**/rollout-*.jsonl`, only sessions started in
>   this project.
> - Also: the Vidbyte runs ledger (`vidbyte-cli runs list`), `git log`, and open PRs.
>
> I'll read the newest 100 sessions at most. A local script removes API keys, tokens, emails,
> home-folder paths, and `.env`-style values, then writes a short digest. Raw transcripts are not
> sent anywhere.
>
> Only the digest, plus short facts I write from it, goes to the Vidbyte Suggestion Agent. That
> agent runs in your local Codex, on your own Codex login, read-only in this repository. Vidbyte's
> admission request carries only the agent host and the number of suggestions. Nothing is
> charged yet; I'll ask again before the paid step.
>
> OK to read the history? You can lower the 100, or name other Claude config folders (for
> example `~/.claude-work`).

Stop here until the user says yes.

### 2. Build the digest (free, local)

The script is `scripts/digest_sessions.py` in this skill's folder, the folder that holds this
`SKILL.md`. It uses only the Python standard library. Find the roots, then run it from the
repository root:

```bash
git rev-parse --show-toplevel          # the project root; if this fails, use the current folder
git worktree list --porcelain          # each "worktree <path>" line is one more root
python3 "<skill folder>/scripts/digest_sessions.py" --root "<project root>" --root "<worktree path>" --limit 100
```

- Pass `--root` once for each worktree path, because sessions in a worktree are about this project.
- Use `--limit <n>` if the user lowered the count.
- Pass `--claude-dir <folder>` once for each extra Claude config folder the user named.
- On Windows, use `python` (or `py -3`) instead of `python3`.

The script reads only local files. It also runs these read-only commands:
- `vidbyte-cli --json runs list --all --limit 200`
- `git log --oneline -30`, `git branch --show-current`, and `git status --porcelain`
- when `gh` is installed, `gh pr list --state open --limit 10 --json number,title,headRefName`

It prints one JSON line. Keep these fields:

| Field | Use it for |
|---|---|
| `digest_path`, `out_dir` | The digest to read and pass with `--files`, and the folder for `result.json`. Both are in the system temp folder, outside the repository. |
| `sessions.total`, `first_day`, `last_day` | The numbers in the cost message and the goal. |
| `project_key`, `project_title` | The suggestion project that remembers accepted and rejected ideas. |

Exit 3 prints `{"error": "no sessions found", ...}` and writes nothing. Tell the user which roots
and folders you checked. Some users start their agent in a parent workspace folder, not in the
repository. In that case, the user can pass that folder as `--root`. Warn them that it also
includes sessions about sibling projects. Do not continue with an empty digest.

### 3. Turn the digest into context values

Read `digest.md` in full. It is local and already redacted. Each session has a tag like `S03`,
and `S01` (or `S001`) is the newest. The newest session is often the one you are in now; ignore
it. Write the values below from the digest:

| Flag | What goes in it | At most |
|---|---|---|
| `--trajectory` | The arc across all sessions, in one or two sentences. | 2 |
| `--completed` | Work the digest shows as finished: merged PRs, final replies that report done. | 8 |
| `--in-progress` | Work started but not finished: interrupted sessions, replies that list remaining steps, threads that stop with no result. Say what remains. | 8 |
| `--mistakes` | Failures that repeat across sessions (the digest's "Repeated errors"), with the cause and the lesson when the digest shows them. | 5 |
| `--blockers` | Conditions that stopped progress, such as missing access, a wait for review, or a broken environment. | 5 |
| `--outcomes` | Results of earlier work: PRs opened or merged, Vidbyte runs that succeeded or failed. | 5 |
| `--context` | Repository state: branch, uncommitted files, open PRs. | 3 |

Follow these rules for every value:

- Start it with the tags of the sessions it comes from, in brackets, exactly as the digest prints
  them: `[S03 S07]`. Use `[git]` or `[runs]` for repository and ledger facts.
- Write one fact per value, as a full sentence of at most 300 characters. Do not pass an empty
  value; leave the flag out instead.
- Use plain words with no double quotes, backticks, or dollar signs, so the command works the same
  in bash and PowerShell.
- Never add back anything the digest redacted.
- Keep the total at 40 values or fewer, so the command line stays far below the Windows limit
  of 32,767 characters.

The tags do two jobs:

- The result quotes the values each idea cites, so the tags tell the user which sessions an idea
  came from.
- The CLI drops any idea whose title, summary, or actions contain a `--completed`,
  `--in-progress`, or `--mistakes` value word for word. A short value such as `auth refactor`
  would remove every idea about finishing the auth refactor. A tagged full sentence will not be
  repeated word for word.

If the user named a focus, a deadline, or something to stay away from, add it with
`--constraint "<text>"` or `--avoid "<text>"`. `--avoid` also removes ideas that contain its text
word for word. Ask the user nothing else.

### 4. Make sure the project exists (free, local)

```bash
vidbyte-cli --json agents suggest project list
```

If no entry in `data.projects` has `key` equal to `project_key`, create it. The title and
description go to the suggestion agent as context, so keep paths and names out of them.

```bash
vidbyte-cli --json agents suggest project create --key "<project_key>" --title "<project_title>" --description "Next-session suggestions for <project_title>, built from the user's coding-agent session history."
```

The message `Suggestion project already exists: <key>.` (exit 2) means it is already there; use it.

### 5. Dry-run the exact command (free, no model, no charge)

Run the command from the Run section with `--dry-run` added and without `--idempotency-key`. A dry
run never buys an admission and never calls a model. It checks every flag, the digest file, and
the project.

- Expect exit 0. `data.status` is `no_suggestions` and `data.stop_reason` is `dry_run`; that is
  the normal dry-run result.
- Every entry in `data.context_manifest` should have `status` `included`. If one is `truncated`
  or `omitted`, shorten that value, or rebuild the digest with a smaller `--max-chars`, and run
  the dry run again.
- On exit 2, fix the flag the message names and run the dry run again.

## Cost and consent

This skill runs `agents suggest run` once. The charge is 2 cents per run at `--count 8` (2 cents
per block of up to 10 suggestions), taken from the user's Vidbyte balance. Model usage is billed
to the user's own Codex login, not by Vidbyte. The run does not edit any files in the workspace.
It saves `result.json` in the temp folder next to the digest. Each run, including the dry run,
adds an entry to vidbyte-cli's local runs ledger, and project memory lives in vidbyte-cli's own
data folder.

Before running it, tell the user the cost and what will happen, and wait for a yes:

> Ready to plan from `<sessions.total>` sessions (`<first_day>` to `<last_day>`). This costs 2
> cents from your Vidbyte balance, plus Codex model usage on your own login. It changes no files.
> The digest is at `<digest_path>` if you want to read what will be sent. Run it?

Generate one idempotency key for this run: `next-from-my-sessions:<YYYYMMDD>:<6 random hex
characters>`, for example `next-from-my-sessions:20261011:7f3a9c`. Reuse it only to recover this
same admission (see Troubleshooting).

## Run

Run from the repository root. Codex runs read-only in the current folder, and the runs ledger
records that folder, so the next digest finds this run.

```bash
vidbyte-cli --json --no-input agents suggest run \
  --goal "Choose the next coding-agent sessions to run in <project_title>, based on what the last <sessions.total> sessions finished, left unfinished, or kept failing at." \
  --project "<project_key>" \
  --category completion \
  --category continuation \
  --category bottleneck \
  --category immediate_next_steps \
  --count 8 \
  --trajectory "<value>" \
  --completed "<value>" \
  --in-progress "<value>" \
  --mistakes "<value>" \
  --blockers "<value>" \
  --outcomes "<value>" \
  --context "<value>" \
  --files "<digest_path>" \
  --idempotency-key "<key>" \
  > "<out_dir>/result.json"
```

Repeat each context flag once per value, and leave out a flag that has no value. Why these
settings:

- `--category completion`, `continuation`, `bottleneck`, `immediate_next_steps`: unfinished work
  to close, accepted plans to move to their next step, repeated failures that limit progress, and
  work that can start now. These four cover what session history shows.
- `--count 8`: the price is the same for 2 to 10 suggestions. Eight covers the threads a few weeks
  of sessions usually leave open, and the user can review it in one sitting.
- No `--horizon`: any value other than the default `any` drops every idea with a different
  horizon. A plan of next sessions needs both `now` and `next` ideas.
- `--project`: loads earlier accepts and rejects. An idea that repeats a rejected one word for word
  is removed.
- `--files`: gives the agent the whole digest, beyond the short values.
- Default `--rounds`: two review rounds are enough for a slate of eight.

How to run it safely:

- The run can take several minutes. Start it so your shell tool cannot kill it early. For
  example, run it in the background and wait for it, or use the longest timeout your shell tool
  allows. A run killed after its admission is verified has already been charged, and suggestion
  runs cannot resume.
- In Windows PowerShell 5.1, put the command on one line. Replace `> file` with
  `| Out-File -Encoding utf8 "<out_dir>\result.json"`, because `>` there writes UTF-16. On a
  UTF-16 file, `handoff` fails with `The --input file is not a readable suggestion result.`
- stdout carries only the result. Progress, the admission retry key, and any `kind: "error"`
  document go to stderr. On a non-zero exit, `result.json` is empty; read the error from stderr.

## Read the result

`result.json` holds one JSON document: `{"schema_version": 1, "kind": "suggestions.result", "data": {...}}`.

| Field | Use it for |
|---|---|
| `data.status` | `complete`, or `partial` (stopped early; the ideas are still valid). `no_suggestions` means nothing survived review; show `data.warnings`. `needs_input` means the agent stopped to ask a question. |
| `data.parent_message` | The agent's question when the status is `needs_input`. Relay it to the user. Answering it means a new run, which is a new charge, so ask first. |
| `data.ideas[]` | The ranked slate: `rank`, `id` (`idea-001`), `title`, `summary`, `primary_category`, `horizon`, `readiness`, `effort_estimate`, `why_now`, `first_action`, `completion_criteria`. |
| `data.ideas[].handoff.evidence[].content` | The context values the idea cites. Their `[S..]` tags name the source sessions. If an idea cites only the digest file, say it draws on the whole history. |
| `data.warnings`, `data.missing_context` | Shortfalls and context kinds that were not supplied. Mention them briefly. |
| `data.charged_cents` | What the run cost. Tell the user. |
| `data.feedback_capture` | The accept and reject command shapes for this project. |

For each idea, get its ready-to-paste prompt. This is free and local:

```bash
vidbyte-cli --json agents suggest handoff --input "<out_dir>/result.json" --idea idea-001
```

The result has `kind` `suggestions.handoff`, and `data.execution_prompt` is the prompt.

## Deliver

1. Show the ranked list. For each idea:

   ````text
   1. <title>   (<id> · <primary_category> · <horizon> · <readiness> · <effort_estimate>)
      Why now: <why_now>
      From: S03 (<date>, <session title from the digest>), S07 (<date>, <title>)
      Done when: <completion_criteria>
      Prompt:
      ```text
      <data.execution_prompt from handoff>
      ```
   ````

   Then add one line with the charge from `data.charged_cents`.
2. Ask: "Which of these will you run? Tell me any you reject and why, and I'll remember it for
   next time."
3. Record only what the user says explicitly. Use the idea's `title`, word for word, as
   `--suggestion`. A rejected title then removes the same idea from later runs. Use the user's
   own words as `--reason`, and leave `--reason` out if they gave none. These commands are free
   and local:

   ```bash
   vidbyte-cli --json agents suggest feedback accept --project "<project_key>" --suggestion "<idea title>" --reason "<the user's words>"
   vidbyte-cli --json agents suggest feedback reject --project "<project_key>" --suggestion "<idea title>" --reason "<the user's words>"
   ```

4. Offer to start the top accepted session now, by using its prompt as the first message of a new
   agent session.
5. Offer to delete `<out_dir>` when the user is done. It holds the digest and `result.json`.

## Troubleshooting

| Exit | Do this |
|---|---|
| 2 | Fix the flag the message names. Do not retry unchanged. A dry run catches most of these for free. |
| 4 | Stop. Follow `references/setup.md` step 2. |
| 5 | Stop. Follow "Out of credits" in `references/troubleshooting.md`. Never top up without the user's yes. |
| Other | Read `references/troubleshooting.md`. |

When the paid run fails, first check whether stderr shows the line that starts with
`Vidbyte has verified the admission for this run.`:

- If that line never appeared (for example, the connection dropped or the terminal was killed
  during admission), rerun the same command once. Use the same `--idempotency-key` and the same
  `--count`. This recovers a charge that may already have happened, without paying twice. The
  stderr line `Suggestion admission retry key: <key>.` names the key.
- If the line appeared and the run then failed (exit 1, for example `The suggestion provider did
  not complete the run.`), the admission was used. A new run is a new charge with a new key, so
  ask the user first.

## Guardrails

- Never ask the user to paste an API key into the chat, and never print one.
- Never pass `vidbyte-cli billing top-up --confirm` unless the user explicitly approved that top-up.
- Run at most one file-writing Vidbyte agent in a workspace at a time.
- State the cost and wait for a yes before `agents suggest run` without `--dry-run`. Give the run
  one idempotency key, and reuse it only to recover that same admission.
- Read no session history before the user agrees. Read only this project's sessions.
- Never copy raw transcript text, the digest, or `result.json` into the repository, a commit, an
  issue, or a pull request.
- Never put back a value the digest redacted, and never write secrets, emails, or home-folder
  paths into a flag value or a project description.
- Record feedback only when the user explicitly accepts or rejects an idea. Silence is not
  feedback.
