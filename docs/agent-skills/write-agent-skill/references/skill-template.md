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
---

# <Title in plain words, named after the problem>

<!-- One or two sentences: the problem, and what the user has at the end. No product pitch. -->

## When to use

<!-- Concrete situations the user would recognize, as they would say them. -->
- "<a phrase the user might type>"
- "<another phrasing>"
- <a situation, such as "the same bug was fixed twice and came back a third time">

## When not to use

<!-- Each line names the sibling skill or the plain action that fits better. -->
- <situation> — use `<sibling-skill>` instead.
- <situation> — just do it directly; this skill adds cost without adding value.

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
     starts with "-" from being read as a flag. agents suggest run takes --goal instead. -->
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
````
