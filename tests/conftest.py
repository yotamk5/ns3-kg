from pathlib import Path

import pytest

from ns3kg import db
from ns3kg.indexer import walker

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_index(tmp_path_factory):
    """Index tests/fixtures once; return (db_path, stats)."""
    db_path = tmp_path_factory.mktemp("idx") / "ns3kg.db"
    stats = walker.index_directory(FIXTURES, db_path)
    return db_path, stats


@pytest.fixture(scope="session")
def con(fixture_index):
    db_path, _ = fixture_index
    con = db.connect(db_path)
    yield con
    con.close()
