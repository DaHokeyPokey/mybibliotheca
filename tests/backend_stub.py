"""Pretend the Kuzu backend is active, without leaking into other test modules.

Importing `app.routes` checks the active backend, and the real check reads `data/`
and the PostgreSQL settings. Import app modules inside `kuzu_backend()`, and import
the autouse `kuzu_backend_for_module` fixture so the module's tests see Kuzu too.
Never assign `persistence_state.get_persistence_state` at module level: it stays
replaced for every later test module, including tests/test_persistence_state.py.
"""

from contextlib import contextmanager

import pytest

from app.infrastructure import persistence_state


def _kuzu_state():
    return {"backend": "kuzu", "migration_run_id": None}


@contextmanager
def kuzu_backend():
    real = persistence_state.get_persistence_state
    persistence_state.get_persistence_state = _kuzu_state
    try:
        yield
    finally:
        persistence_state.get_persistence_state = real


@pytest.fixture(autouse=True)
def kuzu_backend_for_module(monkeypatch):
    monkeypatch.setattr(persistence_state, "get_persistence_state", _kuzu_state)
