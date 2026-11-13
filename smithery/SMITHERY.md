# Smithery publishing — the four remote audit servers

Researched 2026-11-13. Harrison's Smithery namespace: **theharrisontruong**.

## What Smithery requires for remote (URL) servers

- URL-based listings are **bring-your-own-hosting**: Smithery does NOT need
  to build or host anything, and publishing a server hosted elsewhere is
  **free**.
- There are exactly two publish routes, and both run under the namespace
  owner's Smithery account (no repo-file-only route exists for remote
  servers; `smithery.yaml` in a repo is only used for Smithery-hosted
  deployments, which are private early access):
  1. **Web:** https://smithery.ai/new → choose **URL** → set namespace
     `theharrisontruong`, a server ID, and the MCP Server URL → Submit.
  2. **CLI:** `npx @smithery/cli auth login`, then
     `npx @smithery/cli mcp publish "https://<host>/mcp" -n theharrisontruong/<server-id>`
- Smithery scans the live endpoint (Streamable HTTP `tools/list`) for
  metadata. All four servers answer unauthenticated `tools/list`, and the
  full tools are x402-paywalled while the lite tools are free, so the scan
  can connect without payment. Optional hardening: serve a static
  `/.well-known/mcp/server-card.json` (not currently served; only needed
  if a scan is blocked).

## The four publishes (owner action, ~1 minute each)

| Server ID | MCP Server URL | Price |
|---|---|---|
| storefront-grader | https://grader-al6bxcngia-ue.a.run.app/mcp | $1.00/call |
| geo-audit | https://geo-al6bxcngia-ue.a.run.app/mcp | $1.50/call |
| a11y-audit | https://a11y-al6bxcngia-ue.a.run.app/mcp | $1.50/call |
| feed-audit | https://feed-al6bxcngia-ue.a.run.app/mcp | $1.00/call |

Listings will appear at `https://smithery.ai/server/@theharrisontruong/<server-id>`.

Per-server `smithery.yaml` connection configs (classic `startCommand:
type: url` format) sit next to this file: `storefront-grader.yaml`,
`geo-audit.yaml`, `a11y-audit.yaml`, `feed-audit.yaml`.
