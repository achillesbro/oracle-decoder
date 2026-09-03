"""Command line.

    oracle-decoder resolve <chain_id> <address> [<address> ...]
        Probe the addresses (and their children), print the records, keep them
        in the cache. Add --market to print the labeled market view instead.

    oracle-decoder export [--out decoded.json]
        Write every cached record in the output format.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .decoder import Decoder


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="oracle-decoder", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cache-dir", default=".cache", help="registry cache + decoded records (default .cache)")
    p.add_argument("--chains", default=None, help="chains.json override")
    p.add_argument("--no-source", action="store_true", help="skip the Sourcify enrichment")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="probe addresses and their children")
    r.add_argument("chain_id", type=int)
    r.add_argument("addresses", nargs="+")
    r.add_argument("--market", action="store_true", help="print the labeled market view of each address")

    e = sub.add_parser("export", help="write every cached record")
    e.add_argument("--out", default="decoded.json")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    from .config import DEFAULT_PATH, load_chains

    chains = load_chains(args.chains or DEFAULT_PATH)
    dec = Decoder(chains=chains, cache_dir=args.cache_dir, with_source=not args.no_source)
    try:
        if args.cmd == "resolve":
            if args.chain_id not in chains:
                print(f"chain {args.chain_id} is not configured in chains.json", file=sys.stderr)
                return 2
            records = dec.resolve(args.chain_id, args.addresses)
            if args.market:
                out = {a.lower(): dec.market_view(args.chain_id, a) for a in args.addresses}
            else:
                out = records
            print(json.dumps(out, indent=1))
        elif args.cmd == "export":
            path = dec.save(args.out)
            print(f"{len(dec.records)} records -> {path}")
    finally:
        dec.close()
    return 0
