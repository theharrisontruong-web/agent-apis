# AI Storefront Readiness Grader — paid agent API

An agent-callable API that audits any ecommerce storefront for **AI-agent shopping readiness** and returns a machine-readable report: **0–100 score, A–F grade, 20 weighted checks with evidence, and the top 3 prioritized fixes.**

The customer is *someone else's agent* with spending approval: instead of its owner manually auditing a store for 30 minutes, the agent calls `grade_storefront`, pays **$1.00 USDC**, and gets the audit instantly.

## The money math (breakeven)

| Item | Value |
|---|---|
| Price per grade call | **$1.00** (env `X402_PRICE`) |
| Cost target | $20/month Muse subscription |
| Breakeven | **20 paid calls / month** |
| Fly.io hosting | ~$0–5/month (suspends to zero when idle) |
| Margin at 100 calls/mo | ~$95/month before marketplace fees |

At $1.00/call, twenty paid calls a month covers the subscription. Everything above that is profit. The price is one env var — raise it when demand proves out.

**Default network is Base Sepolia (testnet).** The default facilitator (`x402.org`) only supports testnets, so out-of-the-box the API charges *test* USDC — perfect for listing on marketplaces and letting buyers try it free of real-money risk. See **Going live on mainnet** below for the real-money switch.

## How it works

Two agent-facing surfaces, one x402 paywall:

| Surface | Call | Unpaid behavior |
|---|---|---|
| **MCP** (recommended for agents) | `POST /mcp` — streamable HTTP, tool `grade_storefront(url: str)` | tools/call returns a payment-required result carrying the x402 challenge in `_meta`; the agent pays and retries with the credential in `_meta` |
| **REST** | `POST /grade` with `{"url": "https://store.com"}` | HTTP **402** with the x402 v2 challenge in the `PAYMENT-REQUIRED` header |

`GET /health` is free (status + paywall state). `GET /` describes the service and pricing.

### The 20 checks

| Category (weight) | What it looks for |
|---|---|
| Product intelligence (30) | schema.org Product JSON-LD, Offer price, availability, variants, GTIN/MPN |
| AI discovery (25) | robots.txt, llms.txt, agents.md, sitemap.xml, UCP manifest, MCP endpoint |
| Conversation readiness (15) | contact info, FAQ/returns/shipping pages, site search |
| Transaction readiness (20) | HTTPS, checkout/cart signals, tax/shipping *values* |
| Trust (10) | business identity, privacy/terms pages |

Failed or unevaluable checks are `"unknown"` — never guessed. Unknown checks are excluded from the score denominator (score rescales over what could actually be evaluated). Unreachable sites return score 0/F with all checks unknown. Every fetch has an 8s timeout and the whole grade is capped at ~75s.

## Run it locally

```bash
cd ~/workspace/agent-api/grader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install "x402[evm]==2.23.0"
cp .env.example .env        # then set X402_WALLET to your Base wallet
X402_DISABLED=1 uvicorn server:app --port 8000   # free local dev, paywall off
```

```bash
# grade a store (dev mode)
curl -X POST localhost:8000/grade -H 'Content-Type: application/json' \
  -d '{"url":"https://www.adafruit.com"}'
```

## Configuration (env vars)

| Var | Default | Meaning |
|---|---|---|
| `X402_WALLET` | `0x00…00` | Wallet receiving USDC. **Required for paid mode.** |
| `X402_PRICE` | `$1.00` | Price per call, e.g. `"$1.00"`, `"$0.50"` |
| `X402_NETWORK` | `eip155:84532` | Base Sepolia. Mainnet: `eip155:8453` (needs mainnet facilitator) |
| `X402_FACILITATOR_URL` | `https://x402.org/facilitator` | x402 facilitator for verify/settle |
| `X402_DISABLED` | `0` | Set `1` for local dev: paywall fully off |
| `PORT` | `8000` | Listen port |

## Deploy

**Fly.io** (one command, after `fly auth login`):

```bash
cd ~/workspace/agent-api/grader
fly launch   # first time — accepts the fly.toml in this dir
fly secrets set X402_WALLET=0xYourWalletAddress
fly deploy   # updates
```

**Railway**: new project → deploy from repo (uses `railway.json` + `Dockerfile`), set the env vars in the dashboard, healthcheck path is `/health`.

**Docker**:

```bash
docker build -t storefront-grader .
docker run -p 8000:8000 -e X402_WALLET=0xYourWallet -e X402_PRICE='$1.00' storefront-grader
```

⚠️ On first boot with payments enabled, the log **must** show:
`x402 paywall ARMED: $1.00 on eip155:84532 -> 0xYourWallet`.
If you see `!!! x402 paywall NOT armed` instead, the facilitator was unreachable and the service is running **unprotected** — do not sell calls until that line appears.

## x402 calling flow (for integrators)

1. Agent calls `POST /grade` (or MCP `tools/call`) **without** payment.
2. Gets `402` (or payment-required result) with the challenge: scheme `exact`, network `eip155:84532`, amount `1000000` ($1.00 USDC, 6 decimals), `payTo` = your wallet, asset = USDC contract.
3. Agent signs an EIP-3009 `transferWithAuthorization` for 1.00 USDC to your wallet and retries with the payment credential (`X-PAYMENT` / `PAYMENT-SIGNATURE` header, or `_meta` for MCP).
4. Facilitator verifies → your handler runs → facilitator settles → agent gets the report. USDC lands in `X402_WALLET`.

## Going live on mainnet (real money)

1. Pick a mainnet x402 facilitator that supports `exact` on `eip155:8453` (the default `x402.org` facilitator is testnet-only — its `/supported` lists `eip155:84532`, not `eip155:8453`).
2. Set `X402_FACILITATOR_URL` to it and `X402_NETWORK=eip155:8453`.
3. Fund `X402_WALLET` (it only receives; no private key ever touches this service).
4. Redeploy, confirm `x402 paywall ARMED … on eip155:8453` in logs, and make one real $1.00 test purchase before listing.

## Marketplace listing copy (MCPX / Swarms)

**Title:** AI Storefront Readiness Grader — $1 per audit

**Short:** Point an agent at any ecommerce URL; get back a 0–100 AI-shopping-readiness score, A–F grade, 20 evidence-backed checks, and the top 3 fixes. Pays for itself the first time it saves a human a 30-minute manual audit.

**Long:** Ecommerce is about to be bought by agents, not humans — OpenAI/Stripe (ACP) and Google/Shopify (UCP) are already wiring checkout into AI. Most stores are invisible to those agents: no product schema, no llms.txt, no machine-readable checkout. This tool grades any storefront server-side across product intelligence, AI discovery files, conversation readiness, transaction readiness, and trust — 20 weighted checks, honest `unknown` instead of guesses, ~60s per grade. Your agent calls it with the owner's pre-approved $1.00 USDC budget and hands the owner a prioritized fix list instead of a vague "looks fine."

**Pricing:** $1.00 USDC per call via x402 (Base). No API key, no subscription.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

14 tests: scoring math (boundaries, partial credit, unknown-rescaling, recommendations), 3 live real-storefront checks (no mocks), HTTP + MCP surface tests.

## Project layout

```
grader/
  grader/engine.py      # 20-check grading engine, pure + live-fetch
  grader/__init__.py
  server.py             # FastAPI + MCP server + x402 paywall wiring
  tests/test_engine.py
  requirements.txt      # pinned
  Dockerfile  fly.toml  railway.json  .env.example
  README.md  WRITELOG.md
```
