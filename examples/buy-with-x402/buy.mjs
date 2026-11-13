#!/usr/bin/env node
/**
 * buy.mjs — buy one call to a paid x402 audit API with @x402/fetch.
 *
 * DEFAULT MODE IS DRY-RUN. It sends ONE unpaid request, decodes and prints
 * the HTTP 402 payment challenge, and exits. It never signs anything and
 * never pays. No dependencies are needed for dry-run (Node 18+ only).
 *
 * To actually pay — real USDC on Base mainnet, out of YOUR wallet:
 *
 *   npm install
 *   EVM_PRIVATE_KEY=0x<your-key> node buy.mjs <service> <target-url> --pay
 *
 * Use a throwaway wallet funded with a few dollars of USDC on Base.
 * The operator of these APIs never sees your key; it stays in your env.
 *
 * Usage:
 *   node buy.mjs                     # dry-run against the grader
 *   node buy.mjs geo https://example.com
 *   node buy.mjs a11y https://example.com --pay   (needs EVM_PRIVATE_KEY)
 *
 * Services:
 *   grader  Storefront Readiness Grader  $1.00  POST /grade  {"url": ...}
 *   geo     GEO Audit (AI search)        $1.50  POST /geo    {"url": ...}
 *   a11y    A11y Audit (WCAG/ADA risk)   $1.50  POST /a11y   {"url": ...}
 *   feed    Product Feed Audit           $1.00  POST /feed    {"url": ...}
 *
 * Reference: x402 buyer quickstart —
 * https://github.com/x402-foundation/x402/blob/main/docs/getting-started/quickstart-for-buyers.mdx
 */

const SERVICES = {
  grader: { url: "https://grader-al6bxcngia-ue.a.run.app/grade", price: "$1.00" },
  geo:    { url: "https://geo-al6bxcngia-ue.a.run.app/geo",       price: "$1.50" },
  a11y:   { url: "https://a11y-al6bxcngia-ue.a.run.app/a11y",     price: "$1.50" },
  feed:   { url: "https://feed-al6bxcngia-ue.a.run.app/feed",     price: "$1.00" },
};

const args = process.argv.slice(2);
const pay = args.includes("--pay");
const positional = args.filter((a) => !a.startsWith("--"));
const serviceName = (positional[0] || "grader").toLowerCase();
const targetUrl = positional[1] || "https://example.com";
const service = SERVICES[serviceName];

if (!service) {
  console.error(`Unknown service "${serviceName}". Choose one of: ${Object.keys(SERVICES).join(", ")}`);
  process.exit(2);
}

const body = JSON.stringify({ url: targetUrl });

function decodeChallenge(res, text) {
  // x402 v2: requirements ride in the base64 `payment-required` header.
  const header = res.headers.get("payment-required");
  if (header) {
    try {
      return JSON.parse(Buffer.from(header, "base64").toString("utf8"));
    } catch {
      /* fall through to body */
    }
  }
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function printChallenge(challenge) {
  if (!challenge) {
    console.log("(could not decode a payment challenge from the response)");
    return;
  }
  console.log(`x402Version: ${challenge.x402Version}`);
  for (const req of challenge.accepts || []) {
    const usdc = req.amount ? `$${(Number(req.amount) / 1e6).toFixed(2)}` : "?";
    console.log(`  accepts: scheme=${req.scheme} network=${req.network}`);
    console.log(`           asset=${req.asset} amount=${req.amount} (${usdc} USDC)`);
    console.log(`           payTo=${req.payTo} maxTimeoutSeconds=${req.maxTimeoutSeconds}`);
  }
  if (challenge.resource) {
    console.log(`  resource: ${JSON.stringify(challenge.resource)}`);
  }
}

async function dryRun() {
  console.log(`DRY RUN — one unpaid request to the ${serviceName} API (${service.price}/call)`);
  console.log(`POST ${service.url}  body=${body}\n`);
  const res = await fetch(service.url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });
  const text = await res.text();
  console.log(`HTTP ${res.status}`);
  if (res.status !== 402) {
    console.log("Expected a 402 challenge. Response body:");
    console.log(text.slice(0, 2000));
    return;
  }
  printChallenge(decodeChallenge(res, text));
  console.log(
    "\nDRY RUN COMPLETE — nothing was signed and nothing was paid.\n" +
      "To pay for real: npm install, then re-run with --pay and EVM_PRIVATE_KEY set\n" +
      "(a wallet YOU control, funded with USDC on Base). See README.md."
  );
}

async function payRun() {
  const key = process.env.EVM_PRIVATE_KEY;
  if (!key) {
    console.error(
      "--pay requires EVM_PRIVATE_KEY in the environment (a wallet you control,\n" +
        "funded with USDC on Base mainnet). Refusing to continue without it."
    );
    process.exit(2);
  }
  let wrapFetchWithPayment, x402Client, ExactEvmScheme, privateKeyToAccount;
  try {
    ({ wrapFetchWithPayment } = await import("@x402/fetch"));
    ({ x402Client } = await import("@x402/core/client"));
    ({ ExactEvmScheme } = await import("@x402/evm/exact/client"));
    ({ privateKeyToAccount } = await import("viem/accounts"));
  } catch {
    console.error("Missing dependencies. Run: npm install");
    process.exit(2);
  }

  const signer = privateKeyToAccount(key);
  console.log(`Paying from wallet ${signer.address}`);
  console.log(`Buying one ${serviceName} call (${service.price}) for target: ${targetUrl}\n`);

  const client = new x402Client();
  client.register("eip155:*", new ExactEvmScheme(signer));
  const fetchWithPayment = wrapFetchWithPayment(fetch, client);

  // The wrapper sees the 402, signs an EIP-3009 authorization, and retries —
  // the facilitator settles the USDC transfer on Base.
  const res = await fetchWithPayment(service.url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
  });
  console.log(`HTTP ${res.status}`);
  const text = await res.text();
  try {
    console.log(JSON.stringify(JSON.parse(text), null, 2));
  } catch {
    console.log(text);
  }
}

if (pay) {
  await payRun();
} else {
  await dryRun();
}
