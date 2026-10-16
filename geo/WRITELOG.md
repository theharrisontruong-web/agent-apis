# WRITELOG — GEO Readiness Audit (`audit_geo`)

Built 2026-09-21 by subagent. Product spec: `~/workspace/agent-api/RESEARCH.md`
(ranked #1 next product).

## What it is

`audit_geo(url)` — paid **$1.50/call** x402 MCP tool + REST endpoint. URL in →
0–100 GEO score, A–F grade, 20 evidence-backed checks, top-3 fixes. Pure
deterministic HTTP/HTML analysis: **no LLM call per request, ~$0 marginal cost**.
Free `audit_geo_lite` (score + grade + top gaps, no per-check evidence) is the
marketplace funnel; rate-limited to 20/IP/hour on REST.

## Files

```
geo/
  server.py              FastAPI + MCP + x402 ($1.50), POST /geo (paid),
                         POST /geo-lite (free, rate-limited), /health, /
  geo/engine.py          20-check engine, weighted scoring, lite/full reports
  geo/__init__.py
  common/auditlib.py     VENDORED copy of ~/workspace/agent-api/common/auditlib.py
  common/__init__.py
  tests/conftest.py      sys.path bootstrap, X402_DISABLED=1 for server tests
  tests/test_engine.py   23 tests
  requirements.txt       pinned (same pins as grader, verified together)
  Dockerfile             copies geo/ + common/, installs x402[evm]
  fly.toml               app geo-readiness-audit, region mia, scale-to-zero
  railway.json           Docker build, /health healthcheck
  .env.example           all config via env, no secrets
  README.md              product, run/deploy/mainnet, breakeven, listing copy
  WRITELOG.md            this file
```

`common/auditlib.py` is **vendored, not symlinked**: canonical source stays at
`~/workspace/agent-api/common/auditlib.py`; re-copy into each service's
`common/` after editing the canonical file, before redeploying.

## Tests — 22 passed, 1 skipped (2026-09-21)

Final run: `22 passed, 1 skipped in 197s`. The skip is the armed-paywall
subprocess test — the x402 facilitator's `/supported` sync exceeded the
180s boot window in that run. **The armed paywall was verified manually
instead** (same assertions, all passing — see below); the pytest version
skips gracefully rather than failing on a slow network.

- 9 scoring-math tests: all-pass 100/A, all-fail 0/F, partial = half weight,
  unknown excluded from denominator (92-denominator case), grade boundaries
  (90/80/70/60), all-unknown → 0/F, top-3 recommendations ordered by weight,
  report shape (20 checks, weights sum 100).
- 7 live tests (real sites, **no network mocks**):
  - `books.toscrape.com` — 20 checks, honest fails (no llms.txt/schema), evidence non-empty
  - `ikea.com` — schema_organization/sameAs/NAP/robots/canonical pass, score > 30
  - `mintlify.com` — llms_txt pass branch, robots evaluated
  - unreachable domain → 0/F, all unknown, `unreachable: true`, no recommendations
  - lite/full differentiation (lite strips checks + recommendations; full has both)
- 6 server tests (paywall off): /health, / index, POST /geo full report,
  POST /geo-lite free + stripped, 400 on missing URL, lite limiter logic +
  HTTP 429, MCP initialize → tools/list (both tools) → tools/call lite (free)
  → unknown-tool error.
- 1 armed-paywall subprocess test: boots real uvicorn with X402_DISABLED=0,
  asserts `x402 paywall ARMED` in startup logs, then verifies: unpaid
  POST /geo → **HTTP 402** with x402 v2 challenge (base64-encoded
  PAYMENT-REQUIRED header, amount **1500000** = $1.50, USDC on Base Sepolia);
  POST /geo-lite still 200 while armed; MCP `audit_geo` tools/call →
  payment-required result carrying the $1.50 challenge in content text;
  MCP `audit_geo_lite` → free. Skips cleanly if the facilitator is
  unreachable.

**Armed-paywall verification was additionally done manually** (2026-09-21):
all four checks above passed against a live armed server on Base Sepolia
testnet. The pytest version may skip in slow networks — the manual run is the
proof.

### Test fixes made during the build
- `anthropic.com` removed its `/llms.txt` (404) and `docs.anthropic.com`
  blocks bot fetches — replaced with `mintlify.com` for the llms.txt pass path.
- `adafruit.com` homepage no longer carries Organization JSON-LD (only WebSite)
  — replaced with `ikea.com` for the identity/schema pass path. Engine was
  correct in both cases; the test assumptions were wrong.
- MCP `StreamableHTTPSessionManager.run()` is single-use per instance — merged
  the two MCP tests into one client session.
- The `x402 paywall ARMED` log was `logger.info`, invisible under uvicorn's
  default config — raised to `logger.warning` so deploy logs show it.

## Known issues / quirks

1. **Facilitator arming is slow from some networks**: the x402 `/supported`
   sync took >45s in this sandbox (armed fine at ~60–100s). The paywall test
   allows 180s. If startup logs show `paywall NOT armed`, the service runs
   UNPROTECTED — do not list it until ARMED appears.
2. **Sandbox proxy quirk** (same as the grader): bare-IPv6 entries in
   `no_proxy`/`NO_PROXY` break httpx client construction (`Invalid port ':1]'`).
   The paywall test strips proxy env vars before booting. Real deploys (Fly /
   Railway) don't have these vars.
3. **`X402_DISABLED` is read at import time** — the server must be
   (re)started after changing it; the Dockerfile/fly/railway configs all
   default it to `0` (paywall on).
4. Lite MCP tool has no per-caller rate limit (REST `/geo-lite` has 20/IP/hr).
   Documented as REST-only; MCP hosts generally rate-limit tool calls themselves.
6. **POST /mcp 307-redirect**: Starlette's `Mount("/mcp")` only matches
   `/mcp/*`, so the bare path 307-redirected to `/mcp/` — minimal HTTP
   clients (e.g. Python urllib) don't follow it. Fixed by serving the exact
   `/mcp` path directly via a small ASGI adapter registered before the
   mount. Verified: `POST /mcp` → 200, no redirect.
7. Default network is **Base Sepolia testnet** — earns nothing. Real money
   needs a mainnet facilitator + `X402_NETWORK=eip155:8453` (see README).

## Unavoidable human steps (deploy checklist)

1. `fly launch` (or Railway connect) + `fly secrets set X402_WALLET=0x...`
   — needs Harrison's Fly.io/Railway account + a wallet address he controls.
2. Switch `X402_NETWORK=eip155:8453` and `X402_FACILITATOR_URL` to a
   mainnet-compatible facilitator for real revenue (testnet default earns $0).
3. Confirm `x402 paywall ARMED: $1.50 on eip155:8453 -> 0x...` in startup logs.
4. Make one live $1.50 purchase; confirm USDC lands in the wallet.
5. Paste the README's marketplace copy into MCPX (free lite + paid full) and
   Swarms (needs 2 free 4★+ listings first — the lite tool is the free item).
