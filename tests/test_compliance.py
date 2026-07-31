"""Stage 4 constraint sweep: C3 (caps + pagination enforced) and C5
(instructive errors) exercised against purpose-built miniature indexes.

Calls the tool functions directly (they are plain functions under the
FastMCP decorator); the stdio transport itself is covered by test_server.py.
"""

import time
from pathlib import Path

import pytest

from ns3kg.indexer import walker
from ns3kg.server import app, budget


def _make_index(tmp_path: Path, sources: dict[str, str]) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    for name, text in sources.items():
        (src / name).write_text(text, encoding="utf8")
    db_path = tmp_path / "kg.db"
    walker.index_directory(src, db_path)
    return db_path


@pytest.fixture
def use_db(monkeypatch):
    """Point the server module at a db and reset its cached connection."""

    def _use(db_path: Path):
        monkeypatch.setattr(app, "_DB_PATH", db_path)
        monkeypatch.setattr(app, "_CON", None)

    return _use


# --- C3: caps and pagination actually enforced -------------------------------


BIG = "namespace ns3 {\n" + "\n".join(
    f"void DupFn{i}(int x);" for i in range(40)
) + "\nvoid Caller() {\n" + "\n".join(
    f"  DupFn0({i});" for i in range(35)
) + "\n}\n}\n"


def test_over_30_hits_is_capped_and_paginated(tmp_path, use_db):
    use_db(_make_index(tmp_path, {"big.h": BIG}))

    # search_symbols: 40 matches, limit request of 100 must clamp to 30
    page1 = app.search_symbols("DupFn", limit=100)
    assert page1["total"] == 40
    assert len(page1["results"]) == budget.MAX_RESULTS == 30
    assert page1["truncated"] is True and page1["next_cursor"] == "30"

    page2 = app.search_symbols("DupFn", limit=100, cursor=page1["next_cursor"])
    assert len(page2["results"]) == 10
    assert page2["truncated"] is False and page2["next_cursor"] is None
    seen = {r["qualified_name"] for r in page1["results"]} | {
        r["qualified_name"] for r in page2["results"]
    }
    assert len(seen) == 40  # no overlap, nothing lost

    # find_usages: 35 call sites, same contract
    u1 = app.find_usages("DupFn0", limit=999)
    assert u1["total"] == 35 and len(u1["results"]) == 30
    u2 = app.find_usages("DupFn0", cursor=u1["next_cursor"])
    assert len(u2["results"]) == 5 and u2["next_cursor"] is None


def test_char_budget_trims_pages():
    huge = [{"filler": "x" * 1000} for _ in range(30)]
    page = budget.budget_page(huge, total=30, offset=0)
    assert len(page["results"]) < 30  # 30k chars cannot fit in one page
    assert page["truncated"] is True
    assert page["next_cursor"] == str(len(page["results"]))


# --- C5: instructive errors --------------------------------------------------


AMBIG = """
namespace alpha { class Widget { int a; }; }
namespace beta { class Widget { int b; }; }
"""


def test_unknown_and_ambiguous_symbols(tmp_path, use_db):
    use_db(_make_index(tmp_path, {"ambig.h": AMBIG}))

    # unknown symbol: error + suggestions + recovery hint, never an exception
    miss = app.get_signature("Wdget")
    assert "error" in miss and "search_symbols" in miss["hint"]
    assert "alpha::Widget" in miss["closest_matches"] or "beta::Widget" in miss[
        "closest_matches"
    ]

    # ambiguous class: both candidates named so the caller can requalify
    amb = app.get_inheritance_chain("Widget")
    assert "error" in amb
    assert "alpha::Widget" in amb["error"] and "beta::Widget" in amb["error"]
    assert "qualified" in amb["hint"]

    # unknown class for TypeId data: points at the recovery tools
    tid = app.get_typeid_attributes("Widget")
    assert "error" in tid and "search_symbols" in tid["hint"]


def test_stale_index_warning(tmp_path, use_db):
    src = tmp_path / "src"
    src.mkdir()
    f = src / "a.h"
    f.write_text("class One {};\n", encoding="utf8")
    db_path = tmp_path / "kg.db"
    walker.index_directory(src, db_path)
    use_db(db_path)
    assert app.get_index_status()["warning"] is None

    time.sleep(0.01)
    f.write_text("class One { int x; };\n", encoding="utf8")
    (src / "b.h").write_text("class Two {};\n", encoding="utf8")
    status = app.get_index_status()
    assert status["stale_files"] == ["a.h"]
    assert "1 new file" in status["warning"] and "ns3kg-index" in status["warning"]


def test_malformed_parameters(tmp_path, use_db):
    use_db(_make_index(tmp_path, {"big.h": BIG}))

    # bad cursors on every paginated tool
    for out in (
        app.search_symbols("DupFn", cursor="banana"),
        app.find_usages("DupFn0", cursor="-3"),
        app.get_typeid_attributes("X", cursor="banana"),
        app.find_trace_sources("X", cursor="banana"),
    ):
        assert "error" in out and "cursor" in out["hint"]

    # get_source: bad ranges and unparseable line numbers
    bad = app.get_source("big.h", 50, 10)
    assert "error" in bad and "1-based" in bad["hint"]
    bad = app.get_source("big.h", "ten", "twenty")
    assert "error" in bad and "integers" in bad["error"]
    # traversal attempts: embedded .. is rejected outright; a leading ../
    # is normalized away and then safely fails the index lookup (files are
    # only read from disk after an exact match in the files table)
    bad = app.get_source("src/../../../etc/passwd", 1, 5)
    assert "error" in bad and "invalid path" in bad["error"]
    bad = app.get_source("../../etc/passwd", 1, 5)
    assert "error" in bad and "not found" in bad["error"]

    # resolve_config_path: empty and class-terminated paths
    bad = app.resolve_config_path("///")
    assert "error" in bad and "NodeList" in bad["hint"]
    bad = app.resolve_config_path("/NodeList/*/$ns3::WifiNetDevice")
    assert "error" in bad and "get_typeid_attributes" in bad["hint"]
