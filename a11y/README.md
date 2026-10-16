# Accessibility Lawsuit-Risk Scan — `audit_a11y`

**See your site the way a plaintiff firm's scanner does.** One URL in → a
0–100 accessibility score, an A–F grade, a lawsuit-risk level
(Low / Moderate / Elevated / High), evidence for every check, and the top 3
fixes that cut the risk most.

Built for **someone else's agent with spend approval**: instead of a human
clicking through a free checker UI, the agent calls the tool, pays **$1.50**,
and hands its owner a finished risk report in seconds. No LLM call per
request — every check is deterministic HTTP/HTML/CSS analysis, so marginal
cost per call is ~$0.

## Why this exists

ADA website lawsuits are at an all-time record: a projected **6,176 federal
filings in 2026** (+20% YoY), **79% of them targeting e-commerce**, with
35k–50k demand letters sent in 2025 and typical case costs of
**$55k–$270k**. The EU's European Accessibility Act is now enforceable too
(fines up to €3M). Plaintiff firms don't browse sites by hand — they run
automated scanners that flag the same failure classes over and over:
missing alt text, unlabeled form fields, low contrast, keyboard barriers,
missing landmarks, empty links/buttons (present in ~96% of complaints).
This tool runs that class of scan *before* they do, as triage.

**Honest scope note:** this is an automated scan, **not a WCAG or ADA
compliance certification**. Automated tools catch roughly **30% of WCAG
issues** — keyboard flows, screen-reader behavior, focus order in real
browsers, and cognitive accessibility need manual testing. A high score
lowers lawsuit risk; it does not eliminate it. Every report carries this
disclaimer in the payload itself.

## What it checks (20 checks, 100 points)

| Area | Checks |
|---|---|
| Images & media (17) | alt attributes present, alt text quality (no file names/boilerplate), media captions/tracks |
| Forms (18) | programmatic labels, autocomplete input purpose (WCAG 1.3.5), required fields indicated in code — not just with a red asterisk |
| Structure (18) | `lang` attribute, heading order, landmarks, skip link |
| Links & interactive (20) | link text quality (no empty/generic links), button names, tap-target size, keyboard operability (positive tabindex, mouse-only clickables) |
| Contrast & visual (12) | contrast estimated from the page's own inline styles + CSS (WCAG 4.5:1), links not identified by color alone |
| Document basics (15) | page title, viewport zoom not disabled, visible focus indicator in CSS, ARIA sanity (no aria-hidden focus traps, valid roles) |

Checks that can't be evaluated (dead site, no forms on the page, no
evaluable color pairs) are **excluded from the scoring denominator** and
labeled `unknown` — the score never guesses. Contrast is estimated only
from explicit color/background pairs in markup and the page's own CSS;
the evidence string says exactly what was and wasn't evaluated.

## Interfaces

**MCP tools** (for agents):
- `audit_a11y(url)` — **paid, $1.50/call.** Full report: score, grade, risk
  level with the failing plaintiff-scan checks named, all 20 checks with
  evidence, top-3 recommendations, disclaimer.
- `audit_a11y_lite(url)` — **free.** Score, grade, risk level, pass/fail
  counts, top gap titles only. The marketplace funnel: try free, buy the
  full scan.

**REST:**
- `POST /a11y` — paid full scan (`{"url": "https://example.com"}`)
- `POST /a11y-lite` — free summary, rate-limited (20/IP/hour default)
- `GET /health`, `GET /` (tool index), `GET /.well-known/x402` (discovery)

## Local run

```bash
cd a11y
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install "x402[evm]==2.23.0"
cp .env.example .env        # set X402_WALLET to your Base wallet
X402_DISABLED=1 .venv/bin/python -m uvicorn server:app --port 8000
# try it:
curl -X POST localhost:8000/a11y-lite -H 'Content-Type: application/json' \
  -d '{"url":"https://books.toscrape.com"}'
```

`X402_DISABLED=1` runs the paywall off for local dev. **Never deploy with it set.**

## Deploy

Production deploys ride the portfolio Cloud Run script (after gcloud
re-auth):

```bash
PROJECT_ID=agent-api-509318 X402_WALLET=0xd702C8A8Ee30F6950A2a663010d12FC195D5348e ./deploy-cloudrun.sh
```

**Fly.io** (alternative, scale-to-zero): `fly launch` picks up fly.toml;
set the same env vars as secrets. **Railway:** `railway.json` sets the
Docker build and `/health` healthcheck.

**Verify the paywall is live before selling calls** — startup logs must contain:
```
x402 paywall ARMED: $1.50 on eip155:8453 -> 0xYourWallet
```
If you see `paywall NOT armed`, the service runs **unprotected** — fix the
facilitator config before listing it anywhere.

## Going live on mainnet (real money)

The default config uses **Base Sepolia testnet** (`eip155:84532`) with the
public `x402.org` facilitator — good for testing the 402 flow, earns
nothing. For real revenue:

1. Point `X402_FACILITATOR_URL` at a **mainnet-compatible facilitator**
   (production uses PayAI, `https://facilitator.payai.network`) and set
   `X402_NETWORK=eip155:8453` (Base mainnet).
2. Set `X402_WALLET` to a wallet you control (via deploy env — never in
   the repo).
3. Bazaar discovery is declared in code (402 `extensions.bazaar` on the
   HTTP route + MCP discovery extension), so the facilitator can catalog
   the service once a real payment settles.

**Breakeven:** 14 paid calls/month grosses $21 — roughly a $20/mo
subscription. After marketplace fees (~10–15%) and hosting (~$2–5/mo),
practical net breakeven is closer to **18–20 paid calls/month**. The free
lite tool is the funnel: it costs nothing per call and every lite response
tells the caller the full scan exists.

## Marketplace listing copy

**Free — `audit_a11y_lite` (MCPX / Swarms):**
> **Accessibility Risk Lite — free.** Would your site survive a plaintiff
> firm's accessibility scan? Paste any URL for an instant 0–100 score, A–F
> grade, and lawsuit-risk level. Free, no wallet needed. For the full
> 20-check evidence-backed report with the top 3 fixes, call `audit_a11y`
> ($1.50). Automated scan — not a compliance certification.

**Paid — `audit_a11y` (MCPX / Swarms):**
> **Accessibility Lawsuit-Risk Scan — $1.50/call.** 20 weighted checks
> across alt text, form labels, contrast, keyboard access, landmarks, and
> the other failure classes that appear in ~96% of ADA website complaints.
> Returns a 0–100 score, A–F grade, a Low→High lawsuit-risk level naming
> the failing plaintiff-scan checks, evidence for every check, and the 3
> highest-impact fixes. Deterministic — no LLM cost, results in seconds.
> One scan vs a $55k–$270k lawsuit. (Automated triage, not a certification.)

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```
Scoring math, grade boundaries, risk-level mapping, recommendation
ordering, deterministic per-check tests against synthetic HTML fixtures
(every pass/fail branch), live scans of real sites (books.toscrape.com,
example.com — no network mocks), dead-site degradation, lite/full
differentiation, REST surfaces, lite rate limiting, MCP tools/list +
tools/call, and an armed-paywall subprocess test that verifies a real
HTTP 402 challenge ($1.50 = 1500000 base units) while `/a11y-lite` stays
free. The paywall test skips cleanly if the x402 facilitator is
unreachable.

## Layout

```
a11y/
  server.py          # FastAPI + MCP + x402 ($1.50), REST /a11y + /a11y-lite
  a11y/engine.py     # 20-check a11y engine, scoring, risk, lite/full reports
  common/auditlib.py # vendored copy of ~/workspace/agent-api/common/auditlib.py
  tests/             # pytest suite
  Dockerfile / fly.toml / railway.json / .env.example / requirements.txt
```

`common/auditlib.py` is **vendored**: the canonical source is
`~/workspace/agent-api/common/auditlib.py`. To update the shared lib, edit
it there and re-copy into each service's `common/` before redeploying.
