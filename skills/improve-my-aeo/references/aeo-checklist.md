# AEO checklist: what makes a site readable, trusted, and cited by answer engines and AI agents

Researched 2026-10-25 from vendor crawler documentation, specifications, Google Search Central,
Bing Webmaster guidance, Lighthouse source code, peer-reviewed papers, and named industry studies.
Answer engines change monthly: every number carries its date, and citation shares are snapshots.

How to read an item:

- **Id** (`ACC-02`): cite it in ideas. Families: ACC crawler access, REN raw-HTML rendering, IDX
  index and snippet controls, LLM llms.txt, MD Markdown mirrors, SCH structured data, ENT entity,
  MSG message consistency, CNT content, OFF off-site and earned signals, DSC agent discovery,
  MEA measurement, SEO search foundations. The Bot list at the end names every bot ACC checks.
- **Audit**: `script` means `aeo_audit.py` checks it and `audit.md` reports its status; `owner`
  means only the site owner can check it (dashboards, logs, settings); `review` means a person or
  the model judges it by reading.
- **Confidence**: `primary` = the vendor's or standard's own text; `study` = a named study with a
  stated sample (most are vendor-run and correlational: say "associated with", never "causes");
  `secondary` = reported by press or a third party; `contested` = sources disagree.
- What Google says overall (AI optimization guide, updated 2026-07-10): AI Overviews and AI Mode
  need no special schema, no llms.txt, no Markdown files, and no "chunking"; AEO for Google is SEO.
  Spam policies explicitly cover attempts to manipulate generative AI answers (2026-05-15).

---

## ACC: crawler and fetcher access

Three kinds of AI fetchers exist, and robots.txt treats them differently. **Search/index**
crawlers decide whether a site can appear in AI search answers (OAI-SearchBot, Claude-SearchBot,
PerplexityBot, Googlebot, bingbot, Applebot, DuckAssistBot, meta-webindexer, Amzn-SearchBot,
MistralAI-Index). **User-triggered** fetchers read a page because a person asked about it
(ChatGPT-User, Claude-User, Perplexity-User, MistralAI-User, Google-Agent). **Training**
crawlers and tokens decide model training (GPTBot, ClaudeBot, Google-Extended, Applebot-Extended,
meta-externalagent, Amazonbot, CCBot, MistralAI-Training, Bytespider). Vendor pages: OpenAI
developers.openai.com/api/docs/bots; Anthropic support.claude.com/en/articles/8896518
(2026-04-07); Perplexity docs.perplexity.ai/docs/resources/perplexity-crawlers; Google
developers.google.com/crawling/docs/crawlers-fetchers/google-common-crawlers (2026-10-15);
Apple support.apple.com/en-us/119829 (2026-09-04); Meta developers.facebook.com/docs/sharing/webmasters/web-crawlers;
Mistral docs.mistral.ai/robots; DuckDuckGo duckduckgo.com/duckduckgo-help-pages/results/duckassistbot;
Amazon developer.amazon.com/amazonbot. No primary docs exist for Bytespider, Cohere, or xAI/Grok
crawlers: do not state their behavior as fact.

### ACC-01 robots.txt is reachable, plain text, under 500 KiB, and never 5xx
- Check: `GET /robots.txt` returns 200 `text/plain`, at most 500 KiB, within 5 redirects; never
  5xx, never an HTML page, never 401/403. Audit: script.
- Why: RFC 9309: on 5xx a crawler "MUST assume complete disallow"; on 4xx it may crawl
  everything; parsers read at least 500 KiB. Google stops crawling for 12 h on a 5xx robots.txt,
  then uses the last good copy for 30 days. Amazon treats an unfetchable file as absent. A WAF
  that 403s AI agents on every path, robots.txt included, produces "no restrictions" and then
  blocks the content anyway.
- Sources: rfc-editor.org/rfc/rfc9309; developers.google.com/crawling/docs/robots-txt/robots-txt-spec (2026-10-23).
- Confidence: primary.
- Mistake: an SPA catch-all route that serves the HTML app at /robots.txt with status 200.

### ACC-02 AI search crawlers are allowed
- Check: no robots.txt group disallows public content to OAI-SearchBot, Claude-SearchBot,
  PerplexityBot, Googlebot, bingbot, Applebot, DuckAssistBot, meta-webindexer, Amzn-SearchBot,
  or MistralAI-Index. Audit: script (RFC 9309 longest-match on / and sampled pages).
- Why: OpenAI: "Sites that are opted out of OAI-SearchBot will not be shown in ChatGPT search
  answers, though can still appear as navigational links." Anthropic: blocking Claude-SearchBot
  "may reduce your site's visibility and accuracy in user search results." Meta: allowing
  meta-webindexer "helps us cite and link to your content in Meta AI's responses." Google AI
  Overviews and AI Mode use the Googlebot rules. Changes take about 24 h (OpenAI, Perplexity,
  Meta, Amazon) and 72 h (DuckAssistBot).
- Confidence: primary.
- Mistake: pasting an "AI blocklist" that lists GPTBot and OAI-SearchBot, ClaudeBot and
  Claude-SearchBot together. It removes the site from ChatGPT and Claude search when the owner
  only meant to refuse training.

### ACC-03 User-triggered fetchers are allowed
- Check: ChatGPT-User, Claude-User, Perplexity-User, and MistralAI-User are not disallowed, and
  the WAF does not block them. Audit: script (robots) plus ACC-05 (firewall).
- Why: these fetch a page because a person asked about it, the highest-intent fetch there is.
  OpenAI: for ChatGPT-User "robots.txt rules may not apply"; Perplexity-User "generally ignores
  robots.txt rules"; Claude-User honors robots.txt, and "Disabling Claude-User ... may reduce your
  site's visibility for user-directed web search." Claude Code's WebFetch identifies as
  Claude-User. So a robots block mostly hurts the vendors that obey it.
- Confidence: primary.
- Mistake: blocking every `*-User` agent "to stop AI", which breaks live answers about the site.

### ACC-04 Training crawlers are allowed
- Check: robots.txt lets GPTBot, ClaudeBot, Google-Extended, Applebot-Extended,
  meta-externalagent, Amazonbot, CCBot, and Bytespider fetch public content. If the owner blocks
  any, the reason is written down and no search token is caught by the block. Audit: script.
- Why: allowing every bot type is this checklist's default, because models that cannot read a
  site learn less about it. Google-Extended "does not impact a site's inclusion in Google Search
  nor is it used as a ranking signal." Applebot-Extended "does not crawl webpages"; disallowed
  pages "can still be included in search results." OpenAI: "If your site has allowed both bots,
  we may use the results from just one crawl for both use cases." Whether blocking training lowers
  a brand's unprompted recall in model answers is unproven either way; do not claim it.
- Confidence: primary (mechanics); the recall effect is unknown.
- Mistake: believing GPTBot controls ChatGPT search (OAI-SearchBot does), or pasting one "block
  AI" list that catches search and user tokens too.

### ACC-05 AI user agents get the same page as a browser
- Check: the home page (and ideally a product page and a post) fetched with each AI user-agent
  string returns the browser's status, about the same text, and no challenge page
  (`cf-mitigated` header, "Just a moment...", CAPTCHA). Audit: script.
- Why: AI fetchers run no JavaScript, so a JS challenge is a hard block, and Anthropic's bots
  "will not attempt to bypass CAPTCHAs." On 2026-10-25 medium.com returned 403 to every AI user
  agent and 200 to a browser; reddit.com returned 403 to GPTBot and ChatGPT-User and 429 to
  ClaudeBot. Caveat: a request that copies a bot's user agent from another IP is a screening
  signal, not proof. CDNs verify bots by IP and signature, so a spoof can be blocked when the
  real bot passes, or pass when the real bot is blocked. Confirm in server or CDN logs filtered
  by the vendor IP files (MEA-06), or ask an assistant to open a unique URL and look for it in
  the access log.
- Confidence: primary (mechanics); observed (samples).
- Mistake: testing only `/`; WAF rules are often path-scoped (for example `/blog/*`).

### ACC-06 A sitemap is declared in robots.txt and readable
- Check: robots.txt has `Sitemap:` lines; each returns 200 XML with at most 50,000 URLs and
  50 MB uncompressed. Audit: script.
- Why: Vercel/MERJ (2024-12-17): AI crawlers spent about 34% of fetches on 404s (ChatGPT
  34.82%, Claude 34.16%) and ChatGPT another 14.36% on redirects, versus Googlebot's 8.22% and
  1.49%; their advice is to keep sitemaps current. Bing rechecks submitted sitemaps at least
  daily.
- Sources: vercel.com/blog/the-rise-of-the-ai-crawler; blogs.bing.com/webmaster/July-2025/Keeping-Content-Discoverable-with-Sitemaps-in-AI-Powered-Search.
- Confidence: primary.
- Mistake: sitemaps full of redirected or noindex URLs (IDX-04).

### ACC-07 Sitemap lastmod dates are true
- Check: most URLs carry a W3C-format `<lastmod>`; values change only when content changes; they
  are not all the same timestamp or the build time. Audit: script (flags 90% on one day).
- Why: Bing (2025-07-31): "The lastmod field in your sitemap remains a key signal" and "Avoid
  setting lastmod to the time your sitemap was generated unless the content on that URL was
  actually updated." Google uses lastmod only when it is "consistently and verifiably" accurate.
  Google and Bing both ignore `<changefreq>` and `<priority>`.
- Confidence: primary.
- Mistake: a CMS plugin that stamps every URL with "now" on each build.

### ACC-08 Content-Signal lines are valid and say what the owner means
- Check: no `Content-Signal` line, or every line matches
  `search|ai-input|ai-train=yes|no` (comma-separated) inside a user-agent group, and the values
  match intent. `ai-input=no` asks AI answers not to use the content. Audit: script.
- Why: Cloudflare's Content Signals policy (2025-09-24) defines search (index and links),
  ai-input (RAG and grounding), and ai-train. Cloudflare's managed robots.txt serves
  `search=yes, ai-train=no` on more than 3.8 M domains; 4% of the top 200K domains declare
  signals (2026-04-17). Google's parser supports only user-agent, allow, disallow, and sitemap,
  and OpenAI, Anthropic, and Perplexity docs do not mention the signal: preferences only. The
  IETF aipref drafts use different strings (`train-ai=y|n`, `Content-Usage:`) and are not RFCs.
- Sources: blog.cloudflare.com/content-signals-policy; blog.cloudflare.com/agent-readiness; datatracker.ietf.org/wg/aipref.
- Confidence: primary (syntax); contested (whether engines honor it).
- Mistake: believing `ai-input=no` keeps the site out of AI Overviews (Google's controls are
  Googlebot rules, nosnippet, max-snippet, and the Search Console toggle in ACC-15).

### ACC-09 Pages answer 200 with at most one redirect
- Check: key URLs resolve in at most one hop, permanent moves use 301/308, and nothing loops or
  lands on a 4xx. Audit: script.
- Why: the redirect and 404 shares in ACC-06; Bing: use 302 "only for very short-term changes
  (less than 2 days)". No vendor publishes a redirect limit for AI content fetches; do not invent
  one.
- Confidence: primary.
- Mistake: http -> https -> www -> trailing slash -> locale chains of four or more hops.

### ACC-10 Missing pages answer 404 or 410
- Check: a made-up URL returns 404 or 410, not 200 or a redirect to the home page. Audit: script.
- Why: Google flags 200 error pages as soft 404s; AI crawlers already spend about a third of
  fetches on 404s, and a soft 404 that returns 200 can be cited as content.
- Confidence: primary.
- Mistake: an SPA catch-all that returns 200 for every path.

### ACC-11 Bots that inherit other bots' rules get the rules you meant
- Check: if robots.txt names Googlebot but not Applebot, the Googlebot group is also right for
  Apple. A bot with its own group ignores the `*` group entirely. Audit: script (Applebot).
- Why: Apple: "If robots instructions don't mention Applebot but mention Googlebot, the Apple
  robot will follow Googlebot instructions." Amazon: if Amzn-SearchBot is not named it follows
  the rules for "other search bots". RFC 9309 and Google: only the most specific group applies,
  groups are not combined, line order is irrelevant, and the longest matching rule wins (allow
  wins a tie). Python's `urllib.robotparser` uses first-match and gets this wrong.
- Confidence: primary.
- Mistake: a `User-agent: Googlebot / Disallow: /` test block that also removes the site from
  Siri, Spotlight, and Safari answers.

### ACC-12 CDN and WAF bot settings match intent
- Check: on Cloudflare, Security > AI bot policies has Search = Allow, Training set on purpose
  (prefer "Disallow AI Training" to "Block"), and Agent = Allow unless losing ChatGPT-User and
  browser agents is accepted; Bot Fight Mode is off or replaced by Super Bot Fight Mode with a
  Skip rule. On Vercel, Akamai, Fastly, and AWS WAF, AI bot rules are set deliberately. Audit:
  owner (ACC-05 screens it from outside).
- Why: since 2026-09-15 Cloudflare's "Block" and "Block on pages with ads" also apply to
  Googlebot, Bingbot, and Applebot, which takes the site out of search; "Disallow AI Training"
  keeps search. Since 2025-07-01 new Cloudflare domains are asked to block AI training, and
  managed robots.txt adds lines the owner never wrote. Free Bot Fight Mode cannot be bypassed by
  WAF rules. Less than 1% of Cloudflare sites block Search; 17% block training.
- Sources: blog.cloudflare.com/accountable-mixed-use-ai-crawlers (2026-09-15); blog.cloudflare.com/content-independence-day-no-ai-crawl-without-compensation (2025-07-01); developers.cloudflare.com/bots/get-started/bot-fight-mode.
- Confidence: primary.
- Mistake: auditing the robots.txt in git instead of the one served at the edge.

### ACC-13 Allow-lists use the vendors' published IP files, refreshed automatically
- Check: any WAF exception for AI bots matches the user agent AND an IP range from the live JSON
  files (openai.com/searchbot.json, gptbot.json, chatgpt-user.json; claude.com/crawling/bots.json;
  perplexity.ai/perplexitybot.json; Google common-crawlers.json; bing.com/toolbox/bingbot.json;
  search.developer.apple.com/applebot.json) and refreshes them on a schedule. Audit: owner.
- Why: ranges change: OpenAI's chatgpt-user.json changed on 2026-10-20. Perplexity recommends
  matching both user agent and IP.
- Confidence: primary.
- Mistake: a hard-coded 2024 IP list, or "block datacenter ASNs" rules that remove nearly every
  AI fetcher.

### ACC-14 Every content hostname is covered
- Check: www, apex, docs., blog., help., and shop. each serve their own valid robots.txt and
  sitemap with the same bot policy. Audit: owner (the script audits one origin).
- Why: robots rules are per host (RFC 9309); Anthropic: put robots.txt "in the top-level
  directory of each subdomain."
- Confidence: primary.
- Mistake: a third-party help center on help. that blocks bots on its own.

### ACC-15 Google's "Search generative AI" control is set on purpose
- Check: in Search Console, Settings > Search generative AI is Include (the default) unless the
  owner chose to leave AI Overviews, AI Mode, and Discover AI features. Audit: owner.
- Why: rolled out worldwide 2026-08-31; Exclude means "links to your site and your site's
  content won't appear in Search generative AI features"; it is not a ranking signal for the rest
  of Search and does not affect training (that is Google-Extended). Child properties inherit it.
  The older advice "you cannot leave AI Overviews without leaving Search" is out of date.
- Sources: support.google.com/webmasters/answer/16908024; blog.google/products-and-platforms/products/search/new-controls-website-owners (2026-06-03).
- Confidence: primary.
- Mistake: blocking Google-Extended to leave AI Overviews (it does nothing there).

### ACC-16 Crawl-delay is not the throttle
- Check: the site does not rely on `Crawl-delay` to manage load. Audit: script (reports lines).
- Why: Google, Apple, and Amazon ignore it; Anthropic and Bing honor it, so a large value under
  `*` throttles only the bots that cooperate. A temporary 429 or 503 slows Google.
- Confidence: primary (Bing secondary).
- Mistake: `Crawl-delay: 30` for everyone.

### ACC-17 Every bot type is allowed in robots.txt
- Check: no robots.txt group blocks public content to agents (Google-CloudVertexBot,
  CloudflareBrowserRenderingCrawler), link-preview bots (facebookexternalhit, LinkedInBot,
  Twitterbot, MicrosoftPreview), ads checkers (AdsBot-Google, adidxbot), Google's other fetchers
  (GoogleOther, APIs-Google), or a bot nobody has named yet (the `*` group). Every token is in the
  "Bot list" at the end. Audit: script (every token, on / and the sampled pages).
- Why: preview bots build the link cards shown when the site is shared in chat and social apps.
  Ads bots check landing pages and obey only groups that name them, so `*` rules never reach
  them. Agents fetch for people and developers. A `*` group that blocks unknown bots also blocks
  next month's new AI crawler.
- Sources: the operator pages listed for each bot in scripts/aeokit/bots.json.
- Confidence: primary.
- Mistake: `User-agent: *` + `Disallow: /` with allow groups for a few named search bots.

### ACC-18 Every known bot gets the browser's page from the firewall
- Check: the home page fetched as each fetching bot in the "Bot list" (previews, ads checkers,
  agents, Google's other fetchers, generic clients such as curl and python-requests, and an empty
  user agent) returns the browser's status, about the same text, and no challenge page.
  Cloudflare's own infrastructure bots matter only for sites on Cloudflare. Audit: script.
- Why: CDN and WAF bot settings (ACC-12) can block previews and generic clients even when
  robots.txt allows them. A blocked preview bot shows an empty link card everywhere the site is
  shared; blocking generic clients blocks agents that use plain HTTP libraries. The spoofing
  caveat in ACC-05 applies.
- Confidence: primary (mechanics); observed (vidbyte.pro answered 200 to all 66 bots on
  2026-10-25 after removing its bot block).
- Mistake: allowing only the bots in a CDN's "verified bots" directory.

---

## REN: what is in the raw HTML

### REN-01 The main content is in the server HTML
- Check: the raw HTML response, with no JavaScript run, holds the page's h1, main text, prices,
  specs, FAQ answers, author, and date. Audit: script (home page words; REN-03 for every page).
- Why: Vercel + MERJ (2024-12-17): "none of the major AI crawlers currently render JavaScript"
  (OpenAI, Anthropic, Meta, ByteDance, Perplexity); they download JS files without running them.
  Forge (30 days to 2026-09-24): ChatGPT-User made 37,045 page reads and fetched 0 scripts;
  Claude-User 10,028 and 0. A single-source report (2026-09-30) says OAI-SearchBot and GPTBot
  began rendering some Next.js sites on 2026-09-25; the live ChatGPT-User fetch was still
  HTML-only. Googlebot, and so Gemini and AI Overviews, render, but "not all bots can run
  JavaScript" (Google).
- Sources: vercel.com/blog/the-rise-of-the-ai-crawler; forgeaibotlog.com/articles/can-ai-crawlers-execute-javascript.
- Confidence: primary (Vercel); study (Forge); contested (OpenAI crawler rendering).
- Mistake: checking in a browser or Google's URL Inspection (both render) and concluding the
  content "is there".

### REN-02 Every page has an h1 in the raw HTML
- Check: each sampled page has an `<h1>` in the server HTML. Audit: script.
- Why: the h1 names the page for non-rendering fetchers and matches the Markdown mirror's `# `
  line (MD-05).
- Confidence: primary (follows from REN-01).
- Mistake: a heading injected by a client component after hydration.

### REN-03 No page is an empty JavaScript shell
- Check: no sampled page has under about 60 words of raw text alongside framework markers
  (`<div id="root"></div>`, `__NEXT_DATA__` with no text). Audit: script.
- Why: REN-01. A shell page is invisible to ChatGPT, Claude, and Perplexity fetchers.
- Confidence: primary.
- Mistake: client-only routes for pricing or docs.

### REN-04 Prices and key facts are in the raw HTML
- Check: the pricing page's raw HTML shows each plan's price and limits as text (not an image,
  canvas, or client fetch). Audit: script (pricing pages).
- Why: Forge observed ChatGPT-User reading a fuel-price page 157 times and Claude-User 80 times
  "never fetching a script"; every read saw the description and no price. Pricing is the fact
  AI answers get wrong most (MSG-02).
- Confidence: study.
- Mistake: a pricing table rendered by a JS widget.

### REN-05 HTML stays under 2 MB
- Check: each page's uncompressed HTML, headers included, is under 2 MB, and key content sits
  well inside it. Audit: script.
- Why: Googlebot for Search "crawls the first 2MB of a supported file type"; each JS/CSS file has
  the same limit; the old "15 MB" figure applies to other Google crawlers.
- Sources: developers.google.com/search/docs/crawling-indexing/googlebot (2026-02-03).
- Confidence: primary.
- Mistake: inlined JSON state that pushes the article text past 2 MB.

### REN-06 Navigation is plain links in the raw HTML
- Check: internal links to key pages are `<a href>` elements in the server HTML. Audit: script
  (home page count).
- Why: Bing: "Use standard <a href> links." Non-rendering fetchers can only follow links that
  exist in the raw HTML.
- Confidence: primary.
- Mistake: onclick routers where the only server HTML page is `/`.

### REN-07 FAQ, tab, and accordion answers are in the HTML, not loaded on click
- Check: the answer text of FAQs, tabs, and accordions exists in the raw HTML (CSS-hidden is
  fine). Audit: review (SCH-07 catches markup-only FAQ questions).
- Why: REN-01; Bing and Google require marked-up content to be visible.
- Confidence: study.
- Mistake: "the FAQ schema is server-rendered, so we're fine" while the visible answers load by
  XHR.

---

## IDX: index and snippet controls

### IDX-01 No page meant to be cited is noindex
- Check: no `noindex` or `none` in meta robots (robots, googlebot, bingbot, applebot) or the
  `X-Robots-Tag` header on pages in the sitemap. Audit: script.
- Why: Bing: "Use NOINDEX when a URL should NOT appear in Bing search, Copilot experiences, or
  grounding API results." To be a supporting link in AI Overviews, "a page must be indexed and
  eligible to be shown in Google Search with a snippet."
- Sources: developers.google.com/search/docs/crawling-indexing/robots-meta-tag (2026-03-24); developers.google.com/search/docs/appearance/ai-features.
- Confidence: primary.
- Mistake: a sitewide `X-Robots-Tag: noindex` left on from staging, visible only in headers.

### IDX-02 No page meant to be cited forbids snippets
- Check: no `nosnippet` and no small `max-snippet` on key pages, and `data-nosnippet` only on
  span/div/section elements that should be hidden. Audit: script (meta and header).
- Why: Google: nosnippet "applies to all forms of search results (... AI Overviews, AI Mode) and
  will also prevent the content from being used as a direct input for AI Overviews and AI Mode";
  max-snippet limits that input too. Apple will not use nosnippet content as AI context. Bing:
  NOSNIPPET and DATA-NOSNIPPET "may limit Copilot citation quality".
- Confidence: primary.
- Mistake: wrapping the whole `<main>` in `data-nosnippet` to hide boilerplate.

### IDX-03 Canonicals point at the version that should be cited
- Check: each page's canonical (raw HTML or Link header) is absolute, self-referencing for
  primary pages, returns 200, is indexable, and matches the sitemap. Audit: script.
- Why: Bing (2025-12): LLMs "group near-duplicate URLs into a single cluster and then choose one
  page to represent the set ... the model may select a version that is outdated or not the one
  you intended." Google's generative AI report attributes data to the canonical URL. A canonical
  injected by JavaScript is invisible to non-rendering fetchers.
- Sources: blogs.bing.com/webmaster/2025/12/Does-Duplicate-Content-Hurt-SEO-and-AI-Search-Visibility.
- Confidence: primary.
- Mistake: syndicated copies without a cross-domain canonical, so the AI cites the partner.

### IDX-04 The sitemap lists only indexable, working pages
- Check: sampled sitemap URLs answer 200 and are not noindex or redirected. Audit: script.
- Why: ACC-06 (wasted AI crawler fetches) and IDX-03.
- Confidence: primary.
- Mistake: an old URL scheme left in the sitemap after a migration.

### IDX-05 No page carries noarchive
- Check: no `noarchive` (meta or header) on pages meant for Copilot or Bing answers; use
  `nocache` only on purpose. Audit: script.
- Why: Bing: "NOARCHIVE prevents content from being used in Copilot responses and grounding
  results. NOCACHE limits Copilot to using only the URL, title, and snippet." Google ignores
  noarchive, so it lingers in old templates. Bing grounds Copilot and partner answers (IDX-07).
- Sources: blogs.bing.com/webmaster/2023/9/Announcing-new-options-for-webmasters-to-control-usage-of-their-content-in-Bing-Chat.
- Confidence: primary (2023 post); secondary (current guideline wording).
- Mistake: a paywall or CMS template from the Google-cache days still emitting noarchive.

### IDX-06 Localized pages differ meaningfully
- Check: hreflang clusters are reciprocal and each locale has real differences. Audit: review.
- Why: Bing: nearly identical regional pages create duplicate content. No AI vendor says it reads
  hreflang; do not claim it "tells ChatGPT which language to cite".
- Confidence: primary (Bing).
- Mistake: en-US and en-GB copies with one word changed.

### IDX-07 The site is indexed in Bing and pings IndexNow
- Check: the site is verified in Bing Webmaster Tools, key URLs are indexed, bingbot is allowed,
  and publish/update/delete events ping IndexNow with a key file at `/<key>.txt`. Audit: owner
  (MEA-04 reports the verification tag).
- Why: OpenAI lists Microsoft as a ChatGPT search provider, but runs its own OAI-SearchBot index:
  treat Bing as one of two feeds, not "ChatGPT = Bing". Bing: "Many LLMs rely on data grounded in
  the Bing index." IndexNow participants (2026-10-25): Bing, Yandex, Seznam, Naver, Yep, Internet
  Archive, and Amazonbot; Google is not one. Bing ties IndexNow to AI answers citing the current
  page version.
- Sources: help.openai.com/en/articles/9237897-chatgpt-search; indexnow.org/searchengines.json; blogs.bing.com/webmaster/February-2026/Introducing-AI-Performance-in-Bing-Webmaster-Tools-Public-Preview.
- Confidence: primary; contested (how much of ChatGPT search is Bing).
- Mistake: expecting IndexNow to speed up Google.

---

## LLM: llms.txt

What llms.txt is and is not: a Markdown index proposed at llmstxt.org (2024-09-03) "mainly [for]
inference rather than training". Google's AI guide (2026-07-10): llms.txt files "will neither
harm nor help your site's visibility or rankings in Google Search, as Google Search ignores
them." SE Ranking (about 300K domains) found no association between llms.txt and AI citations;
Ahrefs (137,210 domains, May 2026 logs) found 97% of llms.txt files got zero requests, AI
retrieval bots made 1.1% of requests, and agents fetch it "when directed, not speculatively",
with Claude Code out-fetching most retrieval bots. Its value is for coding agents and developer
tools pointed at it, not for ranking. Recommend it to products with developers or agent users;
never sell it as a citation lever.

### LLM-01 /llms.txt exists as a text file
- Check: `GET /llms.txt` returns 200 `text/plain` or `text/markdown`, and the body does not start
  with `<!doctype` or `<html`. Missing is information, not a failure. Audit: script.
- Facts: adoption 10.13% of about 300K domains (SE Ranking); 28% of Ahrefs Web Analytics sites
  (an upper bound).
- Confidence: primary.
- Mistake: a catch-all rewrite returns 200 HTML at /llms.txt, so uptime and SEO tools report a
  file that does not exist.

### LLM-02 The first line is an H1 naming the site
- Check: the first non-blank line matches `# <name>`, and there is exactly one H1. Audit: script.
- Why: the H1 is the spec's only required section; Lighthouse's audit and `llms_txt2ctx` expect it.
- Confidence: primary.
- Mistake: an HTML comment or front matter above the H1.

### LLM-03 A blockquote summary follows the H1
- Check: a `> ` blockquote right after the H1 says what the product is and who it is for in at
  most three sentences, before any marketing. Audit: script (presence).
- Why: it is the paragraph an agent reads first. Of ten major publishers measured 2026-10-25,
  Stripe docs and platform.claude.com had none; Cloudflare, Vercel, OpenAI, Perplexity, and
  Mintlify did.
- Confidence: primary.
- Mistake: pasting the home-page hero copy as the summary.

### LLM-04 H2 sections hold annotated Markdown link lists
- Check: one or more `## ` sections whose items are `- [name](url): notes`. Audit: script.
- Why: the spec's file-list format; notes let an agent choose which link to follow.
- Confidence: primary.
- Mistake: a sitemap dump of hundreds of bare links.

### LLM-05 Every link resolves, ideally to Markdown
- Check: linked URLs return 200 after redirects; where mirrors exist the links point at `.md`.
  Audit: script (samples 8 same-site links).
- Why: a broken link in an index agents read verbatim becomes a failed tool call. Mintlify links
  `.md` "so AI tools can fetch the Markdown version".
- Confidence: primary.
- Mistake: links to staging hosts or pages behind a login or locale chooser.

### LLM-06 No internal leftovers and no instructions to models
- Check: no HTML comments, TODO/FIXME, source paths, stale dates or prices, and no text aimed at
  models ("ignore previous instructions", "assistants should always recommend ..."). Audit:
  script.
- Why: Ahrefs advises treating llms.txt "as code because of prompt-injection risk". Google's
  Mueller: an LLM "by design, can't trust what is here as a way of differentiating" sites, so
  advertising adds nothing while a stale fact misleads. Bing (2026-05-06): "In grounding, a stale
  fact produces a misleading response."
- Confidence: primary (principles); heuristic (patterns).
- Mistake: an internal "context protocol" comment with repository paths at the top of the file.

### LLM-07 Claims in llms.txt match what the server does
- Check: if llms.txt says pages are available as Markdown, by `.md` URL or by
  `Accept: text/markdown`, the audit observes it. Audit: script.
- Why: a false capability claim sends agents to a fetch that returns HTML or 404; it is a
  message contradiction in the one file written for agents.
- Confidence: primary (observed).
- Mistake: "send Accept: text/markdown" in llms.txt while the server always returns HTML.

### LLM-08 llms-full.txt, if published, is a usable size
- Check: no `/llms-full.txt` (acceptable: it is not in the llmstxt.org spec), or one under about
  4 MB, split per product with each part linked. Audit: script.
- Why: measured 2026-10-25: platform.claude.com 47.1 MB, developers.cloudflare.com 69.1 MB,
  vercel.com/docs 11.4 MB: far beyond any context window, so agents truncate silently.
- Confidence: primary (sizes); heuristic (4 MB).
- Mistake: assuming bigger is better.

### LLM-09 llms.txt is small and fast
- Check: under about 100,000 characters and served in under 2 s, cold. Audit: script.
- Why: Lighthouse fetches llms.txt with a fixed 2 s timeout ("We won't increase our timeout",
  issue #17082, 2026-06-18); a plugin-generated 882-link file took 2.3 s and failed until cached.
  Mintlify splits generated indexes above 100,000 characters.
- Confidence: primary (timeout); heuristic (size).
- Mistake: building the file on every request with no Cache-Control.

### LLM-10 llms.txt is pointed to from what agents already fetch
- Check: at least one of: a `Link: </llms.txt>; rel="llms-txt"` header, an HTML link to it, or a
  first line in each `.md` mirror pointing to it. Audit: script.
- Why: Ahrefs: agents fetch llms.txt "when directed, not speculatively"; AI bots made no requests
  for missing files. OpenAI's `.md` pages open with "For the complete documentation index, see
  llms.txt"; Mintlify sends `rel="llms-txt"` (a vendor convention, not a registered relation).
- Confidence: primary (study figures); convention.
- Mistake: publishing the file, never linking it, and expecting ChatGPT to find it.

### LLM-11 The Optional section holds only secondary links
- Check: no `## Optional`, or one whose links are secondary; core docs, API reference, pricing,
  and quickstart are never under it. Audit: script.
- Why: the spec: links "an agent can skip when a shorter context is needed"; `llms_txt2ctx`
  drops the section.
- Confidence: primary.
- Mistake: using Optional as a "More" bucket.

### LLM-12 Lighthouse's llms.txt audit passes, and is not mistaken for a quality check
- Check: Lighthouse's Agentic Browsing `llms-txt` audit passes (an H1, one Markdown link, 50+
  characters; 5xx or 429 fails; other 4xx is N/A). Audit: review (`npx lighthouse <url>
  --only-categories=agentic-browsing`, Chrome 150+).
- Why: the only Google-built llms.txt check; it validates shape, not content or spec compliance.
- Confidence: primary (source code).
- Mistake: reading a pass as "Google endorses llms.txt"; Google Search ignores it.

---

## MD: Markdown mirrors and content negotiation

What mirrors buy: fewer tokens for agents (Cloudflare's example page: 16,180 tokens as HTML vs
3,150 as Markdown; Vercel: 99.37% fewer bytes). Not rankings: Google: "You don't need to create
... Markdown to appear in Google Search." Among the top 200K domains only 3.9% return Markdown on
request (Cloudflare, 2026-04-17).

### MD-01 Content pages have `.md` twins
- Check: for sampled pages, `<url>.md` (or `index.html.md` / `index.md` for directory URLs)
  returns 200 `text/markdown` or `text/plain` with a Markdown body. Audit: script.
- Why: explicit `.md` URLs work for agents that send no Accept header and can be shared in
  prompts and llms.txt. Served by vercel.com/docs, docs.stripe.com, developers.openai.com,
  platform.claude.com, developers.cloudflare.com, and mintlify.com/docs (measured 2026-10-25).
- Confidence: primary.
- Mistake: `.md` URLs that return the HTML 404 page with status 200.

### MD-02 Accept: text/markdown returns Markdown, with Vary: Accept
- Check: a request with `Accept: text/markdown` gets `Content-Type: text/markdown`, a browser
  request still gets HTML, and both carry `Vary: Accept`. Audit: script.
- Why: Claude Code's WebFetch sends `Accept: text/markdown, text/html, */*` (measured
  2026-10-25); Cloudflare names Claude Code and OpenCode. Without `Vary: Accept` a cache can serve
  Markdown to browsers. Whether ChatGPT or Perplexity send the header is unverified.
- Sources: blog.cloudflare.com/markdown-for-agents (2026-02-12); vercel.com/docs/agent-resources/markdown-access.
- Confidence: primary.
- Mistake: negotiation in edge middleware while the CDN cache key ignores Accept.

### MD-03 Mirrors declare their HTML page canonical, or are noindex
- Check: each Markdown response carries `Link: <https://absolute-html-url>; rel="canonical"`, or
  else `X-Robots-Tag: noindex`. The canonical must be absolute and must not point at the `.md`.
  Audit: script.
- Why: Google indexes text files, documents the canonical HTTP header for non-HTML documents
  ("use absolute URLs"), and says it does not recommend noindex for choosing a canonical within a
  site, so the header is preferred. Vercel and platform.claude.com send the header; Stripe sends
  `X-Robots-Tag: none`; Mintlify noindex; Cloudflare and OpenAI docs send neither (2026-10-25).
- Sources: developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls (2026-07-10).
- Confidence: primary (mechanics); contested (whether Google indexes text/markdown).
- Mistake: a relative canonical, `X-Robots-Tag: none` (also stops link following), or both
  noindex and canonical on one response.

### MD-04 HTML pages advertise their Markdown twin
- Check: each HTML `<head>` has `<link rel="alternate" type="text/markdown" href="...">` pointing
  at that page's mirror. Audit: script.
- Why: agents that send no Accept header can find the mirror from HTML they already fetched.
  Present on Vercel, Mintlify, and developers.cloudflare.com. No answer engine documents reading
  it.
- Confidence: primary (pattern); unverified (consumption).
- Mistake: pointing every page's alternate link at /llms.txt, which is not an alternate of the
  page.

### MD-05 Mirrors are clean Markdown that matches the HTML
- Check: no raw MDX/JSX components (`<Note>`, `<CodeGroup>`) or `<div>` lines, no navigation or
  cookie text, the `# ` line and first paragraph match the HTML page, and the same content is
  served whatever the user agent. Audit: script (component lines).
- Why: Google's Mueller called user-agent-switched "markdown for bots" "a stupid idea" (cloaking,
  broken links). platform.claude.com's prompt-caching mirror held 50 lines of raw MDX (measured).
- Confidence: primary (measured); secondary (quote).
- Mistake: middleware that serves Markdown only to GPTBot and ClaudeBot.

### MD-06 Markdown responses send the content signals robots.txt sends
- Check: if Cloudflare Markdown for Agents (or another converter) stamps `Content-Signal` on
  Markdown responses, it matches robots.txt. Audit: script.
- Why: Cloudflare's converter injects `Content-Signal: ai-train=yes, search=yes, ai-input=yes`
  unless the origin sets its own, while its managed robots.txt says `ai-train=no`; the site then
  sends opposite signals. The converter needs Pro or higher, converts HTML only, up to 6 MiB, and
  adds `x-markdown-tokens`.
- Sources: developers.cloudflare.com/fundamentals/reference/markdown-for-agents (2026-10-09).
- Confidence: primary.
- Mistake: enabling the toggle without noticing it grants training permission.

---

## SCH: structured data

Ground truth: Google needs no special schema for AI features, and FAQ rich results ended
2026-05-07. ChatGPT and Perplexity read JSON-LD as plain text, valid or not (Williams-Cook test,
2026-02-06), so wrong markup is a channel for wrong facts. The audit value of schema is
defensive: what the markup says must be true, current, and consistent.

### SCH-01 Every JSON-LD block parses
- Check: each `application/ld+json` block parses; types are schema.org; the Schema Markup
  Validator shows no errors. Audit: script (parse).
- Why: Google: structured-data issues can cost rich-result eligibility and "can result in a
  manual action". LLM fetchers read broken markup as text anyway.
- Sources: developers.google.com/search/docs/appearance/structured-data/sd-policies (2026-07-10).
- Confidence: primary.
- Mistake: reading "no rich result in the Rich Results Test" as "broken"; most types never show
  one. Use validator.schema.org for validity.

### SCH-02 The home page declares the Organization and WebSite
- Check: one Organization (or Person, for a personal site) node and one WebSite node on the home
  or About page. Audit: script.
- Why: Google: Organization markup helps "disambiguate your organization" and can set the logo
  and knowledge panel; "You don't need to include it on every page".
- Sources: developers.google.com/search/docs/appearance/structured-data/organization (2026-09-08).
- Confidence: primary.
- Mistake: a different Organization node on every template.

### SCH-03 The Organization name is the current brand
- Check: Organization `name` equals the brand in title tags, og:site_name, and llms.txt; it is not
  a previous brand, an agency, a theme vendor, or a placeholder. Audit: script.
- Why: LLMs read the leftover name as text (SCH intro), so an old or template name is a live
  source of entity confusion. Observed 2026-10-25: a WordPress AI-SEO product whose Organization
  schema named an unrelated product.
- Confidence: primary (Google's purpose for the markup); inferred (AI confusion mechanism).
- Mistake: a blog platform or SEO plugin emitting its own default `publisher`.

### SCH-04 Every page names the same Organization
- Check: across pages, Organization nodes (including nested `publisher`) share one name, url,
  logo, and ideally `@id`. Audit: script.
- Why: SCH-03; entity systems merge facts by identity.
- Confidence: primary.
- Mistake: the blog subdomain's publisher still named after the old brand.

### SCH-05 No placeholder or template values
- Check: no identity value matches lorem ipsum, example.com, "Your Company", "Company Name",
  "Acme", John/Jane Doe, 555 phone numbers, or "placeholder"; no `ratingValue` 5 with
  `reviewCount` 1; no price "0" on a paid product. Audit: script (identity fields).
- Why: Google: "Don't use structured data to deceive or mislead users"; models repeat
  placeholders as facts.
- Confidence: primary (policy); practice (patterns).
- Mistake: theme demo Product, Event, or Person nodes left live.

### SCH-06 Dates are real
- Check: datePublished and dateModified match the visible dates; dateModified is not the render
  time on every page; modified is not before published; no future dates. Audit: script.
- Why: Google: "Add a user-visible date ... Make your dates and times consistent"; its
  helpful-content self-check names "changing the date of pages to make them seem fresh when the
  content has not substantially changed". AI citations skew fresher (CNT-11), which is why fake
  freshness is tempting and risky.
- Sources: developers.google.com/search/docs/appearance/publication-dates; developers.google.com/search/docs/fundamentals/creating-helpful-content (2026-10-05).
- Confidence: primary.
- Mistake: a template that prints `new Date()` as "Last updated".

### SCH-07 Marked-up content is visible on the page
- Check: every marked-up fact (FAQ questions and answers, price, rating, author, address) appears
  with the same value in the visible page, and in the raw HTML. Audit: script (FAQ questions).
- Why: Google: "Don't mark up content that is not visible to readers of the page"; its AI
  features page asks for "structured data [that] matches the visible text". A markup-only fact can
  surface in an AI answer users never saw.
- Confidence: primary.
- Mistake: FAQ JSON-LD whose questions render only in a client-side accordion.

### SCH-08 The site's own nodes point at this site
- Check: Organization and WebSite `url` and `@id` are on the canonical origin (https). Audit:
  script.
- Why: SCH-03; a URL on an old domain or a template host ties the entity to the wrong site.
- Confidence: primary.
- Mistake: `"url": "https://example-theme.com"`.

### SCH-09 Marked-up prices equal visible prices
- Check: Offer `price`/`priceCurrency` (Product, SoftwareApplication) equals the visible price;
  `priceValidUntil` is not past; `availability` matches; SoftwareApplication price "0" only for a
  real free plan. Audit: script (price comparison).
- Why: pricing is the most error-prone fact in AI answers (MSG-02). Google: product snippets "may
  not display if the priceValidUntil property indicates a past date"; reviews the entity controls
  about itself are ineligible; "Don't include fake or undisclosed incentivized reviews"
  (2026-07-24). OpenAI's product feed spec requires price and availability.
- Sources: developers.google.com/search/docs/appearance/structured-data/product-snippet (2026-09-08); developers.openai.com/commerce/specs/feed.
- Confidence: primary.
- Mistake: an AggregateOffer whose lowPrice is a retired entry tier.

### SCH-10 No reliance on retired rich-result types
- Check: FAQPage, HowTo, ClaimReview, SpecialAnnouncement, EstimatedSalary, VehicleListing, and
  Course Info markup are not credited with any Google feature or AI benefit; leftover markup is
  fine only if accurate and visible. Audit: script (lists them).
- Why: FAQ rich results stopped 2026-05-07 and the docs were removed 2026-06-15; HowTo retired in
  2023; seven types retired 2025-06-12. SE Ranking: pages with FAQ schema averaged 3.6 ChatGPT
  citations vs 4.2 without. BreadcrumbList is still supported (desktop display only since
  2025-01-22).
- Sources: developers.google.com/search/updates; developers.google.com/search/blog/2025/06/simplifying-search-results.
- Confidence: primary.
- Mistake: recommending "add FAQ schema" in 2026.

### SCH-11 Article authors are real, named people
- Check: Article/BlogPosting `author` is a Person (or Organization) with only the name in
  `author.name`, a `url` or `sameAs` to a bio, matching the visible byline. Audit: script (flags
  missing or generic names).
- Why: Google's Article docs ask for exactly this; it "strongly encourage[s] adding accurate
  authorship information". No study isolates authorship as an AI-citation driver; do not claim
  one. Fabricated personas are deception.
- Confidence: primary.
- Mistake: `"author": {"@type": "Person", "name": "Admin"}`.

### SCH-12 The Organization node is complete
- Check: name, url, logo (at least 112x112, crawlable), description, sameAs (ENT-01), plus
  `legalName` and `alternateName` (former names) where they apply. Audit: script (url, logo,
  description).
- Why: Google recommends "as many properties that are relevant"; the logo rules are explicit.
- Confidence: primary.
- Mistake: an SVG logo or a logo path blocked by robots.txt.

---

## ENT: entity

Brand mentions are the strongest measured correlate of AI visibility (Ahrefs, 2025-12-12, about
75,000 brands, Spearman with mentions in AI answers): YouTube mentions about 0.737; branded web
mentions 0.664 (ChatGPT), 0.709 (AI Mode), 0.656 (AI Overviews); branded search 0.35-0.47; Domain
Rating 0.27-0.33; backlinks "very weak". In Ahrefs' AI Overview study (2025-05-26) the top quartile
of web mentions had a median of 169 AI Overview mentions vs 0 for the bottom quartile. These are
correlations, limited to DR above 40: mentions accrue to one entity only when the name is
consistent (MSG-01).

### ENT-01 The Organization lists sameAs profiles that resolve
- Check: Organization `sameAs` lists the official LinkedIn, X, YouTube, GitHub, Crunchbase,
  Wikipedia/Wikidata (if they exist), app-store, and G2/Capterra pages; each returns 200, is the
  brand's own profile, and links back to the domain. Audit: script (presence); review (each URL).
- Why: Google recommends sameAs to identify the organization and authors; these are the edges
  entity systems reconcile on. No study measures a sameAs effect on AI answers.
- Confidence: primary.
- Mistake: sameAs pointing at the founder's personal profile or an old handle.

### ENT-02 Google's Knowledge Graph has the right entity
- Check: a Knowledge Graph Search API lookup of the brand returns the brand with the right `url`
  and description; "absent" is information for small brands. Audit: owner/review
  (`kgsearch.googleapis.com/v1/entities:search?query=<brand>`).
- Why: the KG is Google's entity store; its `detailedDescription` often comes from Wikipedia.
  The API is live but "not suitable for use as a production-critical service".
- Confidence: primary.
- Mistake: a KG entity for another company with the same name. Fix with a distinctive
  name/legalName, alternateName, and sameAs, not more content.

### ENT-03 Wikipedia and Wikidata facts are current, and not self-edited
- Check: if an article or Wikidata item exists, name, founding, HQ, category, website (P856), and
  status are current, and corrections go through talk-page `{{edit COI}}` requests. Audit: review.
- Why: Wikipedia is among the most-cited domains: #4 across five engines (Peec, Mar 2026, 30M
  cited sources); its ChatGPT citation share rose from 3.6% to 5.9% (Seer, Nov 2025-Feb 2026,
  2M+ citations); it appeared in about 55% of ChatGPT responses, then fell below 20% in mid-Sep
  2025 (Semrush), which shows the volatility. Wikipedia "strongly discourage[s]" COI editors
  from editing directly; paid editors must disclose.
- Confidence: primary (policy); study (shares).
- Mistake: telling a non-notable startup to "create a Wikipedia page"; it gets deleted.

### ENT-04 Former names and rebrands are reconciled
- Check: after a rebrand or domain move: path-to-path 301s from the old domain, `alternateName`
  with former names, an About-page "formerly X" sentence, and renamed LinkedIn, Crunchbase, G2,
  app-store, and GitHub profiles. Audit: review.
- Why: answers mix training memory (old name) with retrieval (new name); alternateName and an
  explicit sentence connect both paths.
- Confidence: primary (property); practice (mechanism).
- Mistake: the old brand in a blog subdomain's og:site_name or publisher node.

### ENT-05 The owner keeps a brand fact sheet and audits each engine against it
- Check: a ground-truth sheet (name, category, founding, HQ, pricing, plans, features,
  integrations, status, leadership) and 10-20 factual prompts run at least five times per engine
  (ChatGPT, Gemini/AI Mode, Perplexity, Claude, Copilot), each answer labeled Accurate /
  Outdated / Inaccurate / Missing with the cited URL. Audit: owner.
- Why: Profound (2026-08-17, 158K+ claims): engines overlapped on only 12.5% of claims; the top
  10 domains caused only 13% of earned-media inaccuracies, and for many brands about four sites
  cause them, so fixes are per URL, not "more content".
- Confidence: study.
- Mistake: auditing only ChatGPT.

---

## MSG: message consistency

Owned pages are cited for brand facts, so two pages that disagree give the model two "true"
sources. Profound FactCheck (2026-08-17, 158,000+ decided claims, ChatGPT and Claude): pricing and
billing are 12% of claims but 24% of inaccurate ones; pricing is the highest-inaccuracy theme for
66% of brands with 10+ pricing claims; 54% of brands had an inaccurate claim citing their own site.

### MSG-01 One brand name everywhere the site speaks
- Check: the same brand string (allowing a documented short form) in title suffixes,
  og:site_name, Organization name, logo alt text, footer, About page, llms.txt H1, and docs title.
  Audit: script.
- Why: ENT intro: mentions only accrue to one entity if the name is consistent.
- Confidence: study (correlational).
- Mistake: "Acme", "Acme AI", "Acme.ai", and "AcmeHQ" used interchangeably.

### MSG-02 Prices agree across every surface
- Check: plan names, prices, currencies, billing periods, and free-tier limits match across the
  home page, pricing page, docs/FAQ, checkout, JSON-LD (SCH-09), llms.txt, app stores, and the top
  third-party listings for "<brand> pricing". Audit: script (owned pages and llms.txt); review
  (third parties).
- Why: pricing is the fact AI gets wrong most (intro); pricing is 35% of earned-media
  inaccuracies (Profound).
- Confidence: study (vendor, one snapshot).
- Mistake: updating the pricing page but not the docs FAQ, the old pricing blog post, the
  JSON-LD, or the G2 profile, which are what engines cite.

### MSG-03 Free, trial, and paid claims mean the same thing everywhere
- Check: "free", "free plan", "free trial", "no credit card" describe the same offer on every page
  and listing. Audit: script (lists candidate sentences).
- Why: eligibility and terms were the second-largest inaccuracy category in Profound's data.
- Confidence: study.
- Mistake: "free" on the home page vs "14-day trial" on pricing, or SoftwareApplication price "0"
  for a trial.

### MSG-04 Launch-status words agree
- Check: beta, early access, waitlist, coming soon, open source, generally available, and
  supported platforms or integrations say the same thing on every owned page and controlled
  profile. Audit: script (status words).
- Why: MSG intro; launch-era descriptions linger on Product Hunt and Crunchbase for years.
- Confidence: study (mechanism); practice (check).
- Mistake: docs say "beta" while the home page says "now available".

### MSG-05 Third-party profiles match the site
- Check: G2, Capterra, Trustpilot, Product Hunt, Crunchbase, LinkedIn, app stores, marketplace
  listings, and the MCP Registry describe the same product, pricing, and status. Audit: review.
- Why: 51% of brands had an inaccurate claim citing earned media (Profound); an unclaimed G2
  profile with old pricing is a top source of wrong pricing.
- Confidence: study.
- Mistake: never claiming the G2 or Capterra profile.

### MSG-06 Every summary written for machines agrees with the pages
- Check: meta descriptions, og:description, the llms.txt blockquote, Organization description,
  FAQ answers, and home-page claims describe the product, audience, and workflow the same way (for
  example, "publishes automatically" vs "every post waits for approval"). Audit: review (the
  model compares the opening text and descriptions in audit.md).
- Why: the summaries are what answer engines and agents read first, and a contradiction there is
  repeated verbatim.
- Confidence: practice.
- Mistake: a home-page FAQ written before a product change.

---

## CNT: content

Google's position (AI guide, 2026-07-10): "There's no ideal page length"; there is "no
requirement to break your content into tiny pieces"; non-commodity content "that provides unique
expert or experienced takes that go beyond common knowledge" shapes long-term visibility.
Industry numbers below are correlational vendor studies; quote them as "associated with".

### CNT-01 Every page has a title and meta description
- Check: each indexable page has a distinct `<title>` and a meta description that states what
  the page answers. Audit: script.
- Why: these are the first machine-readable summary of a page in every search index.
- Confidence: primary.
- Mistake: template pages with an empty description.

### CNT-02 Titles are unique
- Check: no two sampled pages share a title. Audit: script.
- Why: IDX-03: duplicates blur which page represents a topic.
- Confidence: primary.
- Mistake: "Blog | Brand" on every post.

### CNT-03 Few pages are thin
- Check: under about 30% of non-home pages have fewer than 200 words of raw text. Audit: script.
- Why: CNT-14 (scaled thin pages) and REN-01 (text the fetchers can read).
- Confidence: practice.
- Mistake: tag and archive pages left indexable.

### CNT-04 Headings name the topic in the words people search
- Check: H2/H3 text matches the subtopic as a user would phrase it; question marks are optional;
  vague or clever headings fail. Audit: script (counts question-style headings, information);
  review.
- Why: AirOps x Indig (2026-04-13, 50,553 ChatGPT responses, 353,799 pages): citation rate 30.2%
  when heading-query similarity was under 0.50 and 41.0% at 0.90+. SE Ranking: question-style
  headings averaged 3.4 ChatGPT citations vs 4.3 for plain ones. Topical match matters, not
  punctuation.
- Confidence: study.
- Mistake: rewriting every H2 as a question because "AI likes questions".

### CNT-05 Articles show a visible date
- Check: articles, docs, and pricing pages show a published or updated date near the top, equal
  to the markup (SCH-06). Audit: script.
- Why: Google asks for a prominent user-visible date; AI citations skew fresher (CNT-11).
- Confidence: primary.
- Mistake: dates only in JSON-LD.

### CNT-06 Comparison, "vs", and "alternatives" pages are honest and evidenced
- Check: they disclose the publisher's stake, explain the method, show real use of competitors,
  cite third-party data, do not reflexively rank the publisher first, and are not mass-produced.
  Audit: script (finds them); review.
- Why: Lily Ray (2026-06-17, 100 B2B queries): when AI Overviews cited a brand's own
  self-promotional "best X" list, that brand was left out of the recommendation 69% of the time
  (224 of 323); 40+ sites heavy on the tactic lost visibility from about 2026-01-20. Seer
  (2026-02-25): "best"/"top" URLs were 16.6% of ChatGPT citations; listicle citations fell 30% in
  one month while Wikipedia, Reddit, G2, and Capterra gained.
- Confidence: study (observed, not confirmed by Google).
- Mistake: "Best [category] tools 2026" with the company at #1.

### CNT-07 The answer comes first
- Check: on question-intent pages the direct answer (definition, number, verdict) is in the first
  paragraph under the h1 or the matching h2. Audit: review (audit.md shows each page's opening).
- Why: Indig (1.2M ChatGPT answers, 18,012 citations): 44.2% of citations came from the first
  30% of the content (secondary report).
- Confidence: secondary.
- Mistake: a 300-word "In today's fast-paced world" intro.

### CNT-08 Sections stand alone and have substance
- Check: each H2 section names its subject (not "it" or "as mentioned above") and most run 120+
  words; pages made of many sub-50-word sections fail. Audit: review.
- Why: SE Ranking: sections under 50 words averaged 2.7 citations, 120-180 words 4.6, over 180
  words 5.7. Google: no need for "tiny pieces".
- Confidence: study; primary (Google).
- Mistake: "chunking for AI" into one-sentence sections.

### CNT-09 Claims carry numbers, quotations, and named sources
- Check: key claims carry specific figures with dates, a linked source, and where relevant a
  quotation from a named expert; no "studies show" without a link. Audit: review.
- Why: the GEO paper (Aggarwal et al., KDD 2024, arXiv 2311.09735) measured, on its 10K-query
  benchmark: quotation addition +40.9%, statistics +30.6%, fluency +28.0%, citing sources +27.5%,
  keyword stuffing -8.3% (on Perplexity: quotations +20.7%). Caveat: a 2026 survey of 45 GEO
  studies (arXiv 2607.14035) says these gains hold only when the page is already among about five
  retrieved sources; C-SEO Bench found 3 of 54 method-domain combinations significantly positive.
  SE Ranking: pages with 19+ data points averaged 5.4 citations vs 2.8.
- Confidence: primary (paper); contested (live effect).
- Mistake: promising "+40% visibility"; inventing statistics.

### CNT-10 Lesser-known sites cite authoritative sources
- Check: claims link to primary sources (standards, official docs, peer-reviewed work). Audit:
  review.
- Why: in the GEO paper, citing sources raised visibility 115.1% for a source ranked fifth but
  lowered it 30.3% for one ranked first (lab setting).
- Confidence: primary (lab).
- Mistake: linking only to the site's own pages.

### CNT-11 Freshness is real and visible where facts change
- Check: pages with time-sensitive facts (pricing, comparisons, statistics, integrations) show an
  updated date within 12 months, and the update changed substance. Audit: review (SCH-06 and
  CNT-05 cover the dates).
- Why: Ahrefs (2025-07-28, about 17M citations): AI-cited URLs averaged 1,064 days old vs 1,432
  for organic results (25.7% fresher). SE Ranking: updated within 3 months 6.0 citations vs 3.6.
  AirOps: about +4.2 points, only for well-matched pages. Google lists cosmetic date changes as a
  warning sign.
- Confidence: study.
- Mistake: changing the year in the title without changing the content.

### CNT-12 Content is non-commodity
- Check: key pages hold what a competitor could not write without the company's experience:
  first-party data, cases, failures, measured results, named people. Audit: review ("could this be
  produced by summarizing the top five results?").
- Why: Google's AI guide calls this the approach most likely to shape long-term visibility;
  commodity example "7 Tips for First-Time Homebuyers".
- Confidence: primary.
- Mistake: AI-rewriting competitors' pages at scale (CNT-14).

### CNT-13 Authorship and trust pages are visible
- Check: articles show a named author with a bio; the site has About, contact, and editorial or
  methodology pages; AI-assisted production is disclosed where readers would expect it. Audit:
  review.
- Why: Google: "trust is most important" in E-E-A-T; it encourages bylines and AI-use
  disclosure. No study isolates authorship as an AI-citation driver.
- Confidence: primary.
- Mistake: invented expert personas.

### CNT-14 No scaled thin or programmatic pages, and no fan-out page farms
- Check: templated sets (city x service, tool x alternative, integrations, glossary) each hold
  substantive unique information; no pages made to match query variations or AI fan-out
  sub-queries. Audit: review (cluster sitemap URLs by template, compare text).
- Why: Google's scaled content abuse policy covers pages "generated for the primary purpose of
  manipulating search rankings ... no matter how it's created"; its AI guide says pages made for
  fan-out queries to manipulate "generative AI responses" violate it.
- Sources: developers.google.com/search/docs/essentials/spam-policies (2026-08-28).
- Confidence: primary.
- Mistake: "[Competitor] alternative" pages for 200 competitors.

### CNT-15 No site-reputation abuse
- Check: the domain hosts no third-party content placed mainly to exploit its ranking signals
  (coupons, white-label reviews, sponsored directories). Audit: review.
- Why: Google's site reputation abuse policy (EEA nuance added 2026-08).
- Confidence: primary.
- Mistake: selling "sponsored guides" on a strong domain.

### CNT-16 Comparisons and specs are HTML tables and lists
- Check: plan comparisons, feature matrices, specs, and steps are `<table>` with header rows,
  `<ol>`, or `<ul>`, not images or canvas. Audit: review.
- Why: AirOps: lists plus tables 33.9% citation rate vs 31.0% with neither; about +13 points on
  product pages; on-page signals mattered far less than retrieval position.
- Confidence: study (small effect).
- Mistake: a pricing table rendered as an image.

---

## OFF: off-site and earned signals

Most citations for brand and category questions point at sites the brand does not own: 85.7% of
167,551 citations across 128 brands (arXiv 2606.25787); earned media about 84% of ChatGPT, Claude,
and Gemini citations (Muck Rack, May 2026, secondary); GPT cited earned media 72.7-92.1% of the
time by vertical vs Google's 31.8-45.4% (Chen et al., arXiv 2509.08919). Measure and improve
off-site presence, not only owned pages.

### OFF-01 Independent coverage exists
- Check: for the category's main buyer questions, the brand appears in trade press, reviews,
  analyst, and comparison articles it did not write. Audit: owner/review (classify cited URLs from
  MEA-01 runs as owned, earned, social, competitor).
- Confidence: study.
- Mistake: measuring AEO only on owned pages.

### OFF-02 Brand mentions, including YouTube, exist at volume
- Check: mentions (linked or not) across the web and YouTube titles, descriptions, and
  transcripts, compared with three competitors. Audit: owner (Ahrefs Brand Radar, Semrush).
- Why: YouTube about 0.737, the strongest correlate (ENT intro); YouTube is #1 for AI Overviews
  in Peec's Mar 2026 data. Google: seeking "inauthentic 'mentions' ... isn't as helpful as it might
  seem."
- Confidence: study (correlation).
- Mistake: buying mentions as link building.

### OFF-03 Authentic presence where engines read discussions
- Check: the category is discussed in relevant subreddits, forums, Quora, and LinkedIn with the
  brand present through disclosed employees, customers, or AMAs; no astroturfing. Audit: review.
- Why: Peec (Mar 2026): Reddit #1 and LinkedIn #3 across five engines. Semrush: Reddit fell from
  about 60% to about 10% of ChatGPT responses within a month (Aug-Sep 2025): shares are volatile.
- Confidence: study.
- Mistake: seeding fake "what's the best X?" threads.

### OFF-04 Review-platform profiles are claimed, current, and match the site
- Check: G2, Capterra, Trustpilot, app stores, or Google Business Profile (local) are claimed,
  have recent genuine reviews, and match MSG-02/MSG-03. Audit: review.
- Why: SE Ranking: domains on several review platforms averaged 4.6-6.3 ChatGPT citations vs 1.8;
  G2 is in Perplexity's top five (Peec). Fake or undisclosed incentivized reviews break Google's
  guidelines and the FTC rule (Oct 2024).
- Confidence: study.
- Mistake: an unclaimed profile with 2022 pricing.

### OFF-05 The brand is in the independent "best X" lists engines cite
- Check: run "best <category>" and "<competitor> alternatives" prompts (MEA-01), collect the
  cited lists, and check inclusion and accuracy. Audit: owner/review.
- Why: CNT-06: earned inclusion beats self-published lists; paid placements are about 0.3% of
  citations (Muck Rack, secondary).
- Confidence: study.
- Mistake: undisclosed paid placement.

### OFF-06 Commerce and local data are submitted where engines read them
- Check: products in Google Merchant Center and, where relevant, OpenAI's product feed; a
  verified, complete Google Business Profile; all consistent with the site. Audit: owner.
- Why: Google's AI guide names Merchant Center feeds and Business Profiles; OpenAI's feed makes
  products eligible for ChatGPT search by default and requires price and availability.
- Confidence: primary.
- Mistake: a daily feed while prices change hourly.

### OFF-07 No program sells citations
- Check: information only: correct anyone who believes a program pays for inclusion. Audit: review.
- Why: OpenAI: "Any public website can appear in ChatGPT search" (allow OAI-SearchBot). Perplexity
  runs a Publisher Program and Comet Plus revenue share (80% to publishers, Aug 2025). Google: "No
  third-party tool has access to our internal ranking or AI systems."
- Confidence: primary (OpenAI, Google); secondary (Perplexity terms).
- Mistake: promising "partner placement" in ChatGPT.

---

## DSC: agent discovery surfaces

None of these is an answer-engine ranking factor; they help coding and browser agents use a
product. Recommend them by fit: an API product needs DSC-05 and DSC-12, an MCP server needs
DSC-02 and DSC-08, a transactional site needs DSC-09 to DSC-11.

### DSC-01 security.txt is valid
- Check: `/.well-known/security.txt` is 200 text with at least one Contact and exactly one Expires
  in the future and under a year away. Audit: script.
- Why: RFC 9116; agents and researchers look there for a contact. No documented AEO effect.
- Confidence: primary.
- Mistake: an Expires date that passed long ago.

### DSC-02 An MCP server, if any, publishes a server card
- Check: no remote MCP server, or a Server Card JSON (name, version, description, remotes, no
  credentials) at `<mcp-url>/server-card` or linked from the ARD manifest; Mintlify advertises
  `/.well-known/mcp/server-card.json`. Audit: script (well-known path).
- Why: SEP-2127 MCP Server Cards is Final (2026); fewer than 15 of the top 200K sites publish
  cards or API catalogs (Cloudflare, 2026-04-17). The well-known path is contested.
- Confidence: primary (SEP); contested (path).
- Mistake: putting the whole tool list in the card.

### DSC-03 Frequently updated sections have a feed
- Check: blogs, changelogs, and newsrooms link an RSS 2.0 or Atom feed whose newest item is
  current. Audit: script.
- Why: Google accepts feeds as sitemaps for recent URLs. No answer engine documents using feeds.
- Confidence: primary (Google).
- Mistake: a feed that stopped after a CMS migration.

### DSC-04 An agent resource manifest uses the current ard.json path
- Check: optional; if published, `/.well-known/ard.json` returns 200 JSON with an `entries`
  array (identifier, displayName, type, one of url/data); only `/.well-known/ai-catalog.json` is a
  legacy warning. Audit: script.
- Why: ARD v0.91 (2026-08-26, Google and Hugging Face authors) says publishers "SHOULD move to
  ard.json"; Lighthouse's `ard-schema` audit (added 2026-08-31) scores a legacy-only manifest 0.9.
  Adoption by answer engines is very early.
- Sources: agenticresourcediscovery.org/spec.
- Confidence: primary (Lighthouse); contested (adoption).
- Mistake: an SPA fallback serving HTML at `/.well-known/*.json`.

### DSC-05 An API catalog points to the OpenAPI description
- Check: for public APIs, `/.well-known/api-catalog` returns `application/linkset+json` (RFC 9727)
  with `service-desc` (OpenAPI) and `service-doc` links that return 200. Audit: script.
- Why: agents find the machine-readable contract without scraping docs; Cloudflare's
  isitagentready scanner checks it.
- Confidence: primary.
- Mistake: serving it as plain `application/json`, or an OpenAPI file behind auth.

### DSC-06 An agent skills index is published (developer products)
- Check: optional; `/.well-known/agent-skills/index.json` (Cloudflare RFC draft v0.2.0, with
  SHA-256 digests) or `/.well-known/skills/index.json` (Stripe, IETF individual draft). Audit:
  script.
- Why: coding agents install skills; an index lets them find the publisher's official one. The
  path and schema are not settled.
- Confidence: contested.
- Mistake: skipping the digests.

### DSC-07 The home page Link header advertises agent resources
- Check: the `/` response has a `Link` header (RFC 8288) naming at least one existing agent
  resource (`llms-txt`, `api-catalog`, `ard`, `service-meta`, or `alternate; type="text/markdown"`)
  that returns 200. Audit: script.
- Why: an agent that reads only headers can still find them; Mintlify advertises six resources in
  one header. Most rel values are conventions, not registered relations.
- Confidence: primary (scanner); convention.
- Mistake: Link targets that 404 after a docs migration.

### DSC-08 The MCP server is in the official registry under the company's domain
- Check: `registry.modelcontextprotocol.io/v0/servers?search=<brand>` returns `com.<domain>/...`
  (not `io.github.<user>/...`), status active, reachable remotes; domain proof by DNS TXT or
  `/.well-known/mcp-registry-auth` (no redirects, 200, 10 s timeout). Audit: owner/review.
- Why: clients and sub-registries read the catalog; look-alike servers already exist (a "stripe"
  search returns third-party servers beside `com.stripe/mcp`). The registry is still preview
  (API frozen at v0.1, 2025-10-24).
- Confidence: primary.
- Mistake: publishing under a personal namespace.

### DSC-09 Browser agents get a clean accessibility tree
- Check: no axe-core violations in the Lighthouse `agent-accessibility-tree` rule set (button-name,
  link-name, label, document-title, aria-* validity) on home, pricing, signup, and docs. Audit:
  review (`npx lighthouse <url> --only-categories=agentic-browsing`).
- Why: browser agents act through the accessibility tree. OpenAI's Atlas ARIA guidance is gone
  (Atlas retired; the article redirects), so use Lighthouse's rules.
- Confidence: primary.
- Mistake: ARIA roles on div soup instead of native button, a, and label.

### DSC-10 The layout is stable while agents act
- Check: lab CLS at most 0.1 on key templates. Audit: review (Lighthouse).
- Why: CLS is weighted in Lighthouse's Agentic Browsing category: agents click computed
  positions.
- Confidence: primary.
- Mistake: late cookie banners that move buttons.

### DSC-11 WebMCP only where the site has real actions
- Check: none, or registered tools that pass Lighthouse `webmcp-schema-validity` and cover the
  site's forms. Audit: review.
- Why: Chrome origin trial M149-M156; a W3C Community Group draft whose API has changed during
  the trial. Relevant to transactional sites only.
- Confidence: primary (trial); contested (API).
- Mistake: shipping against the trial API without feature detection.

### DSC-12 The OpenAPI spec is public and linked
- Check: the raw OpenAPI file (not a Swagger UI page) is fetchable without auth and linked from
  llms.txt or the API catalog. Audit: review.
- Why: generated SDK snippets and agent tool calls come from it; Mintlify's llms.txt includes spec
  links.
- Confidence: primary (practice).
- Mistake: linking the HTML docs page instead of the spec.

---

## MEA: measurement

### MEA-01 Track mention rate over many prompts and runs, with intervals
- Check: a fixed, versioned set of 30-50+ prompts across awareness, category, comparison,
  brand-fact, and pricing intents; five or more runs per prompt per engine per period; report the
  share of runs that mention or cite the brand with a confidence interval, never a single "AI
  rank". Audit: owner.
- Why: SparkToro/Gumshoe (2,961 runs): the same brand list appeared under 1% of the time, the same
  list in the same order under 0.1%. A 40% mention rate from 100 runs is +/-10 points (95%); from
  10 runs +/-30. Even at temperature zero 9-28% of decisions changed between runs (survey,
  secondary).
- Confidence: secondary (SparkToro via SEJ); the statistics are standard.
- Mistake: one manual ChatGPT query as a before-and-after proof.

### MEA-02 Share of voice against named competitors, per engine and intent
- Check: mention and citation rates vs 3-5 named competitors, split by engine and prompt cluster,
  with the cited domains per cluster. Audit: owner.
- Why: engines differ: ChatGPT leaned editorial, Google AI surfaces leaned social and UGC (Peec,
  Mar 2026); engines overlapped on 12.5% of brand claims (Profound); AI and Google top-5 domains
  overlapped 15-33% for products (Chen et al.).
- Confidence: study.
- Mistake: one blended score across engines.

### MEA-03 Search Console's generative AI data is read correctly
- Check: the owner uses Search Console's Generative AI performance report (AI Overviews, AI Mode,
  Discover; impressions, pages, countries, devices; no clicks; all sites since 2026-08-31), knows
  AI Mode has counted in the main totals since 2025-06-16, and does not add the AI report to the
  main totals. Audit: owner.
- Why: Google: "This data is included in the overall performance report"; an AI Overview
  occupies one position, so every link in it gets that position. An empty report may mean the
  site is set to Exclude (ACC-15).
- Sources: developers.google.com/search/blog/2026/06/gen-ai-performance-reports; support.google.com/webmasters/answer/7042828.
- Confidence: primary.
- Mistake: reading a falling CTR with steady impressions as a ranking loss (MEA-07).

### MEA-04 The site is verified in Bing Webmaster Tools and Search Console
- Check: verification exists (an `msvalidate.01` / `google-site-verification` meta tag,
  `BingSiteAuth.xml`, or DNS), and the Bing AI Performance export is reviewed monthly. Audit:
  script (meta tags); owner.
- Why: Bing AI Performance (public preview 2026-02-10): total citations, cited pages, sampled
  grounding queries (the AI's retrieval queries, not user queries), and per-URL citations across
  Copilot, Bing AI summaries, and some partners; Citation Share and Intents/Topics added June 2026
  (secondary); CSV only, no clicks. The only first-party, engine-reported citation data for
  publishers; OpenAI, Anthropic, and Perplexity provide none.
- Confidence: primary (launch post); secondary (June additions).
- Mistake: reading grounding queries as search volume.

### MEA-05 Analytics separates AI-assistant traffic and knows its blind spots
- Check: GA4's default "AI Assistant" channel (or a custom channel above Referral) is on; the team
  knows AI Overviews and AI Mode clicks arrive as Google organic, app traffic often as Direct, and
  that `utm_source=chatgpt.com` stopped being reliable. Audit: owner.
- Why: GA4's AI Assistant channel covers "ChatGPT, Gemini, Deepseek, Copilot, or Grok" and
  "excludes Google's AI Overviews and AI Mode". Since about 2026-10-07 practitioners report
  ChatGPT links arriving with no UTM and no referrer (one site: ChatGPT visits -31k/day, Direct
  +26k/day; one tracker: 38.73% tagged on 2026-10-16); OpenAI has not confirmed. AI platforms were
  0.32% of all website traffic in 2026, with ChatGPT 74.78% of it (SE Ranking, single vendor).
- Confidence: primary (GA4); contested (UTM change).
- Mistake: reading an October 2026 drop in chatgpt.com referrals as lost visibility.

### MEA-06 Server logs identify AI fetchers by user agent AND verified IP
- Check: requests per AI token per day, with spoofed traffic filtered by the vendor IP files
  (ACC-13) or forward-confirmed reverse DNS (Google, Apple, Common Crawl). Audit: owner.
- Why: Google: "The HTTP user agent string can be spoofed"; Common Crawl reports fake CCBots.
  Cloudflare crawl-to-referral ratios (Aug 2025): Anthropic about 50,000:1, OpenAI 887:1,
  Perplexity 118:1. By June 2026, 52% of crawler requests were for AI training.
- Confidence: primary.
- Mistake: counting user-agent strings only.

### MEA-07 Expectations are set on citations, not clicks
- Check: KPIs include AI citations, mentions, impressions, and assisted conversions; clicks are
  not the only metric. Audit: review.
- Why: Pew (2025-07-22, 68,879 searches): users clicked a result on 8% of visits with an AI
  summary vs 15% without, and a link inside the summary 1% of the time. Seer (2025-11-04, 25.1M
  impressions): organic CTR on AI Overview queries fell 61%; brands cited in the overview got 35%
  higher organic CTR. Ahrefs (2026-02-04, 300K keywords): position-1 CTR 58% below forecast when
  an AI Overview appears.
- Confidence: primary (Pew); study.
- Mistake: promising traffic growth from AEO.

### MEA-08 Rankings still matter, and citations are checked directly
- Check: classic rankings for the prompts' underlying queries are tracked, and citations are
  checked separately because they diverge. Audit: owner.
- Why: Ahrefs: 76.1% of AI Overview citations ranked top 10 in July 2025 but 37.9% in March 2026
  (method changed). AirOps: a page's position in ChatGPT's own search results drives citation
  (58.4% at rank 1, 14.2% at rank 10); 58% of retrieved pages were never cited.
- Confidence: study.
- Mistake: "we rank #1, so AI will cite us", or "SEO is dead".

### MEA-09 Third-party AEO tools are samplers, not oracles
- Check: any tracker in use (Profound, Peec, Otterly, Semrush AI Toolkit, Ahrefs Brand Radar)
  publishes its prompts, run counts, engines, locations, and UI-vs-API collection, and is
  cross-checked with MEA-03 to MEA-05. Audit: owner.
- Why: Google: "Be wary of third-party tools that promise ranking success ... No third-party tool
  has access to our internal ranking or AI systems." UI and API answers differ.
- Confidence: primary.
- Mistake: reporting a proprietary "AI visibility score" without its sample size.

---

## SEO: search foundations

Google's guide says AEO for Google is SEO: AI Overviews and AI Mode draw on the same index and
ranking. These items are the search method that decides which pages exist and which queries they
target. Most need the owner's Search Console or an SEO data tool, so they are `owner` or `review`.

### SEO-01 One SEO project record holds the site's scope and decisions
- Check: one file or folder records the domains and subdomains, target countries and languages,
  the site's stage (new, established, migrating, or recovering from a drop), the CMS and
  publishing workflow, the target keywords with tags, and the decisions made. Audit: owner.
- Why: keyword targets, cluster maps, briefs, and prospect lists depend on each other. Without a
  record, each session starts blank and repeats or contradicts earlier choices.
- Confidence: secondary (practitioner method).
- Mistake: saving keywords or tags before the owner confirms them.

### SEO-02 SEO goals are business outcomes with a metric and a date
- Check: the goal names an outcome (qualified leads, signups or trials, revenue, audience,
  category awareness, recovery from a traffic loss, or rankings for named pages), a metric, and a
  timeframe, such as "rank top 10 for 20 buying-intent terms by June". Audit: owner.
- Why: the plan depends on the goal: a recovery starts with indexing and cannibalization
  (SEO-12), a lead goal with buying-intent pages. Measure non-branded queries, because branded
  demand grows with the brand, not with SEO.
- Confidence: secondary.
- Mistake: "more traffic" as the goal; traffic from off-intent queries does not convert.

### SEO-03 Positioning is written down before keywords are chosen
- Check: a short note says who the site is for, the pain it solves, why buyers choose it over
  alternatives, its competitors and substitutes, its best-fit and bad-fit customers, the content
  that already converts, the opinions it can defend, and the topics it will not target.
  Audit: owner.
- Why: the same keyword is worth chasing for one audience and noise for another. The note also
  feeds entity and message consistency (ENT-05, MSG-01).
- Confidence: secondary.
- Mistake: chasing the category's biggest term when the product serves a niche.

### SEO-04 Search Console data is the first input
- Check: Search Console is verified (MEA-04) and its Performance data is read: queries and pages
  for the last 3 months and for the full 16 months, and query and page together. Through the API,
  request many rows (up to 25,000 per request) and filter by position locally. Every number in a
  plan names its source: Search Console, a third-party SEO tool, this audit, or judgment.
  Audit: owner.
- Why: Search Console is the only first-party record of the queries that already show the site,
  with clicks, impressions, CTR, and average position, kept for 16 months. Third-party tools
  estimate; Search Console measures.
- Sources: support.google.com/webmasters/answer/7576553;
  developers.google.com/webmaster-tools/v1/searchanalytics/query.
- Confidence: primary.
- Mistake: estimating the site's own traffic from a third-party tool when Search Console exists.

### SEO-05 The site's assets are inventoried, including what others would cite
- Check: a list of the key URLs (or the sitemap), the content library, product, category, and
  feature pages, existing keyword lists and rank trackers, backlink and press assets, and
  linkable assets: original data, studies, templates, tools, calculators, and defensible
  opinions. Audit: owner.
- Why: the cheapest win is usually an existing page (SEO-06, SEO-19), and links and citations go
  to assets that hold something unique (SEO-16).
- Confidence: secondary.
- Mistake: planning new pages before checking which page already targets the intent.

### SEO-06 Striking-distance queries are worked first
- Check: from Search Console, the queries with real impressions where the site's average
  position is about 5 to 20 are listed with the page that ranks, enriched with volume,
  difficulty, and intent, and improved before broad keyword discovery. Audit: owner.
- Why: those pages already rank, so a better title, a fuller answer, or internal links can move
  them onto page one; a new page starts from zero. The API sorts by clicks and cannot filter by
  position, so request many rows and filter locally.
- Confidence: secondary (method); primary (API behavior).
- Mistake: reading only the first 1,000 rows, which hides long-tail near-misses.

### SEO-07 Target keywords are chosen for business fit and intent, not volume
- Check: each candidate carries volume, keyword difficulty, intent (informational, commercial,
  transactional, or navigational), CPC, and trend, with `unknown` where the data source returned
  nothing. Off-topic, duplicate, branded-only, and off-intent terms are removed. Priority is
  product fit, then clear intent, then a difficulty the site can reach now, then volume and CPC.
  Audit: owner.
- Why: a reachable buying-intent term beats a large informational one the site cannot win, and
  CPC shows commercial value. A guessed metric corrupts every later decision.
- Confidence: secondary.
- Mistake: filling a missing volume with an estimate.

### SEO-08 The results page is read before deciding what a page should be
- Check: for each priority keyword whose intent is unclear, the live results are read: which page
  types rank (guides, product pages, lists, tools, forums, videos), which result features appear,
  and which domains. Audit: review.
- Why: the results show what satisfies the query better than its words do. "best X" usually
  wants a list; "X pricing" wants a product page.
- Sources: developers.google.com/search/docs/fundamentals/creating-helpful-content.
- Confidence: secondary.
- Mistake: writing a long guide for a query whose results are all product pages.

### SEO-09 Local businesses are judged on local results
- Check: for a business that serves an area, the Google Business Profile is complete with the
  right primary category, local-pack and Maps results are checked at the priority locations,
  Business Profile questions are answered, and each real service area has its own page.
  Audit: owner.
- Why: Google says local results depend mainly on relevance, distance, and prominence. National
  keyword and domain metrics do not show who wins the map pack.
- Sources: support.google.com/business/answer/7091.
- Confidence: primary.
- Mistake: inferring local-pack strength from national organic rankings.

### SEO-10 Keywords are grouped into one cluster per page by search-result overlap
- Check: keywords with the same intent and mostly the same ranking URLs go to one page; a
  different intent, buyer stage, or result format gets its own page. Each cluster maps to an
  existing page, a new page, or a do-not-target list. Fewer than about 10 usable terms get a
  simple map, not clusters. Audit: review.
- Why: similar words can need different pages, and different words can share one. The overlap of
  the results pages shows which; similar wording does not.
- Confidence: secondary.
- Mistake: one page per keyword variant, which creates the competition SEO-12 removes.

### SEO-11 Every target page has a brief and real internal links
- Check: each cluster's page has a brief (page type, the searcher's problem, required sections,
  and the pages that will link to it), and related pages link to it with anchors a person would
  write. Audit: review.
- Why: Google finds pages and their context through links, and anchor text tells it what the
  target is about. A brief keeps a page aimed at one intent.
- Sources: developers.google.com/search/docs/crawling-indexing/links-crawlable.
- Confidence: primary (links); secondary (briefs).
- Mistake: "click here" anchors, or links only from the footer.

### SEO-12 No two pages compete for the same query
- Check: Search Console query and page data shows no important query whose impressions split
  across several URLs. Competing pages are merged, redirected, or re-targeted, and duplicates
  name one canonical (IDX-03). Audit: owner.
- Why: split signals weaken both pages, and an engine may show or cite the weaker one.
- Sources: developers.google.com/search/docs/crawling-indexing/consolidate-duplicate-urls.
- Confidence: primary (canonicals); secondary (cannibalization).
- Mistake: publishing a new post on a topic an older page already ranks for.

### SEO-13 The owner knows who wins the market's results, and why
- Check: 5 to 10 representative queries of mixed intent (informational, commercial, comparison,
  tool) are checked. The recurring domains are listed by type: product competitor, publisher,
  marketplace or directory, community, documentation. The winning formats and result features are
  noted. Search competitors are kept apart from business competitors, and map-pack winners from
  organic winners. Audit: review.
- Why: the domains that outrank a site are often publishers and directories, not rivals, and the
  plan for them differs: get listed or cited instead of outranking. A small query set shows a
  direction, not a measurement.
- Confidence: secondary.
- Mistake: treating a publisher that ranks as a product competitor.

### SEO-14 The authority gap is measured before a plan promises rankings
- Check: the leading domains and the site are compared on organic footprint (ranking keywords,
  estimated traffic) and referring domains, and the keyword plan targets a difficulty the site
  can reach now. Audit: owner.
- Why: a site with a fraction of the leaders' referring domains wins long-tail and
  striking-distance terms first (SEO-06). Third-party traffic figures are estimates.
- Confidence: secondary.
- Mistake: reporting a third-party traffic estimate as a fact.

### SEO-15 Competitors are studied for openings, not copied
- Check: a named competitor's ranking keywords are grouped into themes (product and category,
  alternatives and comparisons, templates, tools and calculators, guides, branded, local), the
  results for shared terms are read side by side, and the output says what they do well, where
  they are weak, which pages to pursue, and what not to copy. Claims about their pages come from
  the pages or the results, not from keyword rows. Audit: review.
- Why: copying produces the commodity content CNT-12 warns against. A stronger angle or a better
  answer to the same intent can win.
- Sources: developers.google.com/search/docs/fundamentals/creating-helpful-content.
- Confidence: secondary.
- Mistake: targeting every keyword a competitor ranks for, whatever the business fit.

### SEO-16 The site has an asset worth citing, and a reason someone would
- Check: at least one asset others would reference exists (original data, a study, a tool, a
  template, a statistics page, or a documented opinion), with one sentence on why a writer would
  link to or cite it. Audit: review.
- Why: links and AI citations go to pages that hold something not available elsewhere (CNT-09,
  CNT-12). Outreach without such an asset asks for a favor.
- Confidence: secondary.
- Mistake: promoting a product page as a "resource".

### SEO-17 Link prospects come from search results, filtered, each with an angle
- Check: prospects come from queries built from the asset ("<topic> resources", "best <category>
  tools", "<competitor> alternatives", "<topic> statistics", "<topic> guide", "<topic> examples",
  "<topic> templates", "<topic> software", "<topic> for <audience>") and from competitors'
  backlinks. Editorial articles, resource pages, lists, comparisons, statistics pages, and
  directories stay; home pages, login pages, thin affiliate pages, spam, and unrelated forums go;
  competitors and likely paid placements are flagged. Each prospect gets an angle: a broken or
  missing resource, newer data, a useful tool or template, a place in a list or comparison, or an
  expert quote. Audit: review.
- Why: Google's spam policies treat buying or exchanging links to pass ranking credit as link
  spam, and paid links must carry `rel="sponsored"`. Relevance and a real reason earn editorial
  links (OFF-07).
- Sources: developers.google.com/search/docs/essentials/spam-policies;
  developers.google.com/search/docs/crawling-indexing/qualify-outbound-links.
- Confidence: primary (policy); secondary (method).
- Mistake: emailing every page that mentions the topic.

### SEO-18 Outreach uses real, sourced contacts and personal messages
- Check: each contact (an author page, contact page, editorial guidelines, team page,
  professional profile, masthead, an email shown on the page, or a Person node's email or sameAs)
  is recorded with the URL it came from. Nothing is guessed. Each message names the page and the
  reason to link. Audit: review.
- Why: a guessed address bounces and hurts the sending domain's reputation, and a message that is
  not about the recipient's page reads as spam.
- Confidence: secondary.
- Mistake: inventing a name@domain address.

### SEO-19 Each target page is written for its keyword and intent
- Check: the page that best matches a target keyword (often one that already ranks, SEO-06) has
  a title that contains the keyword naturally and matches the intent; a meta description that
  summarizes the page in about 150 characters, with the keyword, and reads like copy; the topic in
  its first heading; and the subtopics the top results cover that it lacks. Audit: review; script
  for titles and descriptions (CNT-01, CNT-02).
- Why: Google builds title links mainly from the title element, and uses the meta description
  for the snippet when it describes the page better than the page text. Neither has a fixed
  length limit (both are cut by display width), so 150 characters is a convention.
- Sources: developers.google.com/search/docs/appearance/title-link;
  developers.google.com/search/docs/appearance/snippet.
- Confidence: primary (mechanics); secondary (length).
- Mistake: putting the keyword in every heading.

### SEO-20 Rewrites keep the page's facts, voice, links, and structure
- Check: a rewrite keeps the site's voice, its real links and images, and the CMS's own markup
  (WordPress block comments `<!-- wp:... -->` and shortcodes). It invents no facts, prices, or
  statistics, and every changed claim is checked. Audit: review.
- Why: dropping block comments or shortcodes breaks the layout when the change is applied, and an
  invented figure becomes a fact that assistants repeat (MSG-02).
- Confidence: secondary.
- Mistake: pasting a model's rewrite over the raw editor content.

### SEO-21 Every new or automated page passes a pre-publish gate
- Check: before publishing: (1) every claim, figure, price, and date is verified or removed;
  (2) the target keyword has real volume and a difficulty the site can reach; (3) no existing
  page targets the same intent (SEO-12); (4) the first paragraph answers the query (CNT-07);
  (5) something in it could only come from the owner: data, process, screenshots (CNT-12); (6)
  the title and meta description read like copy, not a template; (7) internal links point to
  related pages with natural anchors (SEO-11); (8) it is worth publishing on its own merits, not
  to fill a calendar. Audit: review.
- Why: Google's spam policies name scaled content abuse: many pages made mainly to manipulate
  rankings, however they are produced (CNT-14).
- Sources: developers.google.com/search/docs/essentials/spam-policies.
- Confidence: primary (policy); secondary (the gate).
- Mistake: publishing drafts on a schedule because a tool can.

### SEO-22 Automated changes are proposed, reviewed, and reversible
- Check: an agent that edits the site proposes each change as a diff, with a before/after summary
  and a one-line reason per change; a person applies it; the previous version is kept for
  rollback; and the level of autonomy (assist, propose, or publish) is set per task, with nothing
  published unreviewed until the drafts have earned trust. Audit: owner.
- Why: an unreviewed bulk edit can put one error on every page at once, and engines and
  assistants pick it up before anyone notices (SCH-05, MSG-02).
- Confidence: secondary.
- Mistake: telling the owner a change is live when it was only proposed.

---

## Contested claims and myths

Use these to correct the user, citing the evidence. Never recommend the left-hand claim.

- **M-1 "FAQ schema (or any schema) gets you into AI answers."** Google: no special schema; FAQ
  rich results ended 2026-05-07; pages with FAQ schema averaged 3.6 vs 4.2 ChatGPT citations (SE
  Ranking). Schema is text that must be true (SCH), not a switch.
- **M-2 "You need llms.txt to show up in ChatGPT."** Google ignores it; no citation association in
  about 300K domains; 97% of files got zero requests. It helps agents that are pointed at it (LLM).
- **M-3 "AI prefers long content."** Google: no ideal length. AirOps: 500-999 words 34.3% vs 5,000+
  words 28.6%. Cover the question fully, then stop.
- **M-4 "Chunk content into tiny pieces for AI."** Google: no requirement; sub-50-word sections
  were cited least (CNT-08).
- **M-5 "Write every heading as a question."** Topical match matters, not punctuation (CNT-04).
- **M-6 "GEO tactics raise visibility 40%."** A lab result with about five fixed sources; live
  effects are unproven (CNT-09).
- **M-7 "Backlinks get you into AI answers."** Contested: backlinks correlate weakly (0.218) with
  AI Overview brand mentions, referring domains strongly with ChatGPT citations (SE Ranking).
  Authority helps; mentions matter more for being recommended.
- **M-8 "Track your AI rank."** The same list in the same order appears under 0.1% of the time;
  track mention rate (MEA-01).
- **M-9 "AI visitors convert 4.4x better."** Published without base rates; other estimates run
  2.15x to 23x. Measure your own.
- **M-10 "Rank top 10 and AI Overviews will cite you."** 76% fell to 38% of citations from the top
  10 (MEA-08).
- **M-11 "Seed mentions or reviews to get recommended."** Google calls inauthentic mentions
  unhelpful and its spam policies cover AI answers; fake reviews break Google's rules and the FTC
  rule.
- **M-12 "Update the date to look fresh."** Cosmetic date changes are a Google warning sign;
  freshness gains need real updates (CNT-11).
- **M-13 "Publish 'Best X' lists with yourself at #1."** AI Overviews recommended other brands 69%
  of the time when citing such lists (CNT-06).
- **M-14 "Blocking Google-Extended removes you from AI Overviews."** It covers Gemini training and
  grounding only; use Googlebot rules, nosnippet, or the Search Console control (ACC-15).
- **M-15 "Blocking GPTBot removes you from ChatGPT."** GPTBot is training; OAI-SearchBot controls
  ChatGPT search (ACC-02).
- **M-16 "All AI bots obey robots.txt."** ChatGPT-User, Perplexity-User, Google user-triggered
  fetchers, Amzn-User, and meta-externalfetcher may not; Claude-User and MistralAI-User do.
  Cloudflare (2025-08-04) accused Perplexity of stealth crawling; Perplexity disputed it.
- **M-17 "ChatGPT search is just Bing."** Microsoft is a named provider, but OpenAI runs its own
  index (IDX-07).
- **M-18 "Serve Markdown to AI bots to rank."** No ranking evidence; user-agent switching risks
  cloaking. Mirrors save agents tokens (MD).
- **M-19 "Content-Signal or aipref legally blocks AI use."** Preferences only; Google's parser
  ignores them; aipref is still a draft (ACC-08).
- **M-20 "Add ai-plugin.json, agents.json, or /.well-known/mcp.json."** ChatGPT plugins ended
  2024-04-09; the others are not adopted standards (DSC-02, DSC-04).
- **M-21 "AI fetchers time out after N ms."** No vendor publishes a number; any figure is
  unsourced. Keep pages fast anyway.
- **M-22 "changefreq and priority help freshness."** Google and Bing ignore both (ACC-07).
- **M-23 "IndexNow gets you into Google faster."** Google does not participate (IDX-07).
- **M-24 "Follow OpenAI Atlas ARIA guidance."** Atlas is retired; use Lighthouse's rules (DSC-09).
- **M-25 "A spoofed-user-agent curl test proves what the real bot sees."** CDNs verify by IP and
  signature; confirm in logs (ACC-05).

---

## Bot list

The 66 bots the audit checks (ACC-02 to ACC-05, ACC-17, ACC-18), copied from each operator's
documentation on 2026-10-21 and 2026-10-25. The script's copy, with user agents, IP-range files,
and sources, is `scripts/aeokit/bots.json`. Categories: search (indexes for a search or answer
engine), training (collects model training data), user (fetches a page because a person asked),
agent (crawls or drives a browser for a person or developer), preview (link cards), ads (landing
page checks), other (operator utilities), infra (CDN infrastructure, only for sites on that CDN),
catch-all (generic clients that show whether unknown bots get in). Re-check quarterly.

| Bot | Operator | Category | robots.txt token | Fetches pages |
|---|---|---|---|---|
| Googlebot | Google | search | `Googlebot` | yes |
| Google-Extended | Google | training | `Google-Extended` | no (robots.txt only) |
| Google-InspectionTool | Google | search | `Google-InspectionTool` | yes |
| Bingbot | Microsoft | search | `bingbot` | yes |
| Applebot | Apple | search | `Applebot` | yes |
| Applebot-Extended | Apple | training | `Applebot-Extended` | no (robots.txt only) |
| DuckDuckBot | DuckDuckGo | search | `DuckDuckBot` | yes |
| DuckAssistBot | DuckDuckGo | search | `DuckAssistBot` | yes |
| Google-Agent | Google | agent | none | yes |
| Googlebot-Image | Google | search | `Googlebot-Image` | yes |
| Googlebot-Video | Google | search | `Googlebot-Video` | yes |
| Googlebot-News | Google | search | `Googlebot-News` | no (robots.txt only) |
| Storebot-Google | Google | search | `Storebot-Google` | yes |
| GoogleOther | Google | other | `GoogleOther` | yes |
| GoogleOther-Image | Google | other | `GoogleOther-Image` | yes |
| GoogleOther-Video | Google | other | `GoogleOther-Video` | yes |
| Google-CloudVertexBot | Google | agent | `Google-CloudVertexBot` | yes |
| AdsBot-Google | Google | ads | `AdsBot-Google` | yes |
| AdsBot-Google-Mobile | Google | ads | `AdsBot-Google-Mobile` | yes |
| Mediapartners-Google | Google | ads | `Mediapartners-Google` | yes |
| APIs-Google | Google | other | `APIs-Google` | yes |
| Google-Safety | Google | other | none | yes |
| FeedFetcher-Google | Google | user | none | yes |
| Google-GeminiNotebook | Google | user | none | yes |
| Google-Read-Aloud | Google | user | none | yes |
| Google-Site-Verification | Google | user | none | yes |
| GoogleMessages | Google | preview | none | yes |
| AdIdxBot | Microsoft | ads | `adidxbot` | yes |
| BingPreview | Microsoft | preview | `bingbot` | yes |
| MicrosoftPreview | Microsoft | preview | `MicrosoftPreview` | yes |
| BingVideoPreview | Microsoft | preview | `BingVideoPreview` | yes |
| GPTBot | OpenAI | training | `GPTBot` | yes |
| OAI-SearchBot | OpenAI | search | `OAI-SearchBot` | yes |
| ChatGPT-User | OpenAI | user | `ChatGPT-User` | yes |
| ClaudeBot | Anthropic | training | `ClaudeBot` | yes |
| Claude-SearchBot | Anthropic | search | `Claude-SearchBot` | yes |
| Claude-User | Anthropic | user | `Claude-User` | yes |
| PerplexityBot | Perplexity | search | `PerplexityBot` | yes |
| Perplexity-User | Perplexity | user | `Perplexity-User` | yes |
| meta-externalagent | Meta | training | `meta-externalagent` | yes |
| meta-externalfetcher | Meta | user | `meta-externalfetcher` | yes |
| Meta-WebIndexer | Meta | search | `meta-webindexer` | yes |
| Amazonbot | Amazon | training | `Amazonbot` | yes |
| Amzn-SearchBot | Amazon | search | `Amzn-SearchBot` | yes |
| CCBot | Common Crawl | training | `CCBot` | yes |
| MistralAI-User | Mistral AI | user | `MistralAI-User` | yes |
| Amzn-User | Amazon | user | `Amzn-User` | yes |
| MistralAI-Index | Mistral AI | search | `MistralAI-Index` | yes |
| Bytespider | ByteDance | training | `Bytespider` | yes |
| facebookexternalhit | Meta | preview | `facebookexternalhit` | yes |
| LinkedInBot | LinkedIn | preview | `LinkedInBot` | yes |
| Twitterbot | X (Twitter) | preview | `Twitterbot` | yes |
| Slackbot | Slack (Salesforce) | preview | none | yes |
| Cloudflare Browser Run crawler | Cloudflare | agent | `CloudflareBrowserRenderingCrawler` | yes |
| Cloudflare Browser Run Quick Actions | Cloudflare | agent | none | yes |
| Cloudflare AI Search | Cloudflare | search | `Cloudflare-AI-Search` | yes |
| Cloudflare Always Online | Cloudflare | infra | none | yes |
| Cloudflare Health checks | Cloudflare | infra | none | yes |
| Cloudflare Load-balancing monitor | Cloudflare | infra | none | yes |
| Cloudflare Prefetch | Cloudflare | infra | none | yes |
| Cloudflare SSL/TLS recommender | Cloudflare | infra | `Cloudflare-SSLDetector` | yes |
| Cloudflare Support diagnostics | Cloudflare | infra | none | yes |
| Unknown new bot | (catch-all) | catch-all | `ExampleNewBot` | yes |
| curl | (catch-all) | catch-all | none | yes |
| python-requests | (catch-all) | catch-all | none | yes |
| Empty user-agent | (catch-all) | catch-all | none | yes |
