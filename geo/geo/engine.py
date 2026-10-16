"""GEO (AI-search visibility) readiness audit engine.

Given a brand/storefront URL, fetches the site server-side and runs 20
weighted checks across six categories, producing a 0-100 score, an A-F
grade, per-check evidence, and the top 3 prioritized fixes as
machine-readable JSON.

What this audits: the machine-readable inputs that determine whether AI
search (ChatGPT, AI Overviews, Perplexity) can find, understand, and cite a
brand — schema.org depth, identity signals, AI-crawler policy, content
extractability, freshness, authority signals.

What this does NOT do: query any LLM per call (cost stays ~$0), or claim
that any single signal (e.g. llms.txt) lifts citations — the evidence
strings say what each signal actually does. Checks that cannot be
evaluated return "unknown", never guesses.

Shared fetch/check/scoring primitives come from common.auditlib.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

# Make the shared lib importable both locally (repo layout) and in Docker
# (WORKDIR layout: /srv/geo with ./common copied in).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.auditlib import (  # noqa: E402
    FETCH_TIMEOUT,
    OVERALL_DEADLINE,
    STATUS_FAIL,
    STATUS_PARTIAL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    Check,
    FetchResult,
    as_list,
    compute_score,
    fetch,
    find_nodes,
    host_of,
    is_fully_disallowed,
    json_ld_blocks,
    normalize_url,
    node_types,
    parse_robots_rules,
)

ENGINE_VERSION = "geo-readiness-audit/1.0"

# AI crawler user-agent tokens tracked in robots.txt (lowercase).
AI_BOTS = [
    "gptbot", "chatgpt-user", "claudebot", "claude-web", "anthropic-ai",
    "perplexitybot", "ccbot", "applebot-extended", "bytespider",
    "google-extended", "omgilibot", "diffbot",
]

JS_WALL_PHRASES = [
    "enable javascript", "javascript is disabled", "please enable js",
    "requires javascript", "you need to enable javascript",
    "turn on javascript", "javascript required",
]

CREDENTIAL_WORDS = [
    "certified", "licensed", "award-winning", "award winning", "ph.d", "phd",
    "years of experience", "founded in", "since 19", "since 20",
    "accredited", "board-certified",
]

FRESH_YEAR_RE = re.compile(r"\b(202[4-6])\b")


# ---------------------------------------------------------------------------
# Check definitions (weights sum to 100)
# ---------------------------------------------------------------------------

CHECK_DEFS: list[tuple[str, str, int]] = [
    # schema.org depth (30)
    ("schema_organization", "schema_depth", 8),
    ("schema_product", "schema_depth", 7),
    ("schema_review", "schema_depth", 6),
    ("schema_faq", "schema_depth", 4),
    ("aggregate_rating", "schema_depth", 5),
    # business identity (13)
    ("sameas_identity", "identity", 4),
    ("nap_consistency", "identity", 5),
    ("business_contact", "identity", 4),
    # AI-crawler policy (11)
    ("robots_ai_bots", "ai_crawler_policy", 8),
    ("llms_txt", "ai_crawler_policy", 3),
    # content extractability (20)
    ("clean_text_extract", "extractability", 5),
    ("headings_structure", "extractability", 4),
    ("no_js_wall", "extractability", 5),
    ("meta_description", "extractability", 6),
    # freshness (12)
    ("sitemap_present", "freshness", 4),
    ("sitemap_freshness", "freshness", 4),
    ("content_freshness", "freshness", 4),
    # authority (14)
    ("about_page", "authority", 4),
    ("credentials_signals", "authority", 4),
    ("canonical_https", "authority", 6),
]
assert sum(w for _, _, w in CHECK_DEFS) == 100


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def _clean_text(soup: BeautifulSoup) -> str:
    """Visible text with script/style/noscript removed."""
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def _build_ctx(url: str, deadline: float) -> dict | None:
    """Fetch homepage + well-known files. Returns None if site unreachable."""
    home = fetch(url, deadline)
    if home is None or home.status >= 400 or not home.text:
        return None
    final_url = home.url
    host = host_of(url)
    base = f"{urlparse(url).scheme}://{host}"

    def site_file(path: str) -> FetchResult | None:
        return fetch(base + path, deadline)

    soup = BeautifulSoup(home.text, "lxml")
    text = _clean_text(BeautifulSoup(home.text, "lxml"))
    words = text.split()
    return {
        "url": url, "final_url": final_url, "host": host,
        "home": home, "soup": soup,
        "blocks": json_ld_blocks(soup),
        "text": text, "text_l": text.lower(), "words": words,
        "word_count": len(words),
        "robots": site_file("/robots.txt"),
        "llms": site_file("/llms.txt"),
        "sitemap": site_file("/sitemap.xml"),
        "deadline": deadline,
    }


def _org_nodes(ctx) -> list[dict]:
    """Organization nodes, including LocalBusiness/Store subtypes."""
    nodes = find_nodes(ctx["blocks"], "Organization")
    for b in ctx["blocks"]:
        if b not in nodes and any(
            isinstance(t, str) and ("LocalBusiness" in t or "Store" in t)
            for t in node_types(b)
        ):
            nodes.append(b)
    return nodes


def _org_name(ctx) -> str | None:
    for o in _org_nodes(ctx):
        name = o.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


# ---------------------------------------------------------------------------
# Individual checks — each returns (status, evidence)
# ---------------------------------------------------------------------------

def check_schema_organization(ctx) -> tuple[str, str]:
    nodes = _org_nodes(ctx)
    if not nodes:
        return STATUS_FAIL, "No Organization/LocalBusiness JSON-LD found — AI search has no structured business identity to cite."
    o = nodes[0]
    name = str(o.get("name", "?"))[:60]
    extras = [k for k in ("url", "logo", "address") if o.get(k)]
    if extras:
        return STATUS_PASS, f"Organization markup present: {name} (with {', '.join(extras)})."
    return STATUS_PARTIAL, f"Organization markup present but thin: {name} (no url/logo/address)."


def check_schema_product(ctx) -> tuple[str, str]:
    nodes = find_nodes(ctx["blocks"], "Product")
    if nodes:
        return STATUS_PASS, f"{len(nodes)} Product JSON-LD node(s) on homepage — products are machine-readable for AI shopping answers."
    return STATUS_FAIL, "No Product JSON-LD on homepage — products can't be cited as structured entities."


def check_schema_review(ctx) -> tuple[str, str]:
    for b in ctx["blocks"]:
        if "Review" in node_types(b):
            return STATUS_PASS, "Review JSON-LD present — individual reviews are machine-readable."
    return STATUS_FAIL, "No Review JSON-LD found."


def check_schema_faq(ctx) -> tuple[str, str]:
    if find_nodes(ctx["blocks"], "FAQPage"):
        return STATUS_PASS, "FAQPage JSON-LD present — Q&A content is directly consumable by AI answers."
    return STATUS_FAIL, "No FAQPage JSON-LD — FAQ content (if any) isn't structured for AI answers."


def check_aggregate_rating(ctx) -> tuple[str, str]:
    for b in ctx["blocks"]:
        if "AggregateRating" in node_types(b) or b.get("aggregateRating"):
            return STATUS_PASS, "aggregateRating structured data present — ratings feed the trust signals AI answers surface."
    return STATUS_FAIL, "No aggregateRating markup — star ratings aren't machine-readable."


def check_sameas_identity(ctx) -> tuple[str, str]:
    for o in _org_nodes(ctx):
        same_as = as_list(o.get("sameAs"))
        urls = [str(u) for u in same_as if isinstance(u, str) and u.strip()]
        if urls:
            return STATUS_PASS, f"sameAs links {len(urls)} profile(s): {urls[0][:60]} — identity is verifiable across the web."
    if _org_nodes(ctx):
        return STATUS_PARTIAL, "Organization markup exists but has no sameAs links — social/official profiles aren't tied to the entity."
    return STATUS_FAIL, "No Organization node, so no sameAs identity graph."


def check_nap_consistency(ctx) -> tuple[str, str]:
    name = _org_name(ctx)
    if not name:
        return STATUS_FAIL, "No organization name in structured data to cross-check."
    norm = re.sub(r"[^a-z0-9]", "", name.lower())
    page_norm = re.sub(r"[^a-z0-9]", "", ctx["text_l"])
    if norm and len(norm) > 3 and norm in page_norm:
        return STATUS_PASS, f"Structured name '{name[:50]}' appears in visible page text — NAP is consistent."
    return STATUS_PARTIAL, f"Structured name '{name[:50]}' not found in visible text — possible NAP mismatch."


def check_business_contact(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    has_mail = bool(soup.find("a", href=re.compile(r"^mailto:", re.I)))
    has_tel = bool(soup.find("a", href=re.compile(r"^tel:", re.I)))
    has_page = bool(soup.find("a", href=re.compile(r"contact", re.I)))
    signals = sum([has_mail, has_tel, has_page])
    if signals >= 2:
        return STATUS_PASS, "Contact signals present (email/phone/contact page)."
    if signals == 1:
        return STATUS_PARTIAL, "Only one contact signal found (email, phone, or contact page)."
    return STATUS_FAIL, "No email, phone, or contact-page link detected."


def check_robots_ai_bots(ctx) -> tuple[str, str]:
    fr = ctx["robots"]
    if fr is None:
        return STATUS_UNKNOWN, "robots.txt fetch failed (network error); AI-crawler policy not evaluated."
    if fr.status != 200 or not fr.text.strip():
        return STATUS_PARTIAL, "No robots.txt (HTTP %s) — crawlers default to allow-all, but there is no explicit AI-crawler policy." % fr.status
    rules = parse_robots_rules(fr.text)
    star_blocked = is_fully_disallowed(rules.get("*", set()))
    blocked = []
    for bot in AI_BOTS:
        bot_rules = rules.get(bot, set())
        if is_fully_disallowed(bot_rules) or star_blocked:
            blocked.append(bot)
    if not blocked:
        n = len([b for b in AI_BOTS if b in rules])
        return STATUS_PASS, f"robots.txt allows AI crawlers (checked {len(AI_BOTS)} bot tokens; {n} have explicit rules, none fully disallowed)."
    if len(blocked) >= 4 or star_blocked:
        return STATUS_FAIL, f"robots.txt blocks {len(blocked)} AI crawler(s): {', '.join(blocked[:6])} — AI search can't crawl the site."
    return STATUS_PARTIAL, f"robots.txt disallows some AI crawlers: {', '.join(blocked)}."


def check_llms_txt(ctx) -> tuple[str, str]:
    fr = ctx["llms"]
    if fr is None:
        return STATUS_UNKNOWN, "llms.txt fetch failed (network error); not evaluated."
    if fr.status == 200 and fr.text.strip():
        return STATUS_PASS, f"llms.txt present ({len(fr.text)} bytes). Note: this is optionality, not a proven citation-lift lever."
    return STATUS_FAIL, "No llms.txt — the optional agent-readable summary file is absent."


def check_clean_text_extract(ctx) -> tuple[str, str]:
    wc = ctx["word_count"]
    if wc >= 500:
        return STATUS_PASS, f"{wc} words of extractable body text — enough substance for AI answers to draw on."
    if wc >= 200:
        return STATUS_PARTIAL, f"Only {wc} words of extractable text — thin for citation-worthy content."
    return STATUS_FAIL, f"Only {wc} words extractable — AI systems have almost nothing to cite."


def check_headings_structure(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    h1s = soup.find_all("h1")
    h2s = soup.find_all("h2")
    if len(h1s) == 1 and len(h2s) >= 2:
        return STATUS_PASS, f"Clean heading hierarchy: 1 h1 ('{h1s[0].get_text(strip=True)[:50]}') + {len(h2s)} h2s."
    if h1s:
        return STATUS_PARTIAL, f"{len(h1s)} h1(s), {len(h2s)} h2(s) — hierarchy is imperfect."
    return STATUS_FAIL, "No h1 heading — AI extractors can't identify the page's topic."


def check_no_js_wall(ctx) -> tuple[str, str]:
    text_l = ctx["text_l"]
    for phrase in JS_WALL_PHRASES:
        if phrase in text_l:
            return STATUS_FAIL, f"JS wall detected ('{phrase}') — crawlers see a gate, not content."
    soup = ctx["soup"]
    scripts = len(soup.find_all("script"))
    if ctx["word_count"] < 50 and scripts > 10:
        return STATUS_FAIL, f"Only {ctx['word_count']} words vs {scripts} scripts — likely client-rendered; crawlers may see nothing."
    if ctx["word_count"] < 200:
        return STATUS_PARTIAL, f"Thin server-rendered text ({ctx['word_count']} words) — possible partial JS rendering."
    return STATUS_PASS, f"{ctx['word_count']} words server-rendered, no JS wall — content is crawlable."


def check_meta_description(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    tag = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if tag and tag.get("content", "").strip():
        n = len(tag["content"].strip())
        if 30 <= n <= 300:
            return STATUS_PASS, f"Meta description present ({n} chars) — feeds the summary AI systems show."
        return STATUS_PARTIAL, f"Meta description present but {n} chars (ideal 30-300)."
    return STATUS_FAIL, "No meta description — AI systems invent the summary instead."


def check_sitemap_present(ctx) -> tuple[str, str]:
    fr = ctx["sitemap"]
    if fr is None:
        return STATUS_UNKNOWN, "sitemap.xml fetch failed (network error); not evaluated."
    if fr.status == 200 and fr.text.strip():
        return STATUS_PASS, "sitemap.xml present — AI crawlers can enumerate the site's pages."
    robots = ctx["robots"]
    if robots and robots.status == 200 and "sitemap" in robots.text.lower():
        return STATUS_PARTIAL, "No /sitemap.xml but robots.txt references a sitemap location."
    return STATUS_FAIL, "No sitemap.xml — crawlers must discover pages by guessing."


def check_sitemap_freshness(ctx) -> tuple[str, str]:
    fr = ctx["sitemap"]
    if fr is None or fr.status != 200 or not fr.text.strip():
        return STATUS_UNKNOWN, "No sitemap fetched; freshness not evaluated."
    lastmods = re.findall(r"<lastmod>(.*?)</lastmod>", fr.text, re.I)[:50]
    if not lastmods:
        return STATUS_FAIL, "sitemap.xml has no <lastmod> dates — crawlers can't tell what's fresh."
    now = datetime.now(timezone.utc)
    recent = 0
    for lm in lastmods:
        try:
            dt = datetime.fromisoformat(lm.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (now - dt).days <= 120:
                recent += 1
        except Exception:
            continue
    if recent:
        return STATUS_PASS, f"{recent}/{len(lastmods)} sampled URLs updated within 120 days — the site signals freshness."
    return STATUS_PARTIAL, f"sitemap.xml lists {len(lastmods)} dated URLs but none updated within 120 days."


def check_content_freshness(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    if soup.find("time", attrs={"datetime": True}):
        return STATUS_PASS, "<time datetime> tags present — publish/update dates are machine-readable."
    for prop in ("article:published_time", "article:modified_time", "og:updated_time"):
        if soup.find("meta", attrs={"property": prop}):
            return STATUS_PASS, f"Meta {prop} present — dates are machine-readable."
    text = ctx["text"]
    if FRESH_YEAR_RE.search(text) or "last updated" in ctx["text_l"]:
        return STATUS_PARTIAL, "Freshness hinted in text (recent year / 'updated') but no machine-readable date."
    return STATUS_FAIL, "No dates found — AI systems can't tell if the content is current."


def check_about_page(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    for a in soup.find_all("a", href=True):
        href = a["href"].lower()
        label = a.get_text(" ", strip=True).lower()
        if "/about" in href or href.rstrip("/").endswith("about") or label in ("about", "about us", "our story"):
            return STATUS_PASS, f"About page linked: {a['href'][:80]} — the brand story AI cites exists."
    if "about us" in ctx["text_l"] or "our story" in ctx["text_l"]:
        return STATUS_PARTIAL, "About content mentioned but no about page linked."
    return STATUS_FAIL, "No about page found — AI answers have no brand story to cite."


def check_credentials_signals(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    signals = set()
    if soup.find(attrs={"rel": "author"}) or soup.find("meta", attrs={"name": re.compile(r"author", re.I)}):
        signals.add("author attribution")
    text_l = ctx["text_l"]
    for word in CREDENTIAL_WORDS:
        if word in text_l:
            signals.add(word)
    if len(signals) >= 2:
        return STATUS_PASS, f"Authority signals: {sorted(signals)[:4]} — the brand asserts expertise AI can cite."
    if signals:
        return STATUS_PARTIAL, f"One authority signal: {sorted(signals)[0]}."
    return STATUS_FAIL, "No author attribution or credential signals — expertise is unverifiable."


def check_canonical_https(ctx) -> tuple[str, str]:
    soup = ctx["soup"]
    canon = soup.find("link", attrs={"rel": re.compile(r"canonical", re.I)})
    https = urlparse(ctx["final_url"]).scheme == "https"
    if canon and canon.get("href") and https:
        return STATUS_PASS, "Canonical URL declared and site served over HTTPS — one canonical identity."
    if (canon and canon.get("href")) or https:
        missing = "HTTPS" if not https else "canonical link"
        return STATUS_PARTIAL, f"Only one of canonical/HTTPS present — missing {missing}."
    return STATUS_FAIL, "No canonical link and not HTTPS — identity is fragmented and insecure."


CHECK_FNS = {
    "schema_organization": check_schema_organization,
    "schema_product": check_schema_product,
    "schema_review": check_schema_review,
    "schema_faq": check_schema_faq,
    "aggregate_rating": check_aggregate_rating,
    "sameas_identity": check_sameas_identity,
    "nap_consistency": check_nap_consistency,
    "business_contact": check_business_contact,
    "robots_ai_bots": check_robots_ai_bots,
    "llms_txt": check_llms_txt,
    "clean_text_extract": check_clean_text_extract,
    "headings_structure": check_headings_structure,
    "no_js_wall": check_no_js_wall,
    "meta_description": check_meta_description,
    "sitemap_present": check_sitemap_present,
    "sitemap_freshness": check_sitemap_freshness,
    "content_freshness": check_content_freshness,
    "about_page": check_about_page,
    "credentials_signals": check_credentials_signals,
    "canonical_https": check_canonical_https,
}
assert set(CHECK_FNS) == {cid for cid, _, _ in CHECK_DEFS}


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

RECOMMENDATION_FIXES = {
    "schema_organization": ("Add Organization JSON-LD", "AI answers cite entities, not pages — without Organization markup the brand has no citable identity."),
    "schema_product": ("Add Product JSON-LD", "Products need structured markup to appear as entities in AI shopping answers."),
    "schema_review": ("Add Review JSON-LD", "Individual reviews as structured data feed the social proof AI answers quote."),
    "schema_faq": ("Add FAQPage JSON-LD", "FAQ markup is the most directly consumable format for AI answer engines."),
    "aggregate_rating": ("Add aggregateRating markup", "Machine-readable star ratings are a trust signal AI answers surface."),
    "sameas_identity": ("Add sameAs links to Organization markup", "Tie official profiles to the entity so AI can verify the brand across the web."),
    "nap_consistency": ("Fix NAP consistency", "The structured business name should match the visible site name exactly."),
    "business_contact": ("Publish contact details", "Email/phone/contact page — AI agents treat contactability as a trust input."),
    "robots_ai_bots": ("Allow AI crawlers in robots.txt", "Blocking GPTBot/ClaudeBot/PerplexityBot means AI search literally cannot read the site."),
    "llms_txt": ("Consider publishing /llms.txt", "Optional agent-readable summary; honest note — no proven citation lift, but it's cheap."),
    "clean_text_extract": ("Add substantive crawlable copy", "AI answers cite pages with real substance — 500+ words of real content."),
    "headings_structure": ("Fix heading hierarchy", "One h1 plus descriptive h2s tells AI extractors what each section is about."),
    "no_js_wall": ("Server-render core content", "If crawlers hit a JS wall, the site is invisible no matter how good the content is."),
    "meta_description": ("Write a meta description", "It's the summary AI systems fall back to when describing the page."),
    "sitemap_present": ("Publish sitemap.xml", "Lets AI crawlers enumerate pages instead of discovering them by luck."),
    "sitemap_freshness": ("Add <lastmod> to the sitemap", "Freshness dates tell crawlers what's new and worth re-reading."),
    "content_freshness": ("Add machine-readable dates", "<time datetime> or article:published_time so AI knows the content is current."),
    "about_page": ("Publish an about page", "AI answers need a brand story to cite — give them one."),
    "credentials_signals": ("Show expertise signals", "Author bylines, certifications, awards — verifiable expertise gets cited."),
    "canonical_https": ("Set canonical URL + HTTPS", "One canonical secure identity; fragments split the entity AI sees."),
}


def build_report(url: str, final_url: str, checks: list[Check],
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
            "engine": ENGINE_VERSION,
        },
    }


def _run_checks(ctx: dict) -> list[Check]:
    checks = []
    for cid, cat, w in CHECK_DEFS:
        try:
            status, evidence = CHECK_FNS[cid](ctx)
        except Exception as exc:  # a check must never kill the run
            status, evidence = STATUS_UNKNOWN, f"Check errored and was not evaluated: {exc}"
        checks.append(Check(id=cid, category=cat, weight=w, status=status, evidence=evidence))
    return checks


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def audit_geo(url: str) -> dict:
    """Full paid GEO audit. Returns the complete report dict. Never raises."""
    t_start = time.time()
    deadline = t_start + OVERALL_DEADLINE
    url = normalize_url(url)
    ctx = _build_ctx(url, deadline)
    if ctx is None:
        checks = [Check(id=cid, category=cat, weight=w, status=STATUS_UNKNOWN,
                        evidence="Site unreachable or returned no content; check not evaluated.")
                  for cid, cat, w in CHECK_DEFS]
        return build_report(url, url, checks, t_start, unreachable=True)
    return build_report(url, ctx["final_url"], _run_checks(ctx), t_start)


def audit_geo_lite(url: str) -> dict:
    """Free lite audit: top-line score + grade only.

    Runs the same 20 checks but strips per-check evidence and detailed
    recommendations — the funnel for the paid full audit. Never raises.
    """
    t_start = time.time()
    try:
        full = audit_geo(url)
    except Exception as exc:
        return {"url": url, "error": f"Audit failed: {exc}",
                "graded_at": datetime.now(timezone.utc).isoformat()}
    if full.get("unreachable"):
        return {
            "url": full["url"], "final_url": full["final_url"],
            "graded_at": full["graded_at"],
            "score": 0.0, "grade": "F", "unreachable": True,
            "note": "Site unreachable — no audit possible.",
        }
    by_status: dict[str, int] = {"pass": 0, "partial": 0, "fail": 0, "unknown": 0}
    for c in full["checks"]:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    top_gaps = [r["title"] for r in full["recommendations"]]
    return {
        "url": full["url"],
        "final_url": full["final_url"],
        "graded_at": full["graded_at"],
        "score": full["score"],
        "grade": full["grade"],
        "checks_summary": by_status,
        "top_gaps": top_gaps,
        "note": ("Free lite audit: score + grade only. The full audit_geo report "
                 "($1.50/call) adds per-check evidence and prioritized fixes."),
    }


__all__ = ["audit_geo", "audit_geo_lite", "CHECK_DEFS", "CHECK_FNS",
           "build_report", "compute_score", "ENGINE_VERSION"]
