import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

# pylint: disable=wrong-import-position
from vaillant_boiler_ui.db import open_database  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "boiler.db")


@pytest.fixture
def conn(db_path):
    c = open_database(db_path)
    yield c
    c.close()


class FakeWriter:
    """Collects what would be written."""

    def __init__(self):
        self.rows = []

    def insert(self, table, cols, row):
        self.rows.append((table, tuple(cols), tuple(row)))


@pytest.fixture
def writer():
    return FakeWriter()
