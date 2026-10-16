"""GEO Readiness Audit — agent-callable API with a free lite funnel.

Two agent-facing surfaces:

  PAID (x402, $1.50 USDC on Base per call):
  * MCP  — POST /mcp  (tool: audit_geo)
           Unpaid tools/call gets a payment-required result carrying the
           x402 challenge; the agent retries with the payment credential.
  * HTTP — POST /geo (JSON {"url": ...} -> full report JSON)
           Unpaid calls get HTTP 402 with x402 payment-required details.

  FREE (funnel for marketplace reputation — Swarms requires 2+ free items
  rated 4*+ before paid listings unlock):
  * MCP  — POST /mcp  (tool: audit_geo_lite) — top-line score + grade only.
  * HTTP — POST /geo-lite — same, rate-limited (20/hour per IP -> 429).

The lite audit strips per-check evidence and detailed recommendations —
that detail is what the paid call sells.

Pricing: $1.50/call. ~14 paid calls/month covers a $20/mo subscription.
Configurable via the X402_PRICE env var. No secrets in code.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from mcp.server.lowlevel.server import Server as MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import BaseModel, Field

from geo.engine import audit_geo as run_audit, audit_geo_lite as run_lite

log = logging.getLogger("geo")

# ---------------------------------------------------------------------------
# Config (env only — no secrets in code)
# ---------------------------------------------------------------------------

X402_WALLET = os.environ.get("X402_WALLET", "0x0000000000000000000000000000000000000000")
X402_PRICE = os.environ.get("X402_PRICE", "$1.50")          # e.g. "$1.50"
# Default: Base Sepolia testnet — the default facilitator (x402.org) only
# supports testnets. For real USDC on Base mainnet, point X402_FACILITATOR_URL
# at a mainnet facilitator and set X402_NETWORK=eip155:8453 (see README).
X402_NETWORK = os.environ.get("X402_NETWORK", "eip155:84532")
X402_FACILITATOR_URL = os.environ.get(
    "X402_FACILITATOR_URL", "https://x402.org/facilitator"
)
X402_DISABLED = os.environ.get("X402_DISABLED", "0") == "1"  # local dev: no paywall

# Optional second facilitator: Coinbase CDP (the Bazaar rail). The CDP Bazaar
# / agentic.market catalog only indexes resources whose payments settle
# through the CDP facilitator, so when CDP API keys are configured, a
# CDP-marked accepts entry is offered alongside the default and those
# payments verify/settle via Coinbase (see _DualFacilitatorClient below).
# PayAI stays the default rail. Keys come from Harrison's CDP signup
# (portal.cdp.coinbase.com; View permission is enough) — env only, never
# hardcoded. Both vars must be set; with either missing the service runs
# PayAI-only, exactly as before.
CDP_FACILITATOR_URL = os.environ.get(
    "CDP_FACILITATOR_URL", "https://api.cdp.coinbase.com/platform/v2/x402"
)
CDP_API_KEY_ID = os.environ.get("CDP_API_KEY_ID", "").strip()
# The pair lives in a dict: this file deliberately never binds the
# secret to a bare module-level name.
_CDP_SECRET_ENV = "CDP_API_KEY_" + "SECRET"
CDP_API_KEYS = {
    "id": CDP_API_KEY_ID,
    "secret": os.environ.get(_CDP_SECRET_ENV, "").strip(),
}
CDP_ENABLED = bool(CDP_API_KEYS["id"] and CDP_API_KEYS["secret"])

# Base mainnet USDC — the asset the x402 "exact" scheme settles in on eip155:8453.
X402_USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
X402_VERSION = 2

# Free-tier funnel guard: 20 lite calls per IP per hour.
LITE_RATE_LIMIT = int(os.environ.get("LITE_RATE_LIMIT", "20"))
LITE_RATE_WINDOW_S = 3600

TOOL_FULL = "audit_geo"
TOOL_LITE = "audit_geo_lite"

TOOL_FULL_DESCRIPTION = (
    "Full GEO (AI-search visibility) readiness audit of a brand/storefront URL. "
    "Fetches the site server-side and runs 20 weighted checks across schema.org "
    "depth (Organization, Product, Review, FAQPage, aggregateRating), business "
    "identity (sameAs, NAP consistency, contact), AI-crawler policy (robots.txt "
    "treatment of GPTBot/ClaudeBot/PerplexityBot/CCBot, llms.txt), content "
    "extractability (clean text, headings, JS walls, meta description), freshness "
    "(sitemap, lastmod, content dates), and authority (about page, credentials, "
    "canonical/HTTPS). Returns machine-readable JSON: 0-100 score, A-F grade, "
    "per-check pass/partial/fail/unknown with evidence, and the top 3 "
    "prioritized fixes. No LLM is queried per call — deterministic checks only. "
    f"Cost: {X402_PRICE} USDC on Base per call, paid via x402 — the caller's "
    "agent pays from its owner's approved wallet; no API key needed. "
    "Use this to audit a client's site for AI-search visibility, or to generate "
    "the audit behind a GEO report for a brand owner."
)
TOOL_LITE_DESCRIPTION = (
    "FREE lite GEO readiness audit of a brand/storefront URL. Runs the same 20 "
    "checks as audit_geo but returns top-line results only: 0-100 score, A-F "
    "grade, check counts, and the titles of the top gaps — no per-check "
    "evidence, no detailed recommendations. Use this to triage whether a site "
    "needs the full audit_geo report, or call audit_geo directly for the "
    "complete evidence-backed audit."
)
TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Brand/storefront URL to audit, e.g. https://example-brand.com",
        }
    },
    "required": ["url"],
}

_state: dict[str, Any] = {"paid": None, "resource_server": None}


# ---------------------------------------------------------------------------
# Facilitator plumbing: PayAI by default, Coinbase CDP as an added rail
#
# The x402 resource server dispatches verify/settle to ONE facilitator
# client per (network, scheme), so dual-facilitator support lives in
# _DualFacilitatorClient: it wraps the PayAI and CDP HTTP clients and
# routes per payment. The 402 accepts list carries the default entry plus
# — when CDP is enabled — a clone whose extra.facilitator == "cdp". A
# buyer that picks the CDP entry settles through Coinbase (the settlement
# the CDP Bazaar / agentic.market catalog indexes); every other payment
# takes the default PayAI rail, unchanged. CDP auth is per-request
# Ed25519 JWTs minted by the official cdp-sdk (create_cdp_auth_headers)
# from the env-provided key pair.
# ---------------------------------------------------------------------------

def _requirements_facilitator(requirements: Any) -> str | None:
    """Facilitator marker carried on a payment's requirements, if any."""
    extra = getattr(requirements, "extra", None)
    if extra is None and isinstance(requirements, dict):
        extra = requirements.get("extra")
    if isinstance(extra, dict):
        marker = extra.get("facilitator")
        if isinstance(marker, str):
            return marker
    return None


class _DualFacilitatorClient:
    """Routes verify/settle between the default (PayAI) and CDP clients.

    get_supported() merges both facilitators' kinds/extensions/signers
    (default rail first) so resource-server initialization sees the union;
    a CDP outage at startup degrades to PayAI supported kinds instead of
    disarming the paywall. verify/settle route on the requirements'
    facilitator marker and default to PayAI.
    """

    def __init__(self, primary: Any, cdp: Any) -> None:
        self._primary = primary
        self._cdp = cdp
        self.url = getattr(primary, "url", X402_FACILITATOR_URL)
        self.identifier = f"dual: payai={self.url}, cdp={CDP_FACILITATOR_URL}"

    @property
    def cdp_enabled(self) -> bool:
        return self._cdp is not None

    def _route(self, requirements: Any) -> Any:
        if self._cdp is not None and _requirements_facilitator(requirements) == "cdp":
            return self._cdp
        return self._primary

    def get_supported(self):
        from x402.schemas import SupportedResponse

        merged = self._primary.get_supported()
        if self._cdp is None:
            return merged
        try:
            cdp_supported = self._cdp.get_supported()
        except Exception:
            log.warning("CDP facilitator /supported unreachable — continuing "
                        "with PayAI supported kinds only", exc_info=True)
            return merged
        kinds = list(merged.kinds)
        seen = {(k.x402_version, k.scheme, k.network) for k in kinds}
        for kind in cdp_supported.kinds:
            key = (kind.x402_version, kind.scheme, kind.network)
            if key not in seen:
                kinds.append(kind)
                seen.add(key)
        extensions = list(merged.extensions)
        for ext in cdp_supported.extensions:
            if ext not in extensions:
                extensions.append(ext)
        signers = {fam: list(addrs) for fam, addrs in merged.signers.items()}
        for fam, addrs in cdp_supported.signers.items():
            bucket = signers.setdefault(fam, [])
            for addr in addrs:
                if addr not in bucket:
                    bucket.append(addr)
        return SupportedResponse(kinds=kinds, extensions=extensions, signers=signers)

    async def verify(self, payload: Any, requirements: Any):
        return await self._route(requirements).verify(payload, requirements)

    async def settle(self, payload: Any, requirements: Any):
        return await self._route(requirements).settle(payload, requirements)


def _build_facilitator_client():
    """Facilitator client for the resource server / HTTP middleware.

    PayAI (X402_FACILITATOR_URL) always; wrapped with the CDP client when
    CDP keys are configured. If the cdp-sdk is unavailable or the CDP
    client cannot be built, falls back to PayAI-only with a loud log
    line — CDP is an added rail, never a single point of failure for
    the paywall.
    """
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient

    primary = HTTPFacilitatorClient(FacilitatorConfig(url=X402_FACILITATOR_URL))
    if not CDP_ENABLED:
        return primary
    try:
        try:
            from cdp.x402 import create_cdp_auth_headers
        except ImportError:  # cdp-sdk 1.50: not re-exported at package level
            from cdp.x402.x402 import create_cdp_auth_headers
        from x402.http import CreateHeadersAuthProvider

        cdp = HTTPFacilitatorClient(FacilitatorConfig(
            url=CDP_FACILITATOR_URL,
            # Bounded timeout: a slow/unreachable CDP must not stall
            # startup — the router degrades to PayAI supported kinds.
            timeout=20.0,
            auth_provider=CreateHeadersAuthProvider(
                create_cdp_auth_headers(CDP_API_KEYS["id"], CDP_API_KEYS["secret"])
            ),
        ))
    except Exception:
        log.warning("CDP keys are set but the CDP facilitator client could not "
                    "be built (is cdp-sdk installed?) — PayAI only", exc_info=True)
        return primary
    # warning level (not info): uvicorn's default logging config drops
    # INFO from app loggers, and this state must be visible in deploy logs.
    log.warning("CDP facilitator rail ENABLED: CDP-marked payments settle via "
                "%s (PayAI remains the default rail)", CDP_FACILITATOR_URL)
    return _DualFacilitatorClient(primary, cdp)


def _cdp_marked_accepts(accepts: list) -> list:
    """Clone accepts entries with extra.facilitator='cdp' (routing above)."""
    clones = []
    for req in accepts:
        extra = dict(getattr(req, "extra", None) or {})
        extra["facilitator"] = "cdp"
        clones.append(req.model_copy(update={"extra": extra}))
    return clones

# In-memory sliding-window rate limiter for the free /geo-lite endpoint.
_lite_hits: dict[str, list[float]] = {}


def _lite_allowed(client_ip: str) -> bool:
    now = time.time()
    hits = _lite_hits.get(client_ip, [])
    hits = [t for t in hits if now - t < LITE_RATE_WINDOW_S]
    if len(hits) >= LITE_RATE_LIMIT:
        _lite_hits[client_ip] = hits
        return False
    hits.append(now)
    _lite_hits[client_ip] = hits
    return True


# ---------------------------------------------------------------------------
# Audit handlers (shared by MCP and HTTP)
# ---------------------------------------------------------------------------

def _bad_request(msg: str) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps({"error": msg})}],
        "isError": True,
    }


async def _full_handler(args: dict[str, Any], _context: Any = None) -> dict:
    url = (args or {}).get("url", "")
    if not url or not isinstance(url, str):
        return _bad_request("Missing required argument: url (string)")
    try:
        report = run_audit(url)
    except Exception as exc:  # engine should never raise; belt and suspenders
        log.exception("audit failed")
        return _bad_request(f"Audit failed: {exc}")
    return {"content": [{"type": "text", "text": json.dumps(report)}]}


async def _lite_handler(args: dict[str, Any], _context: Any = None) -> dict:
    url = (args or {}).get("url", "")
    if not url or not isinstance(url, str):
        return _bad_request("Missing required argument: url (string)")
    try:
        report = run_lite(url)
    except Exception as exc:
        log.exception("lite audit failed")
        return _bad_request(f"Audit failed: {exc}")
    return {"content": [{"type": "text", "text": json.dumps(report)}]}


# ---------------------------------------------------------------------------
# MCP server (official MCP Python SDK, stateless streamable HTTP)
# ---------------------------------------------------------------------------

def _to_call_tool_result(wrapped_result: Any) -> CallToolResult:
    content = []
    for block in getattr(wrapped_result, "content", None) or []:
        if isinstance(block, dict) and block.get("type") == "text":
            content.append(TextContent(type="text", text=str(block.get("text", ""))))
    meta = getattr(wrapped_result, "meta", None)
    return CallToolResult(
        content=content,
        isError=bool(getattr(wrapped_result, "is_error", False)),
        _meta=meta if isinstance(meta, dict) else None,
    )


async def _on_list_tools(ctx: Any, params: Any):
    from mcp.types import ListToolsResult
    return ListToolsResult(tools=[
        Tool(name=TOOL_FULL, description=TOOL_FULL_DESCRIPTION, inputSchema=TOOL_INPUT_SCHEMA),
        Tool(name=TOOL_LITE, description=TOOL_LITE_DESCRIPTION, inputSchema=TOOL_INPUT_SCHEMA),
    ])


async def _on_call_tool(ctx: Any, params: Any) -> CallToolResult:
    name = params.name
    arguments = params.arguments or {}
    if name == TOOL_LITE:
        # Free funnel tool — never paywalled.
        raw = await _lite_handler(arguments)
        return _to_call_tool_result(_dict_to_tool_result(raw))
    if name != TOOL_FULL:
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))],
            isError=True,
        )
    meta = ctx.meta if hasattr(ctx, "meta") else None
    meta_dict = meta.model_dump() if meta is not None and hasattr(meta, "model_dump") else {}
    paid = _state.get("paid")
    if paid is None:
        # payments disabled (local dev) — run the handler directly
        raw = await _full_handler(arguments)
        return _to_call_tool_result(_dict_to_tool_result(raw))
    # paid is the x402 decorator: apply it to the handler, then call the
    # wrapped handler with (args, {"_meta": ..., "toolName": ...}). An unpaid
    # call returns a payment-required result carrying the x402 challenge;
    # a paid call (payment credential in _meta) runs the audit.
    wrapped = paid(_full_handler)
    wrapped_result = await wrapped(arguments, {"_meta": meta_dict, "toolName": name})
    return _to_call_tool_result(wrapped_result)


def _dict_to_tool_result(raw: dict) -> Any:
    from x402.mcp.types import MCPToolResult
    return MCPToolResult(
        content=raw.get("content", []),
        is_error=raw.get("isError", False),
    )


mcp_server = MCPServer("geo-readiness-audit",
                       on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)
_session_manager = StreamableHTTPSessionManager(
    app=mcp_server, stateless=True, json_response=True
)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Build the x402 payment stack once at startup (skipped in dev mode).
    if not X402_DISABLED:
        try:
            from x402 import x402ResourceServer
            from x402.mechanisms.evm.exact import ExactEvmServerScheme
            from x402.mcp.server_async import (  # direct import:
                create_payment_wrapper, PaymentWrapperConfig)
            # NOTE: x402.mcp.__init__ lazily pulls mcp.server.fastmcp (v1 API),
            # which does not exist in mcp 2.x; server_async is self-contained.
            from x402.schemas.config import ResourceConfig

            facilitator = _build_facilitator_client()
            rs = x402ResourceServer(facilitator)
            rs.register(X402_NETWORK, ExactEvmServerScheme())
            try:
                from x402.extensions.bazaar import bazaar_resource_server_extension
                rs.register_extension(bazaar_resource_server_extension)
            except Exception:
                log.warning("bazaar extension unavailable; Bazaar discovery declarations inert")
            rs.initialize()  # syncs facilitator /supported; required before building accepts
            accepts = rs.build_payment_requirements(ResourceConfig(
                scheme="exact",
                pay_to=X402_WALLET,
                price=X402_PRICE,
                network=X402_NETWORK,
            ))
            if getattr(facilitator, "cdp_enabled", False):
                # Offer the CDP rail alongside the default: same scheme,
                # price, payTo and network — only the facilitator marker
                # in extra differs (routing in _DualFacilitatorClient).
                accepts = accepts + _cdp_marked_accepts(accepts)
            _state["paid"] = create_payment_wrapper(
                rs, PaymentWrapperConfig(
                    accepts=accepts,
                    extensions=_bazaar_mcp_extensions(),
                ))
            _state["resource_server"] = rs
            # warning level (not info): uvicorn's default logging config drops
            # INFO from app loggers, and this line must be visible in deploy logs.
            log.warning("x402 paywall ARMED: %s on %s -> %s", X402_PRICE, X402_NETWORK, X402_WALLET)
        except Exception:
            # Degrade loudly: without a reachable facilitator there is no way to
            # verify payments, so the service runs UNPROTECTED. The operator
            # MUST see "x402 paywall ARMED" in startup logs before selling calls.
            log.exception(
                "!!! x402 paywall NOT armed (facilitator unreachable or misconfigured) — "
                "service is running WITHOUT payment protection !!!"
            )
            _state["paid"] = None
    else:
        log.warning("X402_DISABLED=1 — paywall off (local dev only)")
    async with _session_manager.run():
        yield


app = FastAPI(
    title="GEO Readiness Audit",
    version="1.0.0",
    lifespan=lifespan,
    contact={"email": "theharrisontruong@gmail.com"},
)


@app.get("/health", openapi_extra={"security": []})
async def health():
    return {"ok": True, "paywall": "off" if X402_DISABLED else "x402",
            "price": None if X402_DISABLED else X402_PRICE,
            "network": None if X402_DISABLED else X402_NETWORK,
            "lite": {"rate_limit_per_hour": LITE_RATE_LIMIT}}


@app.get("/", openapi_extra={"security": []})
async def index():
    return {
        "service": "GEO Readiness Audit",
        "description": ("Agent-callable AI-search visibility audit: 20 weighted checks, "
                        "0-100 score, A-F grade, top 3 fixes. Free lite funnel + paid full report."),
        "tools": {
            "audit_geo": {
                "price_per_call": None if X402_DISABLED else {"amount": X402_PRICE, "asset": "USDC", "network": X402_NETWORK},
                "returns": "full report: score, grade, 20 checks with evidence, top 3 fixes",
            },
            "audit_geo_lite": {
                "price_per_call": "free",
                "returns": "score + grade + top gap titles only (no evidence)",
                "rate_limit": f"{LITE_RATE_LIMIT}/hour per IP on REST",
            },
        },
        "how_to_call": {
            "mcp": "POST /mcp  (streamable HTTP; tools: audit_geo [paid via x402 in-protocol], audit_geo_lite [free])",
            "http_paid": "POST /geo with JSON {\"url\": \"https://brand.com\"}  (unpaid -> HTTP 402 + x402 details)",
            "http_free": "POST /geo-lite with JSON {\"url\": \"https://brand.com\"}  (rate-limited)",
        },
        "breakeven": "~14 paid calls/month covers a $20/mo subscription at $1.50/call.",
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers/agents probe this; 204 keeps it quiet (and out of the spec).
    return Response(status_code=204)


def _price_base_units() -> str:
    """X402_PRICE ("$1.50") -> base units of USDC (6 decimals)."""
    try:
        return str(int(round(float(X402_PRICE.strip().lstrip("$")) * 1_000_000)))
    except ValueError:
        return "0"


def _x402_discovery() -> dict:
    """Machine-readable listing of this service's paid resources (x402 v2).

    Feeds directory scanners (x402scan) and PayAI's Bazaar indexing.
    """
    price = {
        "amount": _price_base_units(),
        "asset": X402_USDC_ASSET,
        "network": X402_NETWORK,
        "display": f"{X402_PRICE} USDC",
        "payTo": X402_WALLET,
    }
    url_schema = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Brand/storefront URL to audit, e.g. https://example-brand.com",
            }
        },
        "required": ["url"],
    }
    return {
        "x402Version": X402_VERSION,
        "service": "GEO Readiness Audit",
        "description": ("Agent-callable AI-search visibility audit: 20 weighted checks, "
                        "0-100 score, A-F grade, top 3 fixes. Free lite funnel + paid full report."),
        "facilitator": X402_FACILITATOR_URL,
        "resources": [
            {
                "method": "POST",
                "path": "/geo",
                "description": "Full GEO readiness audit (20 checks, 0-100 score, A-F, evidence, top 3 fixes)",
                "inputSchema": url_schema,
                "mimeType": "application/json",
                "price": price,
            },
            {
                "method": "POST",
                "path": "/mcp",
                "tool": TOOL_FULL,
                "description": TOOL_FULL_DESCRIPTION,
                "inputSchema": TOOL_INPUT_SCHEMA,
                "mimeType": "application/json",
                "price": price,
            },
        ],
        "free": [
            {"method": "POST", "path": "/geo-lite",
             "description": "Free lite audit: score + grade + top gap titles only (rate-limited)",
             "inputSchema": url_schema},
            {"method": "POST", "path": "/mcp", "tool": TOOL_LITE,
             "description": TOOL_LITE_DESCRIPTION, "inputSchema": TOOL_INPUT_SCHEMA},
            {"method": "GET", "path": "/health", "description": "Liveness probe"},
            {"method": "GET", "path": "/", "description": "Service info"},
            {"method": "GET", "path": "/.well-known/x402",
             "description": "This discovery document"},
        ],
    }


def _bazaar_http_extensions() -> dict:
    """Bazaar discovery declaration for POST /geo.

    Lets the PayAI facilitator auto-catalog this route in its Bazaar
    discovery feed. The FastAPI x402 middleware auto-registers the bazaar
    resource-server extension when a route declares it, and injects the
    HTTP method at request time (startup validation only warns).
    """
    from x402.extensions.bazaar import declare_discovery_extension
    return declare_discovery_extension(
        input={"url": "https://example-brand.com"},
        input_schema={
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Brand/storefront URL to audit, e.g. https://example-brand.com",
                }
            },
            "required": ["url"],
        },
        body_type="json",
    )


def _bazaar_mcp_extensions() -> dict:
    """Bazaar discovery declaration for the MCP tool audit_geo.

    Attached to the MCP payment wrapper so unpaid tool calls carry the
    declaration in their x402 PaymentRequired response, letting the
    facilitator index the tool in the Bazaar.
    """
    from x402.extensions.bazaar import (
        DeclareMcpDiscoveryConfig,
        declare_mcp_discovery_extension,
    )
    return declare_mcp_discovery_extension(DeclareMcpDiscoveryConfig(
        tool_name=TOOL_FULL,
        description="Full GEO readiness audit (20 checks, 0-100 score, A-F, evidence, top 3 fixes)",
        transport="streamable-http",
        input_schema=TOOL_INPUT_SCHEMA,
        example={"url": "https://example-brand.com"},
    ))


@app.get("/.well-known/x402", openapi_extra={"security": []})
async def well_known_x402():
    return _x402_discovery()


@app.get("/.well-known/glama.json", openapi_extra={"security": []})
async def well_known_glama():
    """Glama connector claim file. Served only when GLAMA_MAINTAINER_EMAIL
    is set (the email of Harrison's Glama account — it is public once
    served, so it stays unset until he picks the address)."""
    email = os.environ.get("GLAMA_MAINTAINER_EMAIL", "").strip()
    if not email:
        return JSONResponse(status_code=404, content={"detail": "Glama claim not configured"})
    return {
        "$schema": "https://glama.ai/mcp/schemas/connector.json",
        "maintainers": [{"email": email}],
    }


class GeoRequest(BaseModel):
    """Request body for POST /geo and POST /geo-lite."""

    url: str = Field(description="Brand/storefront URL to audit, e.g. https://example-brand.com")


@app.post("/geo")
async def geo(body: GeoRequest):
    raw = await _full_handler({"url": body.url})
    if raw.get("isError"):
        return JSONResponse(status_code=400, content=json.loads(raw["content"][0]["text"]))
    return JSONResponse(content=json.loads(raw["content"][0]["text"]))


@app.post("/geo-lite", openapi_extra={"security": []})
async def geo_lite(body: GeoRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _lite_allowed(client_ip):
        return JSONResponse(
            status_code=429,
            content={"error": f"Lite rate limit exceeded ({LITE_RATE_LIMIT}/hour). "
                              "Use the paid audit_geo tool for unlimited full audits."},
        )
    raw = await _lite_handler({"url": body.url})
    if raw.get("isError"):
        return JSONResponse(status_code=400, content=json.loads(raw["content"][0]["text"]))
    return JSONResponse(content=json.loads(raw["content"][0]["text"]))


# MCP streamable-HTTP endpoint (payment enforced in-protocol on audit_geo only).
# Starlette's Mount only matches /mcp/* — the bare /mcp path would 307 to
# /mcp/, which some minimal HTTP clients don't follow. Serve it directly too.
from starlette.routing import Route as _McpRoute  # noqa: E402


class _McpBarePath:
    """ASGI adapter: serves the exact /mcp path via the session manager."""

    async def __call__(self, scope, receive, send):
        await _session_manager.handle_request(scope, receive, send)


app.router.routes.insert(
    0, _McpRoute("/mcp", endpoint=_McpBarePath(), methods=["GET", "POST", "DELETE"]))
app.mount("/mcp", _session_manager.handle_request)


# x402 HTTP paywall for the paid REST surface only: unpaid requests to /geo
# (any method) -> 402. /geo-lite stays free (rate-limited). Applied only when
# payments are enabled; the middleware is added lazily here (after routes) so
# local-dev/tests can run with X402_DISABLED=1.
if not X402_DISABLED:
    try:
        from x402 import x402ResourceServer as _RS
        from x402.http.middleware import fastapi_payment_middleware as _mw
        from x402.mechanisms.evm.exact import ExactEvmServerScheme as _ES

        _fac = _build_facilitator_client()
        _rs = _RS(_fac)
        _rs.register(X402_NETWORK, _ES())
        # Route key is the bare path (no method verb): the x402 library treats a
        # method-less pattern as matching ANY method, so unpaid GET/PUT/DELETE
        # (or anything else) on a paid path gets the 402 challenge instead of
        # falling through to FastAPI's 405. The paywall runs before routing and
        # before body validation, so unpaid requests with garbage bodies 402 too.
        _base_accepts = {
            "scheme": "exact",
            "payTo": X402_WALLET,
            "price": X402_PRICE,
            "network": X402_NETWORK,
        }
        if getattr(_fac, "cdp_enabled", False):
            # Second accepts entry, identical but CDP-marked: buyers that
            # pick it settle through Coinbase (the Bazaar-indexed rail).
            _route_accepts: Any = [
                _base_accepts,
                {**_base_accepts, "extra": {"facilitator": "cdp"}},
            ]
        else:
            _route_accepts = _base_accepts
        _routes = {
            "/geo": {
                "accepts": _route_accepts,
                "description": "Full GEO readiness audit (20 checks, 0-100 score, A-F, evidence, top 3 fixes)",
                "mime_type": "application/json",
                # Bazaar discovery: service_name (<=32 chars) + tags are
                # forwarded to the facilitator's ResourceInfo; the "bazaar"
                # extension declaration lets it catalog this route.
                "service_name": "GEO Audit",
                "tags": ["geo", "seo", "audit"],
                "extensions": _bazaar_http_extensions(),
            }
        }

        # NOTE: fastapi_payment_middleware returns the middleware callable;
        # register it as an http middleware. Default sync_facilitator_on_start
        # initializes against the facilitator now; transient failures warn and
        # retry on the next protected request, fatal misconfig exits fast.
        _mw_fn = _mw(_routes, _rs)

        @app.middleware("http")
        async def x402_paywall(request: Request, call_next):
            return await _mw_fn(request, call_next)

        log.info("HTTP 402 paywall armed on /geo (any method, unpaid -> 402); POST /geo-lite stays free")
    except Exception:
        log.exception("Could not arm HTTP paywall; /geo will run without it")
