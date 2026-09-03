# oracle-decoder

Resolve the identity of a Morpho Blue market oracle from its address alone: the oracle contract, each feed and vault it reads, the publisher that operates each one, and the strength of the evidence for that publisher.

## Why one probe is enough

A market's oracle address is set at creation and does not change. A `MorphoChainlinkOracleV2` stores its feed and vault addresses as `immutable` constructor constants. The identity of an oracle therefore does not change after deployment, only its price does. The decoder resolves an address once and keeps the result. A new market costs one probe. A known address costs nothing.

> **Principle.** The self-description of a contract is not proof of identity. Chainlink operates exchange-rate and NAV feeds that do not follow the `"BASE / QUOTE"` description format. Stork forks the Pyth adapter and keeps its `priceId()` getter. Third parties deploy Chainlink's `EACAggregatorProxy` code as their own feeds. A publisher is asserted only from evidence that a third party cannot copy, and each record carries the grade of that evidence.

## Method

1. **Probe** (`probe.py`). 22 view calls with no arguments, batched through Multicall3 `aggregate3` with `allowFailure`. The set of getters that answer is the signal: identity getters, the six `MorphoChainlinkOracleV2` composition slots, `owner()`, the two MODT getters, and eight publisher code signatures.
2. **Registries** (`registry.py`). Chainlink's feed directory, RedStone's relayer manifests, API3's deployment addresses, and the canonical Pyth/Stork contracts from the publishers' documentation. Fetched on demand, cached in `.cache/registries.json`, refreshed after 24 hours.
3. **Ladder** (`probe.classify`). One pass down an ordered list of tests. The publisher tests come first, strongest evidence first: registry, canonical contract, code signature. A described feed that no test confirms keeps a null publisher. Then vault, failover wrapper, composed oracle, constant peg, opaque, unclassified.
4. **Verified source** (`source.py`). Sourcify returns the verified contract name and ABI without an API key. Proxies are resolved first (EIP-1967, EIP-1167). The name maps to a family (`CurveStableswapOracle → curve-stableswap`, `ChainlinkOvalBase → oval-wrapper`, ...). Address getters in the ABI that look like feed references are called and recorded as `upstream`, then probed as children.
5. **Recursion** (`decoder.py`). Composition slots, failover legs and upstream targets are probed in the next round. The cache is the cursor.

## Install and run

```bash
uv sync                      # or: pip install -e .
uv run oracle-decoder resolve 1 0x0f0072fddb300f9375c999cbcf9bdec07e7227d3
uv run oracle-decoder resolve 1 0x0f0072fddb300f9375c999cbcf9bdec07e7227d3 --market
uv run oracle-decoder export --out decoded.json
uv run pytest
```

`resolve` prints every record it touched and keeps them in `.cache/decoded.json`. `--market` prints the labeled view of one oracle: its legs with publishers and evidence, its upstream feeds, its failover configuration. `--no-source` skips Sourcify. `chains.json` holds the RPC endpoint and the registry inputs per chain; edit it to add a chain.

## Output

```json
{
  "_meta": { "generated_at": "...", "rows": 3, "key": "chain_id:address (lowercase)", "fields": { ... } },
  "oracles": {
    "1:0x0f0072fddb300f9375c999cbcf9bdec07e7227d3": {
      "kind": "oracle-custom", "evidence": "none", "family": "oval-wrapper",
      "source_name": "ChainlinkOvalBase", "owner_status": "none", "decimals": 18,
      "upstream": "[{\"getter\": \"CHAINLINK_SOURCE\", \"address\": \"0xee9f...1d46\"}]",
      "fetched_at": "2026-09-03T09:42:00+00:00"
    },
    "1:0xee9f2375b4bdf6387aa8265dd4fb8f16512a1d46": {
      "kind": "feed", "publisher": "Chainlink", "evidence": "registry",
      "description": "USDT / ETH", "decimals": 18, "source_name": "EACAggregatorProxy", ...
    }
  }
}
```

| Field | Values |
|---|---|
| `kind` | `feed` · `vault` · `oracle-resolved` · `oracle-custom` · `oracle` (partial answers) · `opaque` (no answers) |
| `publisher` | `Chainlink` · `Pyth` · `Stork` · `RedStone` · `API3` · `Chronicle` · `ERC4626`; absent when no evidence confirms one |
| `evidence` | `registry` > `canonical-contract` > `code-signature` > `description` > `none` |
| `family` | `meta-deviation-timelock` · `constant-peg` · `curve-stableswap` · `pendle-pt` · `mux-lp` · `oval-wrapper` · `router` · `clamp` · `fixed-feed` · `chainlink-code` · `exchange-rate-adapter` · `dex-twap` · `nav-adapter` |
| `source_name` | Sourcify-verified contract name, implementation-resolved; `unverified` when none exists |
| `base_feed_1` … `quote_vault` | the six composition slots |
| `upstream` | `[{getter, address}]` feeds an adapter reads through its verified ABI |
| `extra` | static family config (MODT primary/backup) |
| `owner`, `owner_status` | `owner()` of the probed contract; `ok` means an admin exists |

## Limitations

- Chronicle has no machine-readable registry. Its feeds stay at code-signature grade.
- The probe detects a failover wrapper but not its thresholds and timelocks. Those need one call per address.
- `owner()` describes the probed contract, not the upstream operators.
- Many bespoke contracts are not verified on Sourcify. The `description` grade exists for them.
- The decoder resolves identity, not health. Live failover state, staleness and price liveness are a different task.

## License

MIT.
