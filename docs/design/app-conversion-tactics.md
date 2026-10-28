# Design Doc: App Conversion Tactics Agent Skill

**Status:** Draft
**Author:** Claude
**Created:** 2026-10-28
**Last Updated:** 2026-10-28

---

## 1. Overview

`app-conversion-tactics` is a public Vidbyte agent skill for app founders who want more of their users to pay. When a user asks their coding agent something like "how do I get more people to subscribe?", the skill reads facts about the user's app from its repository, then makes one `vidbyte-cli agents suggest run` call. That call carries the user's request verbatim as `--goal` and a sourced **fact ledger** of in-app conversion tactics as `--files`. The ledger is derived from Frederick James's X article "The 48 App Conversion Tactics." (2026-10-25), and the run also passes a short rubric as `--instructions`. The Suggestion Agent returns ranked tactics for that app, each citing the ledger facts it rests on, a first step in the user's stack, and a metric to check. The skill text and ledger live in the public `cerredz/vidbyte-cli` repository; the listing lives on `/agents/skills` in this workspace's `vidbyte` repository.

---

## 2. Goals & Non-Goals

### Goals
- Turn the article into a fact ledger with a stable ID for every fact, the article's own tier (allowed / gray area / banned), its provenance (article text, an image, an embedded post, or a number derived from them), and, wherever the article leans on Apple, Apple's own guideline wording fetched raw on 2026-10-28.
- Make that ledger the suggestion agent's source of truth by passing it as `--files` on every run, so it enters the generator's and critic's context. A free `--dry-run` must report it `included`, not `truncated`.
- Pass the user's actual prompt as `--goal`, verbatim (up to the CLI's 4,096-character limit).
- Ship the skill to the `write-agent-skill` contract and list it on `/agents/skills/app-conversion-tactics` through the `add-agent-skill` process. This includes the llms brief, the public mirror path, and the regenerated `llms.txt` that the field guide requires.
- Never let the agent recommend a tactic the article marks as banned, and never present a gray-area tactic without the "describe it in Notes for Review and get explicit approval" step.

### Non-Goals
- No change to vidbyte-cli code. The skill drives released CLI behavior only.
- No bundled script. The user asked for the skill to "simply" add the facts to the prompt, and one `vidbyte-cli` call does that.
- No Google Play policy check. The ledger's tiers are Apple App Review tiers, and the skill says so for Android apps.
- No claim of App Review approval and no revenue promise. The article's numbers are one founder's results and are labeled that way.
- No republishing of the article. The ledger paraphrases it and quotes only short spans, with attribution and a link.

---

## 3. Background & Context

- **Why now:** Vidbyte grows through agent skills; each one is a page named after a problem people search for, and every install is also a `vidbyte-cli` install (`skills/agent-skills/add-agent-skill/SKILL.md` section 11). "App conversion" is a high-intent, money-adjacent search that no published skill covers (checked against `skills/` on cerredz/vidbyte-cli `main` 48577c9 and `next-app/constants/agent-skills/catalog.js` on Vidbyte `main` 86c8cfe9b).
- **The problem:** Indie app founders leave revenue on the table. Some miss tactics the large apps use, such as win-back offers and cancellation-sheet retention messages. Others copy tactics that get apps rejected, such as countdown offers and Live Activity promotions. Generic LLM advice mixes the two and rarely cites Apple's rules.
- **Source material (read raw):** the article JSON from `api.fxtwitter.com` (56 text blocks, two YAML flow snippets, nine images), the three posts it embeds (2085709435498807593, 2104522475761455536, 2112162857567625568) and their two images, Apple's App Review Guidelines HTML (fetched 2026-10-28T19:03:51Z), and Apple's Retention Messaging API documentation JSON (fetched 2026-10-28).
- **Findings that shape the ledger:**
  - The title says 48 tactics; the body lists 18 (8 allowed, 3 gray, 7 banned). The gray section numbers its third item "2" again.
  - The article cites "§§ 2.3, 2.5, 3.1, 3.2, 4.5 and 5.1", but the rule against charging for built-in capabilities is **§ 4.10 Monetizing Built-In Capabilities**.
  - Apple's § 3.1.3(b) adds a condition to web-to-app funnels that the article does not state: web-bought access may unlock in the app only "provided those items are also available as in-app purchases within the app".
  - The paywall mock in an embedded post shows "Save 88% vs Weekly" on $79.99/yr against $3.99/wk, which is a 61% saving. The 88% matches the post's default prices ($49.99/yr against $7.99/wk).
  - In the dynamic-paywall example, both paywalls cost $7.99/week; only the design and audience framing change.
- **Current CLI state (verified in source):** `agents suggest run` on internal `main` (6595e66) accepts `--goal` (≤ 4,096 chars, `types/suggestions.py:252`), repeated `--context`, `--files` (.txt/.md/.json, each up to 5,000,000 chars, `services/suggestions/context.py:24`), `--instructions` (≤ 8 files, ≤ 20,000 bytes, `lib/constants/runtime.py:44`), `--category`, `--count`, `--horizon`, `--max-total-tokens`, and `--dry-run`. The default model is gpt-6-sol at high reasoning effort with 2 review rounds (about 5 model calls). The SDK pricebook (`vidbyte/lib/registries/pricing.py:186`) bills it at $2 / $10 per million input / output tokens ($0.20 cached reads), plus Vidbyte's 2%.
- **Release constraint:** PyPI's newest `vidbyte-cli` is 0.2.0 (checked with `pip index versions vidbyte-cli` on 2026-10-28). The usage-billed suggest command without `--idempotency-key` first ships in 0.3.2 (#165, 2026-10-12), which is unpublished. So, like `improve-my-aeo`, the skill declares `min-cli-version: "0.3.2"` and its listing is `coming-soon`.

---

## 4. Requirements

### Functional Requirements
1. The public folder is `skills/app-conversion-tactics/` with `SKILL.md`, `references/conversion-tactics-ledger.md`, `references/conversion-rubric.md`, and byte-identical copies of the canonical `references/setup.md` and `references/troubleshooting.md`.
2. The ledger records every tactic in the article (18), each with a tier, the article's claim, conditions, provenance tags, and, for every gray and banned tactic, at least one verbatim Apple anchor with its section number.
3. The ledger records the article's supporting material as separate facts: the five-part paywall anatomy, the notification and win-back flows, the event vocabulary, the numbers, cross-cutting rules, and verification notes (the discrepancies listed in section 3).
4. Every number the ledger states is tagged `[article]`, `[image]`, `[post]`, or `[derived]`; every `[derived]` number shows its arithmetic.
5. `SKILL.md` passes the user's request verbatim as `--goal`. When the request is longer than 4,096 characters, the skill saves it to `request.txt` outside the repository, passes that file with `--files`, and uses the request's first sentence as `--goal`.
6. `SKILL.md` passes the ledger with `--files` and the rubric with `--instructions` on both the dry run and the paid run.
7. The skill runs a free `--dry-run` before asking for consent, and checks that the ledger's `context_manifest` entry has `status: included`.
8. The skill asks for consent with the cost stated (usage-billed, capped at 250,000 tokens, at most about $3) before the paid run, and never runs it without a yes.
9. The rubric forbids recommending any banned (B-) tactic, including renamed variants. It requires every gray (G-) idea to include the Notes for Review step, and every idea that sends a push notification to carry the opt-in, disclosure, opt-out, and frequency rules. Every idea must cite at least one ledger ID.
10. The Deliver section groups ideas by tier and shows each idea's ledger IDs, first step, and metric. It drops any idea that recommends a B- tactic and tells the user why.
11. The website entry passes `defineAgentSkill`; its `description` is the public frontmatter `description` verbatim; it is imported into `ENTRIES`; it has a `SKILL_SPECS` brief, a `PUBLIC_MARKDOWN_PATHS` entry, and a regenerated `public/llms.txt` line.
12. The public README lists the skill in its skills table with its required CLI version.

### Non-Functional Requirements
- **Context budget:** the ledger stays under 40,000 characters (about 10,000 tokens), far below the 5,000,000-character file cap and the 272,000-token tier threshold in gpt-6-sol's price row, so a cached ledger costs cents per run. The rubric stays under 8,000 bytes, well inside the 20,000-byte `--instructions` cap.
- **Cost ceiling:** `--max-total-tokens 250000`, which bills at most about $2.55 if every token were output; the CLI may overrun by one call, so the skill states "at most about $3".
- **Security:** no API key in chat, on a command line, or in a file the skill writes. `--context` values carry only facts read from the repository or said by the user; never customer data, never secrets.
- **Observability:** the skill reports `usage` and `stop_reason` from the result and points to `vidbyte.pro/usage` for the exact charge.
- **Reliability:** every exit code has one documented action (2 fix the command, 4 setup, 5 stop and ask, 127 not installed, other → troubleshooting).
- **Copyright:** article quotes stay short (one sentence or a label), with attribution and a link; Apple quotes are single sentences cited by section.

---

## 5. High-Level Design

The skill is a thin, judgment-rich wrapper around one Suggestion Agent run. The judgment lives in three places: `SKILL.md` (which repository facts to gather, which flags fit a conversion question, how to deliver), the ledger (the facts), and the rubric (how the agent must use those facts). The coding agent reads the user's app repository for up to eight facts: platform, purchase SDK, products and prices, paywall, notification permission, ATT, Live Activities, and web checkout. It asks at most two questions the repository cannot answer: current funnel numbers, and acquisition channels and countries. It then runs a free dry run, states the cost, waits for a yes, and runs the agent once.

```
user prompt ──► coding agent (skill) ──► reads app repo ──► --context facts
                     │
                     ├─ --goal "<user prompt, verbatim>"
                     ├─ --files references/conversion-tactics-ledger.md   (task data: the source of truth)
                     ├─ --instructions references/conversion-rubric.md    (system-prompt addendum: how to use it)
                     ▼
     vidbyte-cli --json --no-input agents suggest run ... --dry-run   (free; manifest shows ledger "included")
                     ▼  consent
     vidbyte-cli --json --no-input agents suggest run ...             (usage-billed, ≤ 250,000 tokens)
                     ▼
     generator ⇄ critic (2 rounds) via Vidbyte model gateway ──► suggestions.result
                     ▼
     coding agent groups ideas by ledger tier, drops any B- idea, offers `agents suggest handoff`
```

Key decisions:
- **Facts as `--files`, rules as `--instructions`.** The CLI treats file content as task data and never as instructions (`context.py` docstring), and the agent can cite file content through `evidence_refs`. The rules for using the ledger must bind the generator and every critic, so they travel as the system-prompt addendum that `--instructions` provides.
- **The article is the source of truth; Apple is corroboration.** Every fact comes from the article. Apple's wording is attached as an anchor and never replaces the article's claim; where Apple adds a condition or the article mis-cites, a `V-` verification note says so.
- **`--horizon any`.** Other horizons drop ideas after they are generated and billed (`service.py:516`).
- **Categories `business_growth`, `product_experience`, `quick_wins`, `experiment`, `risk_prevention`.** Revenue levers, paywall and onboarding UX, cheap fixes, A/B tests (which the article asks for everywhere), and compliance guards.
- **`--count 8`.** The ledger has 18 tactics; 8 is a slate an owner can act on in one planning session.

---

## 6. Detailed Design

### 6.1 Public SKILL.md

**File(s):** `skills/app-conversion-tactics/SKILL.md` (cerredz/vidbyte-cli)
**Type:** New file

#### What it does
Teaches a coding agent to turn the user's conversion question into one Suggestion Agent run with the ledger attached, and to deliver ranked, tier-labeled tactics.

#### Interface / API
```yaml
---
name: app-conversion-tactics
description: Use when ... (starts "Use when", quotes three phrasings, ≤ 1024 chars, no price; names the Vidbyte Suggestion Agent and vidbyte-cli)
metadata:
  agent: "suggestion-agent"
  commands: "agents suggest run, agents suggest handoff"
  cost: "Model usage from the Vidbyte balance: gpt-6-sol at $2 / $10 per million input / output tokens + 2%, about 5 model calls, capped at 250,000 tokens per run"
  writes-files: "false"
  min-cli-version: "0.3.2"
  status: stable
  docs: "https://vidbyte.pro/agents/skills/app-conversion-tactics"
---
```
Sections, in contract order: Title, The problem, How vidbyte-cli solves it, Goal and done-when, Preflight, Gather inputs, Steps, CLI commands, Cost and consent, Run, Read the result, Deliver, Troubleshooting, Guardrails.

#### Logic / Algorithm
1. Preflight: `vidbyte-cli --version` (0.3.2 or newer), `vidbyte-cli --json whoami` (exit 4 → `references/setup.md`).
2. Gather inputs: take the request verbatim. Read the app repository for at most eight facts, each one `--context` value. Ask at most two questions. Make an output folder outside the repository.
3. Dry run (free) with the exact paid-run flags plus `--dry-run`; confirm the ledger entry in `data.context_manifest` has `status: included`.
4. State the cost; wait for yes.
5. Paid run; write stdout to `result.json` and stderr to `result.stderr.txt` in the output folder.
6. Read `data.status`, `data.stop_reason`, `data.ideas[]`, `data.usage`, and `data.parent_message`.
7. Deliver grouped by tier; offer `agents suggest handoff` for the idea the user picks.

#### Edge Cases & Error Handling
- Request over 4,096 characters → `request.txt` via `--files`, first sentence as `--goal` (Gather inputs).
- No repository, or not an app → run with `--context "No app repository is available; advice is general."`
- Android-only or web-only app → `--context` says so; the rubric makes the agent label tiers as Apple's.
- Ledger reported `truncated` or missing from the manifest → stop; the skill folder is damaged; reinstall it.
- `status: needs_input` → relay `data.parent_message` and stop.
- An idea that recommends a B- tactic → dropped at delivery, with the reason given.

### 6.2 Fact ledger

**File(s):** `skills/app-conversion-tactics/references/conversion-tactics-ledger.md`
**Type:** New file

#### What it does
Holds every fact derived from the article, in a form the generator and critic can cite by ID.

#### Interface / API
```
ID scheme:  A-01..A-08 allowed · G-01..G-03 gray area · B-01..B-07 banned
            P-01..P-05 paywall anatomy · F-01..F-05 flows and event vocabulary
            N-01..N-xx numbers · X-01..X-xx cross-cutting rules · V-01..V-xx verification notes
Tags:       [article] [image] [post] [derived] [apple]
Per tactic: Tier · Claim · How (as the article describes it) · Conditions · Apple anchor · Ledger notes
```

#### Logic / Algorithm
1. One record per tactic, in the article's order, renumbered where the article repeats a number (V-01).
2. Apple anchors are single verbatim sentences, each with its section number, from the 2026-10-28 snapshot.
3. Derived numbers show their arithmetic, such as `$3.99 × 52 = $207.48`.

#### Edge Cases & Error Handling
- The article promises "more on that later" for dynamic paywalls and never returns to it → recorded as a verification note, not filled in.
- Unsourced claims ("cut churn by 20%", the "App Store Connect CLI") → marked as the author's, with no source given.

### 6.3 Rubric

**File(s):** `skills/app-conversion-tactics/references/conversion-rubric.md`
**Type:** New file

#### What it does
Standing instructions for the generator and every critic: the ledger is the source of truth, tier rules, the required idea shape, ranking, and what not to do.

#### Logic / Algorithm
1. Situation: who the user is and what the run carries.
2. Tier rules: never propose B-; G- needs the Notes for Review step; push ideas carry X-03 and B-02.
3. Idea shape: title in the owner's words; ledger IDs in `summary` and `why_now`; numbers labeled as the author's; `first_action` in the user's stack; `completion_criteria` as a metric.
4. Ranking and don'ts.

#### Edge Cases & Error Handling
- The user's context shows they already ship a B- tactic → the idea is to remove it (`risk_prevention`), citing the B- ID.
- Memory disagrees with the ledger → follow the ledger and its `V-` notes.

### 6.4 Shared references

**File(s):** `skills/app-conversion-tactics/references/setup.md`, `references/troubleshooting.md`
**Type:** New files, byte-identical copies of `docs/agent-skills/write-agent-skill/references/` (themselves byte-identical to Vidbyte's canonical copies).

### 6.5 Public README row

**File(s):** `README.md` (cerredz/vidbyte-cli)
**Type:** Modified: one table row, alphabetical, "0.3.2, needs the next vidbyte-cli release (not on PyPI yet)".

### 6.6 Verification script

**File(s):** `scripts/test-app-conversion-tactics.py` (cerredz/vidbyte-cli)
**Type:** New file

#### What it does
Runs every case in section 10: static contract checks on the skill folder, ledger integrity and arithmetic, quote checks against the article and Apple snapshots, cross-repo equality with the website entry, and real `--dry-run` integration cases through a `vidbyte-cli` command.

#### Interface / API
```
python scripts/test-app-conversion-tactics.py [--contract PATH] [--cli "CMD"] [--article PATH] [--guidelines PATH] [--website PATH] [--canonical PATH]
```
Each optional input that is not given makes its checks FAIL, with a message naming the flag; nothing is silently skipped, so a full pass proves every check ran.

### 6.7 Website listing

**File(s):** `next-app/constants/agent-skills/app-conversion-tactics/catalog.js` (new), `next-app/constants/agent-skills/catalog.js` (import, `ENTRIES`, coming-soon comment), `next-app/lib/llms/briefs/agents.js` (`SKILL_SPECS` entry), `next-app/lib/llms/publicPaths.js` (`/agents/skills/app-conversion-tactics`), `next-app/public/llms.txt` (regenerated by `node --no-warnings scripts/generate-llms-files.mjs`).
**Type:** New + Modified

#### Interface / API
```js
defineAgentSkill({
  slug: "app-conversion-tactics",
  title: "Convert more app users within App Review rules",
  tagline: "Ranked paywall, win-back, and pricing tactics for your app.",
  status: "coming-soon",
  summary: "<≤ 160 chars>",
  description: "<public frontmatter description, verbatim>",
  highlights: [/* 3–6 */],
  agents: ["suggestion-agent"],
  topics: ["growth"],
  tags: ["Paywalls", "Subscriptions", "App Store", "Churn", "iOS"],
  commands: ["vidbyte-cli agents suggest run", "vidbyte-cli agents suggest handoff"],
  minCliVersion: "0.3.2",
  addedOn: "2026-10-28",
  writesFiles: false,
});
```

#### Edge Cases & Error Handling
- The public file is not merged at build time → the coming-soon page logs the expected `fetch-failed` warning; once it is merged, the page renders it.
- A duplicate slug or bad field → `defineAgentSkill` throws at import and the build fails.
- A missing `SKILL_SPECS` entry → the brief builder throws during `next build` (the #831 failure); prevented by adding it in the same commit.

---

## 7. Data Model Changes

N/A - no database, schema, or persisted type changes; the website entry is static content-as-data validated by the existing `agentSkillEntryValidator.js`.

---

## 8. API Changes

N/A - no HTTP route is added or changed; the skill calls the existing `agents suggest run` and `agents suggest handoff` commands, and the website adds static pages through existing routes (`/agents/skills/[slug]`, `/api/markdown`).

---

## 9. File Change Manifest

| Action | File Path | Reason |
|--------|-----------|--------|
| CREATE | `cerredz/vidbyte-cli: docs/design/app-conversion-tactics.md` | This design doc |
| CREATE | `cerredz/vidbyte-cli: skills/app-conversion-tactics/SKILL.md` | The public skill |
| CREATE | `cerredz/vidbyte-cli: skills/app-conversion-tactics/references/conversion-tactics-ledger.md` | The fact ledger (source of truth) |
| CREATE | `cerredz/vidbyte-cli: skills/app-conversion-tactics/references/conversion-rubric.md` | Rules for using the ledger |
| CREATE | `cerredz/vidbyte-cli: skills/app-conversion-tactics/references/setup.md` | Canonical copy |
| CREATE | `cerredz/vidbyte-cli: skills/app-conversion-tactics/references/troubleshooting.md` | Canonical copy |
| CREATE | `cerredz/vidbyte-cli: scripts/test-app-conversion-tactics.py` | Verification script |
| MODIFY | `cerredz/vidbyte-cli: README.md` | Skills table row |
| CREATE | `cerredz/Vidbyte: docs/design/app-conversion-tactics.md` | This design doc |
| CREATE | `cerredz/Vidbyte: next-app/constants/agent-skills/app-conversion-tactics/catalog.js` | Listing entry |
| MODIFY | `cerredz/Vidbyte: next-app/constants/agent-skills/catalog.js` | Import, `ENTRIES`, coming-soon note |
| MODIFY | `cerredz/Vidbyte: next-app/lib/llms/briefs/agents.js` | `SKILL_SPECS` brief |
| MODIFY | `cerredz/Vidbyte: next-app/lib/llms/publicPaths.js` | Markdown mirror path |
| MODIFY | `cerredz/Vidbyte: next-app/public/llms.txt` | Regenerated, never hand-edited |

---

## 10. Testing Plan

### Unit Tests (static, `scripts/test-app-conversion-tactics.py`)
- `SkillContract` → `frontmatter name equals the folder name and the slug` — [Edge Case]
- `SkillContract` → `description starts "Use when", is at most 1024 chars, and has no "$" price` — [Hidden Assumption]
- `SkillContract` → `every metadata field is present and quoted, and status is stable` — [Silent Failure]
- `SkillContract` → `sections appear in contract order and no "## When" heading exists` — [Silent Failure]
- `SkillContract` → `every vidbyte-cli line puts root options before the command path` — [Hidden Assumption]
- `SkillContract` → `every vidbyte-cli command path and long option exists in contracts/cli-commands.json` — [Hidden Failure]
- `SkillContract` → `the Run block passes --goal, --files <ledger>, and --instructions <rubric>` (the facts actually reach the agent) — [Silent Failure]
- `SkillContract` → `the metadata cost cap equals the --max-total-tokens value in Run` — [Silent Failure]
- `SkillContract` → `setup.md and troubleshooting.md are byte-identical to docs/agent-skills/write-agent-skill/references/` — [Silent Failure]
- `SkillContract` → `the docs metadata URL points at /agents/skills/app-conversion-tactics` — [Edge Case]
- `Ledger` → `has exactly 8 A-, 3 G-, and 7 B- tactic records with unique, gap-free IDs` — [Edge Case]
- `Ledger` → `every G- and B- record carries at least one [apple] anchor with a section number` — [Hidden Assumption]
- `Ledger` → `every ID the rubric and SKILL.md cite exists in the ledger` — [Silent Failure]
- `Ledger` → `every [derived] arithmetic line recomputes to the stated result (61%, 88%, 528, 34%)` — [Silent Failure]
- `Ledger` → `is under 40,000 chars; rubric is under 8,000 bytes (inside the 20,000-byte cap)` — [Edge Case]
- `Ledger` → `is valid UTF-8 with no byte-order mark and a .md suffix the CLI accepts` — [Hidden Assumption]
- `Quotes` → `every [apple] quote appears verbatim in the 2026-10-28 Apple snapshot (--guidelines)` — [Silent Failure]
- `Quotes` → `every quoted article span appears verbatim in the article snapshot (--article)` — [Silent Failure]
- `Readme` → `README has one row for the slug naming 0.3.2` — [Edge Case]

### Integration Tests (real CLI `--dry-run`, `--cli`; free, no key read)
- The Run command with `--dry-run` exits 0 with kind `suggestions.result` and `stop_reason: dry_run` — [Hidden Failure]
- Its `context_manifest` has the ledger as `kind: file`, `status: included`, and `chars` equal to the file's length — [Silent Failure]
- The manifest `sha256` equals the first 16 hex characters of sha256 over the ledger text, so the agent receives the file byte for byte — [Silent Failure]
- `settings` echo the skill's choices: five categories, `horizon: any`, `max_total_tokens: 250000`, model gpt-6-sol — [Hidden Assumption]
- A 4,096-character goal passes; a 4,097-character goal exits 2 (the boundary the skill's long-request rule relies on) — [Edge Case]
- A goal with emoji and non-ASCII text passes (the article itself uses emoji) — [Edge Case]
- A missing ledger path exits 2 before any model call (a damaged install is caught for free) — [Hidden Failure]
- `--context` values from Gather inputs appear as separate `kind: context` entries in order — [Hidden Assumption]
- Cross-repo (`--website`): the Vidbyte entry's `description` equals the public frontmatter verbatim; `minCliVersion`, `writesFiles`, and `commands` match `metadata`; `SKILL_SPECS`, `PUBLIC_MARKDOWN_PATHS`, and `llms.txt` contain the slug — [Silent Failure]
- Mocked vs real: only the CLI's model calls are avoided (dry run); no fakes. Silent failure paths between components: truncation (manifest status), drift between public and website copies (cross-repo check), and a flag accepted by `main` but missing from the vendored contract (N050).

### Website gates (Vidbyte; existing suites)
- `python lint/run.py` (N050 checks the entry's commands, N054 the guide change), `node scripts/test-agent-skills-directory.mjs`, `node --no-warnings scripts/generate-llms-files.mjs --check`, `node scripts/test-agent-readable-layer.mjs`, `node scripts/test-page-llms-guides.mjs`, `cd next-app && npm run lint && npm run build`, `python scripts/run_ci.py`. Failures that already exist on pristine `main` are recorded before the change and compared after.

### Manual / QA Test Cases
1. Given the skill installed in a scratch app repository with `vidbyte-cli` 0.3.2+, when the user says "my paywall isn't converting", then the agent loads the skill, gathers context, runs the dry run, states the cost, and waits — [Hidden Assumption]
2. Given the user says "add a 10-minute countdown discount to my paywall", when the run returns, then no delivered idea recommends a countdown, and any idea that touches it is framed as removing B-03 — [Silent Failure]
3. Given an Android-only repository, when the run returns, then each tier is labeled as Apple's and Play policy as unchecked — [Edge Case]
4. Given `vidbyte-cli` is missing, when preflight runs, then the agent follows `references/setup.md` instead of guessing — [Hidden Failure]

---

## 11. Dependencies & External Services

| Dependency | Version / Endpoint | Purpose | Risk |
|------------|--------------------|---------|------|
| `vidbyte-cli` | ≥ 0.3.2 (unpublished; PyPI has 0.2.0) | Runs the Suggestion Agent | Skill stays coming-soon until 0.3.2+ is on PyPI |
| Vidbyte model gateway | gpt-6-sol, $2 / $10 per M tokens + 2% | Bills each model call | Price change → update `metadata.cost` and Cost and consent |
| Apple App Review Guidelines | snapshot 2026-10-28T19:03:51Z | Verbatim anchors | Apple revises the guidelines; anchors carry the fetch date |
| X article (via api.fxtwitter.com) | status 2114347402819690646, 2026-10-25 | Source of truth | Author may edit or delete; the ledger records the read date |
| `npx skills` | current | Installs the folder | Unchanged contract |

---

## 12. Rollout & Deployment

- **Order:** public PR first (cerredz/vidbyte-cli `skill/app-conversion-tactics`), then the website PR (cerredz/Vidbyte `feat/agent-skill-app-conversion-tactics`), as `add-agent-skill` sections 6–7 require.
- **Feature flag:** the listing's `status: "coming-soon"` is the flag: no install box, `noindex`, no sitemap entry, and the brief says it needs an unreleased CLI. Flip to `available` in a one-line PR once vidbyte-cli 0.3.2+ is on PyPI and the skill is run end to end with it.
- **Breaking change:** none.
- **Rollback:** remove the website entry and its import first, then the public folder (`add-agent-skill` section 10).

---

## 13. Open Questions

- [ ] Title: "Convert more app users within App Review rules". The owner may prefer the article's wording, "App conversion tactics".
- [ ] Should the listing flip to `available` together with the other 0.3.2 skills when the release ships? (Assumed yes.)
- [ ] Is the author's consent wanted for derived use beyond attribution? (The ledger paraphrases and quotes short spans with a link; no images are copied.)

---

## 14. Alternatives Considered

### Alternative 1: Pass the ledger with `--instructions`
- What: send the facts as system-prompt text so they carry instruction weight.
- Why rejected: `--instructions` caps all files at 20,000 bytes, and facts are data. File content is citable through `evidence_refs` and shows up in the manifest, so the dry run can prove it arrived.

### Alternative 2: Build the ledger into vidbyte-cli
- What: a built-in "conversion" knowledge pack in the CLI.
- Why rejected: CLI code changes and a release for content that changes when the article or Apple changes; a skill reference file updates with one public PR.

### Alternative 3: A bundled script like improve-my-aeo
- What: a Python wrapper that gathers facts and runs the CLI.
- Why rejected: nothing here needs computation; the user asked for the prompt plus the facts, and a script adds a Python dependency and a second failure surface.

### Alternative 4: Pass the banned tactics as `--forbidden` values too
- What: seven `--forbidden` records in the command.
- Why rejected: the rubric already binds the generator and every critic; two copies of the prohibitions would drift.
