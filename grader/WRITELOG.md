# WRITELOG — AI Storefront Readiness Grader (paid agent API)

Built 2026-09-21. Rebuild of the static checklist as an agent-callable, pay-per-call API per Harrison's direction ("Rebuild it as an agent API").

## What was built

`~/workspace/agent-api/grader/` — Python service exposing one product:

- **`grader/engine.py`** — 20 weighted checks across 5 categories (product intelligence 30, AI discovery 25, conversation readiness 15, transaction readiness 20, trust 10; weights sum to 100). Output: URL/final URL, timestamp, 0–100 score, A–F grade, per-check status/evidence/weight, top-3 recommendations by weight, evaluated-weight denominator, unknown count, runtime. Guards: 8s per-fetch timeout, 75s overall deadline, 2MB body cap, max 2 product pages sampled, every failure → `"unknown"` (never guessed), unreachable site → score 0/F all-unknown.
- **`server.py`** — FastAPI app + MCP server (official MCP Python SDK 2.2.0, stateless streamable HTTP at `POST /mcp`, plain-JSON responses) + x402 paywall on both surfaces:
  - `POST /grade` (JSON `{"url"}` → report); unpaid → HTTP 402 with x402 v2 challenge in `PAYMENT-REQUIRED` header.
  - MCP `grade_storefront(url: str)`; unpaid `tools/call` → payment-required result carrying the x402 challenge; agent retries with payment credential in `_meta`.
  - `GET /health` (free), `GET /` (service + pricing info).
- **Price: `$1.00` USDC per call** (`X402_PRICE` env, default `$1.00`). Breakeven vs the $20/mo Muse subscription = **20 paid calls/month**. Documented in README.
- **Default network: Base Sepolia testnet (`eip155:84532`)** — verified via the default facilitator's `/supported` that `x402.org/facilitator` does NOT support `exact` on Base mainnet (`eip155:8453`); it supports `eip155:84532`. Mainnet switch documented (facilitator URL + `X402_NETWORK` env).
- `Dockerfile`, `fly.toml` (region `mia`, suspend-to-zero), `railway.json`, `.env.example`, pinned `requirements.txt` (incl. `x402[evm]` for USDC), `README.md` (local run, deploy, x402 flow, breakeven math, MCPX/Swarms listing copy), `tests/test_engine.py`.

## Test results — 14/14 PASS (2026-09-21)

```
14 passed in ~15s
```

- Scoring math: all-pass 100/A, all-fail 0/F, partial = half weight, unknown excluded from denominator (rescale), grade boundaries 90/80/70/60, all-unknown → 0/F, recommendations = top-3 by weight, report shape (20 checks).
- Live real-storefront checks (no mocks):
  - `books.toscrape.com` → 5.0/F, 0 unknown, `jsonld_product` fail, `https` pass.
  - `adafruit.com` → `https`/`jsonld_product`/`offer_price` pass, score > 30.
  - Unreachable domain → 0/F, all unknown, `unreachable: true`.
- Server: `POST /grade` 200 with report + 400 on missing URL (paywall off); MCP `initialize` + `tools/list` shows `grade_storefront`.
- Boot smoke test: real `uvicorn` boot, `/health` 200, `POST /grade` 200 with live report.
- **x402 paywall verified for real** (paid mode, facilitator reachable): `POST /grade` unpaid → **HTTP 402** with `PAYMENT-REQUIRED` header = x402 v2 envelope (`scheme: exact`, `network: eip155:84532`, `amount: 1000000` = $1.00 USDC, `payTo` = wallet, USDC asset contract). MCP `tools/call` unpaid → payment-required result with `accepts` challenge, `isError: true`. Paid-call settlement not exercised (needs a funded wallet + real signature) — that's the operator's one live test.

## Integration notes / gotchas found

- x402 2.23.0's `x402.mcp.__init__` lazily imports `mcp.server.fastmcp` (v1 API), which doesn't exist in `mcp==2.2.0`. Fix: import `create_payment_wrapper` from `x402.mcp.server_async` directly (self-contained, no mcp SDK import). Its signature is `(resource_server, PaymentWrapperConfig)`, and the returned value is a *decorator*: `wrapped = paid(handler)`; call `wrapped(args, {"_meta": meta, "toolName": name})`.
- Sync `x402ResourceServer` needs `rs.initialize()` before `build_payment_requirements(ResourceConfig(scheme, pay_to, price, network))` — the dict-config form only exists on the async class.
- MCP lowlevel server 2.x uses constructor handlers: `Server(name, on_list_tools=..., on_call_tool=...)`; inbound `_meta` is at `ctx.meta` (pydantic → `.model_dump()`).
- Streamable HTTP defaults to SSE; `json_response=True` gives plain JSON (friendlier for simple agents).
- Sandbox quirk (not a code bug): `httpx` 0.28.1 fails to construct a client when `no_proxy`/`NO_PROXY` contain bare IPv6 entries (`Invalid port: ':1]'`). Worked around in verification only by unsetting those vars; production hosts (Fly/Railway) don't have them.

## Unavoidable human steps (everything else is done)

1. **Deploy**: `cd ~/workspace/agent-api/grader && fly launch` (or Railway import), then `fly secrets set X402_WALLET=0xYourWalletAddress`.
2. **Verify paywall armed**: startup log must show `x402 paywall ARMED: $1.00 on eip155:84532 -> 0x…`. If `!!! x402 paywall NOT armed` appears, the facilitator was unreachable — do not sell calls.
3. **Money switch (when ready for real USD)**: set `X402_FACILITATOR_URL` to a mainnet facilitator supporting `exact` on `eip155:8453`, set `X402_NETWORK=eip155:8453`, redeploy, make one real $1.00 test purchase.
4. **List it**: paste the README's marketplace copy into MCPX and/or Swarms; set the listing price to match `X402_PRICE`.

No credentials are stored anywhere in this repo. The service never holds a private key — USDC settles directly to `X402_WALLET`.
