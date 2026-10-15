---
name: write-agent-skill
description: The writing contract for a public Vidbyte agent skill - the SKILL.md a user installs from cerredz/vidbyte-cli with `npx skills add` that teaches their coding agent to solve one problem by driving vidbyte-cli. Covers why skills drive the CLI and not the MCP server, problem-first naming, the frontmatter and metadata fields, the fixed section order, the consent and spending rules, exact-command discipline, the shared setup and troubleshooting references, and the review checklist.
---

# Write Agent Skill

## Identity

You are writing instructions for someone else's coding agent, and that agent will follow them
literally in a repository you will never see. The reader is Claude Code, Codex, Cursor, or another
agent that installed this skill because its user said something like "find the bugs in my repo" or
"what should I work on next". It does not know Vidbyte, it does not know which of the CLI's dozens
of flags matter, and it cannot ask you what you meant. Everything it needs to get the user from that
sentence to a finished result has to be on the page, in the order it will need it, with every
command spelled exactly as the CLI accepts it.

A public skill is also a product page and a distribution channel. It is listed at
`/agents/skills/<slug>` on vidbyte.pro, indexed by search engines under the problem it solves, and
installed one folder at a time by people who have never heard of the company. The first thing it
does after installing is make sure `vidbyte-cli` is installed and logged in, so every install of a
skill is also an install of the CLI. That only pays off when the skill actually works the first
time. A skill that names a flag the released CLI does not have, or that silently spends the user's
money, costs more trust than it earns, so precision and consent are not polish; they are the job.

Write like an experienced engineer leaving instructions for a capable colleague. Be concrete,
name the exact command and the exact field to read, explain why a setting was chosen when the
reason is not obvious, and leave out everything the agent does not need in order to act.

## Goal

The goal is a `SKILL.md` that takes a coding agent from the user's request to a delivered,
verified result with exactly one Vidbyte agent run (or a small, named chain of them), with the
user told the cost and asked for consent before anything is spent or any file is changed. A
reader should be able to tell from the first screen what problem the skill solves, how
`vidbyte-cli` solves it, and what "done" looks like.

Each skill encodes judgment the CLI cannot supply on its own: which agent fits the problem, which
flags fit the problem, what context to gather from the repository before running, and how to turn
the JSON result into something the user can act on. The CLI's defaults are tuned for nobody in
particular; a skill earns its existence by choosing settings for one situation and saying why. If
a skill would only restate `--help`, it should not exist.

Success is measured from the user's side. They asked for something in their own words, their
agent found and loaded the skill, ran a preflight that either passed or told them exactly what to
fix, told them what it would cost, did the work, and handed back a result they could check. No
key was pasted into the chat, no money moved without a yes, and no command failed because the
skill guessed at its spelling.

## Use cases

- Writing a new public skill under `skills/<slug>/` in cerredz/vidbyte-cli.
- Reviewing a public skill PR against this contract.
- Updating a published skill after the CLI renames a command or a flag.
- Refreshing the shared `references/` copies after `setup.md` or `troubleshooting.md` changes.

`skills/agent-skills/add-agent-skill/SKILL.md` owns the end-to-end process across both
repositories (where the file goes, the website catalog entry, the gates, and the deploy order).
This skill owns what goes inside the public `SKILL.md`.

---

## 1. Drive the CLI, not the MCP server

Write every skill against `vidbyte-cli`. The MCP server (`vidbyte-cli mcp serve`) is useful, but
it is the wrong tool for a skill, for reasons that come from what the code does today:

| Reason | Detail |
|---|---|
| The MCP server fixes every flag. | It runs five agents (suggestion, same-host ensemble, persistence, research, task board) with fixed settings: ensemble `--roles 3`, persistence `--strength 1`, research `--size small --max-cost-cents 500`, task board `--allow-decompose --no-checkpoint --max-subtasks 5 --retries-per-task 0`. Choosing settings for one problem is the whole value of a skill. |
| Every MCP run starts with a paid routing call. | `vidbyte_plan` makes one metered routing call before `vidbyte_run` can start anything. A skill has already chosen the agent, so that call is pure cost and an extra non-deterministic step. |
| The MCP server cannot reach most commands. | It has no stages, no `research add`, `resume`, or `watch`, no task-board resume or inspection, no `runs`, no billing, no login, and no bug finder. |
| The CLI works in every agent unchanged. | A skill that runs shell commands works the same in Claude Code, Codex, Cursor, and Gemini CLI. The MCP server must be registered separately in each host, and Codex also needs `startup_timeout_sec = 60`. |
| The CLI's output is built to be read by agents. | With `--json --no-input`, every result is `{schema_version, kind, data}` and every exit code has one meaning (4 is authentication, 5 is out of credits), so a skill can say "on exit 5, do X" and be exact. |

The one place MCP fits is a single router skill for a user who does not know which agent they
want, because routing is the job `vidbyte_plan` exists for. Do not write skills that support both
paths; that doubles the testing and makes every instruction conditional.

## 2. Name the skill after the problem

The slug is the folder name, the frontmatter `name`, the website URL segment, and the words a
search engine indexes, so it names the problem the user has, not the Vidbyte agent that solves it.
`fix-every-instance`, `whats-next`, and `run-my-plan` are good; `persistence-agent-skill` and
`vidbyte-ensemble` are not. Use lowercase words joined by single hyphens (`^[a-z0-9]+(?:-[a-z0-9]+)*$`),
at most 64 characters, and check that no published skill already uses the slug.

## 3. Frontmatter

```yaml
---
name: fix-every-instance
description: Use when the user wants one bug pattern fixed everywhere it occurs, not just the first match - "fix all of these", "this keeps coming back", "find every place we do X". Drives the Vidbyte Persistent Agent through vidbyte-cli to keep going until no instance is left.
metadata:
  agent: persistent-agent
  commands: "runtime persistence"
  cost: "2 cents per run + OpenAI usage"
  writes-files: "true"
  min-cli-version: "0.3.0"
  status: stable
---
```

| Field | Rule |
|---|---|
| `name` | Equals the folder name exactly. The website refuses to render a file whose `name` differs from its slug. |
| `description` | Starts with "Use when", quotes two or three phrasings a user would actually type, says what the skill gets done, and names the Vidbyte agent and `vidbyte-cli`. It is the only place the skill says when to use it (section 4). At most 1024 characters. The website catalog entry's `description` is a verbatim copy of this field, so edit both together. No price figures: on the website every price must trace to the backend's price table, and lint rule N038 checks only agent catalog entries and the sentences bound in `lint/allowlists/price_sources.txt`, so a price here would ship unchecked. Prices go in `metadata.cost` and the Cost and consent section. |
| `metadata.agent` | The agent id, from the website vocabulary: `suggestion-agent`, `persistent-researcher`, `persistent-agent`, `ensemble-agent`, `task-board`, `stages-agent`, or `bug-finder`. |
| `metadata.commands` | The command paths the skill runs, without the `vidbyte-cli` prefix, separated by commas. |
| `metadata.cost` | The exact Vidbyte charge plus any provider usage, in the same words the Cost and consent section uses. For a usage-billed agent, which has no exact charge, the model, its per-token rates, and the token cap the skill passes. |
| `metadata.writes-files` | `"true"` when any step edits the user's workspace. |
| `metadata.min-cli-version` | The first released version on PyPI that has every command and flag the skill uses. See section 6. |
| `metadata.status` | `stable`, or `preview` while the agent itself is labelled preview. A skill that is not ready is not published; it appears on the website as a coming-soon entry with no public file. |

Quote every `metadata` value except `status` so it stays a plain string for every agent that
parses the frontmatter. The skills CLI reserves `metadata.internal`; never set it on a public skill.

## 4. Section order

Every public skill uses these sections in this order. `references/skill-template.md` has them
ready to fill in.

| Section | What it holds |
|---|---|
| Title | One `#` heading that names the problem in plain words. |
| The problem | The problem in detail, in several paragraphs: who has it and in what situation, what goes wrong in concrete terms (a short list is fine when the failures are distinct), what it costs the user, and why it is hard to catch or solve without help. Close with what the skill does not cover when that limit matters, such as "a first pass, not a penetration test". |
| How vidbyte-cli solves it | One or two paragraphs that name `vidbyte-cli`, the Vidbyte agent, and its commands, say what the agent does that the user's coding agent cannot do well alone, and say what the user has at the end. |
| Goal and done-when | One goal paragraph and one observable completion check. Do not restate the goal as a mission and an objective too; one statement is enough and costs fewer tokens every time the skill loads. |
| Preflight | `vidbyte-cli --version`, `vidbyte-cli --json whoami`, and one agent-specific check. On failure, read `references/setup.md`. |
| Gather inputs | What to read from the repository before asking the user anything, and the one or two questions only the user can answer. |
| Steps | A numbered, high-level list of the whole procedure, from preflight to hand-back. Each step is one or two lines, names the `vidbyte-cli` command it runs if it runs one, and ends with the section that holds its details, such as "(Run)". |
| CLI commands | A table of every `vidbyte-cli` command the skill runs or asks the user to run, in the order they come up: the full command with root options first, the section that uses it, and what the command does, what it costs, and whether it writes files. Describe each command from its help text in the CLI source (`src/vidbyte_cli/commands/**/prompts/*.md` on the internal repository), never from memory. |
| Cost and consent | The exact charge (or, for a usage-billed agent, what makes the usage grow), whether files change, the consent question, and how to make the idempotency key when the command takes one. |
| Run | The exact command, always `vidbyte-cli --json --no-input ...`, with a one-line reason for each non-default flag. |
| Read the result | The result `kind` and the `data` fields the next step uses. |
| Deliver | How to present the result and which skill to chain to next. |
| Troubleshooting | The exit-code table: 2 fix the command, 4 setup, 5 stop and ask, anything else `references/troubleshooting.md`. |
| Guardrails | The standing rules from section 5 plus any rule specific to this skill. |

Do not add "When to use" or "When not to use" sections. The frontmatter `description` already
holds the phrasings the host agent matches a request against, so the lists only repeat it and cost
tokens every time the skill loads. A limit that changes what the agent does, such as "stop unless
the user owns the app", goes in Preflight, Gather inputs, or Guardrails, where the agent acts on
it. A sibling skill that fits a nearby request goes in Deliver, as the next step to offer.

"The problem" and "How vidbyte-cli solves it" come first because the website renders the whole
body at `/agents/skills/<slug>`, so they are what a visitor reads before installing. "Steps" and
"CLI commands" come after "Gather inputs", so the agent sees the whole plan and every command it
will run before it asks the user for consent.

## 5. Consent and spending

These rules are not optional and every skill restates them in its Guardrails section:

1. Before any command that charges the Vidbyte balance or edits files, tell the user what it costs
   and what will change, and wait for a yes.
2. Never pass `vidbyte-cli billing top-up --confirm` on your own. On exit 5, stop, read
   `data.remediation`, and follow `requires_user_approval`; the full procedure is in
   `references/troubleshooting.md`. Top-up spends real money ($5.00 in USDC through x402) and the
   CLI refuses it without `--confirm` precisely so an agent cannot do it by accident.
3. Never ask for an API key in the chat, never echo one, and never put one on a command line. Keys
   are stored with `login --with-token` from stdin or by the user running `vidbyte-cli login`.
4. Run at most one file-writing Vidbyte agent in a workspace at a time.
5. Give every priced run one idempotency key and reuse it only to recover that same admission.
   `agents suggest run` buys no admission and takes no key; its model calls are billed one by one.

A skill may install a CI workflow that starts a paid run each time an event fires, with nobody
there to ask, but only when what starts the paid run (the event plus any condition the workflow
checks before it) is something only the user or people with write access to the repository can
cause, such as a merged pull request. Never let something an outside contributor can cause start
a paid run, such as opening an issue, posting a comment, or opening or pushing to a pull request.
Rule 1 then applies once, at install: before writing any file, tell the user which event starts
a run and that nobody will be asked again, the most one run can cost and the cap the workflow
passes to hold it there (`--max-total-tokens` for a usage-billed agent), what leaves the
repository on each run and where the result goes, and how to stop the workflow; then wait for a
yes. That yes covers every run of the workflow as installed; a change to what it runs, its cap,
its trigger, or the CLI version it installs needs a new yes. The user stores every credential
the workflow needs in the CI system's secret store themselves; the skill never asks for, reads,
or writes one (rule 3), and the workflow never runs `billing top-up`. A skill that installs such
a workflow restates this paragraph in its Guardrails section.

## 6. Commands are exact

- Always call the binary by its full name, `vidbyte-cli`. The separate Vidbyte-Skills package
  ships a different command named `vidbyte`, and models confuse the two.
- Put root options before the command: `vidbyte-cli --json --no-input agents suggest run ...`,
  never `vidbyte-cli agents suggest run --json`.
- Every command path and long option must exist in `contracts/cli-commands.json` on the internal
  repository's `main`, and in the released version named by `min-cli-version`. Verify against the
  source, never from memory, from `--help` text you remember, or from another skill.
- As of 2026-10-11, PyPI's newest release is `vidbyte-cli` 0.2.0 (tagged 2026-10-03). It does not
  have `runs`, `mcp serve`, `setup-prompt`, `github link`, `browserbase login`, or
  `agents bug-finder`, and its suggestion command predates the model calls billed through
  Vidbyte and caps `--count` at 15 where `main` allows 100. A skill that needs any of these sets `min-cli-version` to the release that
  ships them and is not published until that release is on PyPI.
- Never write a skill around `runtime adversarial-team`. It exists in the command tree but raises
  `NOT_IMPLEMENTED` before doing anything.

## 7. Shared references

`npx skills add cerredz/vidbyte-cli --skill <slug>` installs one skill folder on its own, so a
skill cannot link to a sibling's files. Every public skill therefore carries its own
`references/setup.md` and `references/troubleshooting.md`, copied byte for byte from the canonical
copies in this folder. Keeping them in `references/` also means the agent loads them only when
preflight or a command fails, so a healthy run never pays their token cost.

Edit only the canonical copies here, then regenerate every public copy in the same public PR.
Never fix one public copy by hand.

## 8. Diction

- Plain, direct English. Short sentences. Active voice. The reader may be a non-native speaker.
- Name the thing: the command, the field, the file, the exit code. Avoid "appropriately",
  "as needed", "relevant", and every other word that hands a decision back to the reader.
- Say why when a choice is not obvious: "`--horizon later`, because the user asked about next
  quarter, not this week."
- No marketing claims, no superlatives, no comparisons with other products.
- Quote CLI messages exactly as the CLI prints them.

## 9. Review checklist

- [ ] The folder name, `name`, and website slug are identical, lowercase, and hyphenated.
- [ ] `description` starts with "Use when", quotes real user phrasings, has no price, and is at most 1024 characters.
- [ ] Every `metadata` field is present and matches the body (cost, commands, writes-files).
- [ ] Every section from section 4 is present, in order, and there is no "When to use" or "When not to use" section.
- [ ] "The problem" explains the situation, the concrete failures, and why they are hard to catch,
      and "How vidbyte-cli solves it" names `vidbyte-cli`, the agent, and its commands.
- [ ] Steps covers the whole procedure from preflight to hand-back, and each step names its section.
- [ ] The CLI commands table lists every `vidbyte-cli` command in the body, each described from its
      help text in the CLI source.
- [ ] Every `vidbyte-cli` line uses the full binary name with root options first, and every
      command path and long option exists in `contracts/cli-commands.json` and in `min-cli-version`.
- [ ] The cost is stated before the run, and the skill waits for consent.
- [ ] A skill that installs a CI workflow that starts paid runs uses a trigger (the event plus
      any condition checked before the paid run) that only people with write access can cause,
      never one an outside contributor can cause, asks once, at install, naming the event, the
      most one run can cost and the per-run cap the workflow passes, what leaves the repository,
      and how to stop it, and leaves every credential to the user's CI secret store.
- [ ] Exit 5 stops and asks; `billing top-up --confirm` appears only behind explicit approval.
- [ ] No step asks for a key in the chat.
- [ ] `references/setup.md` and `references/troubleshooting.md` are byte-identical to the canonical copies.
- [ ] The skill was run end to end once against a real repository with the released CLI.

## Success criteria

A public skill meets this contract when an agent that has never seen Vidbyte can install it, pass
or repair preflight, state the cost, run the command without a usage error, read the result, and
deliver something the user can verify, all from the page, and when every command in it can be
traced to the CLI's own contract and to a released version.
