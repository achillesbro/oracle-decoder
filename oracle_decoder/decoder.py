"""Orchestration: resolve oracle addresses recursively and keep the results.

The cache is the cursor. A resolved address is never probed again; only
addresses that were never seen reach the RPC. Children discovered on the
way (composition slots, MODT legs, upstream targets) are probed in the next
round, up to MAX_ROUNDS (oracle -> feed is one round; a failover wrapper or
an adapter needs two).

Output format (also the cache file):

    {
      "_meta": {...},
      "oracles": {
        "<chain_id>:<address>": {kind, publisher, evidence, family, source_name, ...}
      }
    }
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from . import probe, registry, source
from .config import DEFAULT_PATH, Chain, load_chains
from .http import Http

log = logging.getLogger(__name__)

MAX_ROUNDS = 3

FIELDS = {
    "kind": "feed | vault | oracle-resolved | oracle-custom | oracle | opaque",
    "publisher": "Chainlink | Pyth | Stork | RedStone | API3 | Chronicle | ERC4626; absent = no verifiable publisher",
    "evidence": "registry > canonical-contract > code-signature > description > none",
    "family": "custom-oracle family from getters or the verified source name",
    "source_name": "Sourcify-verified contract name (implementation-resolved); unverified = no verification exists",
    "implementation": "EIP-1967 / EIP-1167 implementation behind a proxy",
    "base_feed_1..quote_vault": "MorphoChainlinkOracleV2 composition slots",
    "upstream": "feeds an adapter reads via its verified ABI: [{getter, address}]",
    "extra": "static family config (MODT primary/backup)",
    "owner / owner_status": "owner() of the probed contract; ok = an admin exists",
    "fetched_at": "when this address was probed",
}


class Decoder:
    def __init__(
        self,
        chains: dict[int, Chain] | None = None,
        cache_dir: Path | str = ".cache",
        with_source: bool = True,
    ) -> None:
        self.chains = chains or load_chains(DEFAULT_PATH)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.http = Http()
        self.with_source = with_source
        self._store_path = self.cache_dir / "decoded.json"
        self.records: dict[str, dict] = self._load_store()
        self.registry = registry.load(self.http, self.chains, self.cache_dir / "registries.json")

    # -- persistence ---------------------------------------------------------

    def _load_store(self) -> dict[str, dict]:
        if self._store_path.exists():
            return json.loads(self._store_path.read_text()).get("oracles", {})
        return {}

    def save(self, path: Path | str | None = None) -> Path:
        out = Path(path) if path else self._store_path
        doc = {
            "_meta": {
                "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
                "rows": len(self.records),
                "key": "chain_id:address (lowercase)",
                "fields": FIELDS,
            },
            "oracles": dict(sorted(self.records.items())),
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(doc, indent=1))
        return out

    # -- resolution ----------------------------------------------------------

    def resolve(self, chain_id: int, addresses: list[str]) -> dict[str, dict]:
        """Probe the given addresses and their children; return the records
        for every address touched (cached ones included)."""
        chain = self.chains[chain_id]
        todo = {a.lower() for a in addresses}
        touched: set[str] = set()
        for _ in range(MAX_ROUNDS):
            todo = {a for a in todo if f"{chain_id}:{a}" not in self.records}
            if not todo:
                break
            fetched_at = datetime.now(UTC).replace(microsecond=0).isoformat()
            raw = probe.probe_addresses(chain.rpc_url, sorted(todo))
            children: set[str] = set()
            for addr in sorted(todo):
                if addr not in raw:
                    continue  # failed chunk: stays unknown, retried next call
                row = probe.classify(raw[addr], address=addr, chain_id=chain_id, registry=self.registry)
                if self.with_source:
                    source.enrich(row, self.http, chain.rpc_url, chain_id, addr)
                row["fetched_at"] = fetched_at
                self.records[f"{chain_id}:{addr}"] = {k: v for k, v in row.items() if v is not None}
                touched.add(addr)
                children |= probe.child_addresses(row)
            todo = children
        self.save()
        keys = {f"{chain_id}:{a}" for a in {*touched, *(a.lower() for a in addresses)}}
        return {k: self.records[k] for k in sorted(keys) if k in self.records}

    def market_view(self, chain_id: int, oracle: str) -> dict:
        """One market's oracle with its legs labeled from the records."""
        key = f"{chain_id}:{oracle.lower()}"
        row = self.records.get(key)
        if row is None:
            return {"address": oracle.lower(), "resolved": False}
        legs = []
        for col in probe.COMPOSITION_COLS:
            leg = row.get(col)
            if not leg:
                continue
            target = self.records.get(f"{chain_id}:{leg}", {})
            legs.append(
                {
                    "role": col,
                    "address": leg,
                    "publisher": target.get("publisher"),
                    "evidence": target.get("evidence"),
                    "description": target.get("description") or target.get("name"),
                    "source_name": target.get("source_name"),
                }
            )
        return {
            "address": oracle.lower(),
            "resolved": True,
            "kind": row.get("kind"),
            "family": row.get("family"),
            "source_name": row.get("source_name"),
            "owner_status": row.get("owner_status"),
            "legs": legs,
            "upstream": [
                {**u, **{k: self.records.get(f"{chain_id}:{u['address']}", {}).get(k) for k in ("publisher", "evidence", "description", "source_name")}}
                for u in json.loads(row["upstream"])
            ]
            if row.get("upstream")
            else [],
            "modt": json.loads(row["extra"]).get("modt") if row.get("extra") else None,
        }

    def close(self) -> None:
        self.http.close()
