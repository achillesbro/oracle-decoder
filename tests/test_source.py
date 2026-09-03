"""Verified-source enrichment (Sourcify + RPC stubbed)."""

import json

from oracle_decoder import source

IMPL = "0x" + "9" * 40
UP = "0x" + "c" * 40


def test_family_map():
    assert source.family_for("CurveStableswapOracle") == "curve-stableswap"
    assert source.family_for("PendleChainlinkOracle") == "pendle-pt"
    assert source.family_for("OjoPTFeed") == "pendle-pt"
    assert source.family_for("ChainlinkOvalBase") == "oval-wrapper"
    assert source.family_for("DummyFeed") == "fixed-feed"
    assert source.family_for("EACAggregatorProxy") == "chainlink-code"
    assert source.family_for("UniswapV3PoolChainlinkAdapter") == "dex-twap"
    assert source.family_for("UsualXOracle") is None  # named, no family yet
    assert source.family_for(source.UNVERIFIED) is None


def test_parse_eip1167():
    code = "0x" + source.EIP1167_PREFIX + "ab" * 20 + source.EIP1167_SUFFIX
    assert source.parse_eip1167(code) == "0x" + "ab" * 20
    assert source.parse_eip1167("0x6080604052") is None


def _stub(monkeypatch, *, impl=None, names=None, ups=None):
    names = names or {}
    monkeypatch.setattr(source, "resolve_implementation", lambda rpc, a: impl)
    monkeypatch.setattr(source, "lookup", lambda http, cid, a: names.get(a, {"name": source.UNVERIFIED, "abi": []}))
    monkeypatch.setattr(source, "upstream_addresses", lambda rpc, a, abi: ups or [])


def test_enrich_names_family_and_upgrades_kind(monkeypatch):
    _stub(monkeypatch, names={"0xa": {"name": "CurveStableswapOracle", "abi": []}})
    row = {"kind": "oracle", "publisher": None, "evidence": "none", "family": None}
    source.enrich(row, http=None, rpc_url="rpc", chain_id=1, address="0xa")
    assert (row["source_name"], row["family"], row["kind"]) == ("CurveStableswapOracle", "curve-stableswap", "oracle-custom")


def test_enrich_resolves_proxy_first(monkeypatch):
    _stub(monkeypatch, impl=IMPL, names={IMPL: {"name": "Api3ReaderProxyV1", "abi": []}, "0xp": {"name": "TransparentUpgradeableProxy", "abi": []}})
    row = {"kind": "feed", "publisher": "API3", "evidence": "canonical-contract", "family": None}
    source.enrich(row, http=None, rpc_url="rpc", chain_id=1, address="0xp")
    assert row["implementation"] == IMPL and row["source_name"] == "Api3ReaderProxyV1"


def test_source_name_is_a_family_never_a_publisher(monkeypatch):
    _stub(monkeypatch, names={"0xa": {"name": "EACAggregatorProxy", "abi": []}})
    listed = {"kind": "feed", "publisher": "Chainlink", "evidence": "registry", "family": None}
    source.enrich(listed, http=None, rpc_url="rpc", chain_id=1, address="0xa")
    assert listed["family"] is None  # registry-grade publisher keeps its identity
    unlisted = {"kind": "feed", "publisher": None, "evidence": "description", "family": None}
    source.enrich(unlisted, http=None, rpc_url="rpc", chain_id=1, address="0xa")
    assert unlisted["family"] == "chainlink-code"


def test_enrich_upstream_and_unverified_sentinel(monkeypatch):
    _stub(monkeypatch, names={"0xa": {"name": "ChainlinkOvalBase", "abi": []}}, ups=[{"getter": "CHAINLINK_SOURCE", "address": UP}])
    row = {"kind": "oracle", "publisher": None, "evidence": "none", "family": None}
    source.enrich(row, http=None, rpc_url="rpc", chain_id=1, address="0xa")
    assert row["family"] == "oval-wrapper"
    assert json.loads(row["upstream"]) == [{"getter": "CHAINLINK_SOURCE", "address": UP}]
    row = {"kind": "feed", "publisher": None, "evidence": "description", "family": None}
    source.enrich(row, http=None, rpc_url="rpc", chain_id=1, address="0xzz")
    assert row["source_name"] == source.UNVERIFIED and "upstream" not in row


def test_upstream_getter_selection():
    abi = [
        {"type": "function", "name": "CHAINLINK_SOURCE", "inputs": [], "outputs": [{"type": "address"}]},
        {"type": "function", "name": "owner", "inputs": [], "outputs": [{"type": "address"}]},
        {"type": "function", "name": "BASE_FEED_1", "inputs": [], "outputs": [{"type": "address"}]},
        {"type": "function", "name": "token", "inputs": [], "outputs": [{"type": "address"}]},
        {"type": "function", "name": "priceFeed", "inputs": [{"type": "uint256"}], "outputs": [{"type": "address"}]},
    ]
    assert source.upstream_getters(abi) == ["CHAINLINK_SOURCE"]
