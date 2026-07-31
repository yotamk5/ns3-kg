"""ns3kg-index: build/update the SQLite index for a source tree."""

from __future__ import annotations

import argparse
from pathlib import Path

from .indexer import walker


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="ns3kg-index",
        description="Index a C++/Python source tree (e.g. ns-3 src/wifi) into a "
        "SQLite database for the ns3-kg MCP server.",
    )
    ap.add_argument("root", help="directory to index")
    ap.add_argument(
        "--db", default="ns3kg.db", help="SQLite output path (default: ./ns3kg.db)"
    )
    ap.add_argument(
        "--full", action="store_true", help="force re-parse of all files"
    )
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        ap.error(f"not a directory: {root}")

    stats = walker.index_directory(root, args.db, full=args.full, log=print)

    print(
        f"indexed {root} -> {args.db}\n"
        f"  parsed: {stats['parsed']}  unchanged: {stats['unchanged']}  "
        f"failed: {stats['failed']}  removed: {stats['removed']}  "
        f"({stats['seconds']}s)"
    )
    for table, n in stats["tables"].items():
        print(f"  {table}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
