"""Tests for the AI Storefront Readiness Grader.

Scoring math is tested deterministically. Live checks hit real public
storefronts (no network mocking) with loose, structural assertions so the
suite stays green as sites evolve.
"""

import os

os.environ["X402_DISABLED"] = "1"  # server tests run with the paywall off

from grader.engine import (
    CHECK_DEFS,
    Check,
    STATUS_FAIL,
    STATUS_PARTIAL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    build_report,
    compute_score,
    grade_storefront,
)


def _checks(statuses: dict[str, str]) -> list[Check]:
    return [Check(id=cid, category=cat, weight=w, status=statuses.get(cid, STATUS_UNKNOWN))
            for cid, cat, w in CHECK_DEFS]


# ---------------------------------------------------------------------------
# Scoring math
# ---------------------------------------------------------------------------

def test_all_pass_scores_100_and_A():
    checks = _checks({cid: STATUS_PASS for cid, _, _ in CHECK_DEFS})
    score, grade, denom, unknown = compute_score(checks)
    assert score == 100.0
    assert grade == "A"
    assert denom == 100
    assert unknown == 0


def test_all_fail_scores_0_and_F():
    checks = _checks({cid: STATUS_FAIL for cid, _, _ in CHECK_DEFS})
    score, grade, denom, unknown = compute_score(checks)
    assert score == 0.0
    assert grade == "F"


def test_partial_earns_half_weight():
    # One 8-weight check partial, everything else unknown -> 50% of evaluated
    statuses = {"jsonld_product": STATUS_PARTIAL}
    checks = _checks(statuses)
    score, grade, denom, unknown = compute_score(checks)
    assert denom == 8
    assert score == 50.0
    assert unknown == len(CHECK_DEFS) - 1


def test_unknown_excluded_from_denominator():
    # 92 weight passing, 8 weight unknown -> still 100 (rescaled)
    statuses = {cid: STATUS_PASS for cid, _, _ in CHECK_DEFS if cid != "jsonld_product"}
    checks = _checks(statuses)
    score, grade, denom, unknown = compute_score(checks)
    assert denom == 92
    assert score == 100.0
    assert grade == "A"


def test_grade_boundaries():
    def score_of(pct_pass_weight: int):
        # build checks with pct of weight passing, rest failing
        checks, acc = [], 0
        for cid, cat, w in CHECK_DEFS:
            if acc < pct_pass_weight:
                checks.append(Check(cid, cat, w, STATUS_PASS))
                acc += w
            else:
                checks.append(Check(cid, cat, w, STATUS_FAIL))
        return compute_score(checks)
    assert score_of(90)[1] == "A"
    assert score_of(80)[1] == "B"
    assert score_of(70)[1] == "C"
    assert score_of(60)[1] == "D"
    assert score_of(10)[1] == "F"


def test_all_unknown_scores_zero():
    checks = _checks({})
    score, grade, denom, unknown = compute_score(checks)
    assert (score, grade, denom) == (0.0, "F", 0)


def test_recommendations_are_top3_by_weight():
    statuses = {cid: STATUS_FAIL for cid, _, _ in CHECK_DEFS}
    checks = _checks(statuses)
    report = build_report("https://x.test", "https://x.test", checks, {}, 0.0)
    recs = report["recommendations"]
    assert len(recs) == 3
    weights = [r["weight"] for r in recs]
    assert weights == sorted(weights, reverse=True)
    assert all({"check_id", "title", "why", "evidence", "weight"} <= set(r) for r in recs)


def test_report_shape():
    checks = _checks({"https": STATUS_PASS})
    report = build_report("https://x.test", "https://x.test/", checks, {}, 0.0)
    assert set(report) >= {"url", "final_url", "graded_at", "score", "grade",
                           "checks", "recommendations", "meta"}
    assert len(report["checks"]) == 20
    assert all(set(c) == {"id", "category", "weight", "status", "evidence"}
               for c in report["checks"])
    assert sum(w for _, _, w in CHECK_DEFS) == 100


# ---------------------------------------------------------------------------
# Live checks against real public storefronts (no mocks)
# ---------------------------------------------------------------------------

def test_live_demo_store_all_checks_evaluated():
    report = grade_storefront("https://books.toscrape.com")
    assert len(report["checks"]) == 20
    assert report["meta"]["unknown_count"] == 0
    assert 0 <= report["score"] <= 100
    assert report["grade"] in ("A", "B", "C", "D", "F")
    by_id = {c["id"]: c["status"] for c in report["checks"]}
    # demo site has no JSON-LD product markup and no llms.txt (honest fail)
    assert by_id["jsonld_product"] in ("fail", "unknown")
    assert by_id["llms_txt"] in ("fail", "unknown")
    assert by_id["https"] == "pass"


def test_live_real_store_product_intelligence():
    report = grade_storefront("https://www.adafruit.com")
    by_id = {c["id"]: c["status"] for c in report["checks"]}
    assert by_id["https"] == "pass"
    assert by_id["jsonld_product"] == "pass"
    assert by_id["offer_price"] == "pass"
    assert report["score"] > 30  # a real store scores materially above a bare demo


def test_live_unreachable_site_all_unknown():
    report = grade_storefront("https://this-store-does-not-exist-xyz12345.com")
    assert report["unreachable"] is True
    assert report["score"] == 0.0
    assert report["grade"] == "F"
    assert all(c["status"] == STATUS_UNKNOWN for c in report["checks"])
    assert report["recommendations"] == []


# ---------------------------------------------------------------------------
# Server surfaces (paywall disabled via X402_DISABLED=1)
# ---------------------------------------------------------------------------

def test_http_grade_endpoint():
    from fastapi.testclient import TestClient
    import server as srv
    client = TestClient(srv.app)
    assert client.get("/health").json()["ok"] is True
    resp = client.post("/grade", json={"url": "https://books.toscrape.com"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["grade"] in ("A", "B", "C", "D", "F")
    assert len(body["checks"]) == 20


def test_http_grade_missing_url_is_400():
    from fastapi.testclient import TestClient
    import server as srv
    client = TestClient(srv.app)
    resp = client.post("/grade", json={})
    assert resp.status_code == 422  # Pydantic request schema rejects missing url


def test_mcp_tool_listing():
    from fastapi.testclient import TestClient
    import server as srv
    with TestClient(srv.app) as client:
        # MCP initialize handshake (stateless streamable HTTP)
        init = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18",
                       "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}})
        assert init.status_code == 200
        tools = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert tools.status_code == 200
        names = [t["name"] for t in tools.json()["result"]["tools"]]
        assert "grade_storefront" in names
