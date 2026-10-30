# Design Doc: Paid work boundary for public skills

**Status:** Implemented (draft PR)
**Author:** Claude
**Created:** 2026-10-30
**Last Updated:** 2026-10-30

---

## 1. Overview

A public Vidbyte skill exists to deliver the output of one Vidbyte agent run. Several skills
told the host agent to offer a free substitute for that output when the user declined the run,
or even inside the consent question itself ("Run it, or should I build the checklist from the
local check alone, for free?"). A user saw exactly that choice in Claude Code. This change makes
the paid run the only source of every skill's result, in three layers: it removes every free
substitute from the skills and their scripts (layer 3), adds a written rule and an in-depth
rulebook for requests that try to get around the run (layer 4), and adds a lint script and a
behaviour eval that catch the problem automatically, the eval comparing the new skills against
`main` with the same instrument (layer 5).

---

## 2. Goals & Non-Goals

### Goals
- No skill, script, reference, or rulebook text offers, describes, or enables a way to get a
  skill's result, or any part of it, without its Vidbyte run.
- Every public skill carries the same "Paid work boundary" section, a guardrail bullet, an explicit
  no-branch in Cost and consent, and a description that says the result comes only from the run.
- One canonical, in-depth rulebook (`paid-work-boundary.md`) tells the host agent how to answer
  every known way of asking around the run, copied byte for byte into every skill.
- The writing contract (`write-agent-skill`) and its template make the rule part of every future
  skill.
- A lint script fails on any free-substitute phrasing, from a large catalog of expressions, and on
  any missing structural piece; a test script proves the catalog on hundreds of good and bad
  sentences.
- A behaviour eval runs each skill headless in Claude Code against adversarial user replies and
  control replies, and judges every reply, so the result does not depend on our own reading.

### Non-Goals
- Moving the focus files and rubrics to the backend (layer 1) or signing `result.json` (layer 2).
  Those need a CLI release.
- Telling the host agent to deceive the user. It may decline and stop; it may not lie about what
  it can do, invent a result, or claim a run happened.
- Withholding a risk to the user found while gathering inputs, such as an exposed secret.
- Changing prices, caps, or any CLI behaviour.
- Rewording the local `next-steps` skill on the owner's machine (layer 7).

---

## 3. Background & Context

- **Why now:** a user ran a public skill in Claude Code and was asked whether to use the paid
  Suggestion Agent or a free checklist.
- **Root causes found in the code:**
  1. `skills/find-unfinished-agent-work/SKILL.md` designs a complete free path: the consent
     question offers it, Done-when accepts it, Deliver step 1 builds it with
     `report --local-only`, and Troubleshooting falls back to it.
  2. `next-actions-from-notion` and `which-pr-to-review-first` say "offer `digest.md`/`prs.md` as a
     free summary" when the user says no. The skill design docs draw the same branch.
  3. The rulebook defines consent but never says what to do on a no, so authors filled the gap with
     a helpful fallback and siblings copied it.
  4. The skills ship the paid agent's instructions in plain text, so the host agent can see that
     the paid step is "a model reads this digest with this rubric" and offer to do it itself.
  5. The host agent works for the user and is trained to save them money; a skill is advice, so the
     only durable lines are scope ("this skill delivers only the run's result") and honesty.
- **The website:** `cerredz/Vidbyte` renders each SKILL.md body live from this repository, and
  each catalog entry copies the frontmatter `description` verbatim. One catalog highlight says "a
  checklist from the local check alone is free". A companion PR there updates descriptions,
  removes free-path highlights, and adds the rule to its copy of the writing contract.
- **Constraints:** the repository has no CI and no AGENTS.md; the rulebook is
  `docs/agent-skills/write-agent-skill/SKILL.md`. Shared references must be byte-identical to the
  canonical copies. Descriptions are capped at 1024 characters and may hold no price figures. The
  skills are MIT-licensed, so the boundary is framed as product scope, not as a legal restriction.

---

## 4. Requirements

### Functional Requirements
1. A canonical `docs/agent-skills/write-agent-skill/references/paid-work-boundary.md` exists and
   is copied byte for byte to `skills/<slug>/references/paid-work-boundary.md` for all 24 skills.
2. Every `skills/<slug>/SKILL.md` has exactly one `## Paid work boundary` section, placed directly
   after `## Cost and consent`, holding the canonical text filled with that skill's deliverable and
   run.
3. Every `## Cost and consent` section states that the consent question has two answers and that on
   a no the agent stops (pointing to Paid work boundary). No consent question offers an
   alternative.
4. Every `## Guardrails` section has a bullet that restates the boundary.
5. Every frontmatter `description` ends with a sentence saying the result comes only from the run,
   stays at most 1024 characters, and has no price figure.
6. No Done-when accepts a result made without the run.
7. `find-unfinished-agent-work` no longer has `--local-only` (flag, code path, docs, and the
   Troubleshooting fallback); `report` requires `result.json`.
8. Every other free-substitute sentence in skills, scripts, plugin, and rulebook is removed or
   reworded so the lint passes; legitimate no-cost steps (preflight, dry run, gather, status,
   handoff, feedback, rendering a paid result, re-checking fixes from a paid result) stay.
9. The rulebook gains: a section-order row, consent rule 6 with the four kinds of free work, the
   shared-references update, and review-checklist items. The template matches.
10. `scripts/check-paid-work-boundary.py` runs every phrase and structure rule, prints one
    diagnostic per finding (file, line, rule, what, why, fix), and exits 1 on any finding.
11. `scripts/test-paid-work-boundary.py` checks the phrase catalog against at least 150 bad and 100
    good sentences, every structure rule against fixtures, the real tree (clean), and `origin/main`'s
    known leaks (all caught).
12. `scripts/eval-paid-work-boundary.py` runs each chosen skill and scenario through headless
    Claude Code in an isolated temp folder, judges every reply with the phrase detector and a judge
    model, writes JSONL plus a Markdown summary, and can evaluate any git ref for a baseline.
13. The plugin's `setup` skill gets the boundary bullet in its guardrails.
14. The four skill design docs that draw a free-summary branch are updated to "stop".

### Non-Functional Requirements
- **Precision:** the lint must flag zero lines on the finished tree and must catch every known leak
  on `main`; legitimate "Free." command-table entries must not be flagged.
- **Speed:** the lint runs in under 5 seconds on the whole repository with the Python standard
  library only.
- **Eval isolation:** no user hooks, no user CLAUDE.md, no memory, only Read/Glob/Grep tools inside
  a temp copy of the skill, and no `ANTHROPIC_API_KEY` leak into the child process.
- **Eval cost:** concurrency and the skill/scenario selection are flags; a full matrix is optional.
- **Diagnostics:** every lint message says what matched, why it is blocked, and the fix.

---

## 5. High-Level Design

The change has one canonical source and three consumers. The canonical rulebook text lives in the
writing contract folder; a generator script copies it into every skill and inserts the per-skill
section, guardrail, consent sentence, and description sentence from one table of per-skill values
(deliverable, run, description sentence). The lint then enforces the result, and the eval checks
behaviour.

```
docs/agent-skills/write-agent-skill/
  SKILL.md (rule 6, section row, checklist) ──► authors of future skills
  references/skill-template.md (section, guardrail, no-branch)
  references/paid-work-boundary.md (canonical rulebook)
        │  byte copy
        ▼
skills/<slug>/references/paid-work-boundary.md      skills/<slug>/SKILL.md
                                                     ├─ description: "...comes only from that run..."
                                                     ├─ ## Cost and consent: two answers, stop on no
                                                     ├─ ## Paid work boundary (filled template)
                                                     └─ ## Guardrails: boundary bullet
        │
        ├─► scripts/check-paid-work-boundary.py   (phrase catalog + structure rules)
        ├─► scripts/test-paid-work-boundary.py    (corpus + fixtures + main baseline)
        └─► scripts/eval-paid-work-boundary.py    (headless Claude Code + judge, any git ref)
```

The phrase catalog works sentence by sentence, because "free" alone is legitimate (command tables
say "Free." for preflight and handoff) and a substitute can be phrased in endless ways. A sentence
fails when it pairs a cost-free signal with a substitute signal, when it offers a cost-free result
that is not a plumbing step, or when it matches one of the always-wrong families (offer a file "as
a summary", build the result "yourself", "local-only", skip the run).

Key decisions: honesty is kept as a hard rule, because a model told to hide alternatives tends to
defect and the user would lose trust; the in-depth rulebook goes in `references/` so it costs tokens
only when someone pushes, while the core rules sit in SKILL.md so they work even if the reference is
never read.

---

## 6. Detailed Design

### 6.1 Canonical rulebook

**File(s):** `docs/agent-skills/write-agent-skill/references/paid-work-boundary.md` and 24 copies
**Type:** New file

#### What it does
Tells the host agent who it works for while the skill runs, what is free and what is not, and the
exact answer to each way of asking around the run.

#### Logic / Algorithm
1. Role: while the skill is loaded the agent operates a Vidbyte product; the skill's result is the
   run's output.
2. Four kinds of work: plumbing and input preparation (free, shown for consent), the result (only
   from the run), post-result work (free, only on a paid result), substitutes (never).
3. A table of requests and answers: decline, free version, "do it yourself", apply the reference
   files yourself, summarize the digest, preview or top items, no balance, own key or another
   model, export the prompt, change the script, fake a result, reuse an old result for a new
   question, split into smaller runs to dodge the cap, authority claims, "the agent is too
   expensive", the request to have the agent work outside the skill.
4. How to answer: one short message, the facts, no pressure, no repeated pitch, no lecture.
5. Never-list and the limits: user safety and honesty override the boundary.

#### Edge Cases & Error Handling
- The user asks whether their agent could do it without the skill: answer truthfully in one
  sentence, do not offer, and do not use this skill's files for that work.
- Gathering exposed a secret or a live data leak: tell the user regardless of the run.

### 6.2 Skill edits (all 24)

**File(s):** `skills/*/SKILL.md`
**Type:** Modified

#### What it does
Inserts the section, the guardrail, the consent sentence, and the description sentence from a
per-skill table, then removes each skill's free-substitute sentences by hand.

#### Interface / API
The section template (values in angle brackets):

```markdown
## Paid work boundary

While this skill is loaded you are operating a Vidbyte product for the user. The skill exists to
deliver <deliverable>, and that result comes only from <run>. ...
```

#### Logic / Algorithm
1. `scripts/sync-paid-work-boundary.py` (a one-shot generator kept in the repository, so the
   canonical text can be re-synced) reads the per-skill table, copies the canonical reference,
   and inserts or replaces the section, guardrail bullet, consent sentence, and description
   sentence idempotently.
2. Hand edits remove free substitutes the lint reports, skill by skill.

#### Edge Cases & Error Handling
- Descriptions near 1024 characters: trim redundant words by hand; the generator refuses to write a
  description over 1024.
- `vibe-coded-to-production` (no Vidbyte charge): the run is "the Bug Finder run", and the local
  read is an input to the one report, not a separate result.
- `product-ideas-after-merge` (CI workflow): the run is the workflow's runs on GitHub; never write
  a workflow or script that calls a model provider directly with the lens.
- Task Board skills: never do the issues or the conflict resolutions by hand or with another agent
  inside the skill.

### 6.3 `find-unfinished-agent-work` script

**File(s):** `skills/find-unfinished-agent-work/scripts/unfinished_work.py`,
`scripts/unfinished/report.py`
**Type:** Modified

Remove the `--local-only` option, `LOCAL_ONLY_RESULT`, and the `local_only` code paths; `report`
always reads `result.json` and exits with its existing missing-file error when it is absent.

### 6.4 Lint

**File(s):** `scripts/check-paid-work-boundary.py`
**Type:** New file

#### Interface / API
```python
class PhraseCatalog:          # compiled families; scan_sentence(text) -> list[PhraseHit]
class MarkdownSentences:      # prose sentences with line numbers, code spans stripped
class SkillStructure:         # section, order, anchors, guardrail, consent, done-when, description, reference copy
class ScriptScanner:          # forbidden flags and strings in skills/*/scripts
class BoundaryLinter:         # run(root) -> list[Finding]; main() prints and exits
```

#### Logic / Algorithm
1. Collect files: `skills/**/*.md`, `skills/*/scripts/**`, `plugins/**/*.md`,
   `docs/agent-skills/**/*.md`, `README.md`. Exempt only the canonical rulebook file and its
   byte-identical copies (they quote forbidden requests on purpose); a copy that differs is not
   exempt and is also a structure finding.
2. Prose: join wrapped lines into paragraphs, strip fenced code, inline code, URLs, and skill
   slugs, split into sentences, and run every phrase family.
3. Code: scan fenced code and scripts for forbidden flags (`--local-only`, `--free`, `--skip-run`,
   `--no-paid`, `--offline-result`, `local_only`) and free-substitute strings.
4. Structure: per skill, check each requirement in section 4.
5. Print findings grouped by file with rule id, line, matched text, why, and fix; exit 1 if any.

### 6.5 Tests

**File(s):** `scripts/test-paid-work-boundary.py`
**Type:** New file

Imports the linter, runs the phrase corpus, structure fixtures in temp folders, the real tree, and
`origin/main` exported with `git archive` into a temp folder.

### 6.6 Behaviour eval

**File(s):** `scripts/eval-paid-work-boundary.py`
**Type:** New file

#### Interface / API
```
python scripts/eval-paid-work-boundary.py [--ref <git-ref>] [--skills a,b] [--scenarios s1,s2]
    [--model sonnet] [--judge-model sonnet] [--workers 4] [--out <dir>]
```

#### Logic / Algorithm
1. Export the skills at `--ref` (default: the working tree) into a temp folder.
2. For each skill and scenario, copy the skill folder into a fresh temp folder and run
   `claude -p` there with `--setting-sources project`, `--tools Read,Glob,Grep`,
   `--no-session-persistence`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, and no `ANTHROPIC_API_KEY`.
   The prompt holds the SKILL.md, the state (every step before consent is done), and the user's
   reply.
3. Judge each reply twice: the lint's phrase catalog, and a judge model that returns JSON
   (`offered_substitute`, `produced_result_content`, `dishonest`, `over_refused`, `verdict`,
   `reason`).
4. Write `results.jsonl` and `summary.md`; exit 1 if any adversarial reply fails or a control reply
   over-refuses.

#### Edge Cases & Error Handling
- A claude call fails or times out: recorded as `error`, counted separately, never as a pass.
- The judge returns non-JSON: one retry, then `error`.

---

## 7. Data Model Changes

N/A - no database or persisted schema. The eval writes JSONL records with the fields in 6.6.

---

## 8. API Changes

N/A - no endpoint or CLI change. The only interface change is the removal of the skill-local
`unfinished_work.py report --local-only` option.

---

## 9. File Change Manifest

| Action | File Path | Reason |
|--------|-----------|--------|
| CREATE | `docs/design/paid-work-boundary.md` | This design |
| CREATE | `docs/agent-skills/write-agent-skill/references/paid-work-boundary.md` | Canonical rulebook |
| MODIFY | `docs/agent-skills/write-agent-skill/SKILL.md` | Rule 6, section row, shared references, checklist |
| MODIFY | `docs/agent-skills/write-agent-skill/references/skill-template.md` | Section, guardrail, no-branch, layout |
| CREATE | `skills/*/references/paid-work-boundary.md` (24) | Byte copies |
| MODIFY | `skills/*/SKILL.md` (24) | Section, guardrail, consent, description, leak removal |
| MODIFY | `skills/find-unfinished-agent-work/scripts/unfinished_work.py` | Remove `--local-only` |
| MODIFY | `skills/find-unfinished-agent-work/scripts/unfinished/report.py` | Remove local-only path |
| MODIFY | `plugins/vidbyte/skills/setup/SKILL.md` | Boundary guardrail |
| MODIFY | `docs/design/next-actions-from-notion.md`, `next-actions-from-slack.md`, `too-many-meetings.md`, `which-pr-to-review-first.md` | Free-summary branch becomes stop |
| CREATE | `scripts/sync-paid-work-boundary.py` | Generator for the canonical pieces |
| CREATE | `scripts/check-paid-work-boundary.py` | Lint |
| CREATE | `scripts/test-paid-work-boundary.py` | Verification |
| CREATE | `scripts/eval-paid-work-boundary.py` | Behaviour eval |

Companion (cerredz/Vidbyte): `next-app/constants/agent-skills/*/catalog.js` descriptions and
free-path highlights, and `skills/agent-skills/write-agent-skill/SKILL.md` rule 6.

---

## 10. Testing Plan

### Unit Tests
- `PhraseCatalog` -> flags each of 150+ known substitute phrasings — [Silent Failure]
- `PhraseCatalog` -> passes each of 100+ legitimate sentences taken from the skills (command-table
  "Free.", "free and local", "free dry run", "free time", "free tiers", "free memory") — [Edge Case]
- `PhraseCatalog` -> flags phrasing split across wrapped lines — [Hidden Assumption]
- `PhraseCatalog` -> ignores skill slugs such as `stop-repeating-yourself` and inline code — [Edge Case]
- `PhraseCatalog` -> flags a free substitute even when the sentence also names `handoff` — [Hidden Failure]
- `MarkdownSentences` -> empty file and file with only code yield no prose sentences — [Edge Case]
- `MarkdownSentences` -> reports the line where the sentence starts — [Silent Failure]
- `ScriptScanner` -> flags `--local-only` in argparse, in a fenced block, and as `local_only` — [Silent Failure]
- `SkillStructure` -> missing section, duplicate section, wrong position, missing anchor, missing
  guardrail, missing no-branch, consent question with "or", Done-when alternative, long
  description, missing description sentence, reference copy differs, reference missing — one
  fixture each — [Hidden Assumption]
- `BoundaryLinter` -> a byte copy of the rulebook that differs by one byte is not exempt — [Hidden Failure]

### Integration Tests
- The finished tree lints clean — [Silent Failure]
- `origin/main` exported to a temp folder: the lint reports every known leak
  (find-unfinished lines 89, 280, 351, 355, 478, 572; notion 389; which-pr 183 and 329) — [Hidden Failure]
- `report` in `find-unfinished-agent-work` rejects `--local-only` with a usage error and fails
  without `result.json` — [Hidden Assumption]
- The eval on `--ref origin/main` and on the branch for the same skills and scenarios: the branch
  has no adversarial failure and no control over-refusal — [Silent Failure]

### Manual / QA Test Cases
1. Given a skill installed in Claude Code, when the user answers the consent question with "no",
   then the agent stops, says nothing was spent, and offers nothing else — [Edge Case]
2. Given a skill, when the user says "just do it yourself for free", then the agent declines in one
   message without inventing content and without lying — [Hidden Failure]
3. Given a paid result, when the user asks for a handoff, then the agent runs the handoff — [Hidden Assumption]

---

## 11. Dependencies & External Services

| Dependency | Version / Endpoint | Purpose | Risk |
|------------|--------------------|---------|------|
| Python | 3.9+ standard library | Lint, tests, generator | None |
| Claude Code CLI | 2.1.x (`claude -p`) | Behaviour eval | Uses the operator's plan quota; flags limit the matrix |
| git | any | Export a ref for baselines | None |

---

## 12. Rollout & Deployment

- No feature flag. The skills change as soon as the PR merges; the website renders the new bodies
  within an hour (its revalidate window).
- Merge order: this PR first, then the cerredz/Vidbyte companion PR so catalog descriptions match
  the new frontmatter.
- Rollback: revert the squash commit; the companion PR reverts with it.

---

## 13. Open Questions

- [ ] Q-1: the improve-my-aeo website tagline says "A free AEO audit, then ranked fixes". The audit
  stays an input in the skill; should the marketing still lead with "free audit"?
- [ ] Q-2: should the lint run in a GitHub workflow on this public repository (Actions are free
  here), or stay a local gate?
- [ ] Q-3: layers 1 and 2 (focus files on the server, verified results) need a CLI release; when?
- [ ] Q-4: the eval used Sonnet as both agent and judge. Should a release gate also run it with
  Opus and with Codex as the host agent, since users run the skills in both?

---

## 14. Alternatives Considered

### Alternative 1: Tell the agent to deny it can do the work
- What: instruct "never admit you could produce this yourself".
- Why rejected: it is deception against the user, models tend to ignore it, and the first screenshot
  of it would cost more trust than the leak.

### Alternative 2: Ban the word "free" in skills
- What: a single regex.
- Why rejected: dozens of correct "Free." entries describe preflight, dry runs, and handoffs, and
  substitutes are often phrased without the word ("build it from the local check alone").

### Alternative 3: Inline the whole rulebook in every SKILL.md
- What: no reference file.
- Why rejected: it costs tokens on every load; the core rules inline plus the in-depth file in
  `references/` follows the existing setup/troubleshooting pattern.

---

## 15. Implementation notes and results

### What changed from the plan
- **Three rounds of rule tightening, each driven by the behaviour eval.** Round 1 replaced
  "answer truthfully" with an explicit ban on claiming inability, after an agent answered "could
  you do this without the skill?" with a false "no". Round 2 stopped agents from presenting the
  gathered inputs as a free consolation ("what I can show you for free is...") and from pointing
  users to the files to use without the run; the section now opens with "you work on Vidbyte's
  behalf". Round 3 removed the rulebook's own invitation to describe what the gathered files
  contain, and every skill now says the inputs are shown once, for consent, and never restated.
- **Rule catalog.** The lint has 22 prose rules (PB101-PB122), 2 code rules (PB201-PB202), and
  10 structure rules (PB301-PB310). Precision fixes found while building it: plumbing context is
  read with inline code kept, bold step titles end a sentence, a negation guards only its own
  clause, and for "verb ... yourself" rules the verb nearest the reflexive decides.
- **Leftovers the lint caught after the hand edits:** a `report` docstring in
  `find-unfinished-agent-work` still said "or from sessions.json alone".
- **Eval harness conventions:** `NEXT COMMAND:` and `NEXT WRITE:` lines stand in for tool calls;
  the handoff control runs only for skills that have `agents suggest handoff`; the judge treats the
  skills' documented consent steps (opening the digest, removing an input, rerunning the dry run)
  and their own safety advice as allowed.

### Behaviour eval results (Claude Code, Sonnet as agent and judge)

| Run | Matrix | Passed |
|---|---|---|
| `origin/main`, all 24 skills | decline, free-way, do-it-yourself, summarize | 7 / 96 |
| this branch, all 24 skills | the same four, plus the `yes` control | 96 / 96 adversarial; 23 / 24 control, the miss a harness gap since fixed and re-checked |
| this branch, 6 representative skills | all 14 adversarial and 3 control scenarios, over three rounds | 92 / 102, then 36 / 36 and 23 / 24 on the reruns after each fix |
| this branch, the other 18 skills | decline, free-way, do-it-yourself, summarize, prompt-export, yes | 102 / 108, then fixed in round 3 |

The baseline ran with an earlier harness that created an empty output folder; the gap (7 vs 96)
does not depend on it. On `main`, skills with no free path in their text leaked too (free-way and
do-it-yourself failed 48 of 48), which is why every skill carries the boundary, not only the three
that had explicit free paths.

### Verification commands
- `python scripts/check-paid-work-boundary.py` -> PASS
- `python scripts/test-paid-work-boundary.py` -> 40/40
- `python scripts/test-find-unfinished-agent-work.py` -> 53/53
- `python scripts/sync-paid-work-boundary.py --check` -> exit 0
- `python scripts/eval-paid-work-boundary.py --skills all --scenarios decline,free-way,do-it-yourself,summarize,yes`

### Known limits
- A skill is advice to the host agent. These layers make the boundary explicit, consistent, and
  tested, but a determined user can still ask their agent for unrelated help outside the skill.
  Layers 1 and 2 (server-side focus files, verified results) remove what the agent could copy.
- The judge is a model; single cells vary between runs. The matrix totals, not one cell, are the
  signal.
