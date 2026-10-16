"""AI Storefront Readiness Grader — grading engine.

Server-side storefront analysis. Given a storefront URL, fetches the site and
runs ~20 weighted checks across five categories, producing a 0-100 score,
an A-F grade, and prioritized recommendations as machine-readable JSON.

Design rule: a check that cannot be evaluated returns "unknown" — the engine
never invents check results.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

FETCH_TIMEOUT = 8          # seconds per HTTP fetch
MAX_BYTES = 2_000_000      # cap on response bodies we parse
MAX_PRODUCT_PAGES = 2      # how many product pages to inspect
OVERALL_DEADLINE = 75      # seconds; stop scheduling new fetches after this

USER_AGENT = (
    "Mozilla/5.0 (compatible; StorefrontReadinessGrader/1.0; "
    "+https://github.com/ eighthzwave/grader) readiness-audit-bot"
).replace(" ", "")

STATUS_PASS = "pass"
STATUS_PARTIAL = "partial"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Fetch layer
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str            # final URL after redirects
    status: int
    headers: dict
    text: str           # decoded body (possibly truncated)
    elapsed_ms: int


def fetch(url: str, deadline: float) -> FetchResult | None:
    """Fetch a URL. Returns None on any failure (never raises)."""
    if time.time() > deadline:
        return None
    try:
        t0 = time.time()
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xml,*/*"},
            timeout=FETCH_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        body = b""
        for chunk in resp.iter_content(chunk_size=65536):
            body += chunk
            if len(body) > MAX_BYTES:
                break
        content_type = resp.headers.get("Content-Type", "")
        text = ""
        if "text" in content_type or "xml" in content_type or "json" in content_type or not content_type:
            try:
                text = body.decode(resp.encoding or "utf-8", errors="replace")
            except Exception:
                text = ""
        return FetchResult(
            url=str(resp.url),
            status=resp.status_code,
            headers=dict(resp.headers),
            text=text[:MAX_BYTES],
            elapsed_ms=elapsed_ms,
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Check model
# ---------------------------------------------------------------------------

@dataclass
class Check:
    id: str
    category: str
    weight: int
    status: str = STATUS_UNKNOWN
    evidence: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def json_ld_blocks(soup: BeautifulSoup) -> list[dict]:
    blocks: list[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                if "@graph" in item and isinstance(item["@graph"], list):
                    blocks.extend([g for g in item["@graph"] if isinstance(g, dict)])
                else:
                    blocks.append(item)
    return blocks


def node_types(node: dict) -> set[str]:
    t = node.get("@type")
    if isinstance(t, list):
        return {str(x) for x in t}
    if isinstance(t, str):
        return {t}
    return set()


def find_nodes(blocks: list[dict], want: str) -> list[dict]:
    out = []
    for b in blocks:
        if want in node_types(b):
            out.append(b)
    return out


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def money_mentions(text: str) -> list[str]:
    return re.findall(r"\$\s?\d[\d,]*(?:\.\d{2})?", text)[:8]


PAYMENT_BRANDS = [
    "visa", "mastercard", "american express", "amex", "discover", "paypal",
    "apple pay", "google pay", "shop pay", "stripe", "klarna", "afterpay",
    "affirm", "venmo", "zelle",
]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


def grade_storefront(url: str) -> dict:
    """Grade a storefront URL. Returns the full report dict. Never raises."""
    t_start = time.time()
    deadline = t_start + OVERALL_DEADLINE
    url = normalize_url(url)
    host = urlparse(url).netloc

    def site_file(path: str) -> FetchResult | None:
        base = f"{urlparse(url).scheme}://{host}"
        return fetch(base + path, deadline)

    # -- fetch homepage -----------------------------------------------------
    home = fetch(url, deadline)
    checks: list[Check] = []
    meta: dict = {"fetch_errors": []}

    if home is None or home.status >= 400 or not home.text:
        # Site unreachable: every check is unknown, score 0.
        for cid, cat, w in CHECK_DEFS:
            checks.append(Check(id=cid, category=cat, weight=w,
                                status=STATUS_UNKNOWN,
                                evidence="Site unreachable or returned no content; check not evaluated."))
        return build_report(url, url, checks, meta, t_start, unreachable=True)

    final_url = home.url
    soup = BeautifulSoup(home.text, "lxml")
    page_text = soup.get_text(" ", strip=True)
    page_text_l = page_text.lower()
    blocks = json_ld_blocks(soup)

    # -- find product pages -------------------------------------------------
    product_urls: list[str] = []
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"(^|/)(catalogue|product|products|item|items|shop)(/|$)|/(p|dp)/", href, re.I):
            full = urljoin(final_url, href)
            if urlparse(full).netloc == host and full not in seen:
                seen.add(full)
                product_urls.append(full)
        if len(product_urls) >= MAX_PRODUCT_PAGES:
            break

    product_pages: list[tuple[FetchResult, BeautifulSoup, list[dict]]] = []
    for purl in product_urls:
        pr = fetch(purl, deadline)
        if pr and pr.status < 400 and pr.text:
            psoup = BeautifulSoup(pr.text, "lxml")
            product_pages.append((pr, psoup, json_ld_blocks(psoup)))

    # -- well-known / agent files -------------------------------------------
    robots = site_file("/robots.txt")
    llms = site_file("/llms.txt")
    agents_md = site_file("/agents.md")
    sitemap = site_file("/sitemap.xml")
    ucp_manifest = site_file("/.well-known/ucp.json") or site_file("/.well-known/ucp")
    mcp_manifest = site_file("/.well-known/mcp.json") or site_file("/.well-known/mcp")

    ctx = {
        "url": url, "final_url": final_url, "host": host,
        "home": home, "soup": soup, "blocks": blocks,
        "page_text": page_text, "page_text_l": page_text_l,
        "product_pages": product_pages,
        "robots": robots, "llms": llms, "agents_md": agents_md,
        "sitemap": sitemap, "ucp_manifest": ucp_manifest,
        "mcp_manifest": mcp_manifest,
        "deadline": deadline,
    }

    for cid, cat, w in CHECK_DEFS:
        fn = CHECK_FNS[cid]
        try:
            status, evidence = fn(ctx)
        except Exception as exc:  # a check must never kill the run
            status, evidence = STATUS_UNKNOWN, f"Check errored and was not evaluated: {exc}"
        checks.append(Check(id=cid, category=cat, weight=w, status=status, evidence=evidence))

    return build_report(url, final_url, checks, meta, t_start)


CHECK_DEFS: list[tuple[str, str, int]] = [
    # product intelligence (30)
    ("jsonld_product", "product_intelligence", 8),
    ("offer_price", "product_intelligence", 5),
    ("offer_availability", "product_intelligence", 4),
    ("product_variants", "product_intelligence", 3),
    ("product_identifiers", "product_intelligence", 4),
    ("product_reviews", "product_intelligence", 6),
    # ai discovery (25)
    ("robots_txt", "ai_discovery", 4),
    ("llms_txt", "ai_discovery", 6),
    ("agents_md", "ai_discovery", 3),
    ("sitemap_xml", "ai_discovery", 4),
    ("ucp_manifest", "ai_discovery", 4),
    ("mcp_endpoint", "ai_discovery", 4),
    # conversation readiness (15)
    ("contact_info", "conversation_readiness", 5),
    ("faq_returns", "conversation_readiness", 5),
    ("site_search", "conversation_readiness", 5),
    # transaction readiness (20)
    ("https", "transaction_readiness", 5),
    ("checkout_signals", "transaction_readiness", 7),
    ("tax_shipping_values", "transaction_readiness", 8),
    # trust (10)
    ("business_identity", "trust", 6),
    ("policies", "trust", 4),
]
assert sum(w for _, _, w in CHECK_DEFS) == 100


# ---------------------------------------------------------------------------
# Individual checks — each returns (status, evidence)
# ---------------------------------------------------------------------------

def _product_nodes(ctx) -> list[dict]:
    nodes: list[dict] = []
    for _, _, blocks in ctx["product_pages"]:
        nodes.extend(find_nodes(blocks, "Product"))
    if not nodes:
        nodes.extend(find_nodes(ctx["blocks"], "Product"))
    return nodes


def check_jsonld_product(ctx) -> tuple[str, str]:
    nodes = _product_nodes(ctx)
    if not nodes:
        if not ctx["product_pages"]:
            return STATUS_UNKNOWN, "No product page could be located or fetched; Product markup not evaluated."
        return STATUS_FAIL, "No schema.org Product JSON-LD found on homepage or sampled product pages."
    names = [str(n.get("name", "?"))[:60] for n in nodes[:3]]
    return STATUS_PASS, f"Found {len(nodes)} schema.org Product node(s), e.g. {names}."


def check_offer_price(ctx) -> tuple[str, str]:
    for n in _product_nodes(ctx):
        for offer in as_list(n.get("offers")):
            if isinstance(offer, dict) and (offer.get("price") or offer.get("priceSpecification")):
                return STATUS_PASS, f"Offer price present in structured data: {offer.get('price') or 'priceSpecification'}."
    if not ctx["product_pages"]:
        return STATUS_UNKNOWN, "No product page fetched; price markup not evaluated."
    # fallback: visible price text on product page
    for _, psoup, _ in ctx["product_pages"]:
        if money_mentions(psoup.get_text(" ", strip=True)):
            return STATUS_PARTIAL, "Price visible as text but not in machine-readable Offer markup."
    return STATUS_FAIL, "No machine-readable price found in Product/Offer markup."


def check_offer_availability(ctx) -> tuple[str, str]:
    for n in _product_nodes(ctx):
        for offer in as_list(n.get("offers")):
            if isinstance(offer, dict) and offer.get("availability"):
                avail = str(offer["availability"]).split("/")[-1]
                return STATUS_PASS, f"Structured availability present: {avail}."
    if not ctx["product_pages"]:
        return STATUS_UNKNOWN, "No product page fetched; availability not evaluated."
    return STATUS_FAIL, "No schema.org availability (e.g. InStock) in Offer markup."


def check_product_variants(ctx) -> tuple[str, str]:
    multi = 0
    for n in _product_nodes(ctx):
        offers = as_list(n.get("offers"))
        if len(offers) > 1:
            multi += 1
        if n.get("hasVariant"):
            multi += 1
    if multi:
        return STATUS_PASS, "Product variants expressed as multiple Offers or hasVariant."
    if _product_nodes(ctx):
        return STATUS_PARTIAL, "Single Offer only; no variant structure detected."
    if not ctx["product_pages"]:
        return STATUS_UNKNOWN, "No product page fetched; variants not evaluated."
    return STATUS_FAIL, "No variant structure found."


def check_product_identifiers(ctx) -> tuple[str, str]:
    found = set()
    for n in _product_nodes(ctx):
        for key in ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "mpn", "sku"):
            if n.get(key):
                found.add(key)
    if {"gtin", "gtin8", "gtin12", "gtin13", "gtin14", "mpn"} & found:
        return STATUS_PASS, f"Global identifiers present: {sorted(found)}."
    if "sku" in found:
        return STATUS_PARTIAL, "Only SKU present; no GTIN/MPN for catalog matching."
    if not ctx["product_pages"]:
        return STATUS_UNKNOWN, "No product page fetched; identifiers not evaluated."
    return STATUS_FAIL, "No GTIN/MPN/SKU identifiers in Product markup."


def check_product_reviews(ctx) -> tuple[str, str]:
    for _, _, blocks in ctx["product_pages"]:
        for b in blocks:
            if "AggregateRating" in node_types(b) or b.get("aggregateRating"):
                return STATUS_PASS, "AggregateRating structured data present."
            if "Review" in node_types(b):
                return STATUS_PASS, "Review structured data present."
    for n in _product_nodes(ctx):
        if n.get("aggregateRating") or n.get("review"):
            return STATUS_PASS, "aggregateRating/review embedded in Product node."
    if not ctx["product_pages"]:
        return STATUS_UNKNOWN, "No product page fetched; review markup not evaluated."
    return STATUS_FAIL, "No machine-readable reviews (aggregateRating/Review) found."


def _well_known_status(fr: FetchResult | None, label: str) -> tuple[str, str]:
    if fr is None:
        return STATUS_UNKNOWN, f"{label} fetch failed (network error); not evaluated."
    if fr.status == 200 and fr.text.strip():
        return STATUS_PASS, f"{label} returned HTTP 200 with {len(fr.text)} bytes."
    return STATUS_FAIL, f"{label} not present (HTTP {fr.status})."


def check_robots_txt(ctx) -> tuple[str, str]:
    return _well_known_status(ctx["robots"], "robots.txt")


def check_llms_txt(ctx) -> tuple[str, str]:
    return _well_known_status(ctx["llms"], "llms.txt")


def check_agents_md(ctx) -> tuple[str, str]:
    return _well_known_status(ctx["agents_md"], "agents.md")


def check_sitemap_xml(ctx) -> tuple[str, str]:
    fr = ctx["sitemap"]
    if fr is None:
        return STATUS_UNKNOWN, "sitemap.xml fetch failed (network error); not evaluated."
    if fr.status != 200 or not fr.text.strip():
        # try sitemap index hint from robots.txt
        robots = ctx["robots"]
        if robots and robots.status == 200 and "sitemap" in robots.text.lower():
            return STATUS_PARTIAL, "No /sitemap.xml but robots.txt references a sitemap."
        return STATUS_FAIL, "sitemap.xml not present (HTTP %s)." % fr.status
    locs = re.findall(r"<loc>(.*?)</loc>", fr.text)
    prod_locs = [l for l in locs if re.search(r"/(product|item|p/|dp/)", l, re.I)]
    return STATUS_PASS, f"sitemap.xml lists {len(locs)} URLs ({len(prod_locs)} product-like)."


def check_ucp_manifest(ctx) -> tuple[str, str]:
    fr = ctx["ucp_manifest"]
    if fr and fr.status == 200 and fr.text.strip():
        return STATUS_PASS, "UCP manifest found under /.well-known/."
    mentions = [m for m in ("universal commerce protocol", "agentic commerce protocol", "agentic checkout")
                if m in ctx["page_text_l"]]
    if mentions:
        return STATUS_PARTIAL, f"Agentic-commerce protocol mentioned on site ({mentions[0]}) but no machine-readable UCP manifest found."
    if fr is None:
        return STATUS_UNKNOWN, "Well-known UCP manifest fetch failed; page shows no protocol mentions."
    return STATUS_FAIL, "No UCP manifest and no agentic-commerce protocol signals."


def check_mcp_endpoint(ctx) -> tuple[str, str]:
    fr = ctx["mcp_manifest"]
    if fr and fr.status == 200 and fr.text.strip():
        return STATUS_PASS, "MCP endpoint manifest found under /.well-known/."
    soup = ctx["soup"]
    for a in soup.find_all("a", href=True):
        if "mcp" in a["href"].lower():
            return STATUS_PARTIAL, f"Page links to an MCP-related resource: {a['href'][:80]}."
    if "mcp endpoint" in ctx["page_text_l"] or "mcp server" in ctx["page_text_l"]:
        return STATUS_PARTIAL, "MCP mentioned on site but no discoverable endpoint manifest."
    if fr is None:
        return STATUS_UNKNOWN, "Well-known MCP manifest fetch failed; no MCP signals on page."
    return STATUS_FAIL, "No discoverable MCP endpoint."


def check_contact_info(ctx) -> tuple[str, str]:
    orgs = find_nodes(ctx["blocks"], "Organization") + find_nodes(ctx["blocks"], "LocalBusiness")
    for o in orgs:
        if o.get("email") or o.get("telephone") or o.get("address"):
            return STATUS_PASS, "Contact details in structured Organization markup."
    soup = ctx["soup"]
    if soup.find("a", href=re.compile(r"^mailto:")) or soup.find("a", href=re.compile(r"^tel:")):
        return STATUS_PARTIAL, "mailto:/tel: links present but no structured Organization contact."
    if soup.find("a", href=re.compile(r"contact", re.I)):
        return STATUS_PARTIAL, "Contact page linked but no machine-readable contact details."
    return STATUS_FAIL, "No machine-readable contact information found."


def check_faq_returns(ctx) -> tuple[str, str]:
    if find_nodes(ctx["blocks"], "FAQPage"):
        return STATUS_PASS, "FAQPage structured data present."
    soup = ctx["soup"]
    policy_link = soup.find("a", href=re.compile(r"(return|refund|shipping|faq)", re.I))
    if policy_link:
        return STATUS_PARTIAL, f"Policy page linked ({policy_link.get('href','')[:60]}) but not machine-readable."
    return STATUS_FAIL, "No FAQ or returns/shipping policy discoverable."


def check_site_search(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    if soup.find("form", action=re.compile(r"search", re.I)) or soup.find("input", {"type": "search"}):
        return STATUS_PASS, "Site search form detected."
    if soup.find("a", href=re.compile(r"/search", re.I)):
        return STATUS_PARTIAL, "Search URL pattern present but no search form detected."
    if 'rel="search"' in (ctx["home"].text or ""):
        return STATUS_PASS, "OpenSearch description linked."
    return STATUS_FAIL, "No site search detected."


def check_https(ctx) -> tuple[str, str]:
    if urlparse(ctx["final_url"]).scheme == "https":
        return STATUS_PASS, f"Site served over HTTPS ({ctx['final_url'][:60]})."
    return STATUS_FAIL, "Site not served over HTTPS."


def check_checkout_signals(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    signals = set()
    for a in soup.find_all("a", href=True):
        if re.search(r"(cart|checkout|basket)", a["href"], re.I):
            signals.add("cart/checkout link")
    for brand in PAYMENT_BRANDS:
        if brand in ctx["page_text_l"]:
            signals.add(brand)
    if len(signals) >= 2:
        return STATUS_PASS, f"Checkout signals: {sorted(signals)[:6]}."
    if signals:
        return STATUS_PARTIAL, f"Only one checkout signal: {sorted(signals)}."
    return STATUS_FAIL, "No cart/checkout links or payment-method mentions found."


def check_tax_shipping_values(ctx) -> tuple[str, str]:
    text = ctx["page_text_l"]
    concrete = money_mentions(ctx["page_text"]) and any(
        k in text for k in ("shipping", "delivery", "tax")
    )
    if concrete:
        return STATUS_PASS, "Shipping/tax stated with concrete values on page."
    if any(k in text for k in ("shipping", "delivery", "tax", "duties")):
        return STATUS_PARTIAL, "Shipping/tax mentioned but no concrete values detected."
    return STATUS_FAIL, "No shipping or tax information detected."


def check_business_identity(ctx) -> tuple[str, str]:
    for o in find_nodes(ctx["blocks"], "Organization") + find_nodes(ctx["blocks"], "LocalBusiness"):
        if o.get("name") and (o.get("url") or o.get("logo")):
            return STATUS_PASS, f"Business identity in structured markup: {str(o.get('name'))[:60]}."
    soup = ctx["soup"]
    footer = soup.find("footer")
    if footer and len(footer.get_text(strip=True)) > 30:
        return STATUS_PARTIAL, "Footer business info present but not structured."
    return STATUS_FAIL, "No structured business identity found."


def check_policies(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    links = [a.get("href", "") for a in soup.find_all("a", href=True)]
    has_privacy = any(re.search(r"privacy", h, re.I) for h in links)
    has_terms = any(re.search(r"terms", h, re.I) for h in links)
    if has_privacy and has_terms:
        return STATUS_PASS, "Privacy policy and terms links present."
    if has_privacy or has_terms:
        return STATUS_PARTIAL, "Only one of privacy/terms linked."
    return STATUS_FAIL, "No privacy or terms links found."


CHECK_FNS = {
    "jsonld_product": check_jsonld_product,
    "offer_price": check_offer_price,
    "offer_availability": check_offer_availability,
    "product_variants": check_product_variants,
    "product_identifiers": check_product_identifiers,
    "product_reviews": check_product_reviews,
    "robots_txt": check_robots_txt,
    "llms_txt": check_llms_txt,
    "agents_md": check_agents_md,
    "sitemap_xml": check_sitemap_xml,
    "ucp_manifest": check_ucp_manifest,
    "mcp_endpoint": check_mcp_endpoint,
    "contact_info": check_contact_info,
    "faq_returns": check_faq_returns,
    "site_search": check_site_search,
    "https": check_https,
    "checkout_signals": check_checkout_signals,
    "tax_shipping_values": check_tax_shipping_values,
    "business_identity": check_business_identity,
    "policies": check_policies,
}
assert set(CHECK_FNS) == {cid for cid, _, _ in CHECK_DEFS}


# ---------------------------------------------------------------------------
# Scoring + report
# ---------------------------------------------------------------------------

GRADE_BOUNDS = [(90, "A"), (80, "B"), (70, "C"), (60, "D")]


def compute_score(checks: list[Check]) -> tuple[float, str, int, int]:
    earned = 0.0
    denom = 0
    for c in checks:
        if c.status == STATUS_UNKNOWN:
            continue
        denom += c.weight
        if c.status == STATUS_PASS:
            earned += c.weight
        elif c.status == STATUS_PARTIAL:
            earned += c.weight * 0.5
    if denom == 0:
        return 0.0, "F", 0, 0
    score = round(100.0 * earned / denom, 1)
    grade = "F"
    for bound, letter in GRADE_BOUNDS:
        if score >= bound:
            grade = letter
            break
    return score, grade, denom, sum(1 for c in checks if c.status == STATUS_UNKNOWN)


RECOMMENDATION_FIXES = {
    "jsonld_product": ("Add schema.org Product JSON-LD", "AI agents read structured data first; without Product markup the catalog is invisible to them."),
    "offer_price": ("Expose machine-readable prices", "Put price in Offer markup so agents can compare and quote without scraping."),
    "offer_availability": ("Publish live availability", "Agents won't recommend what they can't confirm is in stock."),
    "product_variants": ("Structure product variants", "Express variants as multiple Offers so agents see the full option set."),
    "product_identifiers": ("Add GTIN/MPN identifiers", "Global identifiers let agents match products across catalogs and feeds."),
    "product_reviews": ("Add structured reviews", "aggregateRating/Review markup feeds the trust signals agents rank on."),
    "robots_txt": ("Publish a robots.txt", "Agents check it before crawling; its absence is a discoverability gap."),
    "llms_txt": ("Publish /llms.txt", "The emerging standard file agents read to understand what a site sells and how to buy."),
    "agents_md": ("Publish /agents.md", "Machine-readable agent instructions: policies, endpoints, and capabilities."),
    "sitemap_xml": ("Publish a sitemap.xml", "Lets agents enumerate the catalog instead of guessing URLs."),
    "ucp_manifest": ("Adopt a UCP manifest", "Universal Commerce Protocol manifest makes the store transactable inside AI chat surfaces."),
    "mcp_endpoint": ("Expose an MCP endpoint", "Lets shopping agents query catalog, price, and stock programmatically."),
    "contact_info": ("Add machine-readable contact info", "Organization markup with email/phone builds agent trust."),
    "faq_returns": ("Structure FAQ and returns policy", "Agents need returns/shipping terms as data, not prose."),
    "site_search": ("Add site search", "Agents use search to resolve ambiguous product requests."),
    "https": ("Serve the site over HTTPS", "Agents and payment rails require encrypted transport."),
    "checkout_signals": ("Make checkout agent-visible", "Clear cart/checkout paths and stated payment methods."),
    "tax_shipping_values": ("State tax and shipping as values", "Agents need numbers ($X shipping, Y% tax), not slogans."),
    "business_identity": ("Add Organization structured data", "A verifiable business identity is a trust prerequisite."),
    "policies": ("Link privacy policy and terms", "Baseline trust signals every agent checks."),
}


def build_report(url: str, final_url: str, checks: list[Check], meta: dict,
                 t_start: float, unreachable: bool = False) -> dict:
    score, grade, denom, unknown_count = compute_score(checks)
    failed = [c for c in checks if c.status in (STATUS_FAIL, STATUS_PARTIAL)]
    failed.sort(key=lambda c: -c.weight)
    recommendations = []
    for c in failed[:3]:
        title, why = RECOMMENDATION_FIXES[c.id]
        recommendations.append({
            "check_id": c.id,
            "title": title,
            "why": why,
            "evidence": c.evidence,
            "weight": c.weight,
        })
    return {
        "url": url,
        "final_url": final_url,
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "grade": grade,
        "unreachable": unreachable,
        "checks": [
            {"id": c.id, "category": c.category, "weight": c.weight,
             "status": c.status, "evidence": c.evidence}
            for c in checks
        ],
        "recommendations": recommendations,
        "meta": {
            "evaluated_weight": denom,
            "total_weight": 100,
            "unknown_count": unknown_count,
            "elapsed_ms": int((time.time() - t_start) * 1000),
            "fetch_errors": meta.get("fetch_errors", []),
            "engine": "storefront-readiness-grader/1.0",
        },
    }
