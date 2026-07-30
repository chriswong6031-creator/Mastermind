# Mastermind asymmetric growth and distribution strategy

**Canonical deliverable:** this file

**Status:** private strategy and build docket; not a public report

**Research date:** 2026-07-15 (live-product observations checked 2026-07-16 UTC)

**Decision horizon:** first 180 days of subscription launch

**Scope:** bootstrap acquisition, product-led distribution, X, creators/affiliates, and the gates for later paid acquisition

---

## Executive decision

Mastermind should not market itself as another financial terminal, an AI stock picker, or a giant collection of dashboards. The defensible category is:

> **The accountable AI investment desk — it shows what changed, why it matters, what the system did, and exactly what would prove it wrong.**

That positioning fits the product that exists. Mastermind is paper-only, read-only for the public, probabilistic, benchmarked, and unusually rich in decision records, rejection reasons, research papers, traces, and falsifiers. It does **not** yet have a mature paid entitlement, checkout, trial, conversion, or subscriber-retention system. Its public track record is also too young to sell skill or alpha honestly.

The growth sequence should therefore be:

1. **Make one recurring paid job unmistakable.** Sell a daily decision workflow, not access to a maze of features.
2. **Turn the product's accountability exhaust into acquisition.** Every meaningful view becomes a timestamped, source-linked, live research object that can also export as a beautiful X image.
3. **Own one contagious public wedge.** Recommended wedge: **"What changed since yesterday — and what would invalidate the read?"**
4. **Build a transparent Mastermind Research Network, not a farm of apparently independent accounts.** Start with one flagship account and one genuinely distinct research desk, both openly owned by MastermindX and human-reviewed.
5. **Recruit creators with shared economics, not sponsorship fees.** Founding offer: 40% of net collected revenue for the subscriber's first 12 paid months, with no lifetime tail and a retention-quality bonus.
6. **Make referrals and creator partnerships separate programs.** Members earn product credit; professional creators earn cash and receive co-branded research lanes.
7. **Do not scale ads until retained contribution economics exist.** A tiny message-validation test can happen earlier; meaningful scaling requires two 90-day cohorts, channel-level sample size, and conservative LTV/payback gates.

The unconventional advantage is not covert distribution. It is **radical, machine-verifiable honesty in a category saturated with manufactured certainty**.

---

## 1. The product truth: the bottleneck is not traffic yet

### What Mastermind already is

The repo's own operating contract is unusually marketable:

- It is a paper-only, medium/long-term equity reasoning system. The LLM reasons; deterministic engines own sizing; nothing auto-executes (`CLAUDE.md`, lines 3–7; `README.md`, lines 3–16).
- Its doctrine is “confirmation over prediction,” probabilities over certainties, observed versus inferred evidence, and a falsifier for every view (`DOCTRINE.md`, lines 20–35).
- It maintains benchmarked paper books, decisions, rejected names, research papers, run traces, calibration/outcome ledgers, and cross-book risk views.
- It already spans US, China, and Hong Kong books and supports English and Chinese—a credible niche rather than a cosmetic translation layer.
- The public Macro site supplies a huge live intelligence surface; the Terminal supplies a usable stock workflow; the Bot supplies the accountable paper desk.

A live snapshot during this study exposed seven paper books, 206 research papers, 854 run traces, seven shadow policies, 63 active but unresolved outcome labels, and 7,869 logged universe predictions. Those counts prove system depth and operating cadence. They do **not** prove investment skill.

### What Mastermind is not yet

- There is no finished Stripe/checkout/billing/entitlement/referral implementation in the Mastermind repo.
- The current account object defaults to `plan: free`; no paid plan behavior was found (`app/account.py`, lines 170–200).
- Public GETs are deliberately open, while public operator/LLM actions are blocked (`app/auth.py`, lines 11–84).
- Macro has first-party page, click, search, dwell, and session analytics, but no complete subscription funnel or source-to-renewal attribution.
- Discovery is fragmented across `mastermind-x.com`, `app.mastermind-x.com`, and `bot.mastermind-x.com`.
- The landing page advertises breadth—countries, asset classes, reports, tools—but does not give one visitor one reason to subscribe now.
- Public product language is inconsistent. “Paper-only / accountability, not alpha” coexists with aggressive report copy about what to own and when to buy. Paid promotion would amplify that mismatch.

### The governing equation

Subscription growth is:

`qualified traffic × activation × paid conversion × retention × contribution margin`

If paid conversion is not implemented and activation is undefined, multiplying traffic mostly creates leakage. An account farm, creator army, or ad budget would make that leakage larger and make the data harder to interpret.

**First conclusion:** the next growth build is a conversion spine and a shareability spine, not more undifferentiated reach.

---

## 2. What a finance subscriber actually buys

Raw data is abundant. Charts are abundant. AI summaries are becoming free. A durable subscriber pays for five things:

1. **Resolution:** fewer decisions and less time spent deciding what matters.
2. **Trust:** provenance, visible limits, corrections, and evidence that losing calls cannot disappear.
3. **Ritual:** a reliable daily/weekly habit that answers the same high-value question.
4. **Personal relevance:** the answer changes with the user's holdings, watchlist, horizon, and existing exposure.
5. **Continuity:** alerts and a decision history that remember what the user and system believed before the outcome.

Mastermind's strongest raw material is trust plus continuity. It should not put a conventional paywall around data that looks similar to ten other products. It should charge for the loop:

> **Observe → interpret → relate to my book → alert only when the state changes → preserve the decision and falsifier → grade the outcome.**

This also identifies the initial customer.

### Primary ICP

**Serious self-directed position investor**

- Holds for weeks or months, not minutes.
- Already consumes macro, fundamentals, and financial X.
- Is overwhelmed by information, not starved for it.
- Values portfolio context, thesis invalidation, risk discipline, and a journal.
- Will pay for time saved and behavioral discipline—not a promise of magical foresight.

### High-potential niche ICP

**Bilingual US–China–Hong Kong investor**

Mastermind already has unusual cross-market depth and bilingual output. This audience is narrower, easier to recognize, and less directly served than generic US technical traders.

### Creator as channel, not first customer

Newsletter writers, podcasters, X analysts, Discord operators, and small finance communities need evidence, charts, and fresh angles. They can become powerful distributors once Mastermind is their content-production engine. They should not define the core product before end-user retention exists.

### Avoid initially

- Day traders seeking real-time calls.
- Beginners seeking a simple “buy this now” service.
- Institutions requiring SLAs, compliance integration, and data entitlements.
- Anyone buying solely because Mastermind claims proven alpha.

---

## 3. The category and offer

### Category

**Accountable AI investment desk**

Not “AI research platform.” Not “Bloomberg for retail.” Not “signals.” The category should make the accountability mechanism the noun.

### One-line promise

> **Know what changed, why it matters to your holdings, and what would prove the view wrong.**

### Proof sentence

> Every view is timestamped, sourced, benchmarked, and kept after the outcome—including the misses.

### What not to claim

- “Beats the market.”
- “Self-improving alpha.”
- “Institutional-grade” unless the exact feature and entitlement justify it.
- “Real-time” where data are delayed or end-of-day.
- Win rates or performance cherry-picked from unresolved or tiny cohorts.
- “Autonomous investing” without the equally prominent paper-only boundary.

### Recommended launch offer

Launch with **Free + one paid Founding Desk**, not a four-column pricing page.

| Layer | User job | Suggested contents |
|---|---|---|
| Free public | Understand today's market weather and verify Mastermind's honesty | Current regime, limited “what changed” tape, public outcome/receipt ledger, delayed shareable research objects, one watchlist, weekly digest |
| Founding Desk | Understand what changed for *my* holdings and preserve the decision loop | Portfolio/watchlist X-ray, state-change alerts, full history, saved/forked packets, private notes, exports, limited Brain questions, full research, weekly office hour |

Recommended price hypothesis:

- **$29/month or $249/year** for the first 100–200 members.
- Founding price remains locked only while the subscription stays continuously active.
- Raise the public price toward $39/month only after activation and 90-day retention prove value.
- No lifetime deal. It destroys the recurring-revenue asset precisely when support and data costs are least known.

This is a testable hypothesis, not a valuation of the finished product. The $29 starting point sits between Fiscal.ai's entry tier and Koyfin's current Plus tier while acknowledging that Mastermind's track record and onboarding are immature.

### Trial design

Do not begin with an auto-renewing credit-card trap. Use an **activation-triggered 14-day paid preview with no card**:

1. User creates a watchlist or imports a small portfolio.
2. Mastermind produces a useful first X-ray immediately.
3. The paid preview begins only when personalized value exists.
4. At expiry the account falls back to Free; the personalized history remains visible but locked.

The conversion prompt should arrive after three value moments, not after an arbitrary page count:

- first portfolio risk or concentration discovery;
- first meaningful “what changed” alert;
- first saved decision/falsifier update.

---

## 4. What the comparable winners actually do

The strongest products use a common architecture: free utility produces an artifact; the artifact is shareable or indexable; paid access unlocks freshness, depth, personalization, collaboration, or execution; creator economics amplify distribution.

| Product | Observable loop | What Mastermind should take |
|---|---|---|
| Koyfin | Usable Free tier; 7-day full trial that falls back to Free; $20 member credit plus friend discount; negotiated affiliate revenue share; public screeners/graphs that recipients can copy | Separate member credit from creator cash; make live objects useful before signup; “save a copy” is the conversion action |
| Fiscal.ai / FinChat | Free-to-paid ladder; exportable charts; co-branded partner pages; 25% of first-year referred revenue plus audience discount and product access for creators | Make Mastermind the creator's research-production engine; use first-year revenue share, not upfront sponsorship |
| TradingView | Free charts; shareable snapshots and live layouts; public ideas distributed to ticker followers; public ideas cannot be quietly hidden after the fact; widgets distribute the product across the web; member and cash partner programs are separate | Build immutable decision receipts, chart-as-URL, embeds, public profiles, and product-native sharing |
| Seeking Alpha | Searchable stock-specific content supplied by outside analysts; subscriber consumption pays contributors; authors sell niche communities/services; affiliates monetize distribution | Later, pay for coverage gaps and paid-member consumption; shape content supply instead of paying for impressions |
| Unusual Whales | A culturally contagious public wedge—congressional trading—built audience before the broader paid tool; delayed public data and Discord deepen the loop | Own one memorable public category; monetize freshness, alerts, history, and synthesis rather than the headline fact |
| TIKR | Search-intent articles that open a relevant free workflow; branded chart exports designed for X/newsletters; selected creator program | Every SEO article should end inside a preconfigured tool, not at a generic signup page |
| Composer | Strategy creation/backtesting/sharing is free; paid value appears at the highest-intent recurring workflow | Let users inspect and share decision packets free; charge for monitoring, alerts, personalization, and history |

Selected current evidence:

- [Koyfin pricing](https://www.koyfin.com/pricing/), [member referral](https://www.koyfin.com/help/referral/), and [affiliate program](https://www.koyfin.com/affiliate-program/)
- [Fiscal.ai pricing](https://fiscal.ai/pricing/) and [affiliate program](https://fiscal.ai/affiliate/)
- [TradingView partner rules](https://www.tradingview.com/partner-rules/), [idea publishing](https://www.tradingview.com/support/solutions/43000591338-publishing-and-updating-ideas/), [snapshot sharing](https://www.tradingview.com/support/solutions/43000482537-how-to-share-a-snapshot/), and [widgets](https://www.tradingview.com/widget-docs/getting-started/)
- [Seeking Alpha's platform model](https://about.seekingalpha.com/) and [Investing Groups](https://help.seekingalpha.com/contributors/what-are-investing-groups)
- [Unusual Whales origin and product model](https://docs.unusualwhales.com/features/1-welcome/)
- [TIKR creator program](https://support.tikr.com/hc/en-us/articles/38745234495899-Does-TIKR-have-an-Affiliate-program)
- [Composer pricing and free sharing boundary](https://www.composer.trade/pricing)

The strategic lesson is not “copy their pricing.” It is: **the output of the product must perform the marketing.**

---

## 5. The asymmetric growth loops

### Priority 1 — The Receipt Engine

Create an append-only public ledger for every eligible published view:

- view and probability;
- timestamp and data as-of;
- observed evidence versus inference;
- check-by date;
- exact falsifier;
- benchmark/comparison set;
- subsequent updates;
- outcome score when resolved;
- correction history.

The original cannot be deleted or silently edited. A public hash/manifest can make this machine-verifiable; the existing git/static build system is already suited to it.

Each receipt has:

- a canonical live URL;
- 1:1 and 16:9 image exports;
- a plain-language headline;
- one chart or state change;
- the falsifier and as-of stamp;
- “open live receipt” CTA;
- no referral code inside the evidence attribution itself.

Why this is asymmetric:

- The product generates the raw material every day at near-zero marginal editorial cost.
- Publishing misses raises trust while competitors optimize for screenshots of wins.
- Every update creates a new distribution event without inventing a new thesis.
- It compounds into a defensible track record later.

### Priority 2 — “What changed?” as the ownable public wedge

Do not promote 80 dashboards. Promote one daily object:

> **Five things that changed since yesterday, ranked by portfolio relevance—and the condition that would reverse each read.**

Free users see the market-wide tape. Paid users see the same logic mapped to their holdings, sectors, factor concentrations, and saved theses.

This turns Mastermind's current home-page “What changed” section into a category, not a small panel.

### Priority 3 — Free Portfolio X-ray

Let a visitor enter 5–20 tickers without brokerage credentials. Return:

- hidden theme/factor overlap;
- macro regime exposure;
- concentration and correlated-risk cluster;
- which holding has a live thesis change;
- one missing hedge/cash-capacity observation stated as information, not personalized advice;
- one example falsifier.

The result is useful without payment and becomes a private saved object after signup. Email capture is justified by delivering an updated packet, not by withholding the initial answer.

High-touch bootstrap version: personally send the first 25–50 users a five-minute screen recording explaining their packet. Ask one question: “What would have made this useful enough to check every week?” This is research, onboarding, retention work, and word-of-mouth at once.

### Priority 4 — Event-triggered public windows

Open one high-value view for 24–48 hours around a real market event:

- CPI/FOMC;
- a regime change;
- a major cross-asset dislocation;
- a large earnings cluster;
- a China/HK policy shock.

Publish the pre-event probabilities before the event and the post-event mark afterward. Real events supply urgency; no fake countdown is needed.

### Priority 5 — Chart-as-URL and “save/fork”

Every screenshot should point to a live, dated object. A recipient can:

- inspect sources and definitions without an account;
- change the ticker or timeframe;
- save/fork the packet after signup;
- follow it for future state changes on a paid plan.

A static screenshot is an impression. A forkable object is an acquisition loop.

### Priority 6 — Embeddable intelligence cards

Offer free, lightweight, responsive widgets for newsletters, blogs, and communities:

- Market Regime;
- Sector Pulse;
- What Changed;
- Ticker Risk/Thesis Status;
- Outcome Receipt.

The widget includes source, as-of date, and canonical link. Affiliate participation is optional and disclosed; evidence provenance must never be converted into a disguised referral link.

### Priority 7 — Falsification bounty

Reward the best documented model/data error each month. Publish the issue, correction, impact, and finder credit.

One small expense produces QA, credibility, technical-community attention, and a public demonstration that corrections are a feature rather than a scandal.

### Priority 8 — Contributor coverage-gap bounties

Only after the core paid product retains users, invite credible analysts to fill specific gaps:

- undercovered ticker or sector;
- post-earnings autopsy;
- bilingual cross-market translation;
- opposing thesis/red-team packet.

Compensation should depend partly on paid-member saves, repeat use, or retention—not raw impressions. This borrows Seeking Alpha's supply-shaping mechanism without becoming a generic content mill.

### Campaign concepts cheap enough to earn their own audience

These are guerrilla campaigns in the useful sense: surprising, low-cash, product-native, and difficult for a better-funded competitor to copy without adopting Mastermind's accountability doctrine.

| Campaign | Mechanism | Why people spread it | First kill gate |
|---|---|---|---|
| **The 100 Portfolio Blind Spots Project** | Give 100 volunteers a free X-ray, aggregate only consented/anonymized exposures, and publish a report on the hidden clusters retail investors actually own | Each participant gets a personal share card; the aggregate report can earn citations, newsletters, and community discussion | Stop if fewer than 25% complete the X-ray or privacy-safe aggregation cannot be guaranteed |
| **Falsifier Friday** | Every Friday publish one popular thesis, its strongest evidence, and the single observable condition that would break it | It teaches a reusable discipline and invites thoughtful disagreement without rage bait | Stop or reformat if it drives debate but no receipt follows/saves |
| **The Uncomfortable Receipt** | Lead the weekly recap with the most consequential miss or confidence reduction, then show the correction | Counter-positioned against finance victory laps; trust grows through evidence rather than claims | Kill if the underlying cohort selection is not mechanically complete |
| **Two Theses Enter** | Pair two disclosed creators with opposing views; Mastermind provides the neutral probability tree, evidence packet, and dated resolution | Both audiences have a reason to participate; neither creator must surrender identity to a scripted endorsement | Stop if creators turn it into stock promotion or the resolution rule is ambiguous |
| **The Precommitment League** | Let a small set of analysts publish immutable probabilistic receipts under public profiles; rank calibration and correction quality, not raw returns | Analysts gain a credibility asset; their followers repeatedly return to the resolution page | Launch only after receipt integrity and fair comparison sets are tested |
| **Open Shock Room** | When a real regime shock fires, open the relevant desk temporarily and update a public event timeline | Urgency is real, the utility is immediate, and the event naturally creates repeat visits | Stop if windows attract spectators but do not produce activated users |
| **Find the Flaw** | Monthly bounty for the best reproducible data/model/UX error, with public fix and finder credit | Technical communities receive status and proof that critique is welcomed | Cap the bounty and require reproducible impact; reject vague opinion contests |
| **One Desk, One Community** | Power a niche newsletter/Discord's weekly market segment with a dedicated embed and audience-requested packet instead of paying a sponsorship fee | The partner receives recurring programming; Mastermind becomes infrastructure rather than an ad | Kill after four weeks if the embed produces no qualified object opens or activations |

The 100 Portfolio project is the best first campaign after the X-ray works. It simultaneously generates user research, activation, shareable personal output, aggregate proprietary insight, press material, and a founding-member pipeline. No fabricated virality is required.

---

## 6. X: replace the farm with a transparent research network

### Ruling on the proposed farm

Do **not** build apparently independent “farmed and raised” financial personas that are commonly controlled and occasionally recommend Mastermind as though they discovered it independently.

The problem is structural, not moralistic:

- [X's current authenticity policy](https://help.x.com/en/rules-and-policies/authenticity) bars non-genuine or non-transparent accounts, manufactured personas used deceptively, and coordinated inauthentic amplification.
- [X's automation rules](https://help.x.com/en/rules-and-policies/x-automation) prohibit duplicative automated accounts and materially similar cross-account posting; AI-powered automated reply bots require prior written approval.
- One enforcement event can erase the whole audience portfolio.
- Disclosure destroys the illusion of independent endorsement; nondisclosure creates platform, advertising, and reputation exposure.
- LLM errors become correlated across the whole network.
- The channel data become dishonest: “earned independent discovery” is actually owned media.
- Discovery of common control would damage the precise trust moat Mastermind needs.

The useful part of the idea is **portfolio distribution**: multiple content franchises, each compounding an audience around a narrow financial job. Keep that part and remove the deception.

### Launch structure

Start with two accounts:

1. **MastermindX flagship** — product, major market context, weekly accountability report, releases.
2. **Mastermind Receipts / Mastermind Macro Lab** — one genuinely distinct beat: timestamped views, falsifiers, score updates, and corrections.

Only add a third desk when it has a distinct editorial mandate, audience, and responsible human editor—perhaps US–China–HK transmission.

Every bio and linked masthead should disclose:

> Official MastermindX research desk. AI-assisted, editor-reviewed. General market information; not personalized advice.

Pseudonymous human editors can be legitimate. Invented “customers,” AI-generated headshots, or accounts pretending to be independent analysts are not.

### Content architecture

Early mix: about eight standalone-useful posts for every direct product CTA.

| Atom | Example | Product connection |
|---|---|---|
| Change card | “Credit weakened while the regime label stayed benign” | Open the live What Changed receipt |
| Falsifier card | “Our semis read survives unless breadth falls below X by date Y” | Follow the receipt |
| Rejection card | “Why the desk rejected a popular name” | Inspect the full decision packet |
| Wrong card | “We were wrong: what failed and what changed in the model” | Open correction history |
| Portfolio lesson | “Five AI names can be one risk bet” | Run a free X-ray |
| Event map | Pre-FOMC probability tree, then the mark | Open the temporary event window |
| Build note | New measurement or data provenance | View public changelog/method |
| Product CTA | Founding Desk invitation | Begin the activated preview |

The screenshot must say it comes from **our** Mastermind dashboard. It must remain useful even if the viewer never clicks.

### LLM operating system

Use the LLM as an editorial copilot, not an autonomous social actor:

1. Deterministic engine emits a signed content packet containing allowed facts, sources, timestamps, and claim class.
2. LLM proposes desk-specific copy and alternative headlines.
3. Automated lint rejects numbers absent from the packet, certainty language, missing as-of dates, duplicate phrasing, and prohibited claims.
4. Human editor checks the source, implication, disclosure, and tone.
5. Official API schedules the original post.
6. A content ledger stores packet, draft, sources, editor, final text, post ID, and corrections.
7. Replies remain human-selected and human-posted unless X gives explicit approval for a reply bot.

Operational controls:

- one accountable human per account;
- hardware-backed 2FA and centralized secret storage;
- no browser scripting;
- no automated follows/unfollows, likes, repost rings, DMs, or keyword replies;
- cross-account content-hash check;
- daily posting ceilings and kill switch;
- correction SLA for financial errors;
- no deletion of substantive losing calls; append a correction.

### Human Reply Studio

Software can identify high-quality unanswered questions from real investors. A human then writes a complete native answer and links only when the Mastermind object directly resolves the question.

This is high-leverage demand capture without bulk replies or engagement manipulation. Measure qualified profile visits, saved objects, email captures, and activated previews—not follower count.

---

## 7. Creator and shared-incentive program

### Keep two programs separate

#### A. Member referral

- New member receives seven extra preview days or a fixed first-invoice account credit.
- Referrer receives a fixed account credit.
- Reward becomes usable only after the referred subscriber's second successful invoice.
- No cash, no self-referrals, and an annual reward cap.

This follows the defensible Koyfin/TradingView pattern: subscription value rather than a cash bounty reduces fraud and improves retention.

#### B. Creator Partner

Founding offer:

- **40% of net collected subscription revenue for the first 12 paid months.**
- **No lifetime commission.**
- Optional **+5 percentage points** after day 120 if the creator cohort retains at or above the direct cohort and refunds/chargebacks are no worse.
- 10–15% audience discount, tested against a higher-commission/no-discount cell.
- 60-day click attribution.
- Unique link plus optional code; an intentionally entered code overrides the cookie.
- Monthly payout after a 45-day aging period.
- Free full product access before the first promotion.
- No follower minimum; choose for audience fit, trust, analytical history, and willingness to use the product.

“Net collected revenue” must be contractually defined as settled customer cash excluding taxes, discounts, credits, refunds, chargebacks, fraudulent transactions, and processor fees.

Why 40%:

- Fiscal.ai publicly offers 25% of first-year revenue; TradingView's current rules describe 30% recurring economics. A founding 40% is meaningfully attention-getting.
- It avoids upfront cash CAC.
- The tail ends after year one, preserving year-two economics and company value.
- A permanent 50% share would surrender too much before retention and cost-to-serve are known.

### Constant-budget offer test

Give each partner a fixed first-year acquisition budget they can allocate:

- Cell A: 10% audience discount + 30% creator share.
- Cell B: 0% audience discount + 40% creator share.

The company gives up roughly the same headline percentage while learning whether the audience responds to subscriber savings or creator enthusiasm.

### Illustrative unit economics

Assumptions only:

- list price: $29/month;
- 15% creator code: $24.65 settled before payment fees and tax;
- payment cost: 3% for this simplified example;
- other variable data/compute/support cost: 17% of settled revenue;
- creator share: 40% of the eligible net revenue after payment fees.

Then:

- payment cost ≈ $0.74 and eligible commission base ≈ $23.91;
- other variable service cost ≈ $4.19;
- creator payout ≈ $9.56;
- company contribution before fixed costs ≈ $10.16/month, or about 41% of settled revenue;
- a fully retained 12-month subscriber produces ≈ $122 of company contribution before fixed costs.

The real rate ceiling is:

`maximum creator rate = pre-commission gross margin - minimum company contribution margin`

If Mastermind requires at least 30 contribution-margin points after creator pay and pre-commission gross margin is 68%, the maximum creator rate is 38%, not 40%. Measure the cost base before signing terms.

### Make the relationship a product collaboration

Each founding creator gets:

- a co-branded landing page;
- a creator-owned public watchlist or research lane;
- preconfigured Mastermind views for their beat;
- 1:1 and 16:9 exports;
- one monthly audience-requested research packet;
- a visible payout/attribution dashboard;
- approved disclosure copy and claim library;
- retention and refund quality reporting, not only clicks.

Mastermind reduces the creator's content cost. The creator supplies distribution and product feedback. That is more durable than a coupon mention.

### Recruit micro-creators first

Recruit 5–10 creators with roughly niche, engaged audiences rather than one expensive celebrity. Suggested beats:

- macro and rates;
- swing/position investing;
- portfolio construction;
- AI infrastructure;
- China/HK bilingual markets;
- risk management and behavioral discipline.

Ask each candidate to use Mastermind for two weeks and produce one honest workflow example before approval. Reject candidates whose audience expects guaranteed picks, copied alerts, or high-pressure trading calls.

### Attribution and fraud controls

Attach these fields to the user and every invoice:

- `creator_id`;
- `content_id`;
- landing-page/offer version;
- first qualified creator touch;
- code override, if any;
- referral timestamp;
- invoice, refund, chargeback, and payout status.

Prohibit:

- self-referrals or controlled payment instruments;
- cookie stuffing, forced redirects, coupon-site leakage, or toolbar injection;
- paid followers/views or incentivized clicks;
- unapproved sub-affiliates;
- bidding on Mastermind trademarks;
- bulk DMs, irrelevant replies, engagement pods, or account transfers;
- false scarcity, fake testimonials, return promises, or unapproved performance claims.

Commissions remain pending until the aging window closes. Refunds reverse only the commission tied to that invoice; future payouts can offset a negative balance. Manual review begins when refund, chargeback, duplicate-account, or failed-payment rates materially exceed the direct cohort.

---

## 8. Disclosure and financial-promotion rails

This is a growth requirement, not decorative legal copy. Trust and channel durability depend on it.

Every compensated social post should visibly begin with language like:

> **#ad — I earn a commission if you subscribe through this link. I use MastermindX and this is my honest experience.**

Also use X's Paid Partnership label. The [FTC influencer guidance](https://www.ftc.gov/business-guidance/resources/disclosures-101-social-media-influencers) says material relationships should be hard to miss and disclosed with the endorsement, not buried behind “more,” an affiliate link, or a hashtag pile. Canada's [Competition Bureau guidance](https://competition-bureau.canada.ca/en/deceptive-marketing-practices/types-deceptive-marketing-practices/influencer-marketing-and-competition-act) similarly treats an affiliate link or discount code alone as insufficient.

### Claim library

**Allowed without special review**

- accurate product features and price;
- genuine personal workflow;
- dated screenshots with source and as-of;
- fair, feature-specific comparisons;
- paper-only and informational boundaries.

**Requires review**

- accuracy and model-performance claims;
- historical returns and backtests;
- testimonials about money saved or returns improved;
- superiority claims;
- resolved outcome summaries.

**Prohibited**

- guaranteed returns, “risk free,” “beats the market,” or “can't lose”;
- personalized recommendations on Mastermind's behalf;
- cherry-picked winners without the full comparable cohort;
- fabricated urgency or scarcity;
- claims based on unresolved outcomes or tiny effective samples.

Before launch, counsel should classify Mastermind's actual activities in the US and Canada. A disclaimer does not determine regulatory status. If the company becomes or must register as an investment adviser, the [SEC Marketing Rule](https://www.sec.gov/resources-small-businesses/small-business-compliance-guides/investment-adviser-marketing) adds promoter disclosure, oversight, agreement, bad-actor, fair-balance, and recordkeeping obligations. FINRA's [M1 Finance influencer enforcement](https://www.finra.org/media-center/newsreleases/2024/finra-fines-m1-finance-850000-violations-regarding-use-social-media) is a useful warning about unsupervised, unbalanced influencer claims.

Pay creators for Mastermind subscriptions only—never for trades, deposits, assets, or transactions in a particular security.

---

## 9. Paid acquisition: when and how to scale

### Paid ads are an amplifier, not a discovery engine

The ad creative should come from proven organic artifacts. If a “we were wrong” receipt, portfolio-overlap card, or event map consistently produces activated users organically, pay to distribute that object. Do not ask an ad agency to invent a different brand promise.

### X-specific constraint

X treats financial products, investment advice, analysis, and wealth-related promotions as restricted content. Its [financial-services advertising policy](https://business.x.com/en/help/ads-policies/ads-content-policies/financial-services) requires pre-authorization and has jurisdiction-specific licensing restrictions. Obtain written classification/approval before spending; do not try to evade classification by calling a financial product “education.”

### Scale gates

Treat these as internal risk limits, not universal SaaS laws:

- at least two paid cohorts observed through day 90;
- at least 50 paid conversions in a channel/offer before treating CAC as stable;
- activation definition fixed before the campaign;
- refund and chargeback rates no worse than direct/referral cohorts;
- conservative contribution LTV:CAC ≥ 4:1 during bootstrap;
- contribution payback ≤ 3 months during bootstrap;
- mature scale may relax to LTV:CAC ≥ 3:1 and payback ≤ 6 months;
- holdout or matched-geo incrementality, not platform attribution alone;
- no unresolved material policy or regulatory classification.

Scale budgets about 20–30% per week only while CAC and early retention remain within the gate. Pause when seven-day CAC breaches the ceiling or the paid cohort activates/retains materially worse than the organic cohort.

### Correct economics

`CM(t) = collected revenue - refunds - payment fees - variable data/compute/support - creator payout`

`12-month contribution LTV = Σ [probability active in month t × CM(t)]`

`paid CAC = incremental media + creative + acquisition incentive, divided by incremental new paid subscribers`

`allowable CAC = min(25% of conservative 12-month contribution LTV, expected contribution in months 1–3)`

Creator revenue share belongs in contribution margin. Do not also count it as CAC.

Never use `monthly revenue ÷ immature monthly churn` for a new product. Tiny early churn estimates create fantasy lifetime values. Use the observed survival curve and cap the horizon at 12 months until cohorts mature.

“ROAS” should mean incremental gross contribution generated by the campaign divided by spend—not top-line subscription revenue divided by the platform's claimed conversions.

### First paid test

Do not start with broad interest targeting. Test the best organic message against a high-intent audience:

- retarget visitors who opened a receipt or completed an X-ray but did not activate;
- creator lookalike/whitelisted content only where platform rules and disclosures allow;
- search intent around a narrow job such as portfolio overlap, market regime change, or thesis tracking;
- one landing page per promise.

Use a small factorial test:

- promise A: “what changed for your holdings”;
- promise B: “a decision record that cannot hide its misses”;
- format 1: receipt card;
- format 2: portfolio X-ray demonstration.

Budget each cell only enough to buy a predeclared number of landing-page activations. Kill weak messages quickly; do not optimize to cheap clicks.

---

## 10. Measurement and experiment operating system

Macro already collects pageviews, clicks, searches, dwell, and sessions. Extend rather than replace it, while minimizing personal data and separating product analytics from ad-platform claims.

### Required event spine

Every event needs anonymous/user ID, source, campaign, creator, content object, offer version, timestamp, and experiment cell where applicable.

```text
landing_view
receipt_opened
share_generated
share_opened
widget_opened
xray_started
xray_completed
account_created
watchlist_saved
trial_started
activation_value_1
activation_value_2
activation_value_3
checkout_started
subscription_started
invoice_paid
subscription_renewed
subscription_cancelled
refund_issued
chargeback
creator_attributed
referral_reward_earned
```

The three activation events should correspond to the actual value moments defined earlier, not generic logins.

### Cohort dashboard

For every source/creator/offer:

- visitor → X-ray/receipt engagement;
- engagement → account;
- account → activated preview;
- activation completion;
- preview → paid;
- day-7, day-30, day-60, day-90 retention;
- first and second invoice success;
- refunds/chargebacks;
- support and variable compute cost;
- share generation and downstream opens;
- contribution LTV and payback.

### Experiment card

Pre-register every meaningful test:

- hypothesis;
- audience;
- single primary metric;
- guardrails;
- minimum sample or time window;
- budget cap;
- decision rule: ship, iterate, or kill;
- owner;
- result and cohort-quality follow-up.

Followers, impressions, CTR, and platform ROAS are diagnostics—not success metrics. The north star is:

> **Retained, activated paid members whose contribution margin repays acquisition.**

---

## 11. Ninety-day operating sequence

### Days 1–14 — make marketing truth possible

- Choose the one-line category and paid job.
- Resolve the public-domain/app/bot journey and one canonical subscribe route.
- Implement paid entitlement, checkout, cancellation, refund, and invoice webhooks.
- Define Free versus Founding Desk access.
- Add the complete event/UTM/creator attribution spine.
- Create a claims library, creator agreement, correction policy, and jurisdiction review.
- Build the first append-only receipt object.
- Establish the flagship X account and one transparent research desk; publish the ownership masthead.

**Exit gate:** a new user can arrive from a specific content object, activate, pay, use the promised workflow, cancel, and be attributed correctly.

### Days 15–30 — create the proof and activation loops

- Launch What Changed as the public daily wedge.
- Launch receipt image export plus canonical live URL.
- Launch the 5–20 ticker Portfolio X-ray.
- Recruit 25 high-touch design partners; deliver manual screen-recorded onboarding.
- Open one real event window.
- Begin the weekly “What we got wrong / what changed” post.

**Exit gate:** at least one repeated organic path produces activated previews without founder explanation.

### Days 31–60 — first paid cohort and creators

- Invite the first 30–50 Founding Desk subscribers.
- Recruit 5–10 niche creators; require two weeks of genuine product use.
- Launch co-branded pages and the two constant-budget offer cells.
- Launch member product-credit referrals.
- Hold one weekly live office hour focused on using the current regime, not hot picks.
- Compare direct, referral, and creator activation/support quality.

**Exit gate:** first-invoice conversion works; early activation is repeatable; no creator cohort shows abnormal fraud/refunds.

### Days 61–90 — retain, prune, and prepare—not scale

- Interview cancellations and low-engagement paid users.
- Prune low-retention creator lanes even if clicks are high.
- Add the best creator-requested share or personalization feature.
- Launch one embed/widget pilot with a newsletter or community.
- Publish the first full transparency report.
- Run at most a tiny retargeting/message-validation ad test if policy clearance, attribution, and activation are all sound.

**Exit gate:** one cohort has meaningful day-30 evidence. This is not enough for scalable paid acquisition.

### Days 91–180 — earn the right to scale

- Observe two cohorts through day 90.
- Calculate 12-month contribution LTV from survival, not fantasy churn.
- Confirm which creator and organic messages produce retained members.
- Run controlled paid cells with holdouts.
- Scale only after the economics gates in section 9 pass.

---

## 12. Build docket in priority order

| Priority | Build | Why it precedes marketing | Acceptance test |
|---|---|---|---|
| P0 | Entitlement + checkout + cancel/refund + invoice ledger | No paid conversion or LTV exists without it | End-to-end test user can subscribe, receive access, renew, cancel, refund, and lose access correctly |
| P0 | Funnel + UTM/creator/content attribution | Every channel decision depends on cohort truth | One test conversion traces from content object to invoice and refund |
| P0 | Canonical product/subscribe journey | Three domains currently fragment trust and activation | One canonical CTA and account session work across all surfaces |
| P1 | Receipt Engine | Creates the core trust and distribution object | Original view is immutable; updates/outcome append; image and live URL share correctly |
| P1 | What Changed personalized digest | Defines the recurring paid job | Paid user sees ranked portfolio-specific changes and can follow a falsifier |
| P1 | Portfolio X-ray | Provides immediate personalized activation | Visitor receives useful answer before signup and can save after account creation |
| P1 | Share/fork/export | Converts use into distribution | Non-user can inspect; new user can fork; attribution persists |
| P2 | Creator Partner ledger/dashboard | Enables shared-incentive distribution without spreadsheet ambiguity | Partner sees qualified clicks, attributed users, invoices, reversals, retention, and pending/paid commission |
| P2 | Claims lint + post provenance | Prevents correlated LLM/creator errors | Unsupported number/certainty/disclosure omission blocks publication |
| P3 | Embeddable intelligence cards | Creates distributed product-led backlinks | Third-party site embeds a fast, dated, canonical-linked card |
| P3 | Coverage-gap contributor system | Expands supply only after retention | Bounty links to paid-member use and quality, not impressions |

---

## 13. Red-team: how this plan fails

### Failure: marketing the whole dashboard

Visitors cannot buy “everything.” They buy a specific recurring relief. Keep the enormous site as proof depth; market one job.

### Failure: hiding all value behind signup

No one trusts a young financial product they cannot inspect. The first useful answer must be public. Gate persistence, monitoring, personalization, freshness, and depth.

### Failure: account-farm short-term wins

Even if some accounts grow, common-control discovery or one platform action can erase the portfolio and poison the brand. The transparent desk network compounds slower but creates a real asset.

### Failure: turning creators into coupon spammers

Co-branded research lanes and genuine use are mandatory. Kill partners who generate trials but not activation/retention.

### Failure: cherry-picked proof

The Receipt Engine must publish the eligible cohort, misses, and corrections. If marketing can choose which outcomes disappear, the moat is fake.

### Failure: premature ad scale

Cheap clicks can conceal a weak product. If organic and creator users do not retain, fix the job and onboarding rather than changing targeting.

### Failure: perpetual affiliate liabilities

Large lifetime revenue shares lower company value and pay creators after they stop contributing. Keep the generous rate time-bounded and renew only through a new agreement tied to continuing value.

### Failure: unbounded AI/support costs

Instrument LLM, data, and support cost by subscriber and channel. Limit Brain questions in the launch plan; reserve higher-cost research for explicit tiers later.

### Failure: confusing activity with skill

Hundreds of papers, traces, and predictions are impressive operating evidence, not proof of alpha. The public outcome set was still unresolved during this study. Sell auditability and decision quality until the sample earns stronger claims.

### Failure: data/privacy overreach

Collect only what is required for product and attribution decisions; document retention and deletion. Do not turn first-party analytics into a shadow profiling product.

---

## 14. The decision scoreboard

At day 90, answer these questions in order:

1. Can a stranger describe the paid job in one sentence?
2. Which public object produces the most completed X-rays or activated previews?
3. Do users reach all three value moments without founder intervention?
4. What behavior separates retained from cancelled members?
5. Which creator cohorts retain as well as direct users?
6. Does the Founding Desk produce positive contribution after compute, data, support, and creator share?
7. Are the strongest acquisition messages honest representations of the retained use case?

- If conversion is weak but activation and retention are strong, fix price and the purchase moment.
- If activation is weak, fix onboarding and the first answer.
- If retention is weak, the paid job is not recurring enough; stop acquisition scale.
- If creator cohorts are weak while direct cohorts are strong, change partner selection/offer—not the product.
- If all retained economics pass, paid distribution becomes an amplifier with a measurable ceiling.

---

## Final recommendation

The highest-value bootstrap strategy is a **proof network**, not an account network:

- Mastermind produces timestamped decisions and corrections.
- Product users turn those objects into shares and forks.
- transparent desks interpret different beats;
- creators co-own useful research lanes and share first-year economics;
- member referrals trade in product value;
- event windows and embeds take the product to existing audiences;
- paid ads later amplify the artifacts that already produce retained members.

Finance audiences have learned to distrust certainty, backtest screenshots, and unexplained black boxes. Mastermind's deepest product doctrine—confirmation, falsifiability, calibration, and visible mistakes—can become the marketing doctrine too.

That is genuinely unconventional, difficult to counterfeit, and aligned with the product's long-term quality rather than dependent on an evasion tactic that can disappear overnight.

---

## Source appendix

### Mastermind and live product

- `CLAUDE.md`, `README.md`, `DOCTRINE.md`
- `app/auth.py`, `app/account.py`, `app/static/index.html`, `app/static/chat.js`, `app/web.py`
- `portfolio/registry.py`, `config/brain.yml`
- Macro Dashboard `templates/theme.js`, `app/main.py`, `scripts/build_vector.py`
- [Mastermind public hub](https://mastermind-x.com/)
- [Mastermind Terminal](https://app.mastermind-x.com/terminal)
- [Mastermind research library](https://mastermind-x.com/reports.html)

### Distribution and monetization comparisons

- [Koyfin pricing](https://www.koyfin.com/pricing/)
- [Koyfin member referral](https://www.koyfin.com/help/referral/)
- [Koyfin affiliate program](https://www.koyfin.com/affiliate-program/)
- [Fiscal.ai pricing](https://fiscal.ai/pricing/)
- [Fiscal.ai affiliate program](https://fiscal.ai/affiliate/)
- [TradingView Partner Program](https://www.tradingview.com/partner-program/)
- [TradingView Partner Rules](https://www.tradingview.com/partner-rules/)
- [TradingView public ideas](https://www.tradingview.com/support/solutions/43000591338-publishing-and-updating-ideas/)
- [TradingView snapshot sharing](https://www.tradingview.com/support/solutions/43000482537-how-to-share-a-snapshot/)
- [TradingView view-only chart sharing](https://www.tradingview.com/support/solutions/43000606515-how-to-share-charts-in-view-only-mode/)
- [TradingView widgets](https://www.tradingview.com/widget-docs/getting-started/)
- [Seeking Alpha platform model](https://about.seekingalpha.com/)
- [Seeking Alpha Investing Groups](https://help.seekingalpha.com/contributors/what-are-investing-groups)
- [Unusual Whales origin and product](https://docs.unusualwhales.com/features/1-welcome/)
- [TIKR affiliate program](https://support.tikr.com/hc/en-us/articles/38745234495899-Does-TIKR-have-an-Affiliate-program)
- [Composer pricing](https://www.composer.trade/pricing)

### Platform and promotion rules

- [X Authenticity](https://help.x.com/en/rules-and-policies/authenticity)
- [X Automation Rules](https://help.x.com/en/rules-and-policies/x-automation)
- [X Financial Services Ads Policy](https://business.x.com/en/help/ads-policies/ads-content-policies/financial-services)
- [FTC Disclosures 101 for Social Media Influencers](https://www.ftc.gov/business-guidance/resources/disclosures-101-social-media-influencers)
- [FTC Reviews and Testimonials Rule Q&A](https://www.ftc.gov/business-guidance/resources/consumer-reviews-testimonials-rule-questions-answers)
- [Competition Bureau Canada influencer marketing guidance](https://competition-bureau.canada.ca/en/deceptive-marketing-practices/types-deceptive-marketing-practices/influencer-marketing-and-competition-act)
- [SEC Investment Adviser Marketing guide](https://www.sec.gov/resources-small-businesses/small-business-compliance-guides/investment-adviser-marketing)
- [FINRA M1 Finance influencer enforcement](https://www.finra.org/media-center/newsreleases/2024/finra-fines-m1-finance-850000-violations-regarding-use-social-media)
