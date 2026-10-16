"""Dual-facilitator routing tests (PayAI default + Coinbase CDP rail).

No network: both facilitator clients are stubs. server.py is imported with
X402_DISABLED=1 so no paywall is armed at import; the routing classes are
exercised directly. The behavior under test (see server.py):

- unmarked payment requirements verify/settle via the default (PayAI) client
- requirements carrying extra.facilitator == "cdp" route to the CDP client
- get_supported() merges both facilitators, and a CDP outage at startup
  degrades to PayAI-only kinds instead of failing initialization
- with no CDP keys configured, the builder returns the plain PayAI client
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("X402_DISABLED", "1")

import server as srv  # noqa: E402
from x402.http import HTTPFacilitatorClient  # noqa: E402
from x402.schemas import (  # noqa: E402
    PaymentRequirements,
    SupportedKind,
    SupportedResponse,
)

K_BASE = SupportedKind(x402_version=2, scheme="exact", network="eip155:8453")
K_MAINNET = SupportedKind(x402_version=2, scheme="exact", network="eip155:1")


class _StubClient:
    def __init__(self, name, kinds=(), extensions=(), fail_supported=False):
        self.url = f"https://{name}.example"
        self._kinds = list(kinds)
        self._extensions = list(extensions)
        self._fail = fail_supported

    def get_supported(self):
        if self._fail:
            raise RuntimeError("supported unavailable")
        return SupportedResponse(
            kinds=self._kinds, extensions=self._extensions, signers={}
        )

    async def verify(self, payload, requirements):
        return f"{self.url}:verify"

    async def settle(self, payload, requirements):
        return f"{self.url}:settle"


class _Req:
    def __init__(self, extra=None):
        self.extra = extra or {}


def _dual(cdp=None):
    payai = _StubClient("payai", kinds=[K_BASE], extensions=["bazaar"])
    if cdp is None:
        cdp = _StubClient(
            "cdp", kinds=[K_BASE, K_MAINNET], extensions=["bazaar", "signer"]
        )
    return srv._DualFacilitatorClient(payai, cdp)


def test_unmarked_requirements_route_to_payai():
    dual = _dual()
    assert asyncio.run(dual.verify(None, _Req())) == "https://payai.example:verify"
    assert asyncio.run(dual.settle(None, _Req())) == "https://payai.example:settle"


def test_cdp_marked_requirements_route_to_cdp():
    dual = _dual()
    marked = _Req({"facilitator": "cdp"})
    assert asyncio.run(dual.verify(None, marked)) == "https://cdp.example:verify"
    assert asyncio.run(dual.settle(None, marked)) == "https://cdp.example:settle"


def test_marker_read_from_dict_requirements():
    assert srv._requirements_facilitator({"extra": {"facilitator": "cdp"}}) == "cdp"
    assert srv._requirements_facilitator(_Req()) is None
    assert srv._requirements_facilitator(_Req({"facilitator": 7})) is None


def test_get_supported_merges_kinds_and_extensions():
    merged = _dual().get_supported()
    assert [(k.scheme, str(k.network)) for k in merged.kinds] == [
        ("exact", "eip155:8453"),
        ("exact", "eip155:1"),
    ]
    assert merged.extensions == ["bazaar", "signer"]


def test_get_supported_cdp_outage_degrades_to_payai():
    dual = _dual(cdp=_StubClient("cdp", fail_supported=True))
    merged = dual.get_supported()
    assert [(k.scheme, str(k.network)) for k in merged.kinds] == [
        ("exact", "eip155:8453")
    ]


def test_cdp_marked_accepts_clones_without_mutating():
    req = PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        amount="1000000",
        pay_to="0xd702C8A8Ee30F6950A2a663010d12FC195D5348e",
        max_timeout_seconds=300,
        extra={"name": "USD Coin", "version": "2"},
    )
    (clone,) = srv._cdp_marked_accepts([req])
    assert clone.extra == {"name": "USD Coin", "version": "2", "facilitator": "cdp"}
    assert req.extra == {"name": "USD Coin", "version": "2"}
    assert clone.amount == req.amount and clone.pay_to == req.pay_to


def test_builder_without_cdp_keys_returns_payai_only():
    assert srv.CDP_ENABLED is False  # test env never sets CDP keys
    client = srv._build_facilitator_client()
    assert isinstance(client, HTTPFacilitatorClient)
    assert getattr(client, "cdp_enabled", False) is False
