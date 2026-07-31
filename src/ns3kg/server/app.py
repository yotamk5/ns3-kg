"""ns3-kg MCP server: FastMCP over stdio, answering from the SQLite index.

The index is read-only here; it is built/updated by the separate
`ns3kg-index` CLI.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..indexer import walker
from . import budget

mcp = FastMCP("ns3-kg")

_DB_PATH: Path | None = None
_CON: sqlite3.Connection | None = None


def _con() -> sqlite3.Connection:
    global _CON
    if _CON is None:
        _CON = sqlite3.connect(
            f"file:{_DB_PATH}?mode=ro", uri=True, check_same_thread=False
        )
    return _CON


def _meta(key: str) -> str | None:
    row = _con().execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _distinct_names() -> list[str]:
    return [r[0] for r in _con().execute("SELECT DISTINCT name FROM symbols")]


def _closest_qualified(name: str, n: int = 5) -> list[str]:
    """Closest qualified names for a possibly-wrong symbol name."""
    last = name.split("::")[-1]
    close = budget.closest(last, _distinct_names(), n=n)
    out: list[str] = []
    for cand in close:
        rows = _con().execute(
            "SELECT DISTINCT qualified_name FROM symbols WHERE name = ? LIMIT 2",
            (cand,),
        ).fetchall()
        out.extend(q for (q,) in rows)
    return out[:n]


# ---------------------------------------------------------------------------


@mcp.tool()
def search_symbols(
    query: str, kind: str | None = None, limit: int = 30, cursor: str | None = None
) -> dict:
    """Search the ns-3 code index for classes/functions/methods by name substring.

    Use this FIRST when you only know part of a name or want to discover what
    exists. If you already know the exact name, use get_signature instead; to
    see callers, use find_usages.

    Parameters:
      query: case-insensitive substring of the qualified name.
             Example: "WifiMac::Enqueue" or "MinstrelHt".
      kind: optional filter, one of: class, struct, enum, function,
            function_decl, method_def, method_decl.
      limit: max results per page, 1-30 (default 30).
      cursor: to get the next page, pass the next_cursor value returned by the
              previous call. Omit for the first page.

    Output: {"results": [{"qualified_name", "kind", "location": "file:line",
    "signature"}], "total", "truncated", "next_cursor"}. If truncated is true,
    more matches exist -- repeat the call with cursor=next_cursor.
    """
    offset = budget.parse_cursor(cursor)
    if offset is None:
        return budget.bad_cursor(cursor)
    limit = budget.clamp_limit(limit)
    last = query.split("::")[-1]

    where = "s.qualified_name LIKE ?"
    args: list = [f"%{query}%"]
    if kind:
        where += " AND s.kind = ?"
        args.append(kind)

    total = _con().execute(
        f"SELECT COUNT(*) FROM symbols s WHERE {where}", args
    ).fetchone()[0]
    if total == 0:
        return budget.not_found(
            f"symbol matching {query!r}" + (f" with kind={kind!r}" if kind else ""),
            _closest_qualified(query),
            "Retry search_symbols with a shorter substring (e.g. just the method "
            "name without the class), or without the kind filter.",
        )

    rows = _con().execute(
        f"""SELECT s.qualified_name, s.kind, s.signature, f.path, s.start_line
            FROM symbols s JOIN files f ON f.id = s.file_id
            WHERE {where}
            ORDER BY CASE WHEN lower(s.name) = lower(?) THEN 0
                          WHEN lower(s.name) LIKE lower(?) THEN 1
                          ELSE 2 END,
                     length(s.qualified_name), s.qualified_name, s.start_line
            LIMIT ? OFFSET ?""",
        args + [last, f"{last}%", limit, offset],
    ).fetchall()

    window = [
        {
            "qualified_name": qn,
            "kind": k,
            "location": f"{path}:{line}",
            "signature": sig,
        }
        for qn, k, sig, path, line in rows
    ]
    return budget.budget_page(window, total, offset)


@mcp.tool()
def get_signature(qualified_name: str) -> dict:
    """Get the exact declaration(s) of one known symbol.

    Use this when you know the name (fully or partially qualified). It returns
    ALL overloads and both the header declaration and the .cc definition, so
    multiple results for one name is normal. If you are unsure of the name,
    use search_symbols first; to read a function body, use get_source with the
    file/lines this tool returns.

    Parameters:
      qualified_name: e.g. "WifiMac::Enqueue". The leading "ns3::" is
                      optional. A bare name like "Enqueue" also works but may
                      return symbols from many classes.

    Output: {"results": [{"qualified_name", "kind", "signature",
    "file", "start_line", "end_line"}], "total", "truncated", "next_cursor"}.
    On an unknown name the output is {"error", "closest_matches", "hint"} --
    pick one of closest_matches and retry.
    """
    qn = qualified_name.strip().removesuffix("()")
    rows = _con().execute(
        """SELECT s.qualified_name, s.kind, s.signature, f.path, s.start_line, s.end_line
           FROM symbols s JOIN files f ON f.id = s.file_id
           WHERE s.qualified_name = ? OR s.qualified_name LIKE ?
           ORDER BY s.qualified_name, f.path, s.start_line""",
        (qn, f"%::{qn}"),
    ).fetchall()
    if not rows and "::" not in qn:
        rows = _con().execute(
            """SELECT s.qualified_name, s.kind, s.signature, f.path, s.start_line, s.end_line
               FROM symbols s JOIN files f ON f.id = s.file_id
               WHERE s.name = ? ORDER BY s.qualified_name, f.path, s.start_line""",
            (qn,),
        ).fetchall()

    if not rows:
        return budget.not_found(
            f"symbol {qualified_name!r}",
            _closest_qualified(qn),
            "Use search_symbols with a substring of the name to locate the "
            "right qualified name, then call get_signature again.",
        )

    window = [
        {
            "qualified_name": q,
            "kind": k,
            "signature": sig,
            "file": path,
            "start_line": s,
            "end_line": e,
        }
        for q, k, sig, path, s, e in rows[: budget.MAX_RESULTS]
    ]
    return budget.budget_page(window, len(rows), 0)


@mcp.tool()
def get_source(file: str, start_line: int, end_line: int) -> dict:
    """Read an exact line range of one indexed source file (max 200 lines).

    Use this ONLY after another tool told you the file and line span (e.g.
    get_signature's file/start_line/end_line). Do not use it to scan files
    blindly -- that is what search_symbols is for.

    Parameters:
      file: index-relative path exactly as reported by the other tools.
            Example: "src/wifi/model/wifi-mac.cc".
      start_line, end_line: 1-based, inclusive. Example: 1752, 1790.
            Ranges longer than 200 lines are cut off at 200 (truncated=true).

    Output: {"file", "start_line", "end_line", "total_lines", "truncated",
    "source"}. total_lines is the whole file's line count, so you can page
    through a long function with repeated calls.
    """
    rel = file.replace("\\", "/").lstrip("./")
    if ".." in rel.split("/"):
        return {
            "error": f"invalid path: {file!r}",
            "hint": "Pass the index-relative path exactly as returned by "
            "search_symbols/get_signature, e.g. 'src/wifi/model/wifi-mac.cc'.",
        }
    row = _con().execute("SELECT id FROM files WHERE path = ?", (rel,)).fetchone()
    if row is None:
        base = rel.rsplit("/", 1)[-1]
        cands = [
            r[0]
            for r in _con().execute(
                "SELECT path FROM files WHERE path LIKE ? LIMIT 5", (f"%{base}%",)
            )
        ]
        return budget.not_found(
            f"file {file!r} in the index",
            cands,
            "Use the exact path reported by search_symbols or get_signature.",
        )

    root = _meta("root")
    abs_path = Path(root) / rel if root else None
    if abs_path is None or not abs_path.is_file():
        return {
            "error": f"file is in the index but missing on disk: {rel}",
            "hint": "The source tree moved or the file was deleted. Ask the "
            "user to re-run ns3kg-index to refresh the index.",
        }

    try:
        start_line, end_line = int(start_line), int(end_line)
    except (TypeError, ValueError):
        return {
            "error": f"start_line/end_line must be integers, got "
            f"{start_line!r}/{end_line!r}",
            "hint": "Example: get_source(file='src/wifi/model/wifi-mac.cc', "
            "start_line=1752, end_line=1790)",
        }
    if end_line < start_line or start_line < 1:
        return {
            "error": f"bad range {start_line}-{end_line}",
            "hint": "start_line must be >= 1 and <= end_line (1-based, inclusive).",
        }

    lines = abs_path.read_text(encoding="utf8", errors="replace").splitlines()
    total = len(lines)
    start = min(start_line, total)
    requested_end = min(end_line, total)
    end = min(requested_end, start + 199)  # hard cap: 200 lines per call
    return {
        "file": rel,
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "truncated": end < requested_end,
        "source": "\n".join(lines[start - 1 : end]),
    }


@mcp.tool()
def find_usages(symbol_name: str, limit: int = 30, cursor: str | None = None) -> dict:
    """List call sites that mention a function/method name ("who calls this?").

    IMPORTANT: matches are TEXTUAL candidates. C++ virtual dispatch and
    overload resolution are NOT performed, so a call to "Enqueue" on any class
    is listed. Treat results as leads to verify with get_source, not as proof.
    Use get_signature first to confirm the symbol exists.

    Parameters:
      symbol_name: bare function/method name, e.g. "Enqueue". A qualified
                   name like "WifiMac::Enqueue" is automatically reduced to
                   its last segment.
      limit: max results per page, 1-30 (default 30).
      cursor: pass next_cursor from the previous response for the next page.

    Output: {"results": [{"location": "file:line", "in_function"}], "total",
    "truncated", "next_cursor", "note"}. in_function is the qualified name of
    the function containing the call (null at file scope).
    """
    offset = budget.parse_cursor(cursor)
    if offset is None:
        return budget.bad_cursor(cursor)
    limit = budget.clamp_limit(limit)
    name = symbol_name.strip().removesuffix("()").split("::")[-1]

    total = _con().execute(
        "SELECT COUNT(*) FROM call_sites WHERE callee_name = ?", (name,)
    ).fetchone()[0]
    if total == 0:
        pool = [
            r[0]
            for r in _con().execute("SELECT DISTINCT callee_name FROM call_sites")
        ]
        return budget.not_found(
            f"call sites for {symbol_name!r}",
            budget.closest(name, pool),
            "Check the spelling with get_signature or search_symbols; the "
            "function may also simply never be called inside the indexed tree.",
        )

    rows = _con().execute(
        """SELECT f.path, c.line, e.qualified_name
           FROM call_sites c
           JOIN files f ON f.id = c.file_id
           LEFT JOIN symbols e ON e.id = c.enclosing_symbol_id
           WHERE c.callee_name = ?
           ORDER BY f.path, c.line
           LIMIT ? OFFSET ?""",
        (name, limit, offset),
    ).fetchall()

    window = [
        {"location": f"{path}:{line}", "in_function": fn}
        for path, line, fn in rows
    ]
    page = budget.budget_page(window, total, offset)
    page["note"] = (
        "Textual candidates only: virtual dispatch is not resolved. "
        "Verify important hits with get_source."
    )
    return page


@mcp.tool()
def get_index_status() -> dict:
    """Check whether the index is fresh enough to trust.

    Use this at the start of a session, or whenever another tool's answer
    contradicts what you see in a file. No parameters.

    Output: {"root", "index_age_minutes", "files_indexed", "symbols",
    "stale_files", "warning"}. stale_files lists up to 20 files edited after
    the index was built. If warning is non-null, tell the user to run the
    suggested ns3kg-index command before trusting further answers.
    """
    root = _meta("root")
    indexed_at = float(_meta("indexed_at") or 0)
    files_indexed = _con().execute("SELECT COUNT(*) FROM files").fetchone()[0]
    symbols = _con().execute("SELECT COUNT(*) FROM symbols").fetchone()[0]

    stale: list[str] = []
    new_files = 0
    if root and Path(root).is_dir():
        indexed = {
            path: mtime
            for path, mtime in _con().execute("SELECT path, mtime FROM files")
        }
        for abs_path, rel, _lang in walker.discover(Path(root)):
            known = indexed.get(rel)
            if known is None:
                new_files += 1
            elif abs_path.stat().st_mtime > known:
                if len(stale) < 20:
                    stale.append(rel)
    warning = None
    if stale or new_files:
        warning = (
            f"{len(stale)}{'+' if len(stale) == 20 else ''} modified and "
            f"{new_files} new file(s) are not reflected in the index. Ask the "
            f'user to run: ns3kg-index "{root}" --db "{_DB_PATH}"'
        )
    return {
        "root": root,
        "index_age_minutes": round((time.time() - indexed_at) / 60, 1),
        "files_indexed": files_indexed,
        "symbols": symbols,
        "stale_files": stale,
        "warning": warning,
    }


# --- ns-3-aware tools (Stage 3) --------------------------------------------


def _norm_class(name: str) -> str:
    return name.strip().removeprefix("$").removeprefix("ns3::")


def _typeid_class_names() -> list[str]:
    rows = _con().execute(
        "SELECT DISTINCT class_name FROM typeid_attributes "
        "UNION SELECT DISTINCT class_name FROM trace_sources"
    )
    return [r[0] for r in rows]


def _class_rows(name: str) -> list[tuple]:
    """Class/struct symbols matching a bare or qualified name.

    Returns (id, qualified_name, file, start_line) rows."""
    bare = _norm_class(name).split("::")[-1]
    return _con().execute(
        """SELECT s.id, s.qualified_name, f.path, s.start_line
           FROM symbols s JOIN files f ON f.id = s.file_id
           WHERE s.kind IN ('class', 'struct') AND s.name = ?
           ORDER BY s.qualified_name, f.path""",
        (bare,),
    ).fetchall()


def _ancestors(class_ids: list[int]) -> list[dict]:
    """BFS over base_classes, resolving each base name back to a class symbol."""
    out: list[dict] = []
    visited: set[str] = set()
    frontier = list(class_ids)
    for _depth in range(30):  # cycle/degenerate-depth guard
        if not frontier:
            break
        marks = ",".join("?" * len(frontier))
        bases = [
            r[0]
            for r in _con().execute(
                f"SELECT DISTINCT base_name FROM base_classes "
                f"WHERE class_symbol_id IN ({marks})",  # noqa: S608
                frontier,
            )
        ]
        frontier = []
        for base in bases:
            rows = _class_rows(base)
            if not rows:
                if base not in visited:
                    visited.add(base)
                    out.append(
                        {"qualified_name": base, "location": None}
                    )  # base outside the index (e.g. std::)
                continue
            for sid, qn, path, line in rows:
                if qn in visited:
                    continue
                visited.add(qn)
                out.append({"qualified_name": qn, "location": f"{path}:{line}"})
                frontier.append(sid)
    return out


def _chain_class_names(class_name: str) -> list[str]:
    """The class plus all ancestors, as TypeId-style qualified names."""
    rows = _class_rows(class_name)
    names = list({qn for _sid, qn, _p, _l in rows})
    names += [
        a["qualified_name"]
        for a in _ancestors([sid for sid, _qn, _p, _l in rows])
    ]
    return names


def _attrs_for_classes(class_names: list[str], attr_name: str | None = None):
    if not class_names:
        return []
    marks = ",".join("?" * len(class_names))
    where = f"class_name IN ({marks})"
    args: list = list(class_names)
    if attr_name is not None:
        where += " AND attr_name = ?"
        args.append(attr_name)
    return _con().execute(
        f"""SELECT a.class_name, a.attr_name, a.attr_type, a.default_value,
                   a.help, f.path, a.line
            FROM typeid_attributes a JOIN files f ON f.id = a.file_id
            WHERE {where} ORDER BY a.class_name, a.attr_name""",  # noqa: S608
        args,
    ).fetchall()


def _traces_for_classes(class_names: list[str], trace_name: str | None = None):
    if not class_names:
        return []
    marks = ",".join("?" * len(class_names))
    where = f"class_name IN ({marks}) AND origin = 'AddTraceSource'"
    args: list = list(class_names)
    if trace_name is not None:
        where += " AND trace_name = ?"
        args.append(trace_name)
    return _con().execute(
        f"""SELECT t.class_name, t.trace_name, t.help, t.callback_type,
                   f.path, t.line
            FROM trace_sources t JOIN files f ON f.id = t.file_id
            WHERE {where} ORDER BY t.class_name, t.trace_name""",  # noqa: S608
        args,
    ).fetchall()


_HELP_CAP = 300


def _short(s: str | None) -> str | None:
    return s if s is None or len(s) <= _HELP_CAP else s[: _HELP_CAP - 3] + "..."


@mcp.tool()
def get_typeid_attributes(
    class_name: str, limit: int = 30, cursor: str | None = None
) -> dict:
    """List the ns-3 attributes and trace sources one class registers in GetTypeId().

    Use this to learn what an ns-3 object can be configured with (attribute
    names, types, defaults) and what it can be traced on -- BEFORE reading the
    class source. Covers only what THIS class registers itself: attributes
    inherited from parent classes are not repeated here. Call
    get_inheritance_chain to find the parents, then call this tool on them.

    Parameters:
      class_name: e.g. "ApWifiMac" or "ns3::ApWifiMac" (prefix optional).
      limit: max results per page, 1-30 (default 30).
      cursor: pass next_cursor from the previous response for the next page.

    Output: {"class_name", "results": [...], "total", "truncated",
    "next_cursor"}. Each result has "kind": "attribute" (with attr_name,
    attr_type, default_value, help, location) or "trace_source" (with
    trace_name, help, callback_type, location). On an unknown class the
    output is {"error", "closest_matches", "hint"}.
    """
    offset = budget.parse_cursor(cursor)
    if offset is None:
        return budget.bad_cursor(cursor)
    limit = budget.clamp_limit(limit)

    bare = _norm_class(class_name)
    classes = [
        c
        for c in _typeid_class_names()
        if c == class_name or _norm_class(c) == bare
    ]
    if not classes:
        return budget.not_found(
            f"TypeId data for class {class_name!r}",
            budget.closest(bare, [_norm_class(c) for c in _typeid_class_names()]),
            "The class may not define GetTypeId() (not an ns3::Object), or the "
            "name is misspelled -- check it with search_symbols. Attributes of "
            "a PARENT class must be queried on the parent (see "
            "get_inheritance_chain).",
        )

    window: list[dict] = []
    for cn, an, at, dv, hp, path, line in _attrs_for_classes(classes):
        window.append(
            {
                "kind": "attribute",
                "attr_name": an,
                "attr_type": at,
                "default_value": dv,
                "help": _short(hp),
                "location": f"{path}:{line}",
            }
        )
    for cn, tn, hp, cb, path, line in _traces_for_classes(classes):
        window.append(
            {
                "kind": "trace_source",
                "trace_name": tn,
                "help": _short(hp),
                "callback_type": cb,
                "location": f"{path}:{line}",
            }
        )
    total = len(window)
    page = budget.budget_page(window[offset : offset + limit], total, offset)
    page["class_name"] = classes[0] if len(classes) == 1 else classes
    return page


@mcp.tool()
def find_trace_sources(
    pattern: str, limit: int = 30, cursor: str | None = None
) -> dict:
    """Search all ns-3 trace sources by name substring ("what can I hook?").

    Use this to discover trace sources when you do not know which class owns
    them, e.g. pattern "Assoc" finds ApWifiMac's "AssociatedSta". If you
    already know the class, get_typeid_attributes(class) lists its traces
    directly.

    Parameters:
      pattern: case-insensitive substring matched against the trace name AND
               the owning class name. Example: "PhyTxBegin" or "ApWifiMac".
      limit: max results per page, 1-30 (default 30).
      cursor: pass next_cursor from the previous response for the next page.

    Output: {"results": [{"trace_name", "class_name", "origin",
    "callback_type", "help", "location"}], "total", "truncated",
    "next_cursor"}. origin is "AddTraceSource" for the public trace name you
    can use in Config paths, or "TracedCallback" for a raw member variable
    declaration (internal name, not connectable via Config).
    """
    offset = budget.parse_cursor(cursor)
    if offset is None:
        return budget.bad_cursor(cursor)
    limit = budget.clamp_limit(limit)

    like = f"%{pattern}%"
    total = _con().execute(
        "SELECT COUNT(*) FROM trace_sources "
        "WHERE trace_name LIKE ? OR class_name LIKE ?",
        (like, like),
    ).fetchone()[0]
    if total == 0:
        pool = [r[0] for r in _con().execute("SELECT DISTINCT trace_name FROM trace_sources")]
        return budget.not_found(
            f"trace sources matching {pattern!r}",
            budget.closest(pattern, pool),
            "Try a shorter substring, or list a specific class's traces with "
            "get_typeid_attributes(class_name).",
        )

    rows = _con().execute(
        """SELECT t.trace_name, t.class_name, t.origin, t.callback_type,
                  t.help, f.path, t.line
           FROM trace_sources t JOIN files f ON f.id = t.file_id
           WHERE t.trace_name LIKE ? OR t.class_name LIKE ?
           ORDER BY CASE t.origin WHEN 'AddTraceSource' THEN 0 ELSE 1 END,
                    t.class_name, t.trace_name
           LIMIT ? OFFSET ?""",
        (like, like, limit, offset),
    ).fetchall()
    window = [
        {
            "trace_name": tn,
            "class_name": cn,
            "origin": origin,
            "callback_type": cb,
            "help": _short(hp),
            "location": f"{path}:{line}",
        }
        for tn, cn, origin, cb, hp, path, line in rows
    ]
    return budget.budget_page(window, total, offset)


@mcp.tool()
def get_inheritance_chain(class_name: str) -> dict:
    """Get a class's full ancestor chain and its direct subclasses.

    Use this to understand where behavior/attributes come from (ancestors) or
    to find all implementations of a base class (direct_children). Combine
    with get_typeid_attributes on each ancestor to see every attribute a class
    effectively has.

    Parameters:
      class_name: e.g. "ApWifiMac" or "ns3::ApWifiMac" (prefix optional).

    Output: {"class", "location", "ancestors": [{"qualified_name",
    "location"}], "direct_children": [{"qualified_name", "location"}],
    "children_total", "children_truncated"}. ancestors are breadth-first:
    direct parent(s) first, root base classes last; location is null for
    bases outside the indexed tree. direct_children is capped at 30 -- if
    children_truncated is true, use search_symbols to enumerate more.
    """
    bare = _norm_class(class_name).split("::")[-1]
    rows = _class_rows(class_name)
    if not rows:
        return budget.not_found(
            f"class {class_name!r}",
            _closest_qualified(bare),
            "Check the spelling with search_symbols(query=..., kind='class').",
        )
    qnames = sorted({qn for _sid, qn, _p, _l in rows})
    if len(qnames) > 1:
        return {
            "error": f"ambiguous class name {class_name!r}: {qnames}",
            "hint": "Repeat the call with one of the fully qualified names.",
        }

    ids = [sid for sid, _qn, _p, _l in rows]
    ancestors = _ancestors(ids)

    children_total = _con().execute(
        "SELECT COUNT(DISTINCT s.qualified_name) FROM base_classes b "
        "JOIN symbols s ON s.id = b.class_symbol_id "
        "WHERE b.base_name = ? OR b.base_name LIKE ?",
        (bare, f"%::{bare}"),
    ).fetchone()[0]
    children = _con().execute(
        """SELECT DISTINCT s.qualified_name, f.path, s.start_line
           FROM base_classes b
           JOIN symbols s ON s.id = b.class_symbol_id
           JOIN files f ON f.id = s.file_id
           WHERE b.base_name = ? OR b.base_name LIKE ?
           ORDER BY s.qualified_name LIMIT ?""",
        (bare, f"%::{bare}", budget.MAX_RESULTS),
    ).fetchall()

    return {
        "class": qnames[0],
        "location": f"{rows[0][2]}:{rows[0][3]}",
        "ancestors": ancestors,
        "direct_children": [
            {"qualified_name": qn, "location": f"{p}:{line}"}
            for qn, p, line in children
        ],
        "children_total": children_total,
        "children_truncated": children_total > len(children),
    }


@mcp.tool()
def resolve_config_path(path_string: str) -> dict:
    """Resolve the trailing attribute/trace name of an ns-3 Config path to its
    owning class and source location.

    Use this when you see a Config::Connect / Config::Set path in code or docs
    and need to know which class defines the final attribute or trace, e.g.
    "/NodeList/*/DeviceList/*/$ns3::WifiNetDevice/Mac/$ns3::ApWifiMac/AssociatedSta".

    How it resolves: the LAST "$ns3::Class" segment anchors the search; any
    segments after it are followed as Pointer-typed attributes when possible;
    the final segment is then looked up as an attribute or trace source of the
    reached class or its ancestors. Without a "$ns3::Class" anchor (or if a
    hop cannot be followed) it falls back to searching ALL classes for the
    final name, which may return several candidates.

    NOT supported (runtime concepts, no static answer): which node/device a
    wildcard "*" or index selects; objects attached by aggregation; hops
    through ObjectVector/ObjectMap attributes or attributes without a
    Pointer<Class> type.

    Parameters:
      path_string: the full Config path. Example above.

    Output: {"path", "final_name", "anchor_class", "matches":
    [{"class_name", "kind": "attribute"|"trace_source", "type"|
    "callback_type", "location"}], "resolution": "anchored"|"global",
    "note"}. Empty matches come back as {"error", "closest_matches", "hint"}.
    """
    segments = [s for s in path_string.strip().split("/") if s and s != "..."]
    if not segments:
        return {
            "error": f"empty or malformed Config path: {path_string!r}",
            "hint": 'Pass a path like "/NodeList/*/DeviceList/*/'
            '$ns3::WifiNetDevice/Mac/$ns3::ApWifiMac/AssociatedSta".',
        }
    final = segments[-1]
    if final.startswith("$"):
        return {
            "error": f"path ends in a class segment ({final}), not an "
            "attribute or trace name",
            "hint": "Append the attribute/trace you want to resolve, or use "
            f"get_typeid_attributes('{_norm_class(final)}') to list what "
            "that class offers.",
        }

    anchor_idx = max(
        (i for i, s in enumerate(segments[:-1]) if s.startswith("$")), default=None
    )
    note_parts: list[str] = []
    current: str | None = None
    if anchor_idx is not None:
        current = segments[anchor_idx].removeprefix("$")
        # Follow intermediate segments as Pointer-typed attributes.
        for hop in segments[anchor_idx + 1 : -1]:
            if hop in ("*",) or hop.isdigit():
                continue  # instance selectors do not change the type
            hits = _attrs_for_classes(_chain_class_names(current), hop)
            nxt = None
            for _cn, _an, at, _dv, _hp, _p, _l in hits:
                m = at and at.startswith("Pointer<") and at.endswith(">")
                if m:
                    nxt = "ns3::" + _norm_class(at[len("Pointer<") : -1])
                    break
            if nxt is None:
                note_parts.append(
                    f"could not follow segment {hop!r} from {current} "
                    "(not a Pointer<Class> attribute); fell back to a global "
                    "search for the final name"
                )
                current = None
                break
            current = nxt

    matches: list[dict] = []
    resolution = "global"
    if current is not None:
        chain = _chain_class_names(current)
        attrs = _attrs_for_classes(chain, final)
        traces = _traces_for_classes(chain, final)
        if attrs or traces:
            resolution = "anchored"
        else:
            note_parts.append(
                f"{final!r} is not an attribute/trace of {current} or its "
                "ancestors; showing global candidates instead"
            )
    if resolution == "global":
        attrs = _con().execute(
            """SELECT a.class_name, a.attr_name, a.attr_type, a.default_value,
                      a.help, f.path, a.line
               FROM typeid_attributes a JOIN files f ON f.id = a.file_id
               WHERE a.attr_name = ? ORDER BY a.class_name""",
            (final,),
        ).fetchall()
        traces = _con().execute(
            """SELECT t.class_name, t.trace_name, t.help, t.callback_type,
                      f.path, t.line
               FROM trace_sources t JOIN files f ON f.id = t.file_id
               WHERE t.trace_name = ? AND t.origin = 'AddTraceSource'
               ORDER BY t.class_name""",
            (final,),
        ).fetchall()

    for cn, _an, at, _dv, _hp, path, line in attrs:
        matches.append(
            {
                "class_name": cn,
                "kind": "attribute",
                "type": at,
                "location": f"{path}:{line}",
            }
        )
    for cn, _tn, _hp, cb, path, line in traces:
        matches.append(
            {
                "class_name": cn,
                "kind": "trace_source",
                "callback_type": cb,
                "location": f"{path}:{line}",
            }
        )

    if not matches:
        pool = [
            r[0]
            for r in _con().execute("SELECT DISTINCT attr_name FROM typeid_attributes")
        ] + [r[0] for r in _con().execute("SELECT DISTINCT trace_name FROM trace_sources")]
        return budget.not_found(
            f"attribute or trace source named {final!r}"
            + (f" reachable from {segments[anchor_idx]}" if anchor_idx is not None else ""),
            budget.closest(final, pool),
            "Check the name with find_trace_sources(pattern) or "
            "get_typeid_attributes(class_name); Config paths are "
            "case-sensitive.",
        )

    return {
        "path": path_string,
        "final_name": final,
        "anchor_class": segments[anchor_idx] if anchor_idx is not None else None,
        "matches": matches[: budget.MAX_RESULTS],
        "resolution": resolution,
        "note": "; ".join(note_parts) or None,
    }


# ---------------------------------------------------------------------------


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        prog="ns3kg-server",
        description="MCP server (stdio) answering code questions from a "
        "ns3kg-index SQLite database.",
    )
    ap.add_argument("--db", required=True, help="path to the SQLite index")
    args = ap.parse_args(argv)

    global _DB_PATH
    _DB_PATH = Path(args.db).resolve()
    if not _DB_PATH.is_file():
        sys.exit(
            f"index not found: {_DB_PATH}\n"
            f"Build it first: ns3kg-index <source-root> --db {_DB_PATH}"
        )
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
