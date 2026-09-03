"""Publisher registries: the authoritative answer to "who operates this
feed". A publisher is asserted only at registry or canonical-contract grade.
A contract's self-description is never treated as identity: Stork forks the
Pyth adapter including `priceId()`, and Chainlink's exchange-rate and NAV
feeds do not follow the "BASE / QUOTE" description format.

Sources (all public, no API key):

- Chainlink: the reference-data-directory JSON per network, the data the
  documentation renders. Membership = proxyAddress | secondaryProxyAddress
  (SVR-enabled feeds record the standard proxy there) | contractAddress.
- RedStone: the relayer manifests in redstone-oracles-monorepo. Every
  priceFeedAddress plus each adapterContract. A feed removed from the
  manifest still counts when its adapter is listed.
- API3: deployments/addresses.json in api3dao/contracts, the canonical
  Api3ServerV1 per chain. A proxy's immutable api3ServerV1() must match.
- Pyth, Stork: canonical contract addresses from the publishers'
  documentation, pinned in chains.json. An adapter's pyth() / stork() target
  must be one of them.
- Chronicle: no machine-readable registry exists. Code-signature grade.

The registries are cached to a JSON file. When a source is unavailable the
cached copy is used, then an empty registry: classification degrades to
code-signature grade and never fails.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config import Chain
from .http import Http

log = logging.getLogger(__name__)

CHAINLINK_DIRECTORY = "https://reference-data-directory.vercel.app/{file}.json"
REDSTONE_MANIFEST_DIRS = [
    "https://api.github.com/repos/redstone-finance/redstone-oracles-monorepo/contents/"
    "packages/relayer-remote-config/main/relayer-manifests",
    "https://api.github.com/repos/redstone-finance/redstone-oracles-monorepo/contents/"
    "packages/relayer-remote-config/main/relayer-manifests-multi-feed",
]
API3_ADDRESSES = "https://raw.githubusercontent.com/api3dao/contracts/main/deployments/addresses.json"


@dataclass
class Registry:
    """Lowercase address sets per chain_id."""

    chainlink: dict[int, set[str]] = field(default_factory=dict)
    redstone: dict[int, set[str]] = field(default_factory=dict)
    api3_server: dict[int, str] = field(default_factory=dict)
    pyth: dict[int, set[str]] = field(default_factory=dict)
    stork: dict[int, set[str]] = field(default_factory=dict)
    fetched_at: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "fetched_at": self.fetched_at,
                "chainlink": {str(k): sorted(v) for k, v in self.chainlink.items()},
                "redstone": {str(k): sorted(v) for k, v in self.redstone.items()},
                "api3_server": {str(k): v for k, v in self.api3_server.items()},
                "pyth": {str(k): sorted(v) for k, v in self.pyth.items()},
                "stork": {str(k): sorted(v) for k, v in self.stork.items()},
            },
            indent=1,
        )

    @classmethod
    def from_json(cls, text: str) -> Registry:
        d = json.loads(text)

        def sets(m: dict) -> dict[int, set[str]]:
            return {int(k): set(v) for k, v in m.items()}

        return cls(
            chainlink=sets(d.get("chainlink", {})),
            redstone=sets(d.get("redstone", {})),
            api3_server={int(k): v for k, v in d.get("api3_server", {}).items()},
            pyth=sets(d.get("pyth", {})),
            stork=sets(d.get("stork", {})),
            fetched_at=d.get("fetched_at"),
        )

    def is_empty(self) -> bool:
        return not (self.chainlink or self.redstone or self.api3_server)


EMPTY = Registry()


def fetch(http: Http, chains: dict[int, Chain]) -> Registry:
    """Pull every registry for the configured chains. A failing source is
    logged and left empty for this run."""
    reg = Registry(fetched_at=datetime.now(UTC).replace(microsecond=0).isoformat())
    for chain in chains.values():
        reg.pyth[chain.chain_id] = set(chain.pyth_contracts)
        if chain.stork_contract:
            reg.stork[chain.chain_id] = {chain.stork_contract}
        if chain.chainlink_registry:
            try:
                entries = http.get_json(CHAINLINK_DIRECTORY.format(file=chain.chainlink_registry))
                reg.chainlink[chain.chain_id] = {
                    e[k].lower()
                    for e in entries
                    for k in ("proxyAddress", "secondaryProxyAddress", "contractAddress")
                    if isinstance(e, dict) and e.get(k)
                }
            except Exception:
                log.exception("chainlink registry fetch failed for chain %d", chain.chain_id)

    try:
        api3 = http.get_json(API3_ADDRESSES)
        servers = api3.get("Api3ServerV1", {}) if isinstance(api3, dict) else {}
        for chain in chains.values():
            addr = servers.get(str(chain.chain_id))
            if addr:
                reg.api3_server[chain.chain_id] = addr.lower()
    except Exception:
        log.exception("api3 addresses fetch failed")

    slugs = {c.chain_id: c.redstone_slug.lower() for c in chains.values() if c.redstone_slug}
    if slugs:
        try:
            for dir_url in REDSTONE_MANIFEST_DIRS:
                for entry in http.get_json(dir_url):
                    name = entry.get("name", "").lower()
                    if not name.endswith(".json"):
                        continue
                    for chain_id, slug in slugs.items():
                        if slug in name:
                            _redstone_manifest(http, entry["download_url"], reg, chain_id)
        except Exception:
            log.exception("redstone manifest listing failed")
    return reg


def _redstone_manifest(http: Http, url: str, reg: Registry, chain_id: int) -> None:
    try:
        m = http.get_json(url)
    except Exception:
        log.exception("redstone manifest fetch failed: %s", url)
        return
    if not isinstance(m, dict):
        return
    addrs = reg.redstone.setdefault(chain_id, set())
    if m.get("adapterContract"):
        addrs.add(str(m["adapterContract"]).lower())
    for v in (m.get("priceFeeds") or {}).values():
        a = v.get("priceFeedAddress") if isinstance(v, dict) else v
        if isinstance(a, str) and a.startswith("0x"):
            addrs.add(a.lower())


def load(http: Http, chains: dict[int, Chain], cache: Path, max_age_hours: float = 24.0) -> Registry:
    """The cached registries when fresh enough, else a fresh fetch (cached on
    success), else the stale cache, else empty."""
    if cache.exists():
        cached = Registry.from_json(cache.read_text())
        if cached.fetched_at:
            age = datetime.now(UTC) - datetime.fromisoformat(cached.fetched_at)
            if age.total_seconds() < max_age_hours * 3600:
                return cached
    reg = fetch(http, chains)
    if not reg.is_empty():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(reg.to_json())
        return reg
    if cache.exists():
        log.warning("registries: live fetch failed, using the cached copy")
        return Registry.from_json(cache.read_text())
    log.warning("registries unavailable: publishers degrade to code-signature grade")
    return EMPTY
