# Paid Agent APIs

**Pay-per-call MCP APIs for AI agents.** Every call is settled in USDC via the
[x402 protocol](https://www.x402.org) on Base — no accounts, no API keys, no
subscriptions. **Payment is the auth:** your agent pays per call and gets the result.

Operated by Harrison Truong · Contact: **theharrisontruong@gmail.com**

---

## Products

| Service | What it does | MCP tool | Price | Status |
|---|---|---|---|---|
| **AI Storefront Readiness Grader** | Grades any storefront URL A–F across 20 weighted conversion-readiness checks (checkout friction, trust signals, mobile readiness, merchandising completeness) and returns the failing checks | `grade_storefront` | **$1.00 / call** | ✅ **LIVE** |
| **GEO Audit** | Scores any URL's visibility to AI search engines (ChatGPT, Perplexity, Claude, Google AI Overviews) across 20 weighted checks and returns concrete fixes. Free tier: `audit_geo_lite`, rate-limited | `audit_geo` | **$1.50 / call** | ✅ **LIVE** |
| **A11y Audit** | Accessibility lawsuit-risk scan: 20 weighted WCAG checks plus a lawsuit-risk level, framed as risk reduction for US businesses. Automated triage, not a certification | `audit_a11y` | **$1.50 / call** | Code complete — deploys with the next release |
| **Feed Audit** | Product-feed hygiene audit: flags the feed errors that get listings disapproved or suppressed before they cost sales | — | **$1.00 / call** | Code complete — deploys with the next release |

### Live endpoints

| Service | MCP endpoint | HTTP endpoint |
|---|---|---|
| Storefront Grader | `POST https://grader-al6bxcngia-ue.a.run.app/mcp` | `POST https://grader-al6bxcngia-ue.a.run.app/grade` |
| GEO Audit | `POST https://geo-al6bxcngia-ue.a.run.app/mcp` | `POST https://geo-al6bxcngia-ue.a.run.app/geo` · free: `POST /geo-lite` |

Both take `{ "url": "https://your-site.example" }`. Source for every service lives in
this repo (`grader/`, `geo/`, `a11y/`).

Add either live service to any MCP client as a remote server:

```json
{
  "mcpServers": {
    "storefront-grader": { "url": "https://grader-al6bxcngia-ue.a.run.app/mcp" },
    "geo-audit": { "url": "https://geo-al6bxcngia-ue.a.run.app/mcp" }
  }
}
```

---

## For agents: how payment works

1. Call the tool or endpoint without payment → you get an HTTP 402 with the full
   payment requirements (amount, network, pay-to address).
2. Pay in USDC on Base mainnet (`eip155:8453`) with any x402 client (`@x402/fetch`,
   the `x402` Python package, CDP or PayAI wrappers) and retry with the payment proof.
3. The call runs and returns the result. No signup, no key management, no invoice.

**Discovery documents** (`/.well-known/x402`):

- https://grader-al6bxcngia-ue.a.run.app/.well-known/x402
- https://geo-al6bxcngia-ue.a.run.app/.well-known/x402

Both services also declare the Bazaar discovery extension (`extensions.bazaar`) in
their 402 responses, so catalog crawlers can index the paid routes from the 402 alone.

Payment terms: x402 `exact` scheme · asset USDC
(`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`) · pay to
`0xd702C8A8Ee30F6950A2a663010d12FC195D5348e` · facilitator PayAI
(`https://facilitator.payai.network`), with Coinbase CDP facilitator support built in.

---

## Listed on

- **Official MCP Registry** —
  [`io.github.theharrisontruong-web/storefront-grader`](https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.theharrisontruong-web/storefront-grader) ·
  [`io.github.theharrisontruong-web/geo-audit`](https://registry.modelcontextprotocol.io/v0.1/servers?search=io.github.theharrisontruong-web/geo-audit)
- **Glama** (both A-rated) —
  [Storefront Grader](https://glama.ai/mcp/connectors/app.run.a.grader-al6bxcngia-ue/storefront-grader) ·
  [GEO Audit](https://glama.ai/mcp/connectors/app.run.a.geo-al6bxcngia-ue/geo-audit)
- **Smithery** — namespace `theharrisontruong`:
  [storefront-grader](https://smithery.ai/servers/theharrisontruong/storefront-grader) ·
  [geo-audit](https://smithery.ai/servers/theharrisontruong/geo-audit)
- **x402scan** —
  [Storefront Grader](https://www.x402scan.com/server/47e39cb6-7143-4052-b4b1-4dac0e1a2b40)

## Registry metadata in this repo

- `server.json` — Official MCP Registry manifest for the Storefront Grader (remote).
- `server-geo.json` — Official MCP Registry manifest for GEO Audit (remote).
- `server-a11y.json` — Official MCP Registry manifest for A11y Audit (publishes on deploy).
- `glama.json` — [Glama](https://glama.ai) maintainer claim for this repository.

## License

MIT — see [LICENSE](LICENSE). The license covers this documentation/metadata repo;
the hosted services themselves are pay-per-call as described above.
