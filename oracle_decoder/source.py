"""Verified source enrichment.

For a bespoke oracle (no publisher registry confirms it), the strongest
programmatic identity is the contract's VERIFIED SOURCE NAME. Sourcify
returns the compiled contract name and the ABI for exact and partial
matches, without an API key. `CurveStableswapOracle`,
`PendleChainlinkOracle`, `ChainlinkOvalBase`, `MuxLpOracle` ... map to
families; unmapped names are kept in `source_name`.

Proxies are resolved first (EIP-1967 storage slot, EIP-1167 clone bytecode)
so the name describes the code that runs; the implementation lands in
`implementation`.

Adapters that read another feed usually expose it through an address
getter (`CHAINLINK_SOURCE()`, `QUOTE_ASSET_FEED()`, `priceFeed()` ...). The
verified ABI says which getters exist; the ones whose names say
feed/oracle/source/aggregator/adapter are called and recorded in `upstream`
and probed as children. An Oval wrapper around Chainlink USDT/ETH then
points at the registry-listed Chainlink proxy.

A source name is a FAMILY, never a publisher: `EACAggregatorProxy` is
Chainlink's proxy code and third parties deploy it too.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .http import Http

log = logging.getLogger(__name__)

SOURCIFY = "https://sourcify.dev/server/v2/contract/{chain_id}/{address}"
UNVERIFIED = "unverified"  # definitive 404: never queried again

EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1167_PREFIX = "363d3d373d3d3d363d73"
EIP1167_SUFFIX = "5af43d82803e903d91602b57fd5bf3"
ZERO_ADDR = "0x" + "0" * 40

# Verified contract name -> family. First match wins. Applied only to records
# without a registry-grade publisher: a registry-listed Chainlink feed named
# EACAggregatorProxy is Chainlink; an unlisted one is chainlink-code.
FAMILY_BY_NAME: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"CurveStableswapOracle", re.I), "curve-stableswap"),
    (re.compile(r"MetaOracleDeviationTimelock", re.I), "meta-deviation-timelock"),
    (re.compile(r"^Pendle.*(Oracle|Wrapper)|^Ojo.*(PT|Pendle)|^PT\w*(Oracle|Feed)", re.I), "pendle-pt"),
    (re.compile(r"MuxLpOracle", re.I), "mux-lp"),
    (re.compile(r"(UniswapV3|Algebra)Pool\w*Adapter", re.I), "dex-twap"),
    (re.compile(r"Nav\w*Adapter|NetAssetValue", re.I), "nav-adapter"),
    (re.compile(r"OracleRouter", re.I), "router"),
    (re.compile(r"Oval", re.I), "oval-wrapper"),
    (re.compile(r"^(DummyFeed|FixedPriceOracle|Constant\w*Oracle|Constant\w*Feed)", re.I), "fixed-feed"),
    (re.compile(r"(ClampFeed|CappedOracle|Bounded)", re.I), "clamp"),
    (re.compile(r"^(EACAggregatorProxy|AccessControlledOCR2Aggregator|AccessControlledOffchainAggregator)$"), "chainlink-code"),
    (re.compile(r"ExchangeRate|Erc4626|ERC4626", re.I), "exchange-rate-adapter"),
]

# Getters worth following as upstream dependencies. Excludes the composition
# slots and MODT legs (already recorded) and admin getters.
UPSTREAM_GETTER = re.compile(r"(source|feed|oracle|aggregator|adapter)", re.I)
UPSTREAM_SKIP = {
    "owner", "BASE_FEED_1", "BASE_FEED_2", "QUOTE_FEED_1", "QUOTE_FEED_2", "BASE_VAULT", "QUOTE_VAULT",
    "primaryOracle", "backupOracle", "currentOracle", "aggregator",
}
MAX_UPSTREAM = 6


def family_for(name: str | None) -> str | None:
    if not name or name == UNVERIFIED:
        return None
    for pattern, family in FAMILY_BY_NAME:
        if pattern.search(name):
            return family
    return None


def parse_eip1167(code_hex: str) -> str | None:
    """Implementation address of an EIP-1167 minimal proxy, or None."""
    hexs = code_hex.lower().removeprefix("0x")
    i = hexs.find(EIP1167_PREFIX)
    if i < 0:
        return None
    start = i + len(EIP1167_PREFIX)
    impl = hexs[start : start + 40]
    if len(impl) != 40 or not hexs[start + 40 :].startswith(EIP1167_SUFFIX):
        return None
    return "0x" + impl


def lookup(http: Http, chain_id: int, address: str) -> dict[str, Any] | None:
    """Sourcify verified metadata {"name", "abi"}; {"name": UNVERIFIED} on a
    definitive 404; None on transport failure (retry later)."""
    try:
        d = http.get_json(
            SOURCIFY.format(chain_id=chain_id, address=address),
            params={"fields": "compilation,abi"},
            accept=frozenset({404}),
        )
    except Exception:
        log.warning("sourcify lookup failed for %d:%s", chain_id, address)
        return None
    if not isinstance(d, dict) or not d.get("match"):
        return {"name": UNVERIFIED, "abi": []}
    return {"name": (d.get("compilation") or {}).get("name") or UNVERIFIED, "abi": d.get("abi") or []}


def resolve_implementation(rpc_url: str, address: str) -> str | None:
    """EIP-1967 implementation slot, else EIP-1167 clone target, else None."""
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
    checksum = Web3.to_checksum_address(address)
    try:
        slot = w3.eth.get_storage_at(checksum, EIP1967_IMPL_SLOT)
        impl = "0x" + slot.hex()[-40:]
        if impl != ZERO_ADDR:
            return impl
        return parse_eip1167(w3.eth.get_code(checksum).hex())
    except Exception:
        log.warning("implementation resolution failed for %s", address)
        return None


def upstream_getters(abi: list[dict]) -> list[str]:
    """Zero-arg address getters in the ABI that look like feed references."""
    return [
        f["name"]
        for f in abi
        if f.get("type") == "function"
        and not f.get("inputs")
        and [o.get("type") for o in f.get("outputs", [])] == ["address"]
        and f["name"] not in UPSTREAM_SKIP
        and UPSTREAM_GETTER.search(f["name"])
    ][:MAX_UPSTREAM]


def upstream_addresses(rpc_url: str, address: str, abi: list[dict]) -> list[dict[str, str]]:
    """Call the upstream getters; return [{getter, address}] for non-zero answers."""
    from eth_abi import decode
    from web3 import Web3

    getters = upstream_getters(abi)
    if not getters:
        return []
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 20}))
    out: list[dict[str, str]] = []
    for name in getters:
        try:
            data = w3.eth.call({"to": Web3.to_checksum_address(address), "data": Web3.keccak(text=f"{name}()")[:4]})
            target = decode(["address"], data)[0].lower()
        except Exception:
            continue
        if target != ZERO_ADDR:
            out.append({"getter": name, "address": target})
    return out


def enrich(row: dict, http: Http, rpc_url: str, chain_id: int, address: str) -> dict:
    """Attach source_name / implementation / family / upstream to a classified
    record. A registry-grade publisher keeps its identity; the source name is
    informational for it. Mutates and returns `row`."""
    impl = resolve_implementation(rpc_url, address)
    if impl:
        row["implementation"] = impl
    info = lookup(http, chain_id, impl or address)
    if info is None:
        return row  # transport failure: source_name stays null for a retry
    if impl and info["name"] == UNVERIFIED:
        info = lookup(http, chain_id, address) or info  # fall back to the proxy's own name
    row["source_name"] = info["name"]

    if row.get("evidence") not in ("registry", "canonical-contract"):
        fam = family_for(info["name"])
        if fam and not row.get("family"):
            row["family"] = fam
            if row.get("kind") in ("oracle", "opaque"):
                row["kind"] = "oracle-custom"
    ups = upstream_addresses(rpc_url, address, info["abi"]) if info["name"] != UNVERIFIED else []
    if ups:
        row["upstream"] = json.dumps(ups)
    return row
