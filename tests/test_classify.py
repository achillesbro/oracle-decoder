"""The ladder (pure). Publisher is evidence-graded, never a convention."""

import json

from oracle_decoder import probe
from oracle_decoder.registry import Registry

ZERO = probe.ZERO_ADDR
A1, A2, A3 = "0x" + "1" * 40, "0x" + "2" * 40, "0x" + "3" * 40
PYTH, STORK, API3 = "0x" + "4" * 40, "0x" + "5" * 40, "0x" + "6" * 40
REG = Registry(
    chainlink={1: {A1}},
    redstone={1: {A2, "0x" + "ad" * 20}},
    api3_server={1: API3},
    pyth={1: {PYTH}},
    stork={1: {STORK}},
)


def cls(raw, address=A3):
    return probe.classify(raw, address=address, chain_id=1, registry=REG)


def test_chainlink_by_registry_even_without_convention_description():
    # A Chainlink exchange-rate feed: proxy signature answers, description
    # breaks the "X / Y" convention. Registry membership settles it.
    row = cls({"description": "weETH / eETH Exchange Rate", "decimals": 18, "phaseId": 2, "aggregator": A3}, address=A1)
    assert (row["kind"], row["publisher"], row["evidence"]) == ("feed", "Chainlink", "registry")


def test_chainlink_signature_without_registry_is_code_grade():
    row = cls({"description": "BTC / USD", "decimals": 8, "phaseId": 1, "aggregator": A3}, address=A2)
    assert (row["publisher"], row["evidence"]) == ("Chainlink", "code-signature")


def test_convention_description_alone_is_not_a_publisher():
    row = cls({"description": "mHYPER/USD", "decimals": 8})
    assert row["kind"] == "feed"
    assert row["publisher"] is None
    assert row["evidence"] == "description"


def test_pyth_needs_the_canonical_contract_not_price_id():
    pid = "0x" + "ab" * 32
    row = cls({"priceId": pid, "decimals": 8, "pyth": PYTH})
    assert (row["publisher"], row["evidence"], row["price_id"]) == ("Pyth", "canonical-contract", pid)
    # Stork forks the Pyth adapter: same priceId(), but stork() answers.
    row = cls({"priceId": pid, "decimals": 8, "stork": STORK})
    assert (row["publisher"], row["evidence"]) == ("Stork", "canonical-contract")
    # priceId alone proves nothing.
    row = cls({"priceId": pid, "decimals": 8})
    assert row["publisher"] is None and row["evidence"] == "description"


def test_api3_redstone_chronicle_grades():
    row = cls({"api3ServerV1": API3, "decimals": 18})
    assert (row["kind"], row["publisher"], row["evidence"]) == ("feed", "API3", "canonical-contract")
    rs = {"description": "RedStone Price Feed for HYPE", "decimals": 8, "getDataFeedId": "0x" + "00" * 32, "getPriceFeedAdapter": "0x" + "ad" * 20}
    row = cls(rs)
    assert (row["publisher"], row["evidence"]) == ("RedStone", "registry")  # adapter listed
    rs["getPriceFeedAdapter"] = "0x" + "bb" * 20
    row = cls(rs)
    assert (row["publisher"], row["evidence"]) == ("RedStone", "code-signature")
    row = cls({"wat": "0x" + "00" * 32, "decimals": 18})
    assert (row["publisher"], row["evidence"]) == ("Chronicle", "code-signature")


def test_without_registries_grades_degrade_to_signature():
    row = probe.classify({"description": "BTC / USD", "phaseId": 2, "aggregator": A3})
    assert (row["publisher"], row["evidence"]) == ("Chainlink", "code-signature")


def test_vault_modt_resolved_constant_peg_opaque_partial():
    row = cls({"name": "Staked USDai", "symbol": "sUSDai", "decimals": 18})
    assert (row["kind"], row["publisher"], row["evidence"]) == ("vault", "ERC4626", "code-signature")

    row = cls({"primaryOracle": A1, "backupOracle": A2})
    assert (row["kind"], row["family"]) == ("oracle-custom", "meta-deviation-timelock")
    assert json.loads(row["extra"])["modt"] == {"primary": A1, "backup": A2}

    row = cls({"BASE_FEED_1": A1, "BASE_FEED_2": ZERO})
    assert row["kind"] == "oracle-resolved"
    assert row["base_feed_1"] == A1 and row["base_feed_2"] is None

    row = cls({k: ZERO for k in probe.STRUCT_COLS})
    assert (row["kind"], row["family"]) == ("oracle-custom", "constant-peg")

    assert cls({})["kind"] == "opaque"
    assert cls({"decimals": 18})["kind"] == "oracle"
    assert cls({})["evidence"] == "none"


def test_children_composition_modt_upstream():
    row = cls({"BASE_FEED_1": A1, "QUOTE_VAULT": A2})
    assert probe.child_addresses(row) == {A1, A2}
    row = cls({"primaryOracle": A1, "backupOracle": A2})
    assert probe.child_addresses(row) == {A1, A2}
    row["upstream"] = json.dumps([{"getter": "CHAINLINK_SOURCE", "address": A3}])
    assert probe.child_addresses(row) == {A1, A2, A3}
