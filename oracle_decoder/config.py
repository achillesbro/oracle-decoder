"""Chain configuration: RPC endpoint and the publisher registry inputs per
chain. Loaded from chains.json next to this module, or from a path you pass."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(__file__).parent / "chains.json"


@dataclass(frozen=True)
class Chain:
    chain_id: int
    name: str
    rpc_url: str
    chainlink_registry: str | None = None  # reference-data-directory file stem
    redstone_slug: str | None = None  # substring selecting this chain's RedStone manifests
    pyth_contracts: tuple[str, ...] = field(default_factory=tuple)  # canonical Pyth contract(s)
    stork_contract: str | None = None  # canonical Stork contract


def load_chains(path: Path | str = DEFAULT_PATH) -> dict[int, Chain]:
    raw = json.loads(Path(path).read_text())
    out: dict[int, Chain] = {}
    for cid, c in raw["chains"].items():
        out[int(cid)] = Chain(
            chain_id=int(cid),
            name=c["name"],
            rpc_url=c["rpc_url"],
            chainlink_registry=c.get("chainlink_registry"),
            redstone_slug=c.get("redstone_slug"),
            pyth_contracts=tuple(a.lower() for a in c.get("pyth_contracts", [])),
            stork_contract=(c.get("stork_contract") or "").lower() or None,
        )
    return out
