#!/usr/bin/env python3
"""Verify an ns3-kg installation end to end.

Launches the MCP server exactly the way an agent client would (as a
subprocess over stdio), then exercises a few tools against your index and
reports PASS/FAIL.

Run it with the interpreter from your virtual environment:

    .venv/bin/python check_install.py --db ~/ns3-kg/ns3.db          (Linux/macOS)
    .venv\\Scripts\\python check_install.py --db C:\\path\\to\\ns3.db   (Windows)

Note: you never launch the server yourself for normal use -- your agent
client does that. This script is only a health check.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _fail(msg: str, hint: str) -> None:
    print(f"\nFAIL: {msg}\n      {hint}")
    raise SystemExit(1)


async def _run(db: Path) -> None:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as e:  # pragma: no cover - environment problem
        _fail(
            f"the MCP library is not importable ({e})",
            'Install the project first: pip install -e ".[dev]"',
        )

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ns3kg.server.app", "--db", str(db)],
    )

    print(f"index:  {db}")
    print(f"python: {sys.executable}")
    print("\nstarting the server the way a client would ...")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = sorted(t.name for t in (await session.list_tools()).tools)
            print(f"  tools exposed: {len(tools)}")
            if len(tools) != 9:
                _fail(
                    f"expected 9 tools, got {len(tools)}: {tools}",
                    "The installed package may be out of date; reinstall with "
                    'pip install -e ".[dev]" --upgrade',
                )

            status = json.loads(
                (await session.call_tool("get_index_status", {})).content[0].text
            )
            print(f"  files indexed: {status['files_indexed']:,}")
            print(f"  symbols:       {status['symbols']:,}")
            if not status["files_indexed"]:
                _fail(
                    "the index is empty",
                    "Rebuild it: ns3kg-index <path-to-ns-3> --db " + str(db),
                )
            if status["warning"]:
                print(f"  NOTE: {status['warning']}")

            res = json.loads(
                (
                    await session.call_tool(
                        "search_symbols", {"query": "WifiMac", "limit": 3}
                    )
                ).content[0].text
            )
            if "error" in res or not res.get("results"):
                _fail(
                    "a search for 'WifiMac' returned nothing",
                    "Is the index built from an ns-3 tree? Check the path you "
                    "passed to ns3kg-index.",
                )
            top = res["results"][0]
            print(f"  sample hit:    {top['qualified_name']}  ({top['location']})")

    print("\nPASS - the server runs and answers queries.")
    print("Next: register it in your agent client (see INSTALL.md section 4).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Health-check an ns3-kg install.")
    ap.add_argument("--db", required=True, help="path to the SQLite index")
    args = ap.parse_args()

    db = Path(args.db).expanduser()
    if not db.is_file():
        _fail(
            f"no index at {db}",
            "Build one first:  ns3kg-index <path-to-ns-3> --db " + str(db),
        )

    asyncio.run(_run(db))


if __name__ == "__main__":
    main()
