"""Migration v7 (journal and progress tables), their dataclasses, and the book delete paths."""

import hashlib
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend_stub import kuzu_backend, kuzu_backend_for_module  # noqa: E402,F401 - autouse fixture

with kuzu_backend():
    from app.domain import models
    from app.infrastructure import postgres_store
    import app.services.postgres_book_service as pg_books


def _checksum(statements):
    return hashlib.sha256("\n-- statement --\n".join(statements).encode("utf-8")).hexdigest()


def _sql(statement):
    return " ".join(statement.split())


def _columns(create_table):
    body = create_table[create_table.index("(") + 1 : create_table.rindex(")")]
    return [line.strip().rstrip(",") for line in body.splitlines() if line.strip()]


# --- Schema ---------------------------------------------------------------------

# initialize_schema refuses to start when an installed version's checksum changes,
# so editing shipped schema text breaks every existing install. Fixes go in a new version.
SHIPPED_CHECKSUMS = {
    1: "bfe30b4c4618c89ce7cf7198d4a63ac42fb8f9239381643f981b67243696511c",
    2: "38f81952d6e949a31862c4bea193a7c2ea7b0da960997475666b1674a6f5a520",
    3: "0ca350bf3241f5c6d2b399a4a00c9b55b3254f5a14460fc2fe3163cf4c6334ed",
    4: "0eb28612a6b87d6278ccd827562ceca8c1b1a1a01e1bfd9bb932339c5530b0b2",
    5: "345d6f5a3194206a4107ade335620ab616380c4d0ba7187a787c059bde20e437",
    6: "d932e8423592d65a1f76549de9f44b2f7b5112b03f04fb1b6c5a090803e3ed90",
    7: "ace67c68b1e9d4b374b711e37bd1e3ba11e72329e823fb519ee9f3eedcf2fb1a",
}


def test_shipped_schema_text_is_unchanged():
    actual = {1: _checksum(postgres_store.SCHEMA_DDL)}
    actual.update(
        (version, _checksum(statements))
        for version, _name, statements in postgres_store._SCHEMA_MIGRATIONS
        if version in SHIPPED_CHECKSUMS
    )
    assert actual == SHIPPED_CHECKSUMS


def test_migration_7_creates_journal_entries_before_progress_updates():
    version, name, statements = postgres_store._SCHEMA_MIGRATIONS[-1]

    assert (version, name) == (7, "progress-and-journal")
    assert len(statements) == 4
    assert _sql(statements[0]).startswith("CREATE TABLE IF NOT EXISTS journal_entries (")
    assert _sql(statements[1]) == (
        "CREATE INDEX IF NOT EXISTS journal_entries_user_book_idx "
        "ON journal_entries (user_id, book_id, updated_at DESC)"
    )
    assert _sql(statements[2]).startswith("CREATE TABLE IF NOT EXISTS progress_updates (")
    assert _sql(statements[3]) == (
        "CREATE INDEX IF NOT EXISTS progress_updates_user_book_time_idx "
        "ON progress_updates (user_id, book_id, occurred_at DESC)"
    )


def test_migration_7_columns_defaults_and_foreign_keys():
    statements = postgres_store._SCHEMA_MIGRATIONS[-1][2]

    assert _columns(statements[0]) == [
        "id TEXT PRIMARY KEY",
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT",
        "book_id TEXT REFERENCES books(id) ON DELETE RESTRICT",
        "title TEXT NOT NULL",
        "body_html TEXT NOT NULL DEFAULT ''",
        "is_private BOOLEAN NOT NULL DEFAULT TRUE",
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "extra JSONB NOT NULL DEFAULT '{}'::jsonb",
    ]
    assert _columns(statements[2]) == [
        "id TEXT PRIMARY KEY",
        "user_id TEXT NOT NULL REFERENCES users(id) ON DELETE RESTRICT",
        "book_id TEXT NOT NULL REFERENCES books(id) ON DELETE RESTRICT",
        "occurred_at TIMESTAMPTZ NOT NULL",
        "current_page INTEGER",
        "position_percent NUMERIC(5,2)",
        "pages_delta INTEGER NOT NULL DEFAULT 0",
        "minutes_read INTEGER",
        "comment TEXT",
        "journal_entry_id TEXT REFERENCES journal_entries(id) ON DELETE SET NULL",
        "reading_log_id TEXT REFERENCES reading_logs(id) ON DELETE SET NULL",
        "source TEXT NOT NULL DEFAULT 'modal'",
        "finished BOOLEAN NOT NULL DEFAULT FALSE",
        "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        "extra JSONB NOT NULL DEFAULT '{}'::jsonb",
    ]


# --- Dataclasses ------------------------------------------------------------------

def test_progress_update_and_journal_entry_defaults():
    update = models.ProgressUpdate(user_id="u1", book_id="b1", occurred_at=models.now_utc())
    assert (update.id, update.current_page, update.position_percent, update.pages_delta) == (None, None, None, 0)
    assert (update.journal_entry_id, update.reading_log_id, update.source, update.finished) == (None, None, "modal", False)

    entry = models.JournalEntry(user_id="u1", title="Notes")
    assert (entry.id, entry.book_id, entry.body_html, entry.is_private) == (None, None, "", True)


# --- Delete paths -----------------------------------------------------------------

class _RecordingDb:
    """Fake store that models who owns book b1 and records every statement in order."""

    def __init__(self, owners):
        self.owners = set(owners)
        self.statements = []
        self.rowcount = 0
        self._rows = []

    @contextmanager
    def transaction(self):
        yield self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        s = _sql(sql)
        self.statements.append((s, tuple(params)))
        self._rows = []
        self.rowcount = 1
        if s.startswith("SELECT id FROM books"):
            self._rows = [(params[0],)]
        elif s.startswith("DELETE FROM user_books WHERE user_id"):
            pair = (params[0], params[1])
            self.rowcount = int(pair in self.owners)
            self.owners.discard(pair)
        elif s.startswith(("SELECT 1 FROM user_books", "SELECT user_id FROM user_books")):
            self._rows = [(user,) for user, _book in self.owners]

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


PROGRESS_FOR_ALICE = ("DELETE FROM progress_updates WHERE user_id = %s AND book_id = %s", ("alice", "b1"))
JOURNAL_FOR_ALICE = ("UPDATE journal_entries SET book_id = NULL WHERE user_id = %s AND book_id = %s", ("alice", "b1"))
PROGRESS_FOR_ALL = ("DELETE FROM progress_updates WHERE book_id = %s", ("b1",))
JOURNAL_FOR_ALL = ("UPDATE journal_entries SET book_id = NULL WHERE book_id = %s", ("b1",))
DELETE_BOOK = ("DELETE FROM books WHERE id = %s", ("b1",))


def _service(monkeypatch, db):
    monkeypatch.setattr(pg_books.redis_cache, "invalidate_tags", lambda *tags: True)
    return pg_books.PostgresBookService(store=db)


def test_removing_a_shared_book_clears_only_that_users_progress_and_journal_links(monkeypatch):
    db = _RecordingDb({("alice", "b1"), ("bob", "b1")})

    assert _service(monkeypatch, db).delete_book_for_user_sync("b1", "alice") is True
    assert PROGRESS_FOR_ALICE in db.statements
    assert JOURNAL_FOR_ALICE in db.statements
    assert PROGRESS_FOR_ALL not in db.statements
    assert JOURNAL_FOR_ALL not in db.statements
    assert DELETE_BOOK not in db.statements


def test_removing_the_last_copy_clears_progress_and_journal_links_before_deleting_the_book(monkeypatch):
    db = _RecordingDb({("alice", "b1")})

    assert _service(monkeypatch, db).delete_book_for_user_sync("b1", "alice") is True
    book_deleted_at = db.statements.index(DELETE_BOOK)
    for statement in (PROGRESS_FOR_ALICE, JOURNAL_FOR_ALICE, PROGRESS_FOR_ALL, JOURNAL_FOR_ALL):
        assert db.statements.index(statement) < book_deleted_at


def test_deleting_a_book_everywhere_clears_all_progress_and_journal_links_first(monkeypatch):
    db = _RecordingDb({("alice", "b1"), ("bob", "b1")})

    assert _service(monkeypatch, db).delete_book_sync("b1") is True
    book_deleted_at = db.statements.index(DELETE_BOOK)
    assert db.statements.index(PROGRESS_FOR_ALL) < book_deleted_at
    assert db.statements.index(JOURNAL_FOR_ALL) < book_deleted_at


def test_removing_a_book_outside_your_library_touches_no_progress_or_journal_rows(monkeypatch):
    db = _RecordingDb({("bob", "b1")})

    assert _service(monkeypatch, db).delete_book_for_user_sync("b1", "alice") is False
    assert not any("progress_updates" in s or "journal_entries" in s for s, _params in db.statements)
