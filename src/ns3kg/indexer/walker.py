"""File discovery and incremental indexing."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .. import db
from . import cpp, py

EXTS = {
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".py": "python",
}
SKIP_DIRS = {"__pycache__", "build", ".git", ".venv"}


def discover(root: Path):
    """Yield (abs_path, rel_posix_path, language) for every indexable file."""
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        lang = EXTS.get(p.suffix.lower())
        if lang is None:
            continue
        parts = p.relative_to(root).parts[:-1]
        if any(d in SKIP_DIRS or d.startswith(".") for d in parts):
            continue
        yield p, p.relative_to(root).as_posix(), lang


def index_directory(root: Path, db_path, full: bool = False, log=None) -> dict:
    log = log or (lambda *a: None)
    t0 = time.time()
    root = Path(root).resolve()
    con = db.connect(db_path)
    db.init_db(con)

    existing = db.get_files(con)
    seen: set[str] = set()
    parsed = failed = unchanged = 0

    for abs_path, rel, lang in discover(root):
        seen.add(rel)
        st = abs_path.stat()
        prev = None if full else existing.get(rel)
        if prev is not None and prev[0] == st.st_mtime:
            unchanged += 1
            continue
        data = abs_path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if prev is not None and prev[1] == sha:
            db.touch_file(con, rel, st.st_mtime)
            unchanged += 1
            continue
        try:
            extraction = cpp.extract(data) if lang == "cpp" else py.extract(data)
        except Exception as e:  # one bad file must never abort the run
            log(f"  PARSE FAILED (skipped): {rel}: {e}")
            db.replace_file(con, rel, lang, st.st_mtime, sha, None)
            failed += 1
            continue
        db.replace_file(con, rel, lang, st.st_mtime, sha, extraction)
        parsed += 1

    removed = set(existing) - seen
    if removed:
        db.delete_files(con, removed)

    db.set_meta(
        con,
        root=str(root),
        indexed_at=str(time.time()),
        schema_version=db.SCHEMA_VERSION,
    )
    stats = {
        "parsed": parsed,
        "failed": failed,
        "unchanged": unchanged,
        "removed": len(removed),
        "seconds": round(time.time() - t0, 2),
        "tables": db.table_counts(con),
    }
    con.close()
    return stats
