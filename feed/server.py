"""Product Feed Hygiene Scan — agent-callable API with a free lite funnel.

Two agent-facing surfaces:

  PAID (x402, $1.00 USDC on Base per call):
  * MCP  — POST /mcp  (tool: audit_feed)
           Unpaid tools/call gets a payment-required result carrying the
           x402 challenge; the agent retries with the payment credential.
  * HTTP — POST /feed (JSON {"url": ...} -> full report JSON)
           Unpaid calls get HTTP 402 with x402 payment-required details.

  FREE (funnel for marketplace reputation):
  * MCP  — POST /mcp  (tool: audit_feed_lite) — score + grade + risk only.
  * HTTP — POST /feed-lite — same, rate-limited (20/hour per IP -> 429).

The caller passes a product feed URL (Google Shopping-style XML, CSV, or
JSON — auto-detected). The lite scan scores the feed but caps item-level
detail; the paid report adds per-check evidence, the worst-offending
items with concrete fixes, and the top 3 prioritized fixes. Feed
disapprovals = products that don't serve on Google Shopping at all, and
agent-commerce surfaces inherit the same dirt — that lost revenue is
what this scan prevents.

Pricing: $1.00/call. ~20 paid calls/month covers a $20/mo subscription.
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

from feed.engine import audit_feed as run_audit, audit_feed_lite as run_lite

log = logging.getLogger("feed")

# ---------------------------------------------------------------------------
# Config (env only — no secrets in code)
# ---------------------------------------------------------------------------

X402_WALLET = os.environ.get("X402_WALLET", "0x0000000000000000000000000000000000000000")
X402_PRICE = os.environ.get("X402_PRICE", "$1.00")          # e.g. "$1.00"
# Default: Base Sepolia testnet — the default facilitator (x402.org) only
# supports testnets. For real USDC on Base mainnet, point X402_FACILITATOR_URL
# at a mainnet facilitator and set X402_NETWORK=eip155:8453 (see README).
X402_NETWORK = os.environ.get("X402_NETWORK", "eip155:84532")
X402_FACILITATOR_URL = os.environ.get(
    "X402_FACILITATOR_URL", "https://x402.org/facilitator"
)
X402_DISABLED = os.environ.get("X402_DISABLED", "0") == "1"  # local dev: no paywall

# Base mainnet USDC — the asset the x402 "exact" scheme settles in on eip155:8453.
X402_USDC_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
X402_VERSION = 2

# Free-tier funnel guard: 20 lite calls per IP per hour.
LITE_RATE_LIMIT = int(os.environ.get("LITE_RATE_LIMIT", "20"))
LITE_RATE_WINDOW_S = 3600

TOOL_FULL = "audit_feed"
TOOL_LITE = "audit_feed_lite"

TOOL_FULL_DESCRIPTION = (
    "Full product feed hygiene scan of a product feed URL (Google "
    "Shopping-style XML, CSV, or JSON — format auto-detected). Fetches the "
    "feed server-side and runs 20 weighted checks: parse validity and "
    "encoding, required attributes (id, title, link, price, availability "
    "values, image_link), GTIN/MPN and brand coverage, identifier_exists "
    "misuse, title/description quality, image-link spot-checks, price "
    "format and sale_price logic, taxonomy and shipping/tax presence, "
    "duplicate ids and variant grouping. These are the errors behind "
    "Google Merchant Center disapprovals — a disapproved feed means the "
    "products do not serve on Google Shopping at all, and agent-commerce "
    "surfaces referencing Merchant Center IDs inherit the same dirt. "
    "Returns machine-readable JSON: 0-100 score, A-F grade, a "
    "disapproval-risk level (Low/Moderate/Elevated/High) with the failing "
    "disapproval-class checks named, per-check pass/partial/fail/unknown "
    "with evidence, the worst-offending items sampled with concrete "
    "fixes, and the top 3 prioritized fixes. HONEST LIMITS: the scan "
    "validates the feed document (capped at 8 MB, first 20,000 items, 8 "
    "image spot-checks); it does not crawl landing pages, so feed-vs-page "
    "price/availability mismatches are out of scope. "
    f"Cost: {X402_PRICE} USDC on Base per call, paid via x402 — the caller's "
    "agent pays from its owner's approved wallet; no API key needed. "
    "Use this after every catalog change, before a Shopping campaign "
    "launch, or to triage a disapproval wave."
)
TOOL_LITE_DESCRIPTION = (
    "FREE lite product feed hygiene scan of a product feed URL (XML, CSV, "
    "or JSON — auto-detected). Runs the same 20 checks as audit_feed but "
    "returns top-line results only: 0-100 score, A-F grade, "
    "disapproval-risk level, detected format, item count, check counts, "
    "and the titles of the top gaps — item-level detail is capped (no "
    "per-check evidence, no offending items, no detailed fixes). Use this "
    "to triage whether a feed needs the full audit_feed report, or call "
    "audit_feed directly for the complete evidence-backed scan."
)
TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Product feed URL to scan (XML, CSV, or JSON), e.g. https://example-store.com/feed.xml",
        }
    },
    "required": ["url"],
}

_state: dict[str, Any] = {"paid": None, "resource_server": None}

# In-memory sliding-window rate limiter for the free /feed-lite endpoint.
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


mcp_server = MCPServer("feed-audit",
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
            from x402.http import HTTPFacilitatorClient, FacilitatorConfig
            from x402.mechanisms.evm.exact import ExactEvmServerScheme
            from x402.mcp.server_async import (  # direct import:
                create_payment_wrapper, PaymentWrapperConfig)
            # NOTE: x402.mcp.__init__ lazily pulls mcp.server.fastmcp (v1 API),
            # which does not exist in mcp 2.x; server_async is self-contained.
            from x402.schemas.config import ResourceConfig

            facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=X402_FACILITATOR_URL))
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
    title="Product Feed Hygiene Scan",
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
        "service": "Product Feed Hygiene Scan",
        "description": ("Agent-callable product feed audit: 20 weighted checks, "
                        "0-100 score, A-F grade, disapproval-risk level, worst "
                        "offenders, top 3 fixes. Feed URL in (XML/CSV/JSON, "
                        "auto-detected). Free lite funnel + paid full report."),
        "tools": {
            "audit_feed": {
                "price_per_call": None if X402_DISABLED else {"amount": X402_PRICE, "asset": "USDC", "network": X402_NETWORK},
                "returns": "full report: score, grade, risk level, 20 checks with evidence, worst offenders, top 3 fixes",
            },
            "audit_feed_lite": {
                "price_per_call": "free",
                "returns": "score + grade + risk level + format/item count + top gap titles only (item detail capped)",
                "rate_limit": f"{LITE_RATE_LIMIT}/hour per IP on REST",
            },
        },
        "how_to_call": {
            "mcp": "POST /mcp  (streamable HTTP; tools: audit_feed [paid via x402 in-protocol], audit_feed_lite [free])",
            "http_paid": "POST /feed with JSON {\"url\": \"https://store.com/feed.xml\"}  (unpaid -> HTTP 402 + x402 details)",
            "http_free": "POST /feed-lite with JSON {\"url\": \"https://store.com/feed.xml\"}  (rate-limited)",
        },
        "breakeven": "~20 paid calls/month covers a $20/mo subscription at $1.00/call.",
    }


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers/agents probe this; 204 keeps it quiet (and out of the spec).
    return Response(status_code=204)


def _price_base_units() -> str:
    """X402_PRICE ("$1.00") -> base units of USDC (6 decimals)."""
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
                "description": "Product feed URL to scan (XML, CSV, or JSON), e.g. https://example-store.com/feed.xml",
            }
        },
        "required": ["url"],
    }
    return {
        "x402Version": X402_VERSION,
        "service": "Product Feed Hygiene Scan",
        "description": ("Agent-callable product feed audit: 20 weighted checks, "
                        "0-100 score, A-F grade, disapproval-risk level, worst "
                        "offenders, top 3 fixes. Feed URL in (XML/CSV/JSON, "
                        "auto-detected). Free lite funnel + paid full report."),
        "facilitator": X402_FACILITATOR_URL,
        "resources": [
            {
                "method": "POST",
                "path": "/feed",
                "description": "Full product feed hygiene scan (20 checks, 0-100 score, A-F, risk level, evidence, worst offenders, top 3 fixes)",
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
            {"method": "POST", "path": "/feed-lite",
             "description": "Free lite scan: score + grade + risk level + format/item count + top gap titles only, item detail capped (rate-limited)",
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
    """Bazaar discovery declaration for POST /feed.

    Lets the PayAI facilitator auto-catalog this route in its Bazaar
    discovery feed. The FastAPI x402 middleware auto-registers the bazaar
    resource-server extension when a route declares it, and injects the
    HTTP method at request time (startup validation only warns).
    """
    from x402.extensions.bazaar import declare_discovery_extension
    return declare_discovery_extension(
        input={"url": "https://example-store.com/feed.xml"},
        input_schema={
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Product feed URL to scan (XML, CSV, or JSON), e.g. https://example-store.com/feed.xml",
                }
            },
            "required": ["url"],
        },
        body_type="json",
    )


def _bazaar_mcp_extensions() -> dict:
    """Bazaar discovery declaration for the MCP tool audit_feed.

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
        description="Full product feed hygiene scan (20 checks, 0-100 score, A-F, risk level, evidence, worst offenders, top 3 fixes)",
        transport="streamable-http",
        input_schema=TOOL_INPUT_SCHEMA,
        example={"url": "https://example-store.com/feed.xml"},
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


class FeedRequest(BaseModel):
    """Request body for POST /feed and POST /feed-lite."""

    url: str = Field(description="Product feed URL to scan (XML, CSV, or JSON), e.g. https://example-store.com/feed.xml")


@app.post("/feed")
async def feed(body: FeedRequest):
    raw = await _full_handler({"url": body.url})
    if raw.get("isError"):
        return JSONResponse(status_code=400, content=json.loads(raw["content"][0]["text"]))
    return JSONResponse(content=json.loads(raw["content"][0]["text"]))


@app.post("/feed-lite", openapi_extra={"security": []})
async def feed_lite(body: FeedRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if not _lite_allowed(client_ip):
        return JSONResponse(
            status_code=429,
            content={"error": f"Lite rate limit exceeded ({LITE_RATE_LIMIT}/hour). "
                              "Use the paid audit_feed tool for unlimited full scans."},
        )
    raw = await _lite_handler({"url": body.url})
    if raw.get("isError"):
        return JSONResponse(status_code=400, content=json.loads(raw["content"][0]["text"]))
    return JSONResponse(content=json.loads(raw["content"][0]["text"]))


# MCP streamable-HTTP endpoint (payment enforced in-protocol on audit_feed only).
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


# x402 HTTP paywall for the paid REST surface only: unpaid requests to /feed
# (any method) -> 402. /feed-lite stays free (rate-limited). Applied only when
# payments are enabled; the middleware is added lazily here (after routes) so
# local-dev/tests can run with X402_DISABLED=1.
if not X402_DISABLED:
    try:
        from x402 import x402ResourceServer as _RS
        from x402.http import HTTPFacilitatorClient as _HF, FacilitatorConfig as _FC
        from x402.http.middleware import fastapi_payment_middleware as _mw
        from x402.mechanisms.evm.exact import ExactEvmServerScheme as _ES

        _fac = _HF(_FC(url=X402_FACILITATOR_URL))
        _rs = _RS(_fac)
        _rs.register(X402_NETWORK, _ES())
        # Route key is the bare path (no method verb): the x402 library treats a
        # method-less pattern as matching ANY method, so unpaid GET/PUT/DELETE
        # (or anything else) on a paid path gets the 402 challenge instead of
        # falling through to FastAPI's 405. The paywall runs before routing and
        # before body validation, so unpaid requests with garbage bodies 402 too.
        _routes = {
            "/feed": {
                "accepts": {
                    "scheme": "exact",
                    "payTo": X402_WALLET,
                    "price": X402_PRICE,
                    "network": X402_NETWORK,
                },
                "description": "Full product feed hygiene scan (20 checks, 0-100 score, A-F, risk level, evidence, worst offenders, top 3 fixes)",
                "mime_type": "application/json",
                # Bazaar discovery: service_name (<=32 chars) + tags are
                # forwarded to the facilitator's ResourceInfo; the "bazaar"
                # extension declaration lets it catalog this route.
                "service_name": "Feed Audit",
                "tags": ["ecommerce", "product-feed", "google-shopping", "merchant-center", "audit"],
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

        log.info("HTTP 402 paywall armed on /feed (any method, unpaid -> 402); POST /feed-lite stays free")
    except Exception:
        log.exception("Could not arm HTTP paywall; /feed will run without it")
