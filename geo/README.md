# GEO Readiness Audit — `audit_geo`

**Get found by AI search.** One URL in → a 0–100 GEO (generative-engine-optimization)
score, an A–F grade, evidence for every check, and the top 3 fixes that move the
score most.

Built for **someone else's agent with spend approval**: instead of reading a
checklist, the agent calls the tool, pays **$1.50**, and hands its owner a
finished audit in seconds. No LLM call per request — every check is deterministic
HTTP/HTML analysis, so marginal cost per call is ~$0.

## Why this exists

AI search (ChatGPT, Perplexity, Gemini, Copilot) is where product discovery is
moving. Fiverr's Holiday Marketing Index (Sept 2026) found **67% of freelance
search specialists seeing rising retailer demand for GEO services** — businesses
know they need to be cited by AI answers and don't know if they are. This tool
answers that question with receipts.

**Honest scope note:** the checks measure *machine-readability* — the things AI
crawlers and answer engines demonstrably need (crawl access, structured data,
extractable text, identity signals). `llms.txt` is checked because it's the
emerging convention, but **there is no proven evidence that publishing llms.txt
lifts citations** — it's weighted accordingly (3/100) and the report says so.

## What it checks (20 checks, 100 points)

| Area | Checks |
|---|---|
| Schema depth (30) | Organization, Product, Review, FAQPage, aggregateRating |
| Business identity (13) | sameAs links, NAP consistency, contact info |
| AI crawler policy (11) | robots.txt treatment of 12 major AI bots, llms.txt |
| Extractability (20) | clean text, heading structure, no JS wall, meta description |
| Freshness (12) | sitemap presence, sitemap lastmod, content dates |
| Authority (14) | about page, credentials/author signals, canonical + HTTPS |

Checks that can't be evaluated (dead site, fetch blocked) are **excluded from the
scoring denominator** and labeled `unknown` — the score never guesses. A dead
site scores 0/F with everything unknown and zero recommendations.

## Interfaces

**MCP tools** (for agents):
- `audit_geo(url)` — **paid, $1.50/call.** Full report: score, grade, all 20
  checks with evidence, top-3 recommendations.
- `audit_geo_lite(url)` — **free.** Score, grade, pass/fail/unknown counts, top
  gap titles only. The marketplace funnel: try free, buy the full audit.

**REST:**
- `POST /geo` — paid full audit (`{"url": "https://example.com"}`)
- `POST /geo-lite` — free summary, rate-limited (20/IP/hour default)
- `GET /health`, `GET /` (tool index)

## Local run

```bash
cd geo
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install "x402[evm]==2.23.0"
cp .env.example .env        # set X402_WALLET to your Base wallet
X402_DISABLED=1 .venv/bin/python -m uvicorn server:app --port 8000
# try it:
curl -X POST localhost:8000/geo-lite -H 'Content-Type: application/json' \
  -d '{"url":"https://www.ikea.com"}'
```

`X402_DISABLED=1` runs the paywall off for local dev. **Never deploy with it set.**

## Deploy

**Fly.io** (recommended — scale-to-zero, ~$2–5/mo shared across tools):
```bash
fly launch            # picks up fly.toml (Miami region, suspends when idle)
fly secrets set X402_WALLET=0xYourWallet X402_NETWORK=eip155:8453
fly deploy
```

**Railway:** connect the repo; `railway.json` sets the Docker build and the
`/health` healthcheck. Set the same env vars in the Railway dashboard.

**Verify the paywall is live before selling calls** — startup logs must contain:
```
x402 paywall ARMED: $1.50 on eip155:8453 -> 0xYourWallet
```
If you see `paywall NOT armed`, the service runs **unprotected** — fix the
facilitator config before listing it anywhere.

## Going live on mainnet (real money)

The default config uses **Base Sepolia testnet** (`eip155:84532`) with the public
`x402.org` facilitator — good for testing the 402 flow, earns nothing. For real
revenue:

1. Point `X402_FACILITATOR_URL` at a **mainnet-compatible facilitator** and set
   `X402_NETWORK=eip155:8453` (Base mainnet).
2. Set `X402_WALLET` to a wallet you control (via `fly secrets set` / Railway
   env — never in the repo).
3. Make one live $1.50 purchase yourself and confirm the USDC lands in the
   wallet before listing publicly.

**Breakeven:** 14 paid calls/month grosses $21 — roughly a $20/mo subscription.
After marketplace fees (~10–15%) and hosting (~$2–5/mo), practical net breakeven
is closer to **18–20 paid calls/month**. The free lite tool is the funnel: it
costs nothing per call and every lite response tells the caller the full audit
exists.

## Marketplace listing copy

**Free — `audit_geo_lite` (MCPX / Swarms):**
> **GEO Readiness Lite — free.** Is your site readable by AI search? Paste any
> URL for an instant 0–100 visibility score, A–F grade, and your top gaps.
> Free, no wallet needed. For the full 20-check evidence-backed audit with the
> top 3 fixes, call `audit_geo` ($1.50).

**Paid — `audit_geo` (MCPX / Swarms):**
> **GEO Readiness Audit — $1.50/call.** 20 weighted checks across schema depth,
> business identity, AI-crawler policy (12 bots), extractability, freshness, and
> authority. Returns a 0–100 score, A–F grade, evidence for every check, and the
> 3 highest-impact fixes. Deterministic — no LLM cost, results in seconds.
> For AI search visibility (ChatGPT, Perplexity, Gemini): know exactly why AI
> answers don't cite you, and what to fix first.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```
23 tests: scoring math, grade boundaries, recommendation ordering, live audits of
real sites (ikea.com, mintlify.com, books.toscrape.com — no network mocks),
dead-site degradation, lite/full differentiation, REST surfaces, lite rate
limiting, MCP tools/list + tools/call, and an armed-paywall subprocess test that
verifies a real HTTP 402 challenge ($1.50 = 1500000 base units) while `/geo-lite`
stays free. The paywall test skips cleanly if the x402 facilitator is
unreachable.

## Layout

```
geo/
  server.py          # FastAPI + MCP + x402 ($1.50), REST /geo + /geo-lite
  geo/engine.py      # 20-check GEO engine, scoring, lite/full reports
  common/auditlib.py # vendored copy of ~/workspace/agent-api/common/auditlib.py
  tests/             # pytest suite
  Dockerfile / fly.toml / railway.json / .env.example / requirements.txt
```

`common/auditlib.py` is **vendored**: the canonical source is
`~/workspace/agent-api/common/auditlib.py`. To update the shared lib, edit it
there and re-copy into each service's `common/` before redeploying.
