"""SQLite schema and write helpers for the ns3-kg index."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = "1"

TABLES = [
    "files",
    "symbols",
    "base_classes",
    "includes",
    "call_sites",
    "typeid_attributes",
    "trace_sources",
]

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id       INTEGER PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    language TEXT NOT NULL,
    mtime    REAL NOT NULL,
    sha256   TEXT NOT NULL,
    parse_ok INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY,
    file_id        INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL,
    name           TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    signature      TEXT,
    start_line     INTEGER NOT NULL,
    end_line       INTEGER NOT NULL,
    parent_id      INTEGER REFERENCES symbols(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_symbols_name  ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qname ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_file  ON symbols(file_id);

CREATE TABLE IF NOT EXISTS base_classes (
    class_symbol_id INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    base_name       TEXT NOT NULL,
    access          TEXT
);
CREATE INDEX IF NOT EXISTS idx_base_name ON base_classes(base_name);

CREATE TABLE IF NOT EXISTS includes (
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    included_path TEXT NOT NULL,
    is_system     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS call_sites (
    file_id             INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    callee_name         TEXT NOT NULL,
    line                INTEGER NOT NULL,
    enclosing_symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_call_callee ON call_sites(callee_name);

CREATE TABLE IF NOT EXISTS typeid_attributes (
    class_name    TEXT NOT NULL,
    attr_name     TEXT NOT NULL,
    attr_type     TEXT,
    default_value TEXT,
    help          TEXT,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    line          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attr_class ON typeid_attributes(class_name);

CREATE TABLE IF NOT EXISTS trace_sources (
    class_name    TEXT NOT NULL,
    trace_name    TEXT NOT NULL,
    help          TEXT,
    callback_type TEXT,
    origin        TEXT NOT NULL,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    line          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trace_class ON trace_sources(class_name);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA foreign_keys = ON")
    return con


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(DDL)
    con.commit()


def set_meta(con: sqlite3.Connection, **kv: str) -> None:
    with con:
        for k, v in kv.items():
            con.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, str(v)),
            )


def get_files(con: sqlite3.Connection) -> dict[str, tuple[float, str]]:
    """Return {path: (mtime, sha256)} for all indexed files."""
    return {
        path: (mtime, sha)
        for path, mtime, sha in con.execute("SELECT path, mtime, sha256 FROM files")
    }


def touch_file(con: sqlite3.Connection, rel_path: str, mtime: float) -> None:
    with con:
        con.execute("UPDATE files SET mtime = ? WHERE path = ?", (mtime, rel_path))


def delete_files(con: sqlite3.Connection, paths: set[str]) -> None:
    with con:
        con.executemany(
            "DELETE FROM files WHERE path = ?", [(p,) for p in paths]
        )


def replace_file(
    con: sqlite3.Connection,
    rel_path: str,
    language: str,
    mtime: float,
    sha256: str,
    extraction: dict | None,
) -> None:
    """Atomically replace all rows for one file. extraction=None marks a parse failure."""
    with con:
        con.execute("DELETE FROM files WHERE path = ?", (rel_path,))
        cur = con.execute(
            "INSERT INTO files (path, language, mtime, sha256, parse_ok) "
            "VALUES (?, ?, ?, ?, ?)",
            (rel_path, language, mtime, sha256, 1 if extraction is not None else 0),
        )
        fid = cur.lastrowid
        if extraction is None:
            return

        # Parents always start earlier in the file than the members they enclose,
        # so inserting in source order guarantees parent rows exist first.
        idmap: dict[int, int] = {}
        for s in sorted(extraction["symbols"], key=lambda s: s["order"]):
            cur = con.execute(
                "INSERT INTO symbols (file_id, kind, name, qualified_name, signature, "
                "start_line, end_line, parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fid,
                    s["kind"],
                    s["name"],
                    s["qualified_name"],
                    s["signature"],
                    s["start_line"],
                    s["end_line"],
                    idmap.get(s["parent_tmp"]),
                ),
            )
            idmap[s["tmp"]] = cur.lastrowid

        for b in extraction["base_classes"]:
            sid = idmap.get(b["class_tmp"])
            if sid is not None:
                con.execute(
                    "INSERT INTO base_classes (class_symbol_id, base_name, access) "
                    "VALUES (?, ?, ?)",
                    (sid, b["base_name"], b["access"]),
                )

        con.executemany(
            "INSERT INTO includes (file_id, included_path, is_system) VALUES (?, ?, ?)",
            [(fid, i["path"], i["is_system"]) for i in extraction["includes"]],
        )

        con.executemany(
            "INSERT INTO call_sites (file_id, callee_name, line, enclosing_symbol_id) "
            "VALUES (?, ?, ?, ?)",
            [
                (fid, c["callee"], c["line"], idmap.get(c["enclosing_tmp"]))
                for c in extraction["calls"]
            ],
        )

        con.executemany(
            "INSERT INTO typeid_attributes (class_name, attr_name, attr_type, "
            "default_value, help, file_id, line) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (a["class_name"], a["attr_name"], a["attr_type"],
                 a["default_value"], a["help"], fid, a["line"])
                for a in extraction.get("typeid_attributes", [])
            ],
        )

        con.executemany(
            "INSERT INTO trace_sources (class_name, trace_name, help, "
            "callback_type, origin, file_id, line) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (t["class_name"], t["trace_name"], t["help"],
                 t["callback_type"], t["origin"], fid, t["line"])
                for t in extraction.get("trace_sources", [])
            ],
        )


def table_counts(con: sqlite3.Connection) -> dict[str, int]:
    return {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
        for t in TABLES
    }
