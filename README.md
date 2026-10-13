# Paid Agent APIs — Storefront Grader & GEO Audit

Two pay-per-call APIs built for AI agents. No accounts, no API keys, no subscriptions —
**payment is the auth**. Each call is paywalled with the
[x402 protocol](https://www.x402.org): send a request, get an HTTP 402 with payment
requirements, pay in USDC on Base, get the result.

Operated by Harrison Truong · Contact: **theharrisontruong@gmail.com**

---

## 1. AI Storefront Readiness Grader — $1.00 / call

Grades any storefront URL **A–F** across 20 weighted conversion-readiness checks
(checkout friction, trust signals, mobile readiness, page performance basics,
merchandising completeness, and more) and returns the grade plus the failing checks.

| | |
|---|---|
| HTTP endpoint | `POST https://grader-al6bxcngia-ue.a.run.app/grade` |
| MCP endpoint | `POST https://grader-al6bxcngia-ue.a.run.app/mcp` |
| MCP tool | `grade_storefront` |
| Input | `{ "url": "https://your-store.example" }` |
| Price | **$1.00 per call** (USDC, Base mainnet) |
| Directory listing | https://www.x402scan.com/server/47e39cb6-7143-4052-b4b1-4dac0e1a2b40 |

## 2. GEO Audit — $1.50 / call

Scores any URL's visibility to AI search engines (ChatGPT, Perplexity, Claude, Google
AI Overviews) across 20 weighted checks — crawlability for AI bots, structured data,
content extractability, entity signals — and returns the score plus concrete fixes.

| | |
|---|---|
| HTTP endpoint | `POST https://geo-al6bxcngia-ue.a.run.app/geo` |
| MCP endpoint | `POST https://geo-al6bxcngia-ue.a.run.app/mcp` |
| MCP tool | `audit_geo` |
| Input | `{ "url": "https://your-site.example" }` |
| Price | **$1.50 per call** (USDC, Base mainnet) |
| Free tier | `POST https://geo-al6bxcngia-ue.a.run.app/geo-lite` — tool `audit_geo_lite`, free, rate-limited to 20/hour |

---

## Payment terms (x402)

- **Protocol:** x402 (HTTP 402 Payment Required), `exact` scheme
- **Network:** Base mainnet (`eip155:8453`)
- **Asset:** USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`)
- **Pay to:** `0xd702C8A8Ee30F6950A2a663010d12FC195D5348e`
- **Facilitator:** PayAI (`https://facilitator.payai.network`)
- **Discovery:** both services serve a discovery document at `/.well-known/x402` and
  declare the Bazaar discovery extension (`extensions.bazaar`) in their 402 responses.

Any x402-aware client works out of the box (`@x402/fetch`, `x402` Python package, CDP
or PayAI client wrappers). Unpaid requests receive a standard 402 challenge with the
full payment requirements — no signup needed to inspect them:

```bash
curl -i -X POST https://grader-al6bxcngia-ue.a.run.app/grade \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com"}'
# → HTTP 402 with payment requirements (1.00 USDC, Base mainnet)
```

## MCP usage

Both services expose a stateless streamable-HTTP MCP endpoint at `/mcp` on their base
URLs. Add either to any MCP client as a remote server:

```json
{
  "mcpServers": {
    "storefront-grader": { "url": "https://grader-al6bxcngia-ue.a.run.app/mcp" },
    "geo-audit": { "url": "https://geo-al6bxcngia-ue.a.run.app/mcp" }
  }
}
```

Tool calls return an in-protocol 402 until the client pays via x402; the free
`audit_geo_lite` tool on the GEO server works without payment.

## Registry metadata in this repo

- `server.json` — Official MCP Registry manifest for the Storefront Grader (remote).
- `server-geo.json` — Official MCP Registry manifest for GEO Audit (remote).
- `glama.json` — [Glama](https://glama.ai) maintainer claim for this repository.

## License

MIT — see [LICENSE](LICENSE). The license covers this documentation/metadata repo;
the hosted services themselves are pay-per-call as described above.
