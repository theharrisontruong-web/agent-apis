"""Product feed hygiene scan engine (Google Shopping / Merchant Center).

Given a product feed URL, fetches the feed server-side, auto-detects the
format (Google-style RSS/Atom XML, CSV, or JSON), parses the items, and
runs 20 weighted checks across parse integrity, required attributes,
identifiers, content quality, pricing, taxonomy/shipping, and data
integrity — producing a 0-100 score, an A-F grade, a disapproval-risk
level, per-check evidence, the top 3 prioritized fixes, and a sample of
the worst-offending items with concrete fixes, as machine-readable JSON.

What this audits: the feed-level errors behind Google Merchant Center
disapprovals — missing required attributes, invalid/missing GTINs,
malformed prices, bad availability values, broken image links, duplicate
IDs. A disapproved feed means the products don't serve on Google
Shopping at all, and agent-commerce surfaces (UCP references Merchant
Center product IDs) inherit the same dirt.

What this does NOT do: verify feed values against the merchant's landing
pages (price/availability mismatch needs per-page crawling), check
account-level Merchant Center settings (shipping/tax can live there —
the feed-level check says exactly that), or guarantee approval. Checks
that cannot be evaluated return "unknown", never guesses.

Fetch limits (stated in every report): the feed document is capped at
8 MB and a 20s fetch timeout inside a 75s overall deadline; at most
20,000 items are analyzed; image links are spot-checked (8 unique URLs,
HEAD with a headers-only GET fallback). Shared fetch/check/scoring
primitives come from common.auditlib.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from urllib.parse import urlparse

import requests

# Make the shared lib importable both locally (repo layout) and in Docker
# (WORKDIR layout: /srv/feed with ./common copied in).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.auditlib import (  # noqa: E402
    OVERALL_DEADLINE,
    STATUS_FAIL,
    STATUS_PARTIAL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    USER_AGENT,
    Check,
    compute_score,
    host_of,
    normalize_url,
)

ENGINE_VERSION = "feed-audit/1.0"

DISCLAIMER = (
    "Automated feed-level scan only — not a Google Merchant Center approval "
    "guarantee. This scan validates the feed document itself (structure, "
    "required attributes, identifiers, price and availability values); it "
    "does NOT crawl landing pages, so feed-vs-page price/availability "
    "mismatches and account-level settings (shipping, tax, promotions) are "
    "outside its scope."
)

# Feed fetch limits — deliberately larger than a web page's, stated openly.
MAX_FEED_BYTES = 8_000_000
FEED_FETCH_TIMEOUT = 20
MAX_ITEMS_ANALYZED = 20_000
IMAGE_SPOT_CHECKS = 8

# The failures that map directly onto Merchant Center disapproval reasons.
DISAPPROVAL_CHECKS = [
    "parse_validity", "id_present", "title_present", "link_present",
    "price_present", "availability_values", "image_link_present",
    "gtin_mpn_coverage", "price_format",
]


# ---------------------------------------------------------------------------
# Field model: canonical name -> accepted source names (lowercased,
# namespace prefix and separators stripped at match time)
# ---------------------------------------------------------------------------

def _norm_key(k: str) -> str:
    k = (k or "").strip().lower()
    k = k.split("}")[-1]            # XML namespace {ns}title
    if ":" in k:
        k = k.split(":")[-1]        # g:title / google:title
    return re.sub(r"[\s\-]+", "_", k)


FIELD_ALIASES: dict[str, set[str]] = {
    "id": {"id", "item_id", "sku", "product_id", "itemid"},
    "title": {"title", "name", "product_title", "product_name"},
    "description": {"description", "desc", "product_description"},
    "link": {"link", "url", "product_url", "permalink", "item_url"},
    "image_link": {"image_link", "image", "image_url", "img_url",
                   "picture", "picture_url", "main_image"},
    "additional_image_link": {"additional_image_link", "additional_image",
                              "extra_image_link"},
    "price": {"price", "list_price", "regular_price"},
    "sale_price": {"sale_price", "saleprice", "discounted_price",
                   "special_price"},
    "availability": {"availability", "stock", "stock_status",
                     "inventory_status", "in_stock"},
    "brand": {"brand", "manufacturer", "vendor"},
    "gtin": {"gtin", "upc", "ean", "isbn", "jan", "gtin13", "gtin14"},
    "mpn": {"mpn", "manufacturer_part_number", "mpn_code"},
    "identifier_exists": {"identifier_exists", "identifierexists",
                          "identifier_exist"},
    "condition": {"condition", "item_condition"},
    "google_product_category": {"google_product_category", "gpc",
                                "google_category", "google_taxonomy"},
    "product_type": {"product_type", "merchant_category", "category_path"},
    "item_group_id": {"item_group_id", "itemgroupid", "group_id",
                      "parent_id", "parent_sku"},
    "shipping": {"shipping", "shipping_price", "shipping_cost"},
    "tax": {"tax", "tax_rate", "tax_category"},
    "color": {"color", "colour"},
    "size": {"size"},
    "material": {"material"},
    "pattern": {"pattern"},
}
_ALIAS_TO_CANON: dict[str, str] = {}
for _canon, _names in FIELD_ALIASES.items():
    for _n in _names:
        _ALIAS_TO_CANON[_n] = _canon
    _ALIAS_TO_CANON[_canon] = _canon

VALID_AVAILABILITY = {
    "in stock", "in_stock", "instock",
    "out of stock", "out_of_stock", "outofstock",
    "preorder", "pre_order", "backorder", "back_order",
    "limited availability", "limited_availability",
    "discontinued", "yes", "no", "true", "false", "1", "0",
}


# ---------------------------------------------------------------------------
# Check definitions (weights sum to 100)
# ---------------------------------------------------------------------------

CHECK_DEFS: list[tuple[str, str, int]] = [
    # parsing (12)
    ("parse_validity", "parsing", 7),
    ("encoding_sanity", "parsing", 5),
    # required fields (30)
    ("id_present", "required_fields", 5),
    ("title_present", "required_fields", 5),
    ("link_present", "required_fields", 5),
    ("price_present", "required_fields", 6),
    ("availability_values", "required_fields", 4),
    ("image_link_present", "required_fields", 5),
    # identifiers (14)
    ("gtin_mpn_coverage", "identifiers", 6),
    ("brand_coverage", "identifiers", 4),
    ("identifier_exists_sanity", "identifiers", 4),
    # content quality (18)
    ("title_quality", "content_quality", 7),
    ("description_quality", "content_quality", 5),
    ("image_link_quality", "content_quality", 6),
    # pricing (12)
    ("price_format", "pricing", 6),
    ("sale_price_logic", "pricing", 6),
    # taxonomy & shipping (8)
    ("taxonomy_presence", "taxonomy_shipping", 4),
    ("shipping_tax_presence", "taxonomy_shipping", 4),
    # integrity (6)
    ("duplicate_ids", "integrity", 3),
    ("variant_grouping", "integrity", 3),
]
assert sum(w for _, _, w in CHECK_DEFS) == 100


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def _fetch_feed(url: str, deadline: float) -> dict | None:
    """Fetch the feed document with feed-sized limits. None on failure."""
    if time.time() > deadline:
        return None
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/xml,text/csv,application/json,*/*"},
            timeout=FEED_FETCH_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        body = b""
        truncated = False
        for chunk in resp.iter_content(chunk_size=65536):
            body += chunk
            if len(body) > MAX_FEED_BYTES:
                body = body[:MAX_FEED_BYTES]
                truncated = True
                break
        try:
            text = body.decode("utf-8-sig")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = body.decode("utf-8", errors="replace")
            encoding = "utf-8-with-errors"
        return {
            "final_url": str(resp.url),
            "status": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
            "text": text,
            "byte_len": len(body),
            "truncated": truncated,
            "encoding": encoding,
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Parsing — XML / CSV / JSON -> list of canonical-field item dicts
# ---------------------------------------------------------------------------

def _canon_item(raw: dict) -> dict:
    item: dict[str, str] = {}
    present: set[str] = set()
    for key, value in raw.items():
        canon = _ALIAS_TO_CANON.get(_norm_key(str(key)))
        if canon is None:
            continue
        present.add(canon)
        if value is None:
            continue
        if isinstance(value, dict):
            # e.g. JSON {"price": {"amount": 19.99, "currency": "USD"}}
            amount = value.get("amount", value.get("value", value.get("price")))
            currency = value.get("currency", value.get("currency_code", ""))
            text = " ".join(str(x) for x in (amount, currency)
                            if x is not None).strip()
        elif isinstance(value, (list, tuple)):
            text = ", ".join(str(v) for v in value if v is not None).strip()
        else:
            text = str(value).strip()
        if text and canon not in item:
            item[canon] = text
    item["_present"] = present  # type: ignore[dict-item]
    return item


def _detect_format(text: str, content_type: str = "") -> str:
    stripped = text.lstrip("\ufeff \t\r\n")
    ct = (content_type or "").lower()
    if stripped.startswith("<"):
        return "xml"
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    if "json" in ct:
        return "json"
    if "xml" in ct:
        return "xml"
    return "csv"


_ITEM_TAGS = {"item", "entry", "product"}


def _parse_xml(text: str) -> tuple[list[dict], str | None]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        return [], f"XML parse error: {exc}"
    item_els = [el for el in root.iter()
                if isinstance(el.tag, str)
                and el.tag.split("}")[-1].lower() in _ITEM_TAGS]
    if not item_els and isinstance(root.tag, str) \
            and root.tag.split("}")[-1].lower() in _ITEM_TAGS:
        item_els = [root]
    items = []
    for el in item_els:
        parents = {c: p for p in el.iter() for c in p}

        def _inside_block(node) -> bool:
            # nested children of <shipping>/<tax> blocks (country, price…)
            # are not item fields — the block itself is what counts.
            seen = set()
            cur = parents.get(node)
            while cur is not None and cur is not el and id(cur) not in seen:
                seen.add(id(cur))
                if isinstance(cur.tag, str) and \
                        cur.tag.split("}")[-1].lower() in ("shipping", "tax"):
                    return True
                cur = parents.get(cur)
            return False

        raw: dict[str, object] = {}
        for node in el.iter():
            if node is el or not isinstance(node.tag, str):
                continue
            if _inside_block(node):
                continue
            lname = node.tag.split("}")[-1].lower()
            value = (node.text or "").strip()
            if lname == "link" and not value and node.get("href"):
                href = node.get("href") or ""
                rel = (node.get("rel") or "alternate").lower()
                if rel in ("alternate", "") or "link" not in raw:
                    value = href
            if lname not in raw:
                raw[lname] = value
            elif not raw[lname] and value:
                raw[lname] = value
        items.append(_canon_item(raw))
    if not items:
        return [], "XML parsed but no <item>/<entry>/<product> elements found."
    return items, None


def _parse_csv(text: str) -> tuple[list[dict], str | None]:
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            return [], "CSV has no header row."
        known = [f for f in reader.fieldnames
                 if _norm_key(f or "") in _ALIAS_TO_CANON]
        if reader.fieldnames and not known:
            return [], ("CSV header row contains no recognizable product-feed "
                        f"fields (saw: {', '.join((f or '?') for f in reader.fieldnames[:8])}).")
        items = [_canon_item({k: v for k, v in row.items() if k is not None})
                 for row in reader]
    except csv.Error as exc:
        return [], f"CSV parse error: {exc}"
    items = [i for i in items if i["_present"]]
    if not items:
        return [], "CSV parsed but contained no data rows."
    return items, None


def _parse_json(text: str) -> tuple[list[dict], str | None]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [], f"JSON parse error: {exc}"
    rows = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        for key in ("products", "items", "data", "feed", "results",
                    "entries", "product"):
            value = data.get(key)
            if isinstance(value, list):
                rows = value
                break
            if isinstance(value, dict):
                rows = [value]
                break
        if rows is None:
            rows = [data]  # a single product object
    if not rows:
        return [], "JSON parsed but contained no product list."
    items = [_canon_item(r) for r in rows if isinstance(r, dict)]
    items = [i for i in items if i["_present"]]
    if not items:
        return [], "JSON product objects carry no recognizable feed fields."
    return items, None


def parse_feed(text: str, content_type: str = "") -> tuple[str, list[dict], str | None]:
    """Auto-detect format and parse. Returns (format, items, error)."""
    fmt = _detect_format(text, content_type)
    if fmt == "xml":
        items, err = _parse_xml(text)
        if err and "parse error" in err:
            # a truncated or mislabelled document sometimes still yields to JSON
            alt_items, alt_err = _parse_json(text)
            if alt_err is None:
                return "json", alt_items, None
    elif fmt == "json":
        items, err = _parse_json(text)
    else:
        items, err = _parse_csv(text)
        if err:
            alt_items, alt_err = _parse_xml(text)
            if alt_err is None:
                return "xml", alt_items, None
    return fmt, items, err


# ---------------------------------------------------------------------------
# Context building
# ---------------------------------------------------------------------------

def _build_ctx(url: str, deadline: float) -> dict | None:
    """Fetch + parse the feed. None if the URL is unreachable."""
    got = _fetch_feed(url, deadline)
    if got is None or got["status"] >= 400 or not got["text"].strip():
        return None
    fmt, items, parse_error = parse_feed(got["text"], got["content_type"])
    total_items = len(items)
    items = items[:MAX_ITEMS_ANALYZED]
    return {
        "url": url, "final_url": got["final_url"], "host": host_of(url),
        "deadline": deadline, "format": fmt, "items": items,
        "total_items": total_items, "parse_error": parse_error,
        "text": got["text"], "byte_len": got["byte_len"],
        "truncated": got["truncated"], "encoding": got["encoding"],
        "image_results": None,
    }


# ---------------------------------------------------------------------------
# Helpers shared by checks
# ---------------------------------------------------------------------------

_BLOCK_FIELDS = {"shipping", "tax"}


def _present(item: dict, field: str) -> bool:
    if field not in item.get("_present", set()):
        return False
    # container blocks (XML <shipping><country/>…) carry no text of their
    # own — being present in the item is enough.
    return bool(item.get(field)) or field in _BLOCK_FIELDS


def _coverage(items: list[dict], field: str) -> float:
    if not items:
        return 0.0
    return sum(1 for i in items if _present(i, field)) / len(items)


def _coverage_status(cov: float, full: float = 1.0, partial: float = 0.9) -> str:
    if cov >= full:
        return STATUS_PASS
    if cov >= partial:
        return STATUS_PARTIAL
    return STATUS_FAIL


_PRICE_RE = re.compile(
    r"^\s*(?:[A-Z]{3}\s*)?\$?\s*(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{1,2}))?"
    r"\s*(?:[A-Z]{3})?\s*$")
_CURRENCY_RE = re.compile(r"\b([A-Z]{3})\b")


def _parse_price(raw: str) -> tuple[float, str | None] | None:
    """Parse '19.99 USD' / 'USD 19.99' / '$19.99' -> (amount, currency|None)."""
    if raw is None:
        return None
    s = str(raw).strip()
    m = _PRICE_RE.match(s)
    if not m:
        return None
    try:
        amount = float(m.group(1).replace(",", "")
                       + ("." + m.group(2) if m.group(2) else ""))
    except ValueError:
        return None
    cur = _CURRENCY_RE.search(s.upper())
    return amount, (cur.group(1) if cur else None)


def _gtin_valid(gtin: str) -> bool | None:
    """Check-digit validation. None if not a plausible GTIN shape."""
    digits = re.sub(r"\D", "", gtin or "")
    if len(digits) not in (8, 12, 13, 14):
        return None
    total = 0
    for idx, ch in enumerate(reversed(digits[:-1])):
        total += int(ch) * (3 if idx % 2 == 0 else 1)
    check = (10 - (total % 10)) % 10
    return check == int(digits[-1])


def _spot_check_images(urls: list[str], deadline: float) -> dict[str, int | None]:
    """HTTP status per unique image URL (HEAD, headers-only GET fallback).
    None = unreachable/error. Only checks what it is given (callers cap)."""
    results: dict[str, int | None] = {}
    session = requests.Session()
    for u in urls:
        if time.time() > deadline:
            results[u] = None
            continue
        status = None
        try:
            r = session.head(u, headers={"User-Agent": USER_AGENT},
                             timeout=5, allow_redirects=True)
            status = r.status_code
            if status in (400, 403, 405, 501):
                r = session.get(u, headers={"User-Agent": USER_AGENT},
                                timeout=5, allow_redirects=True, stream=True)
                status = r.status_code
                r.close()
        except Exception:
            status = None
        results[u] = status
    return results


def _image_results(ctx: dict) -> dict[str, int | None]:
    if ctx.get("image_results") is not None:
        return ctx["image_results"]
    urls: list[str] = []
    for item in ctx["items"]:
        u = item.get("image_link") or ""
        if u.startswith("http") and u not in urls:
            urls.append(u)
        if len(urls) >= IMAGE_SPOT_CHECKS:
            break
    ctx["image_results"] = _spot_check_images(urls, ctx["deadline"])
    return ctx["image_results"]


def _no_items() -> tuple[str, str]:
    return STATUS_FAIL, "Feed yielded no parseable items — attribute checks cannot pass without items (see parse_validity)."


# ---------------------------------------------------------------------------
# Individual checks — each returns (status, evidence)
# ---------------------------------------------------------------------------

def check_parse_validity(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if ctx["parse_error"]:
        return STATUS_FAIL, (f"Feed could not be parsed as {ctx['format']}: "
                             f"{ctx['parse_error']} An unparseable feed is "
                             "rejected by Merchant Center wholesale — every "
                             "product in it is disapproved.")
    if not items:
        return STATUS_FAIL, "Feed fetched but contained zero product items."
    note = f"Parsed as {ctx['format']}: {ctx['total_items']} item(s) extracted"
    if ctx["truncated"]:
        note += (f"; WARNING: the document exceeded the {MAX_FEED_BYTES:,}-byte "
                 "scan cap and was truncated — items past the cut were not audited")
    if ctx["total_items"] > MAX_ITEMS_ANALYZED:
        note += f"; only the first {MAX_ITEMS_ANALYZED:,} were analyzed"
    return STATUS_PASS, note + "."


def check_encoding_sanity(ctx) -> tuple[str, str]:
    text = ctx["text"]
    problems = []
    n_repl = text.count("\ufffd")
    if n_repl:
        problems.append(f"{n_repl} Unicode replacement character(s) (U+FFFD) — "
                        "mojibake that lands verbatim in product titles")
    ctrl = sum(1 for ch in text if ord(ch) < 0x20 and ch not in "\t\n\r")
    if ctrl:
        problems.append(f"{ctrl} raw control character(s) (invalid in XML 1.0 and "
                        "a common silent feed-rejection cause)")
    n_double = text.count("&amp;amp;") + text.count("&amp;lt;") + text.count("&amp;gt;")
    if n_double:
        problems.append(f"{n_double} double-encoded entities (&amp;amp; style) — "
                        "titles will render with a literal '&amp;' in them")
    mojibake = len(re.findall(r"Ã[\x80-\xbf©®]", text))
    if mojibake:
        problems.append(f"{mojibake} likely UTF-8-as-Latin-1 mojibake sequence(s) (Ã©-style)")
    if ctx["encoding"] != "utf-8":
        problems.append("document was not clean UTF-8 and needed error-tolerant decoding")
    if not problems:
        return STATUS_PASS, "No replacement characters, control characters, double-encoded entities, or mojibake patterns detected in the raw feed document."
    if n_repl or ctrl:
        return STATUS_FAIL, "; ".join(problems) + "."
    return STATUS_PARTIAL, "; ".join(problems) + "."


def _field_check(ctx, field: str, label: str, why: str) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    cov = _coverage(items, field)
    missing = [i for i in items if not _present(i, field)]
    status = _coverage_status(cov)
    if status == STATUS_PASS:
        return STATUS_PASS, f"All {len(items)} items carry {label}."
    ex = (missing[0].get("id") or missing[0].get("title") or "?")[:40]
    return status, (f"{len(missing)}/{len(items)} items are missing {label} "
                    f"(first: item '{ex}') — {why}")


def check_id_present(ctx) -> tuple[str, str]:
    return _field_check(ctx, "id", "an id",
                        "id is a required attribute; items without one are disapproved on submission.")


def check_title_present(ctx) -> tuple[str, str]:
    return _field_check(ctx, "title", "a title",
                        "title is required; Google cannot list a product it cannot name.")


def check_link_present(ctx) -> tuple[str, str]:
    return _field_check(ctx, "link", "a landing-page link",
                        "link is required; a product with nowhere to send the click is disapproved.")


def check_price_present(ctx) -> tuple[str, str]:
    return _field_check(ctx, "price", "a price",
                        "price is required for Shopping ads; missing prices are an instant disapproval.")


def check_image_link_present(ctx) -> tuple[str, str]:
    return _field_check(ctx, "image_link", "an image_link",
                        "image_link is required; imageless products are disapproved and cannot serve Shopping ads.")


def check_availability_values(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    cov = _coverage(items, "availability")
    if cov == 0:
        return STATUS_FAIL, ("No item carries availability — it is a required "
                             "attribute; Merchant Center disapproves items "
                             "without it instead of guessing stock state.")
    with_av = [i for i in items if _present(i, "availability")]
    invalid = [i for i in with_av
               if re.sub(r"\s+", " ", i["availability"].strip().lower())
               not in VALID_AVAILABILITY]
    missing_n = len(items) - len(with_av)
    if invalid:
        ex = invalid[0]["availability"][:30]
        if len(invalid) / len(items) > 0.10 or missing_n:
            return STATUS_FAIL, (f"{len(invalid)} item(s) carry unrecognized availability "
                                 f"values (e.g. '{ex}') and {missing_n} carry none — "
                                 "Google accepts only in stock / out of stock / "
                                 "preorder / backorder; anything else reads as missing.")
        return STATUS_PARTIAL, (f"{len(invalid)} item(s) carry unrecognized availability "
                                f"values (e.g. '{ex}') — normalize to Google's accepted set.")
    if missing_n:
        return STATUS_PARTIAL, (f"All present availability values are valid, but "
                                f"{missing_n}/{len(items)} items omit availability entirely.")
    return STATUS_PASS, (f"All {len(items)} items carry availability with values in "
                         "Google's accepted set (in stock / out of stock / preorder / backorder).")


def check_gtin_mpn_coverage(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    have, exempt, invalid_gtin = 0, 0, 0
    for i in items:
        gtin_ok = False
        if _present(i, "gtin"):
            v = _gtin_valid(i["gtin"])
            if v is True:
                gtin_ok = True
            elif v is False:
                invalid_gtin += 1
        if gtin_ok or _present(i, "mpn"):
            have += 1
        elif (_present(i, "identifier_exists")
              and i["identifier_exists"].strip().lower() in ("false", "no", "0")):
            exempt += 1
    covered = have + exempt
    cov = covered / len(items)
    detail = (f"{have}/{len(items)} items carry a valid GTIN (check-digit verified) "
              f"or an MPN; {exempt} claim identifier_exists=no")
    if invalid_gtin:
        detail += f"; {invalid_gtin} GTIN(s) FAIL the check digit (typo'd or fabricated identifiers — a named disapproval reason)"
    if cov >= 0.95 and not invalid_gtin:
        return STATUS_PASS, detail + "."
    if cov >= 0.80:
        return STATUS_PARTIAL, detail + ". Missing unique product identifiers limit Shopping reach and trigger limited-performance warnings."
    return STATUS_FAIL, detail + ". Most items lack GTIN/MPN — Google restricts or disapproves products without unique identifiers when the brand assigns them."


def check_brand_coverage(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    cov = _coverage(items, "brand")
    status = _coverage_status(cov, full=0.99, partial=0.85)
    if status == STATUS_PASS:
        return STATUS_PASS, f"Brand present on {cov:.0%} of items — brand is required alongside GTIN/MPN for manufacturer products."
    return status, (f"Brand present on only {cov:.0%} of items — brand is a required "
                    "attribute for new manufactured products and the key Google "
                    "uses to match items to its catalog.")


def check_identifier_exists_sanity(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    misuse, declared = [], 0
    for i in items:
        if not _present(i, "identifier_exists"):
            continue
        declared += 1
        val = i["identifier_exists"].strip().lower()
        if val in ("false", "no", "0"):
            if _present(i, "gtin") or _present(i, "mpn"):
                misuse.append(("claims identifier_exists=no while submitting a GTIN/MPN", i))
            elif _present(i, "brand"):
                misuse.append(("claims identifier_exists=no on a branded product (brands almost always assign GTINs)", i))
        elif val in ("true", "yes", "1"):
            if not _present(i, "gtin") and not _present(i, "mpn"):
                misuse.append(("identifier_exists=yes but no GTIN or MPN actually submitted", i))
    if not declared:
        return STATUS_UNKNOWN, "No item declares identifier_exists — not evaluated. (Only needed when an item has no GTIN/MPN; absence is valid when identifiers are present.)"
    if misuse:
        ex_id = (misuse[0][1].get("id") or "?")[:30]
        if len(misuse) / max(declared, 1) > 0.25:
            return STATUS_FAIL, (f"{len(misuse)}/{declared} identifier_exists declarations are "
                                 f"self-contradictory (first: item '{ex_id}' {misuse[0][0]}) — "
                                 "Google treats this as a data-quality violation and it can "
                                 "suppress the whole account's identifier matching.")
        return STATUS_PARTIAL, (f"{len(misuse)}/{declared} identifier_exists declarations look "
                                f"wrong (first: item '{ex_id}' {misuse[0][0]}).")
    return STATUS_PASS, f"All {declared} identifier_exists declaration(s) are consistent with the identifiers actually submitted."


def check_title_quality(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    titled = [i for i in items if _present(i, "title")]
    if not titled:
        return STATUS_FAIL, "No titles to evaluate (see title_present)."
    short, stuffed, caps, overlong = [], [], [], []
    for i in titled:
        t = i["title"]
        words = re.findall(r"[A-Za-z0-9']+", t.lower())
        if len(t) < 10:
            short.append(i)
        if len(t) > 150:
            overlong.append(i)
        letters = [c for c in t if c.isalpha()]
        if len(letters) >= 10 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
            caps.append(i)
        counts = Counter(w for w in words if len(w) > 2)
        if (counts and counts.most_common(1)[0][1] >= 4) or t.count("|") >= 3 \
                or t.count("!") >= 2:
            stuffed.append(i)
    flagged = len({id(x) for x in short + stuffed + caps + overlong})
    ratio = flagged / len(titled)
    bits = []
    if short:
        bits.append(f"{len(short)} under 10 characters (too thin to match search queries)")
    if stuffed:
        bits.append(f"{len(stuffed)} keyword-stuffed (repeated words / pipe spam / !!!)")
    if caps:
        bits.append(f"{len(caps)} in ALL CAPS (reads as shouting; Google may rewrite or suppress)")
    if overlong:
        bits.append(f"{len(overlong)} over 150 characters (truncated in results)")
    if not bits:
        med = int(median(len(i["title"]) for i in titled))
        return STATUS_PASS, f"{len(titled)} titles are clean: no stuffing, no ALL-CAPS, none under 10 or over 150 chars (median length {med})."
    if ratio > 0.25:
        return STATUS_FAIL, f"{flagged}/{len(titled)} titles have quality problems: " + "; ".join(bits) + "."
    return STATUS_PARTIAL, f"{flagged}/{len(titled)} titles have quality problems: " + "; ".join(bits) + "."


def check_description_quality(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    cov = _coverage(items, "description")
    if cov == 0:
        return STATUS_FAIL, ("No item carries a description — description is a required "
                             "attribute for most categories and the main text AI/search "
                             "surfaces mine for product facts.")
    descs = [i["description"] for i in items if _present(i, "description")]
    med = int(median(len(d) for d in descs))
    thin = sum(1 for d in descs if len(d) < 50)
    if cov >= 0.99 and med >= 100:
        return STATUS_PASS, f"Descriptions on {cov:.0%} of items with real substance (median {med} chars)."
    if cov >= 0.90 and thin / len(descs) <= 0.25:
        return STATUS_PARTIAL, (f"Descriptions on {cov:.0%} of items (median {med} chars) — "
                                "present, but thin copy limits matching and AI citation.")
    return STATUS_PARTIAL if cov >= 0.5 else STATUS_FAIL, (
        f"Descriptions on only {cov:.0%} of items; {thin} of those are under 50 chars "
        f"(median {med}) — sparse descriptions starve both Shopping matching and "
        "agent-commerce answers.")


def check_image_link_quality(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    with_img = [i for i in items if _present(i, "image_link")]
    if not with_img:
        return STATUS_FAIL, "No image links to check (see image_link_present)."
    insecure = [i for i in with_img
                if not i["image_link"].lower().startswith("https://")]
    malformed = [i for i in with_img
                 if not re.match(r"^https?://[^\s/]+\.[^\s/]+/", i["image_link"])]
    results = _image_results(ctx)
    checked = [u for u, s in results.items() if s is not None]
    ok = [u for u, s in results.items() if s == 200]
    broken = [u for u, s in results.items() if s is not None and s != 200]
    bits = []
    if insecure:
        bits.append(f"{len(insecure)} image URL(s) are plain http (Google requires working https for Shopping)")
    if malformed:
        bits.append(f"{len(malformed)} image URL(s) are malformed")
    if broken:
        bits.append(f"{len(broken)}/{len(checked)} spot-checked image URLs returned non-200 "
                    f"(e.g. {broken[0][:60]} -> {results[broken[0]]})")
    if results and not checked and not bits:
        return STATUS_UNKNOWN, ("Image URLs are well-formed, but none of the "
                                f"{len(results)} spot-checked image host(s) could be "
                                "reached inside the scan deadline — liveness not evaluated.")
    if not results:
        bits.append("image spot-checks could not run inside the scan deadline")
    if not bits:
        return STATUS_PASS, (f"Image URLs are well-formed https; all {len(ok)} spot-checked "
                             f"images (of {len(with_img)} in the feed) return HTTP 200.")
    if broken and len(broken) / max(len(checked), 1) >= 0.5:
        return STATUS_FAIL, "; ".join(bits) + ". Dead product images are an automatic disapproval."
    return STATUS_PARTIAL, "; ".join(bits) + "."


def check_price_format(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    priced = [i for i in items if _present(i, "price")]
    if not priced:
        return STATUS_FAIL, "No prices to validate (see price_present)."
    unparsed, no_currency, nonpositive = [], [], []
    currencies: Counter = Counter()
    for i in priced:
        parsed = _parse_price(i["price"])
        if parsed is None:
            unparsed.append(i)
            continue
        amount, cur = parsed
        if amount <= 0:
            nonpositive.append(i)
        if cur:
            currencies[cur] += 1
        else:
            no_currency.append(i)
    bits = []
    if unparsed:
        bits.append(f"{len(unparsed)} price(s) don't parse as an amount "
                    f"(e.g. '{unparsed[0]['price'][:30]}') — expected '19.99 USD' style")
    if nonpositive:
        bits.append(f"{len(nonpositive)} price(s) are zero or negative")
    if len(currencies) > 1:
        bits.append(f"mixed currencies across the feed ({', '.join(currencies)}) — "
                    "one feed must use one currency")
    if not bits:
        extra = ""
        if no_currency:
            extra = (f" Note: {len(no_currency)} price(s) omit the currency code — "
                     "Merchant Center falls back to the account's target country, "
                     "but explicit 'amount USD' is unambiguous for agent consumers.")
        if len(no_currency) / len(priced) > 0.5:
            return STATUS_PARTIAL, (f"All {len(priced)} prices parse and are positive, "
                                    f"but {len(no_currency)} omit a currency code." + extra)
        cur = currencies.most_common(1)[0][0] if currencies else "?"
        return STATUS_PASS, f"All {len(priced)} prices parse as amount + currency ({cur}), positive, single-currency feed." + extra
    if len(unparsed) / len(priced) > 0.10 or nonpositive or len(currencies) > 1:
        return STATUS_FAIL, "; ".join(bits) + ". Malformed prices are disapproved as 'invalid price' without review."
    return STATUS_PARTIAL, "; ".join(bits) + "."


def check_sale_price_logic(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    on_sale = [i for i in items if _present(i, "sale_price")]
    if not on_sale:
        return STATUS_UNKNOWN, "No item uses sale_price — optional attribute; nothing to validate. (If items ARE on sale on the landing page but not in the feed, that mismatch is a separate disapproval class this scan can't see.)"
    bad = []
    for i in on_sale:
        price = _parse_price(i.get("price", ""))
        sale = _parse_price(i["sale_price"])
        if price is None or sale is None:
            bad.append((i, "sale_price (or base price) doesn't parse"))
        elif sale[0] >= price[0]:
            bad.append((i, f"sale_price {i['sale_price'][:20]} is not below price {i['price'][:20]}"))
    if bad:
        ex_i, ex_why = bad[0]
        if len(bad) / len(on_sale) > 0.10:
            return STATUS_FAIL, (f"{len(bad)}/{len(on_sale)} sale prices are invalid "
                                 f"(first: item '{(ex_i.get('id') or '?')[:30]}' — {ex_why}). "
                                 "A sale_price at or above the base price is rejected and "
                                 "can flag the item's price as misleading.")
        return STATUS_PARTIAL, (f"{len(bad)}/{len(on_sale)} sale prices are invalid "
                                f"(first: {ex_why}).")
    return STATUS_PASS, f"All {len(on_sale)} sale_price values parse and sit strictly below their base price."


def check_taxonomy_presence(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    gpc = _coverage(items, "google_product_category")
    ptype = _coverage(items, "product_type")
    best = max(gpc, ptype)
    if best >= 0.95:
        which = "google_product_category" if gpc >= ptype else "product_type"
        return STATUS_PASS, f"Taxonomy present: {which} on {best:.0%} of items — categorization drives feed rules, bidding segments, and agent product understanding."
    if best >= 0.5:
        return STATUS_PARTIAL, (f"Taxonomy is patchy (google_product_category {gpc:.0%}, "
                                f"product_type {ptype:.0%}) — Google auto-categorizes the "
                                "gaps, often wrong, which misroutes items in browse and AI answers.")
    return STATUS_FAIL, (f"Almost no taxonomy (google_product_category {gpc:.0%}, "
                         f"product_type {ptype:.0%}) — uncategorized items get "
                         "auto-assigned categories and lose category-specific "
                         "required attributes (apparel needs color/size/gender).")


def check_shipping_tax_presence(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    ship = _coverage(items, "shipping")
    tax = _coverage(items, "tax")
    if ship >= 0.95:
        return STATUS_PASS, f"Shipping info is in the feed on {ship:.0%} of items (tax on {tax:.0%}) — total-cost transparency in the feed itself, which agent consumers require."
    if ship >= 0.5:
        return STATUS_PARTIAL, f"Shipping present on {ship:.0%} of items, tax on {tax:.0%} — partial coverage means inconsistent delivered-cost data across the catalog."
    return STATUS_PARTIAL, (f"Shipping is in the feed on only {ship:.0%} of items and tax on "
                            f"{tax:.0%}. Merchant Center allows account-level shipping/tax "
                            "settings, so this is not automatically a disapproval — but "
                            "feed-level absence means Shopping listings and agent-commerce "
                            "surfaces can't show a true delivered cost.")


def check_duplicate_ids(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    ids = [i["id"] for i in items if _present(i, "id")]
    if not ids:
        return STATUS_UNKNOWN, "No ids to deduplicate (see id_present)."
    counts = Counter(ids)
    dup_values = {k: v for k, v in counts.items() if v > 1}
    dup_rows = sum(v - 1 for v in dup_values.values())
    if not dup_values:
        return STATUS_PASS, f"All {len(ids)} ids are unique — one row per product, as Merchant Center requires."
    if dup_rows / len(ids) > 0.05:
        return STATUS_FAIL, (f"{len(dup_values)} id value(s) repeat, wasting {dup_rows} row(s) "
                             f"(worst: '{next(iter(dup_values))[:30]}' x{dup_values[next(iter(dup_values))]}). "
                             "Duplicate ids collide in Merchant Center — later rows overwrite "
                             "earlier ones, silently dropping products.")
    return STATUS_PARTIAL, (f"{len(dup_values)} id value(s) repeat ({dup_rows} extra row(s)) — "
                            "dedupe before upload; collisions overwrite products silently.")


_VARIANT_ATTRS = ("color", "size", "material", "pattern")


def check_variant_grouping(ctx) -> tuple[str, str]:
    items = ctx["items"]
    if not items:
        return _no_items()
    clusters: dict[tuple[str, str], list[dict]] = {}
    for i in items:
        key = ((i.get("title") or "").strip().lower(),
               (i.get("brand") or "").strip().lower())
        if key[0]:
            clusters.setdefault(key, []).append(i)
    variant_like = {k: v for k, v in clusters.items() if len(v) > 1}
    if not variant_like:
        return STATUS_UNKNOWN, "No same-title item clusters found — the catalog reads as standalone products; variant grouping not applicable."
    ungrouped_rows, total_rows = 0, 0
    differs = 0
    for rows in variant_like.values():
        total_rows += len(rows)
        signatures = {tuple(r.get(a, "") for a in _VARIANT_ATTRS) for r in rows}
        if len(signatures) > 1:
            differs += 1
        for r in rows:
            if not _present(r, "item_group_id"):
                ungrouped_rows += 1
    if ungrouped_rows == 0:
        return STATUS_PASS, (f"{len(variant_like)} variant cluster(s) ({total_rows} items) all "
                             "carry item_group_id — variants group into one Shopping "
                             "listing with selectable options, as intended.")
    if ungrouped_rows / total_rows > 0.5:
        return STATUS_FAIL, (f"{ungrouped_rows}/{total_rows} items in {len(variant_like)} "
                             "same-title clusters lack item_group_id — variants will "
                             "list as separate near-duplicate products (or be flagged "
                             "as duplicates) instead of one listing with options.")
    return STATUS_PARTIAL, (f"{ungrouped_rows}/{total_rows} items in variant clusters lack "
                            "item_group_id — group them so variants serve as one listing.")


CHECK_FNS = {
    "parse_validity": check_parse_validity,
    "encoding_sanity": check_encoding_sanity,
    "id_present": check_id_present,
    "title_present": check_title_present,
    "link_present": check_link_present,
    "price_present": check_price_present,
    "availability_values": check_availability_values,
    "image_link_present": check_image_link_present,
    "gtin_mpn_coverage": check_gtin_mpn_coverage,
    "brand_coverage": check_brand_coverage,
    "identifier_exists_sanity": check_identifier_exists_sanity,
    "title_quality": check_title_quality,
    "description_quality": check_description_quality,
    "image_link_quality": check_image_link_quality,
    "price_format": check_price_format,
    "sale_price_logic": check_sale_price_logic,
    "taxonomy_presence": check_taxonomy_presence,
    "shipping_tax_presence": check_shipping_tax_presence,
    "duplicate_ids": check_duplicate_ids,
    "variant_grouping": check_variant_grouping,
}
assert set(CHECK_FNS) == {cid for cid, _, _ in CHECK_DEFS}


# ---------------------------------------------------------------------------
# Worst offenders — per-item issues with concrete fixes
# ---------------------------------------------------------------------------

def _item_issues(item: dict, id_counts: Counter,
                 image_results: dict[str, int | None]) -> list[dict]:
    issues = []

    def add(problem: str, fix: str):
        issues.append({"problem": problem, "fix": fix})

    label = item.get("id") or item.get("title") or "(unnamed item)"
    for field, gname in (("id", "id"), ("title", "title"), ("link", "link"),
                         ("price", "price"), ("availability", "availability"),
                         ("image_link", "image_link")):
        if not _present(item, field):
            add(f"missing {gname}",
                f"Add a {gname} for this item — it is disapproved without one.")
    if _present(item, "id") and id_counts.get(item["id"], 0) > 1:
        add(f"duplicate id '{item['id'][:30]}' x{id_counts[item['id']]}",
            "Give every product row a unique id; duplicates overwrite each other in Merchant Center.")
    if _present(item, "availability") and \
            re.sub(r"\s+", " ", item["availability"].strip().lower()) not in VALID_AVAILABILITY:
        add(f"availability '{item['availability'][:30]}' is not a Google value",
            "Use exactly: in stock, out of stock, preorder, or backorder.")
    if _present(item, "price") and _parse_price(item["price"]) is None:
        add(f"price '{item['price'][:30]}' doesn't parse",
            "Format price as '19.99 USD' (amount, space, ISO currency).")
    if _present(item, "sale_price"):
        p, s = _parse_price(item.get("price", "")), _parse_price(item["sale_price"])
        if p is None or s is None or s[0] >= p[0]:
            add("sale_price is not below price",
                "sale_price must parse and be strictly less than price, or be removed.")
    if _present(item, "brand") and not _present(item, "gtin") and not _present(item, "mpn") \
            and not (_present(item, "identifier_exists")
                     and item["identifier_exists"].strip().lower() in ("false", "no", "0")):
        add("branded item with no GTIN, MPN, or identifier_exists=no",
            "Add the manufacturer GTIN (or MPN), or set identifier_exists=no only if the brand truly assigns none.")
    if _present(item, "gtin") and _gtin_valid(item["gtin"]) is False:
        add(f"GTIN '{item['gtin'][:20]}' fails its check digit",
            "Re-copy the GTIN from the product packaging — one wrong digit invalidates it.")
    if _present(item, "identifier_exists") \
            and item["identifier_exists"].strip().lower() in ("false", "no", "0") \
            and (_present(item, "gtin") or _present(item, "mpn")):
        add("identifier_exists=no contradicts the submitted GTIN/MPN",
            "Remove identifier_exists (or set it to yes) when identifiers are submitted.")
    if _present(item, "title"):
        t = item["title"]
        letters = [c for c in t if c.isalpha()]
        if len(letters) >= 10 and sum(c.isupper() for c in letters) / len(letters) > 0.8:
            add("title is ALL CAPS", "Use sentence case — brand + product + key attribute.")
        elif len(t) < 10:
            add(f"title too thin ('{t[:20]}')",
                "Expand to brand + product type + key attribute (aim 30-140 chars).")
    if _present(item, "image_link"):
        u = item["image_link"]
        if not u.lower().startswith("https://"):
            add("image_link is not https", "Serve the image over https and update the URL.")
        status = image_results.get(u)
        if status is not None and status != 200:
            add(f"image_link returns HTTP {status}",
                "Restore the image at that URL or point image_link at the live asset.")
    return [{"item": str(label)[:60], "issues": issues}] if issues else []


def worst_offenders(ctx: dict, limit: int = 5) -> list[dict]:
    items = ctx["items"]
    if not items:
        return []
    id_counts = Counter(i["id"] for i in items if _present(i, "id"))
    image_results = _image_results(ctx)
    scored = []
    for idx, item in enumerate(items):
        rows = _item_issues(item, id_counts, image_results)
        if rows:
            scored.append((len(rows[0]["issues"]), idx, rows[0]))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [row for _, _, row in scored[:limit]]


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

RECOMMENDATION_FIXES = {
    "parse_validity": ("Make the feed parse cleanly", "Merchant Center rejects an unparseable file wholesale — validate the export against the format spec (well-formed XML / consistent CSV columns / valid JSON) and re-export before anything else matters."),
    "encoding_sanity": ("Fix feed encoding at the source", "Export as UTF-8 without a BOM, strip control characters, and stop double-encoding entities — mojibake lands verbatim in titles customers and agents read."),
    "id_present": ("Give every item a stable, unique id", "id is required and is the key UCP/agent commerce uses to reference the product — a missing id means the product does not exist downstream."),
    "title_present": ("Add titles to the items missing them", "No title, no listing: Google cannot name the product, and agents cannot cite it."),
    "link_present": ("Add landing-page links", "link is required — every item needs the canonical product-page URL, matching the domain verified in Merchant Center."),
    "price_present": ("Add prices to every item", "price is a required attribute; items without one are disapproved on submission, full stop."),
    "availability_values": ("Normalize availability to Google's vocabulary", "Exactly: in stock / out of stock / preorder / backorder, present on every item, refreshed at least daily — stale or invented values read as missing."),
    "image_link_present": ("Add an image_link to every item", "image_link is required; an imageless product is disapproved and invisible on Shopping and in agent answers with images."),
    "gtin_mpn_coverage": ("Close the GTIN/MPN gap", "Add the manufacturer GTIN (check-digit valid) or MPN to every branded item — missing identifiers are the #1 feed disapproval and they cap reach even when items survive."),
    "brand_coverage": ("Add brand to every manufactured item", "Brand is required with GTIN/MPN and is how Google matches your rows to its catalog; unbranded rows lose that matching."),
    "identifier_exists_sanity": ("Stop contradicting yourself on identifier_exists", "Set identifier_exists=no ONLY for items with genuinely no GTIN/MPN (custom/handmade). Declaring it while submitting identifiers — or vice versa — is a data-quality violation."),
    "title_quality": ("Rewrite titles as brand + product + key attribute", "Kill the stuffing, pipes, and ALL CAPS: 'Brand Running Shoes Men's Trail Waterproof' beats 'SHOES!!! BEST SHOES | shoes shoes'. Descriptive titles match more queries and get cited by agents."),
    "description_quality": ("Write real descriptions", "At least a few factual sentences per item (materials, use, specs). Thin or missing descriptions starve Shopping matching and the AI answers that increasingly front the catalog."),
    "image_link_quality": ("Repair image links", "Every image_link must be a working https URL returning 200 — crawl the feed's image URLs in CI the same way this scan spot-checks them."),
    "price_format": ("Format prices as 'amount CURRENCY'", "One currency per feed, amount first ('19.99 USD'), no zero/negative prices. Malformed prices are disapproved as 'invalid price' with no review."),
    "sale_price_logic": ("Fix sale_price pairs", "sale_price must parse and be strictly below price. A sale price at/above the base price reads as a misleading promotion and gets the item rejected."),
    "taxonomy_presence": ("Categorize every item", "Add google_product_category (Google's taxonomy) or at least product_type to every item — uncategorized items are auto-categorized, often wrong, and miss category-required attributes."),
    "shipping_tax_presence": ("Put shipping in the feed (or confirm account-level settings)", "Feed-level shipping lets every surface show a true delivered cost. If shipping/tax live in Merchant Center account settings instead, verify they're actually configured — an unconfigured account silently disapproves US offers."),
    "duplicate_ids": ("Deduplicate ids", "One row per product per id. Duplicates overwrite each other on upload, silently dropping products from the catalog."),
    "variant_grouping": ("Group variants with item_group_id", "Color/size siblings sharing a title need one item_group_id so they serve as a single listing with options instead of near-duplicate listings competing with each other."),
}


# ---------------------------------------------------------------------------
# Risk + report
# ---------------------------------------------------------------------------

def risk_level(score: float, checks: list[Check], unreachable: bool = False) -> dict:
    """Disapproval-risk framing: what Merchant Center's validator sees."""
    if unreachable:
        return {"level": "unknown",
                "disapproval_triggers": [],
                "basis": "Feed unreachable — risk not evaluated."}
    by_id = {c.id: c for c in checks}
    triggers = [cid for cid in DISAPPROVAL_CHECKS
                if by_id.get(cid) is not None and by_id[cid].status == STATUS_FAIL]
    if by_id.get("parse_validity") is not None \
            and by_id["parse_validity"].status == STATUS_FAIL:
        level = "High"
    elif score >= 90:
        level = "Low"
    elif score >= 75:
        level = "Moderate"
    elif score >= 55:
        level = "Elevated"
    else:
        level = "High"
    if len(triggers) >= 3 and level in ("Low", "Moderate"):
        level = "Elevated"
    return {
        "level": level,
        "disapproval_triggers": triggers,
        "basis": (f"Score {score} (A-F grade bands) plus {len(triggers)} failing "
                  "check(s) in the classes Merchant Center disapproves on "
                  "(unparseable feed, missing id/title/link/price/availability/"
                  "image, missing identifiers, malformed prices)."),
    }


def build_report(url: str, final_url: str, checks: list[Check],
                 t_start: float, ctx: dict | None = None,
                 unreachable: bool = False) -> dict:
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
    report = {
        "url": url,
        "final_url": final_url,
        "graded_at": datetime.now(timezone.utc).isoformat(),
        "score": score,
        "grade": grade,
        "risk": risk_level(score, checks, unreachable=unreachable),
        "unreachable": unreachable,
        "checks": [
            {"id": c.id, "category": c.category, "weight": c.weight,
             "status": c.status, "evidence": c.evidence}
            for c in checks
        ],
        "recommendations": recommendations,
        "worst_offenders": worst_offenders(ctx) if ctx and not unreachable else [],
        "disclaimer": DISCLAIMER,
        "meta": {
            "evaluated_weight": denom,
            "total_weight": 100,
            "unknown_count": unknown_count,
            "elapsed_ms": int((time.time() - t_start) * 1000),
            "engine": ENGINE_VERSION,
        },
    }
    if ctx is not None:
        report["feed"] = {
            "format": ctx["format"],
            "items_analyzed": len(ctx["items"]),
            "items_total": ctx["total_items"],
            "bytes": ctx["byte_len"],
            "truncated": ctx["truncated"],
        }
        report["meta"]["limits"] = {
            "max_feed_bytes": MAX_FEED_BYTES,
            "max_items_analyzed": MAX_ITEMS_ANALYZED,
            "image_spot_checks": IMAGE_SPOT_CHECKS,
            "note": ("Feed document capped at 8 MB; first 20,000 items "
                     "analyzed; image links spot-checked (8 unique URLs). "
                     "Landing pages are not crawled."),
        }
    return report


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

def audit_feed(url: str) -> dict:
    """Full paid product-feed hygiene scan. Never raises."""
    t_start = time.time()
    deadline = t_start + OVERALL_DEADLINE
    url = normalize_url(url)
    ctx = _build_ctx(url, deadline)
    if ctx is None:
        checks = [Check(id=cid, category=cat, weight=w, status=STATUS_UNKNOWN,
                        evidence="Feed unreachable or returned no content; check not evaluated.")
                  for cid, cat, w in CHECK_DEFS]
        return build_report(url, url, checks, t_start, unreachable=True)
    return build_report(url, ctx["final_url"], _run_checks(ctx), t_start, ctx=ctx)


def audit_feed_lite(url: str) -> dict:
    """Free lite scan: score + grade + risk + feed shape only.

    Runs the same 20 checks but caps item-level detail — no per-check
    evidence, no worst-offender items, no detailed recommendations.
    The funnel for the paid full scan. Never raises.
    """
    t_start = time.time()
    try:
        full = audit_feed(url)
    except Exception as exc:
        return {"url": url, "error": f"Audit failed: {exc}",
                "graded_at": datetime.now(timezone.utc).isoformat()}
    if full.get("unreachable"):
        return {
            "url": full["url"], "final_url": full["final_url"],
            "graded_at": full["graded_at"],
            "score": 0.0, "grade": "F", "risk_level": "unknown",
            "unreachable": True,
            "note": "Feed unreachable — no scan possible.",
        }
    by_status: dict[str, int] = {"pass": 0, "partial": 0, "fail": 0, "unknown": 0}
    for c in full["checks"]:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    top_gaps = [r["title"] for r in full["recommendations"]]
    feed = full.get("feed", {})
    return {
        "url": full["url"],
        "final_url": full["final_url"],
        "graded_at": full["graded_at"],
        "score": full["score"],
        "grade": full["grade"],
        "risk_level": full["risk"]["level"],
        "feed_format": feed.get("format"),
        "items_analyzed": feed.get("items_analyzed"),
        "checks_summary": by_status,
        "top_gaps": top_gaps,
        "note": ("Free lite scan: score, grade, disapproval-risk level, and feed "
                 "shape only — item-level detail is capped. The full audit_feed "
                 "report ($1.00/call) adds per-check evidence, the worst-offending "
                 "items with fixes, and the prioritized fix list."),
    }


__all__ = ["audit_feed", "audit_feed_lite", "CHECK_DEFS", "CHECK_FNS",
           "build_report", "compute_score", "risk_level", "parse_feed",
           "DISCLAIMER", "ENGINE_VERSION"]
