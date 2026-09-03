import os
from pathlib import Path

# Tests must never touch a configured Render/PostgreSQL database.
os.environ['DATABASE_URL'] = 'sqlite:///./pytest_luqman_trade.db'
os.environ.setdefault('BROKER_MODE', 'simulator')

import pytest

from app.db import Base, engine


@pytest.fixture(autouse=True)
def isolated_database():
    """Provide a clean, dedicated SQLite schema for every test.

    The guard above is intentionally set before any app import so running pytest
    cannot accidentally drop or mutate a production database from the shell env.
    """
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


def pytest_sessionfinish(session, exitstatus):
    try:
        engine.dispose()
    finally:
        Path('pytest_luqman_trade.db').unlink(missing_ok=True)
