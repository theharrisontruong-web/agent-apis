# Buy a call with `@x402/fetch` — runnable example

A complete, runnable buyer for the four paid audit APIs in this repo
(Storefront Grader, GEO Audit, A11y Audit, Feed Audit). It demonstrates the
whole x402 flow: send a request → receive HTTP 402 with payment requirements →
sign a USDC payment authorization → retry and get the report.

**Safety first: the default mode is a dry run.** With no flags it sends one
*unpaid* request, decodes and prints the 402 challenge, and exits. It cannot
pay — paying requires both `--pay` **and** a wallet key that **you** supply
via environment variable. The operator of these APIs never provides, sees, or
needs your key.

## Quick start

```bash
# Dry run (no wallet, no dependencies, Node 18+):
node buy.mjs
node buy.mjs geo https://your-site.example

# Real payment (real USDC on Base mainnet, from YOUR wallet):
npm install
EVM_PRIVATE_KEY=0x<your-key> node buy.mjs grader https://your-store.example --pay
```

Use a throwaway wallet funded with a few dollars of USDC on Base. Each call
costs exactly the listed price — $1.00 (grader, feed) or $1.50 (geo, a11y) —
settled by the facilitator; the buyer pays no gas for the transfer itself.

## What the dry run prints (captured live, 2026-11-13)

```text
DRY RUN — one unpaid request to the grader API ($1.00/call)
POST https://grader-al6bxcngia-ue.a.run.app/grade  body={"url":"https://example.com"}

HTTP 402
x402Version: 2
  accepts: scheme=exact network=eip155:8453
           asset=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913 amount=1000000 ($1.00 USDC)
           payTo=0xd702C8A8Ee30F6950A2a663010d12FC195D5348e maxTimeoutSeconds=300
  resource: {"url":"https://grader-al6bxcngia-ue.a.run.app/grade","description":"AI storefront readiness grade (20 checks, 0-100 score, A-F, top 3 fixes)", ...}

DRY RUN COMPLETE — nothing was signed and nothing was paid.
```

## Services

| Argument | API | Endpoint | Body | Price |
|---|---|---|---|---|
| `grader` | AI Storefront Readiness Grader | `POST https://grader-al6bxcngia-ue.a.run.app/grade` | `{"url": "..."}` | $1.00 |
| `geo` | GEO Audit (AI search visibility) | `POST https://geo-al6bxcngia-ue.a.run.app/geo` | `{"url": "..."}` | $1.50 |
| `a11y` | A11y Audit (WCAG / ADA risk) | `POST https://a11y-al6bxcngia-ue.a.run.app/a11y` | `{"url": "..."}` | $1.50 |
| `feed` | Product Feed Audit | `POST https://feed-al6bxcngia-ue.a.run.app/feed` | `{"url": "..."}` | $1.00 |

All four settle in USDC on Base mainnet (`eip155:8453`) to
`0xd702C8A8Ee30F6950A2a663010d12FC195D5348e` via the PayAI facilitator.

## How the payment works

With `--pay`, `buy.mjs` builds the standard client from the
[x402 buyer quickstart](https://github.com/x402-foundation/x402/blob/main/docs/getting-started/quickstart-for-buyers.mdx):

```js
import { wrapFetchWithPayment } from "@x402/fetch";
import { x402Client } from "@x402/core/client";
import { ExactEvmScheme } from "@x402/evm/exact/client";
import { privateKeyToAccount } from "viem/accounts";

const signer = privateKeyToAccount(process.env.EVM_PRIVATE_KEY);
const client = new x402Client();
client.register("eip155:*", new ExactEvmScheme(signer));
const fetchWithPayment = wrapFetchWithPayment(fetch, client);
// fetchWithPayment(url, init) — sees 402, signs EIP-3009, retries, done.
```

The same wrapper works unchanged against any x402 endpoint — and inside
agent frameworks: hand `fetchWithPayment` to your LangChain / Vercel AI SDK /
AgentKit tool layer and the agent can buy these audits (or any x402 API)
mid-task.

## Prefer MCP?

Each service also exposes a streamable-HTTP MCP endpoint at `/mcp` on the
same host, with the same x402 terms enforced in-protocol, plus a free
rate-limited `*_lite` tool on each server for evaluation. Manifests:
`server.json`, `server-geo.json`, `server-a11y.json`, `server-feed.json` in
the repo root.
