"""Assertions against real ns-3.48 files copied into tests/fixtures/.

Every expected value below was read from the actual fixture content
(file:line noted per test), not invented.
"""

import shutil
import time
from pathlib import Path

from ns3kg.indexer import walker

FIXTURES = Path(__file__).parent / "fixtures"


def one(con, sql, *args):
    rows = con.execute(sql, args).fetchall()
    assert len(rows) == 1, f"expected 1 row, got {rows}"
    return rows[0]


# --- classes -----------------------------------------------------------------


def test_class_apwifimac(con):
    # ap-wifi-mac.h:61  "class ApWifiMac : public WifiMac"
    kind, start, sig = one(
        con,
        "SELECT kind, start_line, signature FROM symbols WHERE qualified_name = ?",
        "ns3::ApWifiMac",
    )
    assert kind == "class"
    assert start == 61
    assert "public WifiMac" in sig


def test_class_wifimac_and_qostxop(con):
    # wifi-mac.h:89  "class WifiMac : public Object"
    (start,) = one(
        con, "SELECT start_line FROM symbols WHERE qualified_name = ?", "ns3::WifiMac"
    )
    assert start == 89
    # qos-txop.h:51  "class QosTxop : public Txop"
    (start,) = one(
        con, "SELECT start_line FROM symbols WHERE qualified_name = ?", "ns3::QosTxop"
    )
    assert start == 51


def test_base_classes(con):
    rows = con.execute(
        "SELECT s.qualified_name, b.base_name, b.access FROM base_classes b "
        "JOIN symbols s ON s.id = b.class_symbol_id "
        "WHERE s.qualified_name IN ('ns3::ApWifiMac', 'ns3::WifiMac', 'ns3::QosTxop')"
    ).fetchall()
    assert ("ns3::ApWifiMac", "WifiMac", "public") in rows
    assert ("ns3::WifiMac", "Object", "public") in rows
    assert ("ns3::QosTxop", "Txop", "public") in rows


def test_nested_class_has_parent(con):
    # ap-wifi-mac.h:331  nested "class ApLinkEntity : public WifiMac::LinkEntity"
    qname, parent_qname = one(
        con,
        "SELECT s.qualified_name, p.qualified_name FROM symbols s "
        "JOIN symbols p ON p.id = s.parent_id "
        "WHERE s.qualified_name = 'ns3::ApWifiMac::ApLinkEntity'",
    )
    assert parent_qname == "ns3::ApWifiMac"


# --- methods -----------------------------------------------------------------


def test_gettypeid_definition(con):
    # ap-wifi-mac.cc:50-51  "TypeId\nApWifiMac::GetTypeId()"
    kind, start, sig, path = one(
        con,
        "SELECT s.kind, s.start_line, s.signature, f.path FROM symbols s "
        "JOIN files f ON f.id = s.file_id "
        "WHERE s.qualified_name = 'ns3::ApWifiMac::GetTypeId' AND s.kind = 'method_def'",
    )
    assert path == "ap-wifi-mac.cc"
    assert start == 50
    assert sig == "TypeId ApWifiMac::GetTypeId()"


def test_enqueue_declaration_signature(con):
    # ap-wifi-mac.h: "void Enqueue(Ptr<WifiMpdu> mpdu, Mac48Address to, Mac48Address from) override;"
    rows = con.execute(
        "SELECT signature FROM symbols "
        "WHERE qualified_name = 'ns3::ApWifiMac::Enqueue' AND kind = 'method_decl'"
    ).fetchall()
    assert any(
        "Ptr<WifiMpdu> mpdu, Mac48Address to, Mac48Address from" in sig
        and "override" in sig
        for (sig,) in rows
    )


# --- includes ----------------------------------------------------------------


def test_includes(con):
    rows = set(
        con.execute(
            "SELECT f.path, i.included_path, i.is_system FROM includes i "
            "JOIN files f ON f.id = i.file_id"
        ).fetchall()
    )
    # ap-wifi-mac.cc:6  #include "ap-wifi-mac.h"
    assert ("ap-wifi-mac.cc", "ap-wifi-mac.h", 0) in rows
    # qos-txop.h:16 / :20
    assert ("qos-txop.h", "txop.h", 0) in rows
    assert ("qos-txop.h", "optional", 1) in rows


# --- call sites --------------------------------------------------------------


def test_call_site_addtracesource(con):
    # ap-wifi-mac.cc:188  ".AddTraceSource("AssociatedSta", ..." inside GetTypeId
    rows = con.execute(
        "SELECT c.line, e.qualified_name FROM call_sites c "
        "JOIN files f ON f.id = c.file_id "
        "LEFT JOIN symbols e ON e.id = c.enclosing_symbol_id "
        "WHERE f.path = 'ap-wifi-mac.cc' AND c.callee_name = 'AddTraceSource'"
    ).fetchall()
    assert any(
        qname == "ns3::ApWifiMac::GetTypeId" for _line, qname in rows
    ), rows


# --- ns-3 TypeId data --------------------------------------------------------


def test_typeid_attributes(con):
    rows = con.execute(
        "SELECT attr_name, attr_type, default_value, help, line "
        "FROM typeid_attributes WHERE class_name = 'ns3::ApWifiMac' ORDER BY line"
    ).fetchall()
    # ap-wifi-mac.cc:58-186  15 .AddAttribute(...) calls inside GetTypeId()
    assert len(rows) == 15
    by_name = {r[0]: r for r in rows}

    # ap-wifi-mac.cc:58  first attribute, name on the line after .AddAttribute(
    assert rows[0][0] == "BeaconInterval" and rows[0][4] == 58

    # ap-wifi-mac.cc:64  StringValue default but Pointer checker wins for the type
    bj = by_name["BeaconJitter"]
    assert bj[1] == "Pointer<RandomVariableStream>"
    assert bj[2] == 'StringValue("ns3::UniformRandomVariable")'

    # ap-wifi-mac.cc:71-75
    eb = by_name["EnableBeaconJitter"]
    assert eb[1] == "Boolean" and eb[2] == "BooleanValue(true)"
    assert eb[3] == "If beacons are enabled, whether to jitter the initial send event."


def test_trace_sources(con):
    rows = con.execute(
        "SELECT trace_name, callback_type, origin, line FROM trace_sources "
        "WHERE class_name = 'ns3::ApWifiMac'"
    ).fetchall()
    # ap-wifi-mac.cc:188 / :192  AddTraceSource registrations
    assert ("AssociatedSta", "ns3::ApWifiMac::AssociationCallback", "AddTraceSource", 188) in rows
    assert ("DeAssociatedSta", "ns3::ApWifiMac::AssociationCallback", "AddTraceSource", 192) in rows
    # ap-wifi-mac.h:797 / :798  TracedCallback member declarations
    members = {r[0] for r in rows if r[2] == "TracedCallback"}
    assert members == {"m_assocLogger", "m_deAssocLogger"}

    # wifi-mac.h:1323  TracedCallback<Ptr<const Packet>> m_macTxTrace;
    (cb,) = one(
        con,
        "SELECT callback_type FROM trace_sources "
        "WHERE class_name = 'ns3::WifiMac' AND trace_name = 'm_macTxTrace'",
    )
    assert cb == "TracedCallback<Ptr<const Packet>>"


# --- python ------------------------------------------------------------------


def test_python_functions_and_imports(con):
    # bianchi11ax.py:13  "def bianchi_ax(data_rate, ack_rate, k, difs):"
    kind, start, sig = one(
        con,
        "SELECT kind, start_line, signature FROM symbols WHERE qualified_name = ? ",
        "bianchi_ax",
    )
    assert kind == "function"
    assert start == 13
    assert sig == "def bianchi_ax(data_rate, ack_rate, k, difs)"
    rows = set(
        con.execute(
            "SELECT i.included_path FROM includes i JOIN files f ON f.id = i.file_id "
            "WHERE f.path = 'bianchi11ax.py'"
        ).fetchall()
    )
    assert ("math",) in rows
    assert ("numpy",) in rows


# --- incremental & robustness ------------------------------------------------


def test_incremental_reindex(tmp_path):
    work = tmp_path / "src"
    shutil.copytree(FIXTURES, work)
    dbf = tmp_path / "kg.db"

    s1 = walker.index_directory(work, dbf)
    assert s1["parsed"] == 5 and s1["failed"] == 0

    # rerun with nothing changed
    s2 = walker.index_directory(work, dbf)
    assert s2["parsed"] == 0 and s2["unchanged"] == 5

    # touch mtime, same content -> hash check catches it, no re-parse
    target = work / "qos-txop.h"
    time.sleep(0.01)
    target.touch()
    s3 = walker.index_directory(work, dbf)
    assert s3["parsed"] == 0 and s3["unchanged"] == 5

    # actually change content -> exactly one file re-parsed
    target.write_text(target.read_text(encoding="utf8") + "\n// touched\n", encoding="utf8")
    s4 = walker.index_directory(work, dbf)
    assert s4["parsed"] == 1 and s4["unchanged"] == 4

    # delete a file -> its rows disappear
    (work / "bianchi11ax.py").unlink()
    s5 = walker.index_directory(work, dbf)
    assert s5["removed"] == 1
    from ns3kg import db as dbmod

    con = dbmod.connect(dbf)
    assert (
        con.execute(
            "SELECT COUNT(*) FROM symbols WHERE qualified_name = 'bianchi_ax'"
        ).fetchone()[0]
        == 0
    )
    con.close()


def test_bad_file_does_not_abort(tmp_path):
    work = tmp_path / "src"
    work.mkdir()
    shutil.copy(FIXTURES / "qos-txop.h", work)
    (work / "broken.py").write_text("def broken(:\n  ???", encoding="utf8")

    stats = walker.index_directory(work, tmp_path / "kg.db")
    assert stats["failed"] == 1
    assert stats["parsed"] == 1  # the good file still made it in
