"""Shared audit primitives for Harrison's agent-API tool portfolio.

Extracted from the storefront-readiness grader's engine (2026-09-21) so every
paid tool reuses the same fetch layer, check model, and scoring math instead
of duplicating them. Each service bundles a copy of this package at build
time (see each service's Dockerfile); the grader predates this module and
remains standalone.

Design rule inherited everywhere: a check that cannot be evaluated returns
"unknown" — an engine never invents check results.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

FETCH_TIMEOUT = 8          # seconds per HTTP fetch
MAX_BYTES = 2_000_000      # cap on response bodies we parse
OVERALL_DEADLINE = 75      # seconds; stop scheduling new fetches after this

USER_AGENT = (
    "Mozilla/5.0 (compatible; AgentAuditBot/1.0; "
    "+https://github.com/eighthzwave/agent-api) readiness-audit-bot"
)

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


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    return url


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
# JSON-LD helpers
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
    return [b for b in blocks if want in node_types(b)]


def as_list(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def money_mentions(text: str) -> list[str]:
    return re.findall(r"\$\s?\d[\d,]*(?:\.\d{2})?", text)[:8]


# ---------------------------------------------------------------------------
# robots.txt parsing (shared: AI-crawler policy checks)
# ---------------------------------------------------------------------------

def parse_robots_rules(robots_text: str) -> dict[str, set[str]]:
    """Map lowercase user-agent token -> set of disallowed path prefixes.

    Only the rules in the group whose User-agent lines name the token (or '*'
    when no named group exists) are collected. Case-insensitive.
    """
    groups: list[tuple[list[str], set[str]]] = []  # (agents, disallows)
    cur_agents: list[str] = []
    cur_disallows: set[str] = set()
    seen_rule = False

    def flush():
        if cur_agents or cur_disallows or seen_rule:
            groups.append((cur_agents, cur_disallows))

    for line in robots_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if seen_rule:
                flush()
                cur_agents, cur_disallows, seen_rule = [], set(), False
            cur_agents.append(value.lower())
        elif field == "disallow":
            seen_rule = True
            if value:
                cur_disallows.add(value)
            # bare "Disallow:" with empty value = allow-all; record nothing
        elif field == "allow":
            seen_rule = True
            # Allow rules narrow Disallows; the audit only needs disallow sets.

    flush()

    rules: dict[str, set[str]] = {}
    for agents, disallows in groups:
        for agent in agents:
            rules.setdefault(agent, set()).update(disallows)
    return rules


def is_fully_disallowed(disallows: set[str]) -> bool:
    return "/" in disallows


# ---------------------------------------------------------------------------
# Scoring + report
# ---------------------------------------------------------------------------

GRADE_BOUNDS = [(90, "A"), (80, "B"), (70, "C"), (60, "D")]


def compute_score(checks: list[Check]) -> tuple[float, str, int, int]:
    """Returns (score, grade, evaluated_weight, unknown_count).

    Unknown checks are excluded from the denominator — the score rescales over
    what could actually be evaluated. All-unknown -> 0/F.
    """
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


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower()
