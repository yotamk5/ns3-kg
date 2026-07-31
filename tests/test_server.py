"""Integration test: launch the MCP server as a subprocess and exercise every
tool over real MCP stdio, against an index of tests/fixtures."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ns3kg.indexer import walker

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def server_db(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("srv") / "kg.db"
    walker.index_directory(FIXTURES, db_path)
    return db_path


def _payload(result):
    """Tool results arrive as JSON text content; parse it."""
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


async def _exercise_all_tools(db_path):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "ns3kg.server.app", "--db", str(db_path)]
    )
    out = {}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            tools = await s.list_tools()
            out["tool_names"] = sorted(t.name for t in tools.tools)

            out["search"] = _payload(
                await s.call_tool("search_symbols", {"query": "ApWifiMac", "limit": 5})
            )
            out["search_paged"] = _payload(
                await s.call_tool(
                    "search_symbols", {"query": "ApWifiMac", "limit": 5, "cursor": "5"}
                )
            )
            out["sig"] = _payload(
                await s.call_tool("get_signature", {"qualified_name": "WifiMac::Enqueue"})
            )
            out["sig_missing"] = _payload(
                await s.call_tool("get_signature", {"qualified_name": "Enqueeu"})
            )
            out["source"] = _payload(
                await s.call_tool(
                    "get_source",
                    {"file": "ap-wifi-mac.h", "start_line": 55, "end_line": 70},
                )
            )
            out["source_big"] = _payload(
                await s.call_tool(
                    "get_source",
                    {"file": "ap-wifi-mac.cc", "start_line": 1, "end_line": 3000},
                )
            )
            out["usages"] = _payload(
                await s.call_tool("find_usages", {"symbol_name": "AddTraceSource"})
            )
            out["bad_cursor"] = _payload(
                await s.call_tool(
                    "search_symbols", {"query": "Wifi", "cursor": "banana"}
                )
            )
            out["status"] = _payload(await s.call_tool("get_index_status", {}))

            # --- Stage 3 tools ---
            out["typeid"] = _payload(
                await s.call_tool("get_typeid_attributes", {"class_name": "ApWifiMac"})
            )
            out["typeid_miss"] = _payload(
                await s.call_tool("get_typeid_attributes", {"class_name": "ApWifiMacc"})
            )
            out["traces"] = _payload(
                await s.call_tool("find_trace_sources", {"pattern": "Assoc"})
            )
            out["chain"] = _payload(
                await s.call_tool("get_inheritance_chain", {"class_name": "ApWifiMac"})
            )
            out["chain_children"] = _payload(
                await s.call_tool("get_inheritance_chain", {"class_name": "WifiMac"})
            )
            out["config"] = _payload(
                await s.call_tool(
                    "resolve_config_path",
                    {
                        "path_string": "/NodeList/*/DeviceList/*/"
                        "$ns3::WifiNetDevice/Mac/$ns3::ApWifiMac/AssociatedSta"
                    },
                )
            )
            out["config_global"] = _payload(
                await s.call_tool(
                    "resolve_config_path",
                    {"path_string": "/NodeList/3/EnableBeaconJitter"},
                )
            )
            out["config_miss"] = _payload(
                await s.call_tool(
                    "resolve_config_path",
                    {"path_string": "/NodeList/*/$ns3::ApWifiMac/Bogus"},
                )
            )
    return out


def test_all_tools_over_stdio(server_db):
    out = asyncio.run(_exercise_all_tools(server_db))

    assert out["tool_names"] == [
        "find_trace_sources",
        "find_usages",
        "get_index_status",
        "get_inheritance_chain",
        "get_signature",
        "get_source",
        "get_typeid_attributes",
        "resolve_config_path",
        "search_symbols",
    ]

    # search_symbols: finds the class, respects limit, pages with cursor
    search = out["search"]
    assert len(search["results"]) == 5
    assert search["truncated"] is True and search["next_cursor"] == "5"
    assert search["results"][0]["qualified_name"] == "ns3::ApWifiMac"
    assert search["results"][0]["location"] == "ap-wifi-mac.h:61"
    paged = out["search_paged"]
    assert paged["results"][0] != search["results"][0]

    # get_signature: all WifiMac::Enqueue overloads from wifi-mac.h
    sigs = [r["signature"] for r in out["sig"]["results"]]
    assert len(sigs) >= 3  # 3 public overloads + the pure-virtual one
    assert any("Ptr<Packet> packet, Mac48Address to, Mac48Address from" in s for s in sigs)

    # get_signature miss: instructive error with suggestions
    miss = out["sig_missing"]
    assert "error" in miss and "search_symbols" in miss["hint"]
    assert any("Enqueue" in c for c in miss["closest_matches"])

    # get_source: exact slice + total line count
    src = out["source"]
    assert "class ApWifiMac : public WifiMac" in src["source"]
    assert src["total_lines"] > 800
    # 3000-line request is capped at 200 lines
    big = out["source_big"]
    assert big["end_line"] - big["start_line"] + 1 == 200
    assert big["truncated"] is True

    # find_usages: textual candidates with enclosing function
    usages = out["usages"]
    assert usages["total"] > 0
    assert "virtual dispatch" in usages["note"]
    assert any(
        u["in_function"] == "ns3::ApWifiMac::GetTypeId" for u in usages["results"]
    )

    # malformed cursor: instructive error, not a crash
    assert "error" in out["bad_cursor"] and "cursor" in out["bad_cursor"]["hint"]

    # index status
    status = out["status"]
    assert status["files_indexed"] == 5
    assert status["warning"] is None

    # get_typeid_attributes: 15 attributes + 2 AddTraceSource traces, one page
    typeid = out["typeid"]
    assert typeid["class_name"] == "ns3::ApWifiMac"
    assert typeid["total"] == 17 and typeid["truncated"] is False
    kinds = {r["kind"] for r in typeid["results"]}
    assert kinds == {"attribute", "trace_source"}
    beacon = next(r for r in typeid["results"] if r.get("attr_name") == "BeaconInterval")
    assert beacon["attr_type"] == "Time"
    assert any(r.get("trace_name") == "AssociatedSta" for r in typeid["results"])

    # unknown class: instructive error pointing at the right recovery tools
    miss = out["typeid_miss"]
    assert "error" in miss and "get_inheritance_chain" in miss["hint"]
    assert "ApWifiMac" in miss["closest_matches"]

    # find_trace_sources: public AddTraceSource rows sort before raw members
    traces = out["traces"]
    assert traces["results"][0]["origin"] == "AddTraceSource"
    names = [r["trace_name"] for r in traces["results"]]
    assert "AssociatedSta" in names and "m_assocLogger" in names

    # get_inheritance_chain: parent first, out-of-index base has null location
    chain = out["chain"]
    assert chain["class"] == "ns3::ApWifiMac"
    assert chain["ancestors"][0]["qualified_name"] == "ns3::WifiMac"
    assert chain["ancestors"][0]["location"] == "wifi-mac.h:89"
    obj = next(a for a in chain["ancestors"] if a["qualified_name"] == "Object")
    assert obj["location"] is None
    kids = out["chain_children"]
    assert any(
        c["qualified_name"] == "ns3::ApWifiMac" for c in kids["direct_children"]
    )

    # resolve_config_path: anchored on the LAST $ns3::Class segment
    config = out["config"]
    assert config["resolution"] == "anchored"
    assert config["anchor_class"] == "$ns3::ApWifiMac"
    assert config["matches"] == [
        {
            "class_name": "ns3::ApWifiMac",
            "kind": "trace_source",
            "callback_type": "ns3::ApWifiMac::AssociationCallback",
            "location": "ap-wifi-mac.cc:188",
        }
    ]

    # no $anchor: global fallback still finds the owning class
    cfg_global = out["config_global"]
    assert cfg_global["resolution"] == "global"
    assert cfg_global["matches"][0]["class_name"] == "ns3::ApWifiMac"
    assert cfg_global["matches"][0]["kind"] == "attribute"

    # unknown final name: instructive error with candidates
    cfg_miss = out["config_miss"]
    assert "error" in cfg_miss and "find_trace_sources" in cfg_miss["hint"]
