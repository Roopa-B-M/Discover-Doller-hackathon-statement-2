import pytest
from helpers import seeded_db


@pytest.fixture
def db(tmp_path):
    """Fresh temporary database seeded from the real data/seed.json."""
    return seeded_db(tmp_path)
