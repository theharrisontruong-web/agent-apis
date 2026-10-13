# Awesome-list submission kit — ready to paste

All three major lists are **PR-only**. No major awesome list accepts email/web
submissions without a GitHub account (checked 2026-10-12) — so these entries are
prepared for the moment the GitHub repo exists and Harrison (or Muse in his logged-in
browser session) can open the PRs. Repo assumed: `github.com/theharrisontruong-web/agent-apis`
(adjust if the repo lands under a different name/owner).

---

## 1. punkpeye/awesome-mcp-servers (~82K stars — the big one)

- **Target file:** `README.md` in https://github.com/punkpeye/awesome-mcp-servers
- **Suggested section:** Marketing (confirm the current heading name in their README at
  PR time; if no Marketing section fits, "SEO" / "Other Tools" is the fallback).
- **Hard requirement (enforced by their triage bot):** the server must be listed AND
  claimed on Glama first, and the PR entry must carry the Glama score badge. Order of
  operations: publish repo → Glama crawl/claim → then this PR.
- **Entry line** (one repo = one entry; it covers both remote servers):

```markdown
- [theharrisontruong-web/agent-apis](https://github.com/theharrisontruong-web/agent-apis) [![theharrisontruong-web/agent-apis MCP server](https://glama.ai/mcp/servers/theharrisontruong-web/agent-apis/badges/score.svg)](https://glama.ai/mcp/servers/theharrisontruong-web/agent-apis) 🐍 ☁️ - Two pay-per-call remote MCP servers via x402 (USDC on Base, no API keys): `grade_storefront` grades any storefront URL A–F on 20 weighted conversion checks ($1.00/call), and `audit_geo` scores any URL's AI-search visibility with fixes ($1.50/call, free `audit_geo_lite` tier included).
```

- Emoji key per their legend: 🐍 = Python, ☁️ = cloud/remote. Verify against the
  legend in their README at PR time.
- PR title suggestion: `Add Storefront Grader + GEO Audit (pay-per-call x402 MCP servers)`

## 2. xpaysh/awesome-x402 (the x402 ecosystem list)

- **Target file:** `README.md` in https://github.com/xpaysh/awesome-x402
  (see their `CONTRIBUTING.md` for PR rules).
- **Suggested section:** "🤖 AI Agent Integration → Model Context Protocol (MCP)".
  Fallback: "🌟 Ecosystem Projects → Tools & Services".
- **Entry lines:**

```markdown
- [Storefront Grader](https://grader-al6bxcngia-ue.a.run.app) - Pay-per-call storefront audit API + MCP server (`grade_storefront`): grades any storefront URL A–F across 20 weighted conversion-readiness checks. $1.00/call in USDC on Base via x402 (PayAI facilitator), no API keys.
- [GEO Audit](https://geo-al6bxcngia-ue.a.run.app) - Pay-per-call AI-search visibility API + MCP server (`audit_geo`): scores any URL's visibility to AI search engines across 20 weighted checks and returns concrete fixes. $1.50/call in USDC on Base via x402, with a free rate-limited `audit_geo_lite` tier.
```

- PR title suggestion: `Add Storefront Grader and GEO Audit (x402 pay-per-call APIs + MCP)`

## 3. gtcc777/gold-402 (24K Labs' curated x402 list)

- **Target file:** `README.md` in https://github.com/gtcc777/gold-402 — match their
  current section structure at PR time (they curate tools/services/data APIs).
- **Entry lines:** reuse the two lines from §2 verbatim.

---

## Lists checked, no free no-account route

- **Glama connectors** (glama.ai/mcp/connectors) — web form, but requires GitHub login.
- **mcpservers.org / mcp.so / mcpmarket** — already covered in STATUS.md (dead or paid).
- Everything else of size routes through the Official MCP Registry (`server.json` in
  this package) or a GitHub PR. The repo is the unlock for all of them.
