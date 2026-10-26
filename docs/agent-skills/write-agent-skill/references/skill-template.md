# Public skill template

Copy everything below the line into `skills/<slug>/SKILL.md` in cerredz/vidbyte-cli, then copy
`setup.md` and `troubleshooting.md` from this folder into `skills/<slug>/references/`. Replace
every `<...>` placeholder. Delete every `<!-- guidance -->` comment before opening the PR.
`skills/agent-skills/write-agent-skill/SKILL.md` explains each section and the rules behind it.

The folder layout the template assumes:

```
skills/<slug>/
  SKILL.md
  references/
    setup.md            copied from skills/agent-skills/write-agent-skill/references/setup.md
    troubleshooting.md  copied from skills/agent-skills/write-agent-skill/references/troubleshooting.md
```

---

````markdown
---
name: <slug>
description: Use when <the situation in the user's own words, two or three phrasings of it>. <One sentence on what the skill gets done.> Drives the Vidbyte <Agent name> through vidbyte-cli.
metadata:
  agent: <agent-id>
  commands: "<vidbyte-cli command path>, <vidbyte-cli command path>"
  cost: "<exact Vidbyte charge> + <provider usage, if any>"
  writes-files: "<true|false>"
  min-cli-version: "<x.y.z>"
  status: <stable|preview>
  docs: "https://vidbyte.pro/agents/skills/<slug>"
---

# <Title in plain words, named after the problem>

## The problem

<!-- The problem in detail, in several paragraphs. No product pitch. No "When to use" or "When not
     to use" lists: the frontmatter description already says when to use the skill. -->
<Who has the problem and in what situation.>

<What goes wrong, in concrete terms. A short list is fine when the failures are distinct.>

<What it costs the user, and why it is hard to catch or solve without help.>

<What this skill does not cover, when that limit matters.>

## How vidbyte-cli solves it

<!-- Name vidbyte-cli, the agent, and its commands. Say what the agent does that the user's coding
     agent cannot do well alone, and what the user has at the end. -->
This skill drives `vidbyte-cli` and its <Agent name>. <What the agent does, and why it fits this
problem.>

<What the user has at the end.>

## Goal and done-when

<!-- One goal in one paragraph, then an observable completion check. -->
<The goal in one paragraph.>

Done when: <an observable state, such as "every call site the run reported is changed, the test
suite passes, and the user has seen the list of files changed">.

## Preflight

Run these in order. If any fails, read `references/setup.md`, fix only what failed, and run the
preflight again.

```bash
vidbyte-cli --version                  # must be <min-cli-version> or newer
vidbyte-cli --json whoami              # exit 4 means no key is stored
<one agent-specific check, such as: vidbyte-cli runtime doctor>
```

## Gather inputs

<!-- Read the repository first. Ask the user only for what cannot be found. -->
1. <What to read from the repo, such as `git log --oneline -30` for completed work.>
2. <What to read next.>
3. Ask the user only for: <the one or two things the repo cannot answer>.

## Steps

<!-- The whole procedure from preflight to hand-back, one or two lines per step. Name the
     vidbyte-cli command a step runs, and end each step with the section that holds its details. -->
1. **Check the setup.** Run `vidbyte-cli --version` and `whoami`<, plus the agent-specific check>.
   (Preflight)
2. **<Gather the inputs>.** <What is read from the repository.> (Gather inputs)
3. **Get the user's yes** for the cost and the changes. (Cost and consent)
4. **<Run the agent>** with `vidbyte-cli <command>`. (Run)
5. **<Read the result>.** <What is taken from it.> (Read the result)
6. **<Deliver>.** <What the user gets and what to offer next.> (Deliver)

## CLI commands

<!-- Every vidbyte-cli command the skill runs or asks the user to run, in the order they come up.
     Describe each one from its help text in the CLI source, never from memory. -->
Every `vidbyte-cli` command this skill runs or asks the user to run, in the order they come up.

| Command | Used in | What it does |
|---|---|---|
| `vidbyte-cli --version` | Preflight | Prints the installed CLI version. This skill needs <min-cli-version> or newer. Free and offline. |
| `vidbyte-cli --json whoami` | Preflight | Shows the Vidbyte account behind the stored key. Exit 4 means no key is stored. Free. |
| `vidbyte-cli --json --no-input <command>` | Run | <What it does, what it costs, and whether it writes files.> |

## Cost and consent

This skill runs `<command>`, which costs <exact charge> from the user's Vidbyte balance<, plus
model usage billed to their own provider account>. <It edits files in this workspace. | It does
not edit any files.>

Before running it, tell the user the cost and what will change, and wait for a yes. Generate one
idempotency key for this run, such as `<slug>:<YYYYMMDD>:<short-random>`, and reuse it if you
must retry the same admission.

## Run

```bash
vidbyte-cli --json --no-input <command> \
  <--flag value> \
  <--flag value> \
  --idempotency-key "<key>" \
  -- "<task text>"
```

<!-- Keep `-- "<task text>"` only when the command takes a positional task (runtime persistence,
     runtime same-host-ensemble, runtime stages run, research start); `--` stops a task that
     starts with "-" from being read as a flag. agents suggest run takes --goal instead, and it
     takes no --idempotency-key: it buys no admission, and each model call is billed by usage. -->
<!-- Explain each non-obvious flag choice in one line, so a reader knows why this skill sets it. -->
- `<--flag value>`: <why this value for this problem>.

## Read the result

The result is one JSON document, `{"schema_version": 1, "kind": "<kind>", "data": {...}}`.

| Field | Use it for |
|---|---|
| `data.<field>` | <what to do with it> |
| `data.<field>` | <what to do with it> |

## Deliver

<!-- How to present the result, then what to offer next. -->
1. <Show the user ... in this format ...>
2. <Offer the next step, naming the skill to chain to, such as `<sibling-skill>`.>

## Troubleshooting

| Exit | Do this |
|---|---|
| 2 | Fix the flag the message names. Do not retry unchanged. |
| 4 | Stop. Follow `references/setup.md` step 2. |
| 5 | Stop. Follow "Out of credits" in `references/troubleshooting.md`. Never top up without the user's yes. |
| Other | Read `references/troubleshooting.md`. |

## Guardrails

- Never ask the user to paste an API key into the chat, and never print one.
- Never pass `vidbyte-cli billing top-up --confirm` unless the user explicitly approved that top-up.
- Run at most one file-writing Vidbyte agent in a workspace at a time.
- <A guardrail specific to this skill, such as "never push or open a PR; leave the changes for the user to review".>

Docs: <https://vidbyte.pro/agents/skills/<slug>>
````
