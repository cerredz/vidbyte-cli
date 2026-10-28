# App conversion tactics: fact ledger

This ledger is the source of truth for in-app conversion advice in this skill. Every fact comes from
one article; Apple's own wording is attached where the article relies on Apple's rules.

- **The article:** Frederick James (@frederickjames), "The 48 App Conversion Tactics.", an X Article
  published 2026-10-25, <https://x.com/frederickjames/status/2114347402819690646>. Read on
  2026-10-28: its text, two flow snippets, nine images, and the three posts it embeds
  (2085709435498807593, 2104522475761455536, 2112162857567625568).
- **Apple:** App Review Guidelines, <https://developer.apple.com/app-store/review/guidelines/>,
  fetched 2026-10-28T19:03:51Z; Retention Messaging API documentation,
  <https://developer.apple.com/documentation/retentionmessaging>, fetched 2026-10-28.

## How to read this ledger

- **IDs.** Tactics: `A-` allowed, `G-` gray area, `B-` banned. Supporting facts: `P-` paywall
  structure, `F-` flows and events, `N-` numbers, `X-` rules that apply to every tactic, `V-`
  verification notes (gaps and errors found while checking the article).
- **Source tags.** `[article]` the article's text, or the post that published it; `[image]` read
  from an image in the article; `[post <id>]` a post the article embeds, or an image in it;
  `[apple § x]` Apple's wording, quoted; `[derived]` arithmetic on the article's own numbers, shown
  in full. A quote from the article, a post, or Apple is followed directly by its tag; words read
  from an image are quoted and the line carries `[image]`. "Ledger note" marks a connection this
  ledger draws between two sourced facts.
- **Tiers are Apple App Review tiers.** The article sorts tactics by what Apple's reviewers allow.
  It says nothing about Google Play.
- **Numbers are anecdotes.** Every revenue and conversion figure is the author's own result for his
  own apps, at different times. None is a benchmark (see `N-`).

## Tiers

- **Allowed (`A-`).** The article calls these allowed. Most still carry conditions.
- **Gray area (`G-`).** "These tactics are a gray area, meaning they are subjective and down to the reviewer." [article] Mention each one in your review notes and get explicit approval from Apple (`X-02`).
- **Banned (`B-`).** "These tactics are explicitly not allowed." [article] Shipping one risks
  rejection or worse; the author advises removing them today.

---

## Allowed tactics

### A-01 Dynamic paywalls

- **Claim:** Show a different paywall depending on ad attribution, or on the referring platform,
  for example one paywall for TikTok and another for Instagram. [article]
- **Condition:** "This is allowed, but it has to be done in the right way." [article] The article
  never says what the right way is (`V-04`).
- **Example:** Ad 1 leads to Paywall 1: $7.99/week, dark mode, male-focused. Ad 2 leads to
  Paywall 2: $7.99/week, light mode, female-focused. [image] The price is the same; only design and
  audience framing change (`V-05`).
- **Apple:** "promoting a false price, whether within or outside of the App Store, is grounds for removal of your app from the App Store" [apple § 2.3.1(a)]; "Before asking a customer to subscribe, you should clearly describe what the user will get for the price." [apple § 3.1.2(c)]

### A-02 Marketing notifications

- **Claim:** The big apps all do this; indie founders rarely do. The target is a user who downloads
  the app and goes through onboarding but does not finish it or does not convert; the notification
  brings them back into the app. [article]
- **How:** flow `F-01` (follow-up offer after closing the app) and its example `F-02`.
- **Conditions:** The user must opt in to marketing notifications, you disclose this when you ask
  for notification permission, and the app has an in-app way to opt out. [article] Frequency: `B-02`.
- **Apple:** "Push Notifications should not be used for promotions or direct marketing purposes unless customers have explicitly opted in to receive them via consent language displayed in your app’s UI, and you provide a method in your app for a user to opt out from receiving such messages." [apple § 4.5.4]

### A-03 Onboarding and paywall optimization

- **Claim:** Personalize everything to the user, using their name as an input so the app feels
  unique. Measure and A/B test everything to see where users drop off, and fix it. [article]
- **How:** the author's paywall structure, `P-01` to `P-05`.

### A-04 In-app support

- **Claim:** Fixing churn is one of the easiest ways to make more money. "You already did the hard part in converting the user." [article] Start with a good, high-quality, high-value app, then provide in-app support and solve users' problems before they churn. [article]
- **Vendor note:** The author built Reception, in-app support for iOS apps, for this use case, and
  the article discloses it as his own SaaS (`V-10`). Its launch post lists: turn chat into sales,
  ask happy customers for reviews, solve problems to lower churn, convince users during onboarding,
  and answer an abandoned checkout with a chat. [post 2112162857567625568]
- **Apple, when asking for reviews:** "Use the provided API to prompt users to review your app; this functionality allows customers to provide an App Store rating and review without the inconvenience of leaving your app, and we will disallow custom review prompts." [apple § 5.6.1] See `B-06`.

### A-05 Win-back campaigns

- **Claim:** A user who cancelled but kept the app can be won back with a tasteful discount. When
  the user cancels, your subscription provider sends a webhook, and you can trigger actions from
  it. [article]
- **How:** flow `F-03` and its example `F-04`; event names in `F-06`.
- **Ledger note:** A win-back offer sent as a push notification is a promotion, so `A-02`'s opt-in
  conditions and `B-02`'s frequency apply, and the discount must not be timed or random (`B-03`).

### A-06 Apple's retention API

- **Claim:** "There's good data suggesting the retention API can cut churn by 20%." [article] The
  article gives no source for the 20% (`N-07`). Apply for access: "it's a no brainer" [article].
  Many developers do not have access yet; without it, run a win-back flow like `A-05` that notifies
  users whose trial ended or whose subscription expired. [article]
- **Examples** (captioned "RevenueCat Examples"): Apple's Confirm Cancellation screen with a "From
  the developer" card: an image message ("Sad to see you go", "You will lose access to your premium
  features"); a switch-plan message ("Save by switching plans!", "Switch your plan now and save up
  to 50%", next to a $45.99 per year plan); and a promotional offer ("Get 30% off your next two
  months", "3 months free, then $4.99 per month"). [image] See `V-09`.
- **Apple:** "The Retention Messaging API is a server-to-server service that enables you to select which message the system displays to customers when they view a subscription details page and might cancel." [apple Retention Messaging API] Apple lists four message types (text, text with an image, a switch-plan message, and a promotional-offer message), shows them after the customer taps Cancel Subscription, and describes the API as a pre-release that developers express interest in. [apple Retention Messaging API]

### A-07 Price localization

- **Claim:** Scale the subscription price to each country, especially if your app grows
  organically and gets users from everywhere: a $10 subscription in the US becomes a $4
  subscription in the Philippines, for example. [article] The author used to skip this and says you
  should not.
- **How:** "Have your agent do this using the App Store Connect CLI." [article] The article does not
  say which tool it means (`V-06`).

### A-08 Web-to-app funnels

- **Claim:** Web-to-app is fairly new but growing fast; the big apps adopt it to avoid paying 30%
  fees, and indie developers can use it to test other acquisition and checkout flows. Completion
  rates are generally lower, but each user who converts brings in more money. [article]
- **How:** flow `F-05`.
- **Apple:** "Apps that operate across multiple platforms may allow users to access content, subscriptions, or features they have acquired in your app on other platforms or your web site, including consumable items in multi-platform games, provided those items are also available as in-app purchases within the app." [apple § 3.1.3(b)] "Developers can send communications outside of the app to their user base about purchasing methods other than in-app purchase." [apple § 3.1.3] "These entitlements are not required for developers to include buttons, external links, or other calls to action in their United States storefront apps." [apple § 3.1.1(a)]
- **Ledger note:** The article's funnel starts outside the app (ad to website), which § 3.1.3
  allows. A link from inside the app to web checkout depends on the storefront (§ 3.1.1(a)), and
  web-bought access must also be sold as an in-app purchase (§ 3.1.3(b), `V-03`).

---

## Gray-area tactics

Every tactic here needs the `X-02` step: describe it in Notes for Review and get explicit approval.

### G-01 Multiple paywalls during onboarding

- **Claim:** You can show more than one paywall during onboarding. It can get very annoying, so
  do it "extremely tastefully!" [article]
- **Example:** onboarding screens 1 to 3, then a paywall the user dismisses, then onboarding screens
  4 to 12, then a second, final paywall. [image]
- **Apple:** "All new features, functionality, and product changes must be described with specificity in the Notes for Review section of App Store Connect" [apple § 2.3.1(a)]; "Before asking a customer to subscribe, you should clearly describe what the user will get for the price." [apple § 3.1.2(c)]

### G-02 Abandoned-transaction offers

- **Claim:** "This is extremely effective but it is a gray area." [article] The trigger: the user
  starts a transaction, opens the purchase sheet (the article calls it the Apple Pay screen), and
  exits it. "It's likely that your entire app is taken into account when making the decision." [article]
- **To raise the chance of approval:** have a good, high-quality app; make the offer simple,
  tasteful, compliant, and clear; never mislead the user. [article]
- **What the author has seen allowed** [post 2104522475761455536]: not allowed are random discounts,
  fake one-time offers, fake "you won" offers, and timed offers. Very likely allowed are a clear and
  tasteful discount, a different plan such as a lifetime offer, and extending a trial. "The golden rule: never mislead the user." [post 2104522475761455536] If in doubt, add it through App Review, put it in the review notes, and get explicit approval.
- **Results:** `N-04`, `N-05`. Event: `checkout_cancelled` (`F-06`).
- **Apple:** "All new features, functionality, and product changes must be described with specificity in the Notes for Review section of App Store Connect" [apple § 2.3.1(a)]; "Apps should never prey on users or attempt to rip off customers, trick them into making unwanted purchases" [apple § 5.6]

### G-03 Close-paywall offers

- **Claim:** The same pattern as `G-02`, but triggered when the user dismisses the paywall instead
  of abandoning a payment. [article] Event: `paywall_declined` (`F-06`).
- **Apple:** "All new features, functionality, and product changes must be described with specificity in the Notes for Review section of App Store Connect" [apple § 2.3.1(a)]; see `G-02` for the § 5.6 rule.

---

## Banned tactics

Never suggest these, under any name. If an app already does one, the advice is to remove it.

### B-01 Live Activity marketing

- **Claim:** Live Activities must not be used for marketing: no discount offer and no timed offer
  there. [article] The image shows the intended use: an Uber pickup ("1 min until pickup"). [image]
- **Apple:** "Do not use Apple Services to spam, phish, or send unsolicited messages to customers, including Game Center, Push Notifications, Live Activities, etc." [apple § 4.5.3]

### B-02 Spam notifications

- **Claim:** Do not send users lots of notifications, even non-marketing ones, and especially
  marketing ones. Apple's wording is vague, but "as a general rule 1-2 marketing notis a week should be fine" [article]. The user must know about the notifications when you ask for permission, and
  opt in. [article]
- **Apple:** "Abuse of these services may result in revocation of your privileges." [apple § 4.5.4]
  The full rules are § 4.5.3 (`B-01`) and § 4.5.4 (`A-02`). Apple states no number (`V-07`).

### B-03 Limited-time and random offers

- **Claim:** Any offer that pressures the user into deciding, or misleads them (for example, a fake
  random offer), is not allowed. [article] Example: "SPECIAL OFFER ACT FAST" with a 00:44 countdown
  and a "Buy!" button. [image]
- **Apple:** "This includes apps that attempt to trick users into purchasing a subscription under false pretenses or engage in bait-and-switch and scam practices; these will be removed from the App Store and you may be removed from the Apple Developer Program." [apple § 3.1.2(a)] "Apps should never prey on users or attempt to rip off customers, trick them into making unwanted purchases, force them to share unnecessary data, raise prices in a tricky manner, charge for features or content that are not delivered, or engage in any other manipulative practices within or outside of the app." [apple § 5.6]

### B-04 Charging for capabilities Apple provides for free

- **Claim:** You may not charge for something the system already gives users, such as camera access
  or simply showing Screen Time. You may add functionality on top and charge for that, such as a
  camera app with custom cinematic looks. [article]
- **Apple:** "You may not monetize built-in capabilities provided by the hardware or operating system, such as Push Notifications, the camera, or the gyroscope; or Apple services and technologies, such as Apple Music access, iCloud storage, or Screen Time APIs." [apple § 4.10] See `V-02`.

### B-05 Fake testimonials and fabricated reviews

- **Claim:** Testimonials and social proof raise conversion, but they must be real; this is also a
  legal matter for websites, not just apps. A good way to get real ones is to ask people in your
  network to use the app and review it objectively, which also gives you real feedback. [article]
  The paywall structure uses social proof only if you have it (`P-01`).
- **Apple:** "Your representation of yourself, your business, and your offerings on the App Store or alternative distribution must be accurate." [apple § 5.6.2] "If we find that you have attempted to manipulate reviews, inflate your chart rankings with paid, incentivized, filtered, or fake feedback, or engage with third-party services to do so on your behalf, we will take steps to preserve the integrity of the App Store, which may include expelling you from the Apple Developer Program." [apple § 3]

### B-06 Rewarding users for positive App Store reviews

- **Claim:** Better reviews generally bring better conversion, but you may never submit fake ones,
  and rewarding reviews is a real risk. Never do it. [article]
- **Apple:** "Manipulating any element of the App Store customer experience such as charts, search, reviews, or referrals to your app erodes customer trust and is not permitted." [apple § 5.6.3] "Apps must not force users to rate the app, review the app, download other apps, or other store-related actions in order to access functionality, content, or use of the app." [apple § 3.2.2(x)] For asking at all, use Apple's review prompt (§ 5.6.1, `A-04`).

### B-07 Tracking users without ATT permission

- **Claim:** Tracking is how apps convert many more users through ad networks, but you must add
  App Tracking Transparency (ATT) to stay compliant. Tracking without ATT is not allowed. [article]
- **Apple:** "You must receive explicit permission from users via the App Tracking Transparency APIs to track their activity." [apple § 5.1.2(i)]

---

## Paywall structure

From an embedded post: "This paywall structure made me $10,578 in 62 days." [post 2085709435498807593]
The post reports about 10% average conversion and says the author studied more than 100 paywalls;
the example is the paywall of his app His 75. The image lists five sections.

- **P-01 Social proof.** Reviews and testimonials, only if you have them. [post 2085709435498807593]
- **P-02 Sell the vision, not the product.** Lifestyle advertising: people only care how it makes
  them feel. [post 2085709435498807593]
- **P-03 Explain the key product features.** Show the impact of the product. [post 2085709435498807593]
- **P-04 Pricing.** Research your niche. Default prices: $49.99/year, $14.99/month, $7.99/week.
  A/B test your paywalls. [post 2085709435498807593]
- **P-05 Motivational call to action.** Do not waste the button on "Continue". With a trial, use
  "Try 7 days Free". [post 2085709435498807593]
- **Example screen** [post 2085709435498807593]: a "join community" row of member photos; headline
  "Ascend in 75 days / Join the brotherhood"; benefits "Achieve goals", "Join community", "Stay
  accountable"; plans Yearly $79.99/year (preselected, badge "Save 88% vs Weekly"), Monthly
  $9.99/month, Weekly $3.99/week; button "Unlock your potential"; footer links Terms of Service,
  Restore, and Privacy Policy. The badge is wrong for these prices (`V-08`).
- **Apple, for the Restore link:** "you should make sure you have a restore mechanism for any restorable in-app purchases" [apple § 3.1.1]

---

## Flows and events

- **F-01 Marketing notification flow** [article]: the user closes the app; has not subscribed; wait
  5 minutes; send a follow-up offer; wait 4 days; still not subscribed; send a follow-up.
- **F-02 Exit offer example** [image]: trigger `app_close`; who: users without a subscription;
  wait: right away; action: an exit offer with the message "Hey, here's 50% off".
- **F-03 Win-back flow** [article]: `sub_cancel` or `trial_cancel`; wait 30 seconds; send a
  discount offer.
- **F-04 Win-back example** [image]: trigger `subscription_cancelled` ("A paid subscription was
  cancelled"), received from the subscription provider's webhook and sent to every registered
  device of the person; who: everyone; wait: right away; message: "We're sorry to see you leave. Here's a discounted offer if you'd like it." [image]
- **F-05 Web-to-app flow** [article]: the user sees an ad or a reel and taps the link; the link
  opens a website; the user pays with Stripe on the website and you store the email; the user is
  redirected to the app and logs in with the email.
- **F-06 Event names** [image]: user actions `paywall_declined`, `checkout_cancelled`,
  `onboarding_completed`, `app_open`, `app_close`; subscription states (from RevenueCat)
  `trial_started`, `trial_converted`, `trial_cancelled`, `trial_expired`, `subscription_started`,
  `subscription_renewed`, `subscription_cancelled`, `billing_issue`, `subscription_expired`,
  `refunded`.

---

## Numbers

One founder's results for his own apps, reported at different times. The article does not say
which app each number belongs to, or whether `N-01` and `N-05` describe the same app.

- **N-01** The author's own app reached $13,000/month [article], in 3 months according to the post
  that announced the article [article]. A revenue card shows $12,698 for the last 28 days. [image]
- **N-02** The founders the author works with make from $5k/month to $150k/month [article]; the
  announcing post says more than $50k/month [article].
- **N-03** The paywall structure: $10,578 in 62 days, about 10% average conversion, more than 100
  paywalls studied. [post 2085709435498807593]
- **N-04** Abandoned-transaction offers made the author $2,700 [post 2104522475761455536]. An
  example pairing from the post it quotes: a $49.99/year plan with a $24.99/year abandoned-checkout
  offer. [post 2104522475761455536]
- **N-05** A subscription dashboard in the same post [post 2104522475761455536]: 528 active
  subscriptions: yearly 210 (40%), new monthly 132 (25%), discounted yearly 110 (21%), new weekly
  74 (14%), monthly 1, weekly 1. Revenue: $28,555 over 122 days (May 30 to Sep 28), with $24,176
  estimated proceeds.
- **N-06** [derived] 210 + 132 + 110 + 74 + 1 + 1 = 528. Discounted yearly as a share of all yearly
  subscriptions: 110 / (210 + 110) = 34%. Estimated proceeds as a share of revenue:
  24,176 / 28,555 = 85%.
- **N-07** "cut churn by 20%" for Apple's retention API, with no source given. [article]
- **N-08** [derived] The price localization example ($10 in the US, $4 in the Philippines) is a 60%
  lower price: (10 - 4) / 10 = 60%.
- **N-09** Marketing notifications: 1 to 2 a week, the author's rule of thumb. [article]
- **N-10** Flow timings: 5 minutes, then 4 days (`F-01`); 30 seconds (`F-03`); right away (`F-02`,
  `F-04`).
- **N-11** Web-to-app is adopted to avoid 30% fees. [article]

---

## Rules for every tactic

- **X-01** Never mislead the user. The article calls it the golden rule. [post 2104522475761455536]
- **X-02** For a gray-area tactic, mention it in your review notes and get explicit approval from
  Apple. [article] "All new features, functionality, and product changes must be described with specificity in the Notes for Review section of App Store Connect (generic descriptions will be rejected) and accessible for review." [apple § 2.3.1(a)]
- **X-03** Marketing notifications need the user's opt-in, a disclosure when you ask for
  permission, and an in-app opt-out (`A-02`, `B-02`).
- **X-04** Start with a good, high-quality app. Reviewers likely judge a gray-area offer against the
  whole app. [article]
- **X-05** Measure and A/B test everything; find where users drop off and fix it. [article]
- **X-06** Fixing churn is one of the easiest ways to make more money (`A-04`). [article]
- **X-07** "All of these depend on implementation, transparency, applicable law and regional payment rules." [article]

---

## Verification notes

- **V-01** The title says 48 tactics; the body lists 18 (8 allowed, 3 gray, 7 banned). The gray
  section numbers its third item "2" again; this ledger numbers them `G-01` to `G-03`.
- **V-02** The article names Apple's sections "§§ 2.3, 2.5, 3.1, 3.2, 4.5 and 5.1" [article] as
  its source. The rule behind `B-04` is § 4.10, and the review rules behind `B-05` and `B-06` are in
  § 3 and § 5.6.
- **V-03** § 3.1.3(b) adds a condition to web-to-app (`A-08`) that the article does not state: access
  bought on the web may be used in the app only if it is also sold as an in-app purchase.
- **V-04** For dynamic paywalls (`A-01`) the article says "More on that later." [article] and never
  comes back to it.
- **V-05** In the `A-01` example both paywalls cost $7.99/week. The article gives no example of a
  different price for a different channel.
- **V-06** The article names an "App Store Connect CLI" (`A-07`) without a link; this ledger did not
  check which tool it means.
- **V-07** Apple states no number for notification frequency; 1 to 2 marketing notifications a week
  is the author's rule of thumb (`B-02`).
- **V-08** [derived] The example paywall's badge "Save 88% vs Weekly" does not match its own prices.
  Weekly $3.99 × 52 = $207.48 a year, and $79.99 / $207.48 = 38.6%, a 61% saving. The 88% matches
  the default prices in `P-04` instead: $7.99 × 52 = $415.48, and $49.99 / $415.48 = 12.0%, an 88%
  saving. Compute a savings badge from the prices on the same screen (§ 2.3.1(a), § 3.1.2(c)).
- **V-09** In the RevenueCat promotional-offer example (`A-06`), the headline "Get 30% off your next
  two months" and the offer line "3 months free, then $4.99 per month" describe different offers.
  It is a third-party example, not a template; a retention message's text must match its offer.
- **V-10** The flow screenshots come from Reception, the author's own product (the article
  discloses this), and the cancellation examples come from RevenueCat. No tactic depends on either
  tool.
- **V-11** Apple's wording here was fetched on 2026-10-28. Apple revises its guidelines, so check a
  section again before relying on it for a submission.
