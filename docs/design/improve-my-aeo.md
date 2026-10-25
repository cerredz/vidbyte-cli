# Design Doc: improve-my-aeo

**Status:** Draft
**Author:** Claude
**Created:** 2026-10-25
**Last Updated:** 2026-10-25

---

## 1. Overview

`improve-my-aeo` is a public agent skill that tells a site owner why answer engines (ChatGPT,
Claude, Perplexity, Google AI Overviews and AI Mode, Copilot) and coding agents misread, skip, or
never cite their site, and what to change first. A bundled, standard-library Python script audits
any public site for free: it fetches `robots.txt`, sitemaps, `llms.txt` and `llms-full.txt`, the
home page as a browser and as each major AI crawler and fetcher, the `.md` mirrors and
`Accept: text/markdown` negotiation, and up to 60 pages. It runs about 45 mechanical checks
(crawler access, raw-HTML rendering, index controls, llms.txt quality, Markdown canonicals,
structured-data leftovers, and message contradictions between pages) and writes a tagged evidence
digest. One usage-billed `vidbyte-cli agents suggest run` then sends that digest, a compact AEO
rubric through `--instructions` (it explains every checklist item to the generator and every
critic), and the in-depth, sourced AEO checklist through `--files` as reference. The user can also
ask their own AEO questions (`--question`), with or without a site. The result is a ranked list of
fixes, each tied to evidence from the user's own site, a checklist item, an exact change, and a
check the free audit re-runs. The skill is then listed on `/agents/skills` in the Vidbyte website
as a `coming-soon` entry, like every skill that needs vidbyte-cli 0.3.x, which is not on PyPI yet.

---

## 2. Goals & Non-Goals

### Goals
- A public skill folder `skills/improve-my-aeo/` that meets the `write-agent-skill` contract.
- An in-depth AEO checklist (`references/aeo-checklist.md`) researched from primary sources (vendor
  crawler docs, specs, Google Search Central, named studies with numbers), with a "contested or
  myth" section, that the Suggestion Agent reads as reference.
- A suggestion-agent rubric (`references/aeo-rubric.md`, at most 20,000 bytes, the CLI's
  `--instructions` limit) that explains every checklist item and what a grounded fix contains.
- A free, deterministic site audit (`scripts/aeo_audit.py gather`) that checks every mechanically
  checkable item and leaves the rest for the model with evidence.
- A question mode: the user's own AEO questions become `--question` values, answered against the
  checklist and, when a site was audited, against its evidence.
- Usage-billed run plumbing copied from the sibling skills (inputs validation, dry run, paid-run
  guard, cost estimate, report, handoff, feedback).
- A coming-soon listing in the vidbyte repository (`next-app/constants/agent-skills/improve-my-aeo/`).

### Non-Goals
- No JavaScript rendering (no headless browser). The audit reads raw HTML on purpose, because the
  AI crawlers that matter for citations do not run JavaScript; it reports the gap, it does not
  render around it.
- No rank tracking or prompt-share measurement (that needs a paid tracker and many sampled runs);
  the checklist explains how, the skill does not do it.
- No changes to the user's site. Fixes reach the site only through a handoff the user starts.
- No new CLI commands or flags; no change to the write-agent-skill contract.
- No crawling beyond 60 pages, no login, no form submission.

---

## 3. Background & Context

- The user's goal list item: "Build an AEO agent that audits any site against this checklist
  (llms.txt quality, .md canonicals, crawler access, schema leftovers, message contradictions) and
  sell it as a public vidbyte-cli skill. Builds on: the scripted audit in the scratchpad." Their
  instructions add: explain each checklist item in the suggestion agent's instructions, let users
  ask questions about improving their AEO, let the agent reference the in-depth checklist, and
  research more niche facts to make the checklist strong.
- The scratchpad `audit.py` (session 28f3e5e9, 2026-10-25 Conqueror teardown) parsed a WordPress
  sitemap and printed title, description, canonical, robots, og:image, hreflang, JSON-LD types,
  headings, word count, and FAQ count per page. That teardown found the failure classes this skill
  checks: an Organization schema named after another product, an indexable old-brand archive, a
  post date rendered as today, an llms.txt claiming `Accept: text/markdown` works when it does not,
  a homepage FAQ contradicting the approval flow, and vidbyte.pro's own `.md` mirrors lacking a
  `Link: rel=canonical` header. The new script generalizes it to any site and adds the AI-specific
  checks.
- Sibling pattern: `improve-my-prompting` (free local digest, rubric via `--instructions`, one
  usage-billed run, `report`/`feedback` subcommands). This skill reuses that structure and copies
  its `runner.py` plumbing, because each public skill installs as one self-contained folder.
- Constraints verified against `cerredz/vidbyte-cli-internal`:
  - `agents suggest run` takes `--goal` (≤4096 chars), repeated `--question`, `--outcomes`,
    `--mistakes`, `--context`, `--capability`, `--constraint`, `--avoid`, `--success`, `--files`
    (.txt/.md/.json, 5,000,000-char context bound), `--instructions` (≤8 files, ≤20,000 bytes in
    total, `InstructionsLimit`), `--max-total-tokens`, `--dry-run`. All exist in the contract at the
    0.3.2 bump commit `abd31b4`.
  - PyPI's newest `vidbyte-cli` is still 0.2.0 (checked 2026-10-25), so the listing is coming-soon
    with `minCliVersion` 0.3.2.

---

## 4. Requirements

### Functional Requirements
1. `gather --url <site>` fetches robots.txt, sitemap(s) (robots `Sitemap:` lines, then
   `/sitemap.xml`, `/sitemap_index.xml`), `/llms.txt`, `/llms-full.txt`, the home page (browser UA),
   the home page with each AI user agent in the probe list, the home page with
   `Accept: text/markdown`, the `.md` mirror of the home page and of sampled pages, and
   `/.well-known/` discovery files.
2. It samples at most `--max-pages` pages (default 25, range 1–60): the home page, pages whose path
   names pricing, about, docs, features, FAQ, compare/vs/alternatives, then sitemap order, then
   home-page links, same host only.
3. Every fetch has a timeout (15 s), a body cap (3 MB), and follows at most 5 redirects; one failed
   fetch is recorded as evidence, never a crash.
4. It evaluates every mechanical check in the checklist and gives each a status: `pass`, `fail`,
   `warn`, `info`, or `not_checked` (with a reason), plus evidence tags `[E#]`.
5. It writes into a new folder under the system temp folder: `audit.md` (the digest sent to the
   model), `aeo-rubric.md` and `aeo-checklist.md` (byte copies of the references), `checks.json`
   (all check results), `evidence.json` (the tag index, never sent), and `inputs.json` (typed inputs
   pre-filled from the checks), and prints a JSON summary.
6. `gather --question "<text>"` with no `--url` writes the same files with every site check
   `not_checked`, so the user can ask general AEO questions.
7. `suggest --out <dir> --dry-run` validates `inputs.json` (allowed keys, counts, lengths, tags that
   exist in `evidence.json` or name a checklist id), checks the rubric is at most 20,000 bytes, and
   runs `vidbyte-cli --json --no-input agents suggest run ... --dry-run` from inside the folder.
8. `suggest --out <dir> --paid` refuses to start if the folder already holds a paid attempt, writes
   `paid-run.txt`, runs the same command without `--dry-run` with `--max-total-tokens 400000`, saves
   `result.json`, and prints status, idea count, usage, and estimated cost.
9. `report --out <dir>` ties every idea to the evidence tags and checklist ids it cites, flags
   ideas that cite no site evidence (`grounded: false`), and writes `report.md`.
10. `feedback --out <dir> --idea <id> --accept|--reject [--reason] [--project]` runs
    `agents suggest feedback accept|reject` with the idea's exact title.
11. The rubric explains every checklist item by id, states what every idea must contain, and lists
    what not to recommend (myths).
12. The checklist gives, for every item: the check, how to test it, why it matters, facts with
    numbers, sources with dates, confidence, and the common mistake.
13. The website listing passes `defineAgentSkill`, its `description` equals the public frontmatter
    verbatim, and it is `coming-soon`.

### Non-Functional Requirements
- Standard library only, Python 3.8+, same behavior on Windows, macOS, and Linux.
- Politeness: requests are sequential with a 0.25 s pause, at most about 2 × max-pages + 30
  requests per audit, and identify the probe in the browser UA (`... improve-my-aeo-audit`).
- No secrets: the audit reads public pages only; `inputs.json` values are redacted with the sibling
  redactor before any paid run.
- Cost bound: `--max-total-tokens 400000` (≤ about $4 at gpt-6-sol rates plus 2%); the checklist
  file is kept under 70,000 bytes so each call stays small.
- Observability: the gather summary lists every fetch with status, bytes, and elapsed ms.

---

## 5. High-Level Design

```
user request ─▶ host agent loads SKILL.md
                 │
                 ├─ Preflight: vidbyte-cli --version / --json whoami / python3 --version
                 │
                 ├─ aeo_audit.py gather --url https://site [--question ...]   (free, local)
                 │     HttpFetcher ─▶ SiteProbe (robots, sitemaps, llms, UA probes, md mirrors, pages)
                 │                     ─▶ CheckSuite (≈45 checks) ─▶ DigestWriter
                 │                     out_dir/ audit.md  checks.json  evidence.json  inputs.json
                 │                              aeo-rubric.md  aeo-checklist.md
                 │
                 ├─ aeo_audit.py suggest --dry-run   ─▶ vidbyte-cli agents suggest run --dry-run (free)
                 ├─ consent (cost, what is sent)
                 ├─ aeo_audit.py suggest --paid      ─▶ vidbyte-cli agents suggest run (usage-billed)
                 │        --files audit.md --files aeo-checklist.md --instructions aeo-rubric.md
                 └─ aeo_audit.py report ─▶ ranked fixes with evidence; handoff; feedback; re-audit
```

The script is split into small modules under `scripts/aeokit/`: `fetch.py` (HTTP with limits),
`probe.py` (what to fetch and the parsed facts), `html.py` (a stdlib `HTMLParser` extractor for
title, meta, links, headings, JSON-LD, visible text), `checks.py` (one method per check family),
`digest.py` (the evidence index, `audit.md`, and `inputs.json`), `runner.py` (copied from the
sibling skills: validation, argv, paid-run guard, CLI invoker, cost), and `report.py`.

Key decisions: (1) the rubric travels by `--instructions` because only instructions reach the
generator and every critic as standing rules, and it must fit 20,000 bytes, so it is a compact
explanation of each item; (2) the long checklist travels by `--files`, because the user asked that
the agent can consult the in-depth version and files have a 5,000,000-char bound; (3) checks are
mechanical wherever possible so the "done" check of every fix is the free audit re-run, which is
independent of the model's judgment; (4) contradictions are surfaced as candidate facts (brand
strings, prices, "free" claims, status words) for the model to judge, because deciding that two
sentences conflict needs reading, not regex.

---

## 6. Detailed Design

### 6.1 HttpFetcher
**File(s):** `skills/improve-my-aeo/scripts/aeokit/fetch.py` — New file

#### What it does
One HTTP GET with a user agent, extra headers, timeout, body cap, and manual redirect following;
returns a `FetchResult` record and never raises for network errors.

#### Interface / API
```python
class FetchResult: url, final_url, status, headers (lowercased dict), body (str), bytes, elapsed_ms, error, redirects
class HttpFetcher:
    def __init__(self, timeout: float = 15.0, max_bytes: int = 3_000_000, pause: float = 0.25, opener=None) -> None
    def get(self, url: str, user_agent: str = BROWSER_UA, accept: str = None) -> FetchResult
    def log(self) -> list
```

#### Logic
1. Build a `urllib.request.Request`; disable automatic redirects with a custom handler; follow up to
   5 `Location` hops, recording each.
2. Read at most `max_bytes`; decode with the charset from `Content-Type`, else UTF-8 with replace.
3. `HTTPError` keeps its status, headers, and body; `URLError`/timeout/SSL errors set `status=0`
   and `error`.
4. Append a summary row to the log; sleep `pause` between requests.

#### Edge Cases
- Non-HTTP(S) URL → `status 0`, `error "unsupported scheme"`.
- Redirect loop → stops at 5 with `error "too many redirects"`.
- Gzip bodies are requested off (`Accept-Encoding: identity`).

### 6.2 SiteProbe
**File(s):** `scripts/aeokit/probe.py` — New file

#### What it does
Decides what to fetch for one site and returns a `SiteFacts` dictionary of raw observations.

#### Interface / API
```python
class SiteProbe:
    def __init__(self, fetcher: HttpFetcher, max_pages: int = 25) -> None
    def run(self, url: str) -> dict
```

#### Logic
1. Normalize the URL to its origin (scheme + host); refuse anything not http(s).
2. Fetch `robots.txt`, parse groups (`RobotsTxt` class: user-agent groups, allow/disallow, sitemap
   lines, content-signal lines, longest-match evaluation per RFC 9309).
3. Fetch sitemaps (robots lines, then defaults), follow one level of sitemap indexes, at most 5
   sitemap files, collect `<loc>` and `<lastmod>`.
4. Fetch `/llms.txt`, `/llms-full.txt`.
5. Fetch the home page as a browser, as each probe UA (GPTBot, OAI-SearchBot, ChatGPT-User,
   ClaudeBot, Claude-User, Claude-SearchBot, PerplexityBot, Perplexity-User, Google-Extended is not
   a fetcher so it is robots-only, Bingbot, Applebot), and with `Accept: text/markdown`.
6. Pick pages (FR 2), fetch each as a browser, parse with `PageExtractor`, and fetch its `.md` mirror
   (`<path>.md`, or `/index.md` for `/`).
7. Fetch `/.well-known/security.txt`, `/.well-known/mcp.json`, `/.well-known/mcp/server.json`,
   `/feed.xml`/`/rss.xml` only when not linked from the home page head.

#### Edge Cases
- robots.txt 404 → everything allowed (RFC 9309); 5xx → treat as full disallow (Google's rule).
- robots.txt served as HTML (a soft 404) → flagged, parsed as empty.
- Sitemap that is gzip (`.xml.gz`) → recorded `not_checked` (no decompression of large files).

### 6.3 PageExtractor
**File(s):** `scripts/aeokit/html.py` — New file

Stdlib `HTMLParser` subclass that collects `<title>`, `<meta name|property>`, `<link rel>`,
`<h1>`–`<h3>`, `<a href>`, `<script type="application/ld+json">` bodies, `<time datetime>`, and
visible text (skipping script/style/noscript/template). JSON-LD is parsed with `json.loads`; parse
errors are kept as evidence. Exposes `facts() -> dict` with `title, description, canonical,
robots_meta, og_site_name, og_title, alternates (type/href), h1, h2, links, jsonld (list of nodes
flattened from @graph), jsonld_errors, words, text_sample, times, framework_markers`.

### 6.4 CheckSuite
**File(s):** `scripts/aeokit/checks.py` — New file

#### What it does
Turns `SiteFacts` into check results. One class, one method per family, each appending
`CheckResult(id, family, title, status, detail, evidence_tags)`.

#### Interface / API
```python
class CheckSuite:
    def __init__(self, facts: dict, evidence: EvidenceIndex, today: date) -> None
    def run(self) -> list
```

#### Families and checks (ids match the checklist and rubric)
- `ACC` crawler access: robots reachable and well-formed; AI search crawlers allowed; user-triggered
  fetchers allowed; training-crawler policy stated (info); home page status to each AI UA equals the
  browser's; sitemap declared in robots; Content-Signal present (info).
- `REN` rendering: raw HTML main text ≥ 150 words on the home page; h1 present in raw HTML; JS-shell
  markers (`<div id="root"></div>`, `__NEXT_DATA__` with almost no text) flagged.
- `IDX` index controls: no `noindex` on sampled pages that are in the sitemap; no `nosnippet` /
  `max-snippet:0`; canonical present, absolute, and self or same-site; X-Robots-Tag on HTML.
- `LLM` llms.txt: exists (200, text); first line is `# ` H1; blockquote summary; at least one `## `
  section of Markdown links; links resolve (sample of 10); size under 50 KB; no HTML comments or
  local paths; claims checked (mentions of `.md`/`text/markdown` vs observed behavior);
  llms-full.txt present (info).
- `MD` Markdown mirrors: `.md` mirror of the home page returns Markdown; `Accept: text/markdown`
  returns Markdown; mirrors send `Link: <html>; rel="canonical"` or `X-Robots-Tag: noindex`; HTML
  advertises the mirror with `<link rel="alternate" type="text/markdown">`.
- `SCH` structured data: JSON-LD parses; an Organization or WebSite node exists; its `name` matches
  the site's brand (title suffix / og:site_name / llms.txt H1); at most one distinct Organization
  name; `sameAs` present; no placeholder values (`example.com`, `Lorem`, `Your Company`, `ChatPack`-
  style names that match no other brand string); `dateModified`/`datePublished` not equal to today on
  most pages; `FAQPage` questions appear in visible text.
- `MSG` message consistency: brand strings across title suffixes, og:site_name, schema, and llms.txt
  H1; price strings per page and in llms.txt; "free" claims; status words (beta, waitlist, coming
  soon) — the check reports `warn` when values differ and lists them as candidate contradictions.
- `CNT` content shape: meta description present; question-style headings share (info); visible
  date present on articles (info); thin pages (< 200 words) share; comparison/alternatives pages
  exist (info).
- `DSC` discovery: security.txt, MCP discovery file, RSS/Atom feed, OpenAPI link (all info).

#### Edge Cases
- No site (question-only) → every check `not_checked` with reason `no site audited`.
- A family whose inputs failed to fetch → its checks `not_checked` with the fetch error as reason.

### 6.5 EvidenceIndex and DigestWriter
**File(s):** `scripts/aeokit/digest.py` — New file

`EvidenceIndex.add(kind, url, text) -> "E#"` stores short observed facts (≤ 300 chars).
`DigestWriter.write(out_dir, facts, results, questions)` writes `audit.md` (site header, totals,
failing and warning checks first with their evidence lines, then passes, then raw facts: robots
groups for AI agents, llms.txt head, UA probe table, page table, schema nodes, candidate
contradictions), `checks.json`, `evidence.json`, and `inputs.json`:
- `goal`: audit mode "Find the changes that most improve how answer engines and AI agents read,
  trust, and cite <host>…"; question mode adds the questions; question-only mode "Answer my AEO
  questions using the AEO checklist."
- `mistakes`: up to 8 failing/warning checks, most severe first, each `"[ACC-02 E3 E4] …"`.
- `outcomes`: up to 5 passes worth keeping, tagged.
- `context`: up to 4: site type guess, page count, platform markers, what was not checked.
- `question`: the user's questions verbatim (≤ 3).
- `capability`, `constraint`, `avoid`, `success`: empty for the host agent to fill from the user's
  words.

`audit.md` is capped at 60,000 characters; sections are trimmed from the bottom (raw facts first).

### 6.6 Runner (copied)
**File(s):** `scripts/aeokit/runner.py` — New file (adapted copy of
`skills/improve-my-prompting/scripts/sessionkit/runner.py`)

Same classes (`CommandIO`, `InputsValidator`, `SuggestArgv`, `FeedbackArgv`, `PaidRunGuard`,
`CliInvoker`, `CostEstimator`, `RunReporter`) plus a `Redactor` (keys, tokens, emails) since the
sibling's lives in `sessionkit/redaction.py`. `SuggestArgv.build` gains the `--question` kind
through `FLAG_KINDS`.

### 6.7 AeoRun and entry point
**File(s):** `scripts/aeokit/suggest.py`, `scripts/aeo_audit.py` — New files

`AeoRun` holds the settings: categories `quick_wins, risk_prevention, leverage, strategy,
verification`, count 8, files `("audit.md", "aeo-checklist.md")`, instructions
`("aeo-rubric.md",)`, project key `improve-my-aeo`, flag kinds, tag pattern
`^\[((?:E\d+|[A-Z]{3}-\d{2})(?:[ ,]+(?:E\d+|[A-Z]{3}-\d{2}))*|all)\]\s+\S`. `aeo_audit.py`
implements `gather`, `suggest`, `report`, `feedback` with argparse, exit codes 0 (ok), 2 (usage),
3 (site unreachable: home page status 0 or ≥ 500 for every request), 127 (CLI not found), or the
CLI's own exit code.

### 6.8 IdeaReporter
**File(s):** `scripts/aeokit/report.py` — New file

Reads `result.json`, `evidence.json`, `checks.json`; for each idea collects `E#` tags and checklist
ids found in title, summary, first_action, completion_criteria, and handoff evidence; marks
`grounded` when at least one `E#` or a failing check id appears (question-only mode: grounded when a
checklist id appears); writes `report.md` and prints JSON.

### 6.9 References
- `references/aeo-checklist.md` — New: the in-depth checklist (≈80 items in families ACC, REN, IDX,
  LLM, MD, SCH, MSG, CNT, ENT, OFF, DSC, MEA), each with check / test / why / facts / sources /
  confidence / mistake, plus "Contested or myth". Under 70,000 bytes.
- `references/aeo-rubric.md` — New: situation, what every idea must contain, what not to do, every
  checklist id with a one-to-three-line explanation, ranking rule. At most 20,000 bytes.
- `references/setup.md`, `references/troubleshooting.md` — byte copies of the canonical files.

### 6.10 SKILL.md
Sections in the contract order: title, The problem, How vidbyte-cli solves it, Goal and done-when,
Preflight, Gather inputs, Steps, CLI commands, Cost and consent, Run, Read the result, Deliver,
Troubleshooting, Guardrails. Frontmatter `metadata.min-cli-version: "0.3.2"`.

### 6.11 Website listing
**File(s):** vidbyte `next-app/constants/agent-skills/improve-my-aeo/catalog.js` (New),
`next-app/constants/agent-skills/catalog.js` (Modified: import + ENTRIES position among the
coming-soon suggestion skills).

---

## 7. Data Model Changes

N/A - no database. The only data shapes are the skill's local files (`checks.json`:
`[{id, family, title, status, detail, evidence}]`; `evidence.json`: `{"E1": {kind, url, text}}`),
defined in section 6.

---

## 8. API Changes

N/A - no endpoints. The skill calls existing `vidbyte-cli` commands only.

---

## 9. File Change Manifest

| Action | File Path | Reason |
|--------|-----------|--------|
| CREATE | `docs/design/improve-my-aeo.md` | This design doc |
| CREATE | `skills/improve-my-aeo/SKILL.md` | The public skill |
| CREATE | `skills/improve-my-aeo/references/aeo-checklist.md` | In-depth sourced checklist (sent with `--files`) |
| CREATE | `skills/improve-my-aeo/references/aeo-rubric.md` | Suggestion Agent instructions explaining every item |
| CREATE | `skills/improve-my-aeo/references/setup.md` | Canonical copy |
| CREATE | `skills/improve-my-aeo/references/troubleshooting.md` | Canonical copy |
| CREATE | `skills/improve-my-aeo/scripts/aeo_audit.py` | Entry point: gather, suggest, report, feedback |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/__init__.py` | Package |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/fetch.py` | HttpFetcher |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/html.py` | PageExtractor |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/robots.py` | RobotsTxt parser (RFC 9309 matching) |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/probe.py` | SiteProbe |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/checks.py` | CheckSuite |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/digest.py` | EvidenceIndex, DigestWriter |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/runner.py` | Copied usage-billed run plumbing |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/suggest.py` | AeoRun settings |
| CREATE | `skills/improve-my-aeo/scripts/aeokit/report.py` | IdeaReporter |
| CREATE | `scripts/test-improve-my-aeo.py` | Verification script (section 10) |
| CREATE | vidbyte `next-app/constants/agent-skills/improve-my-aeo/catalog.js` | Website listing |
| MODIFY | vidbyte `next-app/constants/agent-skills/catalog.js` | Import + ENTRIES |

---

## 10. Testing Plan

All tests run offline against a local `http.server` fixture site on 127.0.0.1 and a recording fake
`vidbyte-cli` passed with `--cli`; PATH holds no real CLI.

### Unit Tests
- `RobotsTxt` → longest match wins between Allow and Disallow for the same agent — [Silent Failure]
- `RobotsTxt` → a named group (`User-agent: GPTBot`) overrides `*`, and an agent not named falls back to `*` — [Hidden Assumption]
- `RobotsTxt` → empty file and 404 allow everything; 5xx disallows everything — [Edge Case]
- `RobotsTxt` → `$` end anchor and `*` wildcard match per RFC 9309 — [Edge Case]
- `RobotsTxt` → case-insensitive agent tokens and comments after `#` ignored — [Hidden Assumption]
- `PageExtractor` → JSON-LD with `@graph` is flattened; invalid JSON-LD is recorded as an error, not dropped — [Silent Failure]
- `PageExtractor` → text inside script/style/noscript is not counted as words — [Silent Failure]
- `PageExtractor` → page with no `<head>`/`<title>` returns None fields, not crashes — [Edge Case]
- `HttpFetcher` → redirect loop stops at 5 with an error — [Hidden Failure]
- `HttpFetcher` → connection refused yields status 0 and an error, no exception — [Hidden Failure]
- `HttpFetcher` → body over the cap is truncated and flagged — [Edge Case]
- `CheckSuite` → Organization name that matches no other brand string fails SCH name check — [Silent Failure]
- `CheckSuite` → `dateModified` equal to today on most pages warns — [Silent Failure]
- `CheckSuite` → AI UA receiving 403 while browser gets 200 fails ACC UA parity — [Hidden Failure]
- `CheckSuite` → llms.txt that claims `text/markdown` support while negotiation returns HTML fails LLM claims check — [Silent Failure]
- `CheckSuite` → `.md` mirror without canonical Link header and without noindex fails MD canonical — [Edge Case]
- `CheckSuite` → JS shell (root div, 20 words) fails REN raw-text check — [Hidden Assumption]
- `CheckSuite` → differing prices between pricing page and llms.txt produce a MSG warn listing both — [Silent Failure]
- `CheckSuite` → question-only mode marks every site check `not_checked` — [Edge Case]
- `InputsValidator` → value without a leading tag, or with an unknown `E#`, is refused; a checklist id like `[ACC-02]` is accepted — [Hidden Assumption]
- `InputsValidator` → more than 3 questions or a goal over 700 chars is refused — [Edge Case]
- `Redactor` → API keys and emails in a question are redacted before argv — [Hidden Failure]
- `rubric file` → `aeo-rubric.md` is at most 20,000 bytes and names every checklist id that `checks.py` emits — [Silent Failure]
- `checklist file` → every id the rubric explains exists in `aeo-checklist.md`, and every item has a source URL — [Silent Failure]

### Integration Tests
- `gather --url http://127.0.0.1:<port>` on a deliberately broken fixture produces the expected failing check ids, `audit.md` under 60,000 chars, and `inputs.json` whose tags exist in `evidence.json` — [Hidden Failure]
- The same on a clean fixture produces no `fail` — [Silent Failure] (false positives would be sold as advice)
- `gather` against a port with nothing listening exits 3 with a readable error — [Hidden Assumption]
- `gather --question` with no URL writes files and a question-only goal — [Edge Case]
- `suggest --dry-run` builds argv with root options first, `--question`, both `--files`, `--instructions aeo-rubric.md`, and `--max-total-tokens 400000` — [Hidden Assumption]
- `suggest --paid` twice in one folder: the second exits 2 and never calls the CLI — [Hidden Failure]
- `suggest --paid` with the fake CLI exiting 5 propagates exit 5 and the error envelope — [Silent Failure]
- `report` marks an idea that cites no `E#` as `grounded: false` — [Silent Failure]
- `feedback --reject` passes the idea's exact title — [Hidden Assumption]

### Manual / QA Test Cases
1. Given vidbyte.pro, when `gather` runs, then MD canonical-header fails (known gap 2026-10-25) and the llms.txt HTML-comment check fails — [Silent Failure]
2. Given conquerorapp.com, when `gather` runs, then SCH Organization-name and LLM claims checks fail — [Hidden Failure]
3. Given a Cloudflare site that blocks AI bots by default, then ACC parity fails with 403 evidence — [Edge Case]
4. A real usage-billed run once the released CLI is on PyPI (blocked today: PyPI has 0.2.0) — [Hidden Assumption]

---

## 11. Dependencies & External Services

| Dependency | Version / Endpoint | Purpose | Risk |
|------------|--------------------|---------|------|
| vidbyte-cli | ≥ 0.3.2 (not on PyPI yet) | `agents suggest run/handoff/project/feedback` | Listing stays coming-soon until released |
| Python | ≥ 3.8 stdlib | Audit script | None |
| Target site | public HTTP(S) | Audit input | WAFs may block the probe; recorded as evidence |
| gpt-6-sol via Vidbyte gateway | usage-billed | Suggestion run | Price change → update cost text |

---

## 12. Rollout & Deployment

- No feature flags. Public PR first (cerredz/vidbyte-cli, draft), then the vidbyte catalog PR
  (draft), per add-agent-skill's deploy order.
- Not breaking. Rollback: remove the website entry first, then the public folder.

---

## 13. Open Questions

- [ ] Q-1: Should the listing go `available` together with the other 0.3.x skills when vidbyte-cli
      0.3.2 reaches PyPI? (Default: yes, in the same flip PR.)
- [ ] Q-2: Should a later version add headless rendering (Browserbase) to compare rendered vs raw
      HTML? Not built (non-goal).
- [ ] Q-3: Should the skill offer a scheduled re-audit (CI workflow) to catch regressions? Not built.

---

## 14. Alternatives Considered

### Alternative 1: Put the whole checklist in `--instructions`
- What: one rubric file with every fact.
- Why rejected: the CLI caps instructions at 20,000 bytes in total; the researched checklist is
  several times larger.

### Alternative 2: Only `--files` (no rubric)
- What: send the checklist as a file and no instructions.
- Why rejected: files are evidence to the generator, not standing rules for the critic; the critic
  would not know what a grounded AEO fix must contain.

### Alternative 3: Let the model fetch the site
- What: no script; ask the model to browse.
- Why rejected: the suggestion agent has no web tool, results would not be reproducible, and the
  done-check of every fix would depend on the model's judgment instead of a re-run audit.

### Alternative 4: Use the scratchpad `audit.py` as is
- What: ship the WordPress sitemap parser.
- Why rejected: it assumes Yoast sitemap names, fetches without limits, and checks no AI-specific
  surface; the new script keeps its per-page fields and adds the rest.
