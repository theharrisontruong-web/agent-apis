# Product Feed Hygiene Scan — `audit_feed`

**See your product feed the way Merchant Center's validator does.** One
feed URL in → a 0–100 hygiene score, an A–F grade, a disapproval-risk
level (Low / Moderate / Elevated / High), evidence for every check, the
worst-offending items with concrete fixes, and the top 3 fixes that
recover the most revenue.

Built for **someone else's agent with spend approval**: instead of a human
exporting diagnostics screenshots, the agent calls the tool, pays
**$1.00**, and hands its owner a finished feed report in seconds. No LLM
call per request — every check is deterministic parsing and rule
evaluation, so marginal cost per call is ~$0.

## Why this exists

A disapproved product feed means the products **do not serve on Google
Shopping at all** — and agent-commerce surfaces inherit the same dirt,
because UCP references Merchant Center product IDs directly. Feed
disapprovals (missing GTINs, malformed prices, bad availability values,
broken images, duplicate IDs) are a chronic, top-5 merchant complaint
with whole agencies built around fixing them, and feeds drift after
every catalog change — which is exactly why a per-call scan an agent can
run after each change beats a monthly SaaS dashboard. As the 2026 CTO
literature puts it: *your product feed is your new storefront.*

**Honest scope note:** this scan validates the **feed document itself** —
structure, required attributes, identifiers, price/availability values,
image-link liveness spot-checks. It does **not** crawl landing pages, so
feed-vs-page price/availability mismatches are out of scope, and it is
**not a Merchant Center approval guarantee**. Shipping/tax may live in
account-level settings instead of the feed; the check says so rather
than failing you for it. Every report carries this disclaimer in the
payload itself.

## What it checks (20 checks, 100 points)

| Area | Checks |
|---|---|
| Parsing (12) | document parses in its detected format (Google RSS/Atom XML, CSV, or JSON) and yields items; encoding sanity (mojibake, control characters, double-encoded entities) |
| Required fields (30) | id, title, link, price, availability (present **and** a value in Google's accepted set), image_link |
| Identifiers (14) | GTIN/MPN coverage (GTIN check digits verified), brand coverage, identifier_exists not contradicting the identifiers actually submitted |
| Content quality (18) | title quality (no stuffing, no ALL-CAPS, sane length), description presence/substance, image links (https, well-formed, HTTP-200 spot-checks) |
| Pricing (12) | price format (parseable amount, single currency, positive), sale_price strictly below price |
| Taxonomy & shipping (8) | google_product_category / product_type coverage; shipping/tax presence (with the account-level caveat stated) |
| Integrity (6) | duplicate ids (later rows silently overwrite earlier ones), variant clusters grouped with item_group_id |

Checks that can't be evaluated (unreachable feed, no sale prices in the
catalog, no variant clusters) are **excluded from the scoring
denominator** and labeled `unknown` — the score never guesses.

**Scan limits, stated in every report:** the feed document is capped at
8 MB with a 20s fetch inside a 75s overall deadline; the first 20,000
items are analyzed; image links are spot-checked (8 unique URLs, HEAD
with a headers-only GET fallback); landing pages are not crawled.

## Interfaces

**MCP tools** (for agents):
- `audit_feed(url)` — **paid, $1.00/call.** Full report: score, grade,
  disapproval-risk level with the failing disapproval-class checks
  named, all 20 checks with evidence, worst-offending items sampled
  with concrete fixes, top-3 recommendations, disclaimer.
- `audit_feed_lite(url)` — **free.** Score, grade, risk level, detected
  format, item count, pass/fail counts, top gap titles only — item-level
  detail is capped. The marketplace funnel: try free, buy the full scan.

**REST:**
- `POST /feed` — paid full scan (`{"url": "https://store.com/feed.xml"}`)
- `POST /feed-lite` — free summary, rate-limited (20/IP/hour default)
- `GET /health`, `GET /` (tool index), `GET /.well-known/x402` (discovery)

## Local run

```bash
cd feed
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install "x402[evm]==2.23.0"
cp .env.example .env        # set X402_WALLET to your Base wallet
X402_DISABLED=1 .venv/bin/python -m uvicorn server:app --port 8000
# try it:
curl -X POST localhost:8000/feed-lite -H 'Content-Type: application/json' \
  -d '{"url":"https://your-store.com/feed.xml"}'
```

`X402_DISABLED=1` runs the paywall off for local dev. **Never deploy with it set.**

## Deploy

Production deploys ride the portfolio Cloud Run script (after gcloud
re-auth) — feed is one of the services it builds:

```bash
PROJECT_ID=agent-api-509318 X402_WALLET=0xd702C8A8Ee30F6950A2a663010d12FC195D5348e ./deploy-cloudrun.sh
```

**Fly.io** (alternative, scale-to-zero): `fly launch` picks up fly.toml;
set the same env vars as secrets. **Railway:** `railway.json` sets the
Docker build and `/health` healthcheck.

**Verify the paywall is live before selling calls** — startup logs must contain:
```
x402 paywall ARMED: $1.00 on eip155:8453 -> 0xYourWallet
```
If you see `paywall NOT armed`, the service runs **unprotected** — fix the
facilitator config before listing it anywhere.

## Going live on mainnet (real money)

Set `X402_NETWORK=eip155:8453` and
`X402_FACILITATOR_URL=https://facilitator.payai.network` (PayAI settles
real USDC on Base mainnet, no API key needed). The default config targets
Base Sepolia testnet via the x402.org facilitator.
