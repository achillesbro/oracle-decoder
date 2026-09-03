"""The probe and the ladder.

Morpho Blue oracle configuration is immutable: an oracle address, the feeds
it reads, and each feed's publisher never change after deployment. Each
address is therefore probed ONCE and the result is kept.

One probe = 22 view calls with no arguments (identity getters, the six
MorphoChainlinkOracleV2 composition slots, owner, the two MODT getters, and
eight publisher code signatures), batched through Multicall3 aggregate3 with
allowFailure so a missing getter reverts on its sub-call only. The set of
getters that answer is the signal.

`classify` turns the raw answers plus the publisher registries into one
record. It is a pure function and is unit-tested.
"""

from __future__ import annotations

import json
import logging
import time

from .registry import EMPTY as EMPTY_REGISTRY
from .registry import Registry

log = logging.getLogger(__name__)

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
ZERO_ADDR = "0x" + "0" * 40
ZERO_B32 = "0x" + "0" * 64
CHUNK = 80  # sub-calls per aggregate3 batch

# (key, solidity signature, return abi type). All zero-argument getters.
PROBES: list[tuple[str, str, str]] = [
    # Identity probes: feeds and ERC4626 vault hooks.
    ("description", "description()", "string"),
    ("name", "name()", "string"),
    ("symbol", "symbol()", "string"),
    ("decimals", "decimals()", "uint8"),
    ("priceId", "priceId()", "bytes32"),
    # MorphoChainlinkOracleV2 composition slots.
    ("BASE_FEED_1", "BASE_FEED_1()", "address"),
    ("BASE_FEED_2", "BASE_FEED_2()", "address"),
    ("QUOTE_FEED_1", "QUOTE_FEED_1()", "address"),
    ("QUOTE_FEED_2", "QUOTE_FEED_2()", "address"),
    ("BASE_VAULT", "BASE_VAULT()", "address"),
    ("QUOTE_VAULT", "QUOTE_VAULT()", "address"),
    # Ownable.owner(): an admin that can change what the contract serves.
    ("owner", "owner()", "address"),
    # Steakhouse MetaOracleDeviationTimelock (primary/backup failover).
    ("primaryOracle", "primaryOracle()", "address"),
    ("backupOracle", "backupOracle()", "address"),
    # Publisher code signatures, corroborated against the registries.
    ("phaseId", "phaseId()", "uint16"),  # Chainlink EACAggregatorProxy
    ("aggregator", "aggregator()", "address"),  # Chainlink EACAggregatorProxy
    ("pyth", "pyth()", "address"),  # PythAggregatorV3 -> the Pyth contract
    ("stork", "stork()", "address"),  # StorkChainlinkAdapter -> the Stork contract
    ("getDataFeedId", "getDataFeedId()", "bytes32"),  # RedStone PriceFeedBase
    ("getPriceFeedAdapter", "getPriceFeedAdapter()", "address"),  # RedStone feed -> adapter
    ("api3ServerV1", "api3ServerV1()", "address"),  # Api3ReaderProxyV1 -> Api3ServerV1
    ("wat", "wat()", "bytes32"),  # Chronicle Scribe pair id
]

STRUCT_COLS = {
    "BASE_FEED_1": "base_feed_1",
    "BASE_FEED_2": "base_feed_2",
    "QUOTE_FEED_1": "quote_feed_1",
    "QUOTE_FEED_2": "quote_feed_2",
    "BASE_VAULT": "base_vault",
    "QUOTE_VAULT": "quote_vault",
}
COMPOSITION_COLS = list(STRUCT_COLS.values())

MULTICALL3_ABI = [
    {
        "name": "aggregate3",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {
                "name": "returnData",
                "type": "tuple[]",
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"},
                ],
            }
        ],
    }
]


def _nonzero(v: object) -> str | None:
    return v if isinstance(v, str) and v != ZERO_ADDR else None


def probe_addresses(rpc_url: str, addresses: list[str]) -> dict[str, dict]:
    """Probe each address with every getter in PROBES through Multicall3.
    Returns {address: raw} where raw maps probe key -> decoded value for the
    calls that succeeded and decoded.

    An address touched by a FAILED multicall chunk (an RPC outage that
    outlives the retries) is dropped from the result. "Never probed" must stay
    distinct from "probed, nothing answered" (opaque), or a transient outage
    writes permanent opaque records that are never revisited."""
    from eth_abi import decode as abi_decode
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    mc = w3.eth.contract(address=Web3.to_checksum_address(MULTICALL3), abi=MULTICALL3_ABI)
    selectors = {key: Web3.keccak(text=sig)[:4] for key, sig, _ in PROBES}

    calls = [(Web3.to_checksum_address(a), True, selectors[key]) for a in addresses for key, _, _ in PROBES]
    meta = [(a, key, rtype) for a in addresses for key, _, rtype in PROBES]

    raw: dict[str, dict] = {a: {} for a in addresses}
    failed: set[str] = set()
    for i in range(0, len(calls), CHUNK):
        results = None
        for attempt in range(4):  # public RPCs throttle bursts: back off and retry
            try:
                results = mc.functions.aggregate3(calls[i : i + CHUNK]).call()
                break
            except Exception:
                time.sleep(1.5 * (attempt + 1))
        if results is None:
            log.warning("multicall chunk %d-%d failed after retries", i, i + CHUNK)
            failed.update(a for a, _, _ in meta[i : i + CHUNK])
            continue
        time.sleep(0.2)
        for (addr, key, rtype), (success, data) in zip(meta[i : i + CHUNK], results):
            if not success or not data:
                continue
            try:
                value = abi_decode([rtype], data)[0]
            except Exception:
                continue  # malformed return data
            if rtype == "bytes32":
                value = "0x" + value.hex()
            elif rtype == "address":
                value = value.lower()
            raw[addr.lower()][key] = value
    for a in failed:
        raw.pop(a.lower(), None)
    return raw


def classify(
    raw: dict,
    *,
    address: str | None = None,
    chain_id: int | None = None,
    registry: Registry | None = None,
) -> dict:
    """The ladder: one address's raw answers -> one record. First match wins.

    Publisher evidence grades (`evidence`):
      registry            the address is in the publisher's own feed list
      canonical-contract  the adapter points at the publisher's canonical contract
      code-signature      the publisher ABI shape answered; nothing else confirms it
      description         a described feed with no verifiable publisher (publisher null)
      none                not a feed (composed / bespoke oracle, opaque, unclassified)
    """
    reg = registry or EMPTY_REGISTRY
    addr = (address or "").lower()
    row: dict = {
        "kind": None,
        "publisher": None,
        "evidence": None,
        "description": raw.get("description") or None,
        "name": raw.get("name") or None,
        "symbol": raw.get("symbol") or None,
        "decimals": raw.get("decimals") if isinstance(raw.get("decimals"), int) else None,
        "price_id": None,
        "owner": None,
        "owner_status": "none",
        "family": None,
        "implementation": None,
        "source_name": None,
        "upstream": None,
        "extra": None,
        **{c: None for c in COMPOSITION_COLS},
    }

    # Composition: structure getters that answered with a non-zero address.
    struct_answered = 0
    struct_nonzero = 0
    for key, col in STRUCT_COLS.items():
        v = raw.get(key)
        if not isinstance(v, str):
            continue
        struct_answered += 1
        if v != ZERO_ADDR:
            struct_nonzero += 1
            row[col] = v
    has_composition = struct_nonzero > 0

    owner = _nonzero(raw.get("owner"))
    if owner:
        row["owner"] = owner
        row["owner_status"] = "ok"

    desc = row["description"] or ""
    price_id = raw.get("priceId")
    has_price_id = isinstance(price_id, str) and price_id != ZERO_B32
    if has_price_id:
        row["price_id"] = price_id
    primary, backup = _nonzero(raw.get("primaryOracle")), _nonzero(raw.get("backupOracle"))
    is_modt = primary is not None and backup is not None

    # -- publisher identity (feeds), strongest evidence first ----------------
    publisher: str | None = None
    evidence: str | None = None
    pyth_t, stork_t = _nonzero(raw.get("pyth")), _nonzero(raw.get("stork"))
    api3_t, rs_adapter = _nonzero(raw.get("api3ServerV1")), _nonzero(raw.get("getPriceFeedAdapter"))
    chainlink_sig = "phaseId" in raw and _nonzero(raw.get("aggregator")) is not None
    in_chainlink = addr in reg.chainlink.get(chain_id, ())
    redstone_sig = "getDataFeedId" in raw or rs_adapter is not None
    rs_listed = reg.redstone.get(chain_id, ())
    if in_chainlink or chainlink_sig:
        publisher, evidence = "Chainlink", "registry" if in_chainlink else "code-signature"
    elif pyth_t:
        publisher = "Pyth"
        evidence = "canonical-contract" if pyth_t in reg.pyth.get(chain_id, ()) else "code-signature"
    elif stork_t:
        publisher = "Stork"
        evidence = "canonical-contract" if stork_t in reg.stork.get(chain_id, ()) else "code-signature"
    elif api3_t:
        publisher = "API3"
        evidence = "canonical-contract" if api3_t == reg.api3_server.get(chain_id) else "code-signature"
    elif redstone_sig:
        publisher = "RedStone"
        evidence = "registry" if (addr in rs_listed or rs_adapter in rs_listed) else "code-signature"
    elif "wat" in raw:
        publisher, evidence = "Chronicle", "code-signature"  # no public registry exists

    if publisher:
        row["kind"] = "feed"
        row["publisher"] = publisher
        row["evidence"] = evidence
    elif desc or has_price_id:
        # A feed that describes itself, with no registry or canonical
        # contract to confirm a publisher. The brand it claims is not identity.
        row["kind"] = "feed"
        row["evidence"] = "description"
    elif row["name"] and row["symbol"] and row["decimals"] is not None:
        row["kind"] = "vault"
        row["publisher"] = "ERC4626"
        row["evidence"] = "code-signature"
    elif is_modt:
        row["kind"] = "oracle-custom"
        row["family"] = "meta-deviation-timelock"
        row["extra"] = json.dumps({"modt": {"primary": primary, "backup": backup}})
    elif has_composition:
        row["kind"] = "oracle-resolved"
    elif struct_answered == 6:
        # Every MorphoChainlinkOracleV2 slot answered address(0): the price is
        # a hardcoded constant with no live input.
        row["kind"] = "oracle-custom"
        row["family"] = "constant-peg"
    elif not raw:
        row["kind"] = "opaque"  # nothing answered: no code, an EOA, or no standard getters
    else:
        row["kind"] = "oracle"  # partial answers, not classified
    if row["evidence"] is None:
        row["evidence"] = "none"
    return row


def child_addresses(row: dict) -> set[str]:
    """Addresses referenced by a record that need their own probe:
    composition slots, MODT primary/backup, and upstream targets."""
    out = {row[c] for c in COMPOSITION_COLS if row.get(c)}
    for blob, path in ((row.get("extra"), ("modt",)), (row.get("upstream"), ())):
        if not blob:
            continue
        try:
            data = json.loads(blob) if isinstance(blob, str) else blob
        except ValueError:
            continue
        if path:  # extra.modt.{primary,backup}
            modt = (data or {}).get("modt") or {}
            for k in ("primary", "backup"):
                if isinstance(modt.get(k), str):
                    out.add(modt[k])
        else:  # upstream: [{getter, address}]
            out.update(u["address"] for u in data if isinstance(u, dict) and isinstance(u.get("address"), str))
    return out
