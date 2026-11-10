"""Progress service rules, and the daily-log merge it shares with the reading-log service."""

import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend_stub import kuzu_backend, kuzu_backend_for_module  # noqa: E402,F401 - autouse fixture

with kuzu_backend():
    import app.services as services
    from app.domain.models import ReadingLog
    from app.services import postgres_progress_service as progress
    from app.services import postgres_reading_log_service as logs
    from app.services.postgres_service_facade import PostgresServiceFacade

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
LOG_DAY = date(2026, 11, 9)
LOG_ROW = ("log-1", "alice", "b1", LOG_DAY, 8, 12, "old\n\nnew", NOW, NOW)


class _Db:
    """Fake store: records statements and answers each fetch by the first matching SQL fragment."""

    def __init__(self, answers=(), fail_on=None):
        self.answers = list(answers)
        self.fail_on = fail_on
        self.statements = []
        self.committed = False
        self.rowcount = 1
        self._current = None

    @contextmanager
    def transaction(self):
        yield self
        self.committed = True

    @contextmanager
    def connection(self):
        yield self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("database failure")
        self.statements.append((sql, tuple(params)))
        self._current = next((value for fragment, value in self.answers if fragment in sql), None)

    def fetchone(self):
        return self._current

    def fetchall(self):
        return self._current or []

    def has(self, fragment):
        return any(fragment in sql for sql, _params in self.statements)

    def params(self, fragment):
        matches = [params for sql, params in self.statements if fragment in sql]
        assert len(matches) == 1, f"{fragment!r} ran {len(matches)} times"
        return matches[0]


# --- create_reading_log_sync must not change for its existing callers -----------------

def _log(**overrides):
    fields = {"user_id": "alice", "book_id": "b1", "date": LOG_DAY, "pages_read": 3, "minutes_read": 2,
              "notes": "new", "created_at": NOW, "updated_at": NOW}
    return ReadingLog(**{**fields, **overrides})


def _reading_logs(monkeypatch, db):
    invalidated = []
    monkeypatch.setattr(logs.redis_cache, "invalidate_tags", lambda *tags: invalidated.append((tags, db.committed)) or True)
    return logs.PostgresReadingLogService(store=db), invalidated


_FIND_DAILY = "SELECT id,pages_read,minutes_read,notes FROM reading_logs"
_READ_BACK = "FROM reading_logs WHERE id=%s AND user_id=%s"
_ALL_TAGS = ("postgres:reading_logs:alice", "postgres:user_books:alice", "postgres:reading_logs")


def test_create_log_inserts_a_new_daily_row_and_upserts_the_start_date(monkeypatch):
    db = _Db([(_READ_BACK, LOG_ROW)])
    service, invalidated = _reading_logs(monkeypatch, db)

    result = service.create_reading_log_sync(_log(id="new-id"))

    starts = ["SELECT pg_advisory_xact_lock", _FIND_DAILY, "INSERT INTO reading_logs", "SELECT id, user_id", "INSERT INTO user_books"]
    assert [sql for sql, _params in db.statements if not any(sql.startswith(s) for s in starts)] == []
    assert [next(s for s in starts if sql.startswith(s)) for sql, _params in db.statements] == starts
    assert db.statements[0][1] == ("reading-log:alice:b1:2026-11-09",)
    assert db.params("INSERT INTO reading_logs") == ("new-id", "alice", "b1", LOG_DAY, 3, 2, "new", NOW, NOW)
    assert db.params("INSERT INTO user_books") == ("alice", "b1", datetime(2026, 11, 9, tzinfo=timezone.utc))
    assert result["id"] == "log-1" and result["date"] == "2026-11-09" and result["updated_at"] == NOW.isoformat()
    assert invalidated == [(_ALL_TAGS, True)]


def test_create_log_merges_into_the_existing_daily_row(monkeypatch):
    db = _Db([(_FIND_DAILY, ("log-1", 5, 10, "old")), (_READ_BACK, LOG_ROW)])
    service, _invalidated = _reading_logs(monkeypatch, db)

    service.create_reading_log_sync(_log(id="new-id"))

    assert db.params("UPDATE reading_logs") == (8, 12, "old\n\nnew", NOW, "log-1")
    assert not db.has("INSERT INTO reading_logs")


def test_retrying_a_create_with_the_same_id_does_not_count_twice(monkeypatch):
    db = _Db([(_FIND_DAILY, ("log-1", 5, 10, "old")), (_READ_BACK, LOG_ROW)])
    service, _invalidated = _reading_logs(monkeypatch, db)

    service.create_reading_log_sync(_log(id="log-1"))

    assert not db.has("UPDATE reading_logs")


def test_a_bookless_log_does_not_touch_user_books(monkeypatch):
    db = _Db([(_READ_BACK, LOG_ROW)])
    service, _invalidated = _reading_logs(monkeypatch, db)

    service.create_reading_log_sync(_log(book_id=None))

    assert db.statements[0][1] == ("reading-log:alice:none:2026-11-09",)
    assert not db.has("INSERT INTO user_books")


def test_a_failed_create_returns_none_and_invalidates_nothing(monkeypatch):
    db = _Db(fail_on="INSERT INTO reading_logs")
    service, invalidated = _reading_logs(monkeypatch, db)

    assert service.create_reading_log_sync(_log()) is None
    assert invalidated == []


# --- Progress service: helpers --------------------------------------------------------

WHEN = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
EARLIER = WHEN - timedelta(days=2)
_LIBRARY = "FROM user_books ub"
_NEWEST_TIME = "max(occurred_at)"
_INSERT_UPDATE = "INSERT INTO progress_updates"
_SAVE_BOOK = "UPDATE user_books SET current_page=%s, total_pages"
_RESTORE_BOOK = "UPDATE user_books SET current_page=%s, reading_status"
_FIND_UPDATE = "SELECT book_id FROM progress_updates"
_READ_UPDATE = "SELECT book_id, pages_delta"
_NEWEST_ID = "SELECT id FROM progress_updates"
_READ_DAILY = "SELECT pages_read, minutes_read, notes FROM reading_logs"
_JOURNAL_FOR_BOOK = "FROM journal_entries WHERE id=%s AND user_id=%s AND book_id=%s"
_JOURNAL_ROW = ("j1", "alice", "b1", "Title", "<p>x</p>", True, WHEN, WHEN)


def _book(current_page=10, total_pages=300, page_count=None, status="reading", finish_date=None):
    return (current_page, total_pages, page_count, status, finish_date)


def _update_row(**overrides):
    row = {"id": "u1", "user_id": "alice", "book_id": "b1", "occurred_at": WHEN, "current_page": 50,
           "position_percent": None, "pages_delta": 40, "minutes_read": None, "comment": None,
           "journal_entry_id": None, "reading_log_id": "log-1", "source": "modal", "finished": False,
           "created_at": WHEN, "updated_at": WHEN}
    return tuple({**row, **overrides}.values())


def _db(book=None, newest=None, more=(), **kwargs):
    return _Db([(_LIBRARY, book or _book()), (_NEWEST_TIME, (newest,)), (_INSERT_UPDATE, _update_row()), *more], **kwargs)


def _harness(monkeypatch, db):
    invalidated, merged = [], []
    monkeypatch.setattr(progress.redis_cache, "invalidate_tags", lambda *tags: invalidated.append((tags, db.committed)) or True)
    service = progress.PostgresProgressService(store=db)

    def fake_merge(_cursor, payload):
        merged.append(payload)
        return {"id": "log-1", "date": payload["date"], "pages_read": payload["pages_read"],
                "minutes_read": payload["minutes_read"], "notes": payload["notes"]}

    monkeypatch.setattr(service.reading_logs, "_merge_daily_log", fake_merge)
    return SimpleNamespace(service=service, invalidated=invalidated, merged=merged)


def _record(service, **kwargs):
    kwargs.setdefault("occurred_at", WHEN)
    return service.log_progress_sync(user_id="alice", book_id="b1", **kwargs)


def _inserted(db):
    names = ("id", "user_id", "book_id", "occurred_at", "current_page", "position_percent", "pages_delta",
             "minutes_read", "comment", "journal_entry_id", "reading_log_id", "source", "finished", "extra")
    return dict(zip(names, db.params(_INSERT_UPDATE)))


def _saved(db):
    names = ("current_page", "total_pages", "reading_status", "start_date", "finish_date", "user_id", "book_id")
    return dict(zip(names, db.params(_SAVE_BOOK)))


# --- Validation -----------------------------------------------------------------------

@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"current_page": 1, "position_percent": 5},
        {"current_page": -1},
        {"current_page": "5"},
        {"current_page": 1.5},
        {"current_page": True},
        {"current_page": 2**31},
        {"position_percent": -1},
        {"position_percent": 100.5},
        {"position_percent": "abc"},
        {"position_percent": "NaN"},
        {"position_percent": True},
        {"current_page": 1, "total_pages": 0},
        {"current_page": 1, "total_pages": -3},
        {"current_page": 1, "minutes_read": -1},
        {"current_page": 1, "minutes_read": "5"},
        {"current_page": 1, "occurred_at": datetime(2026, 1, 15, 12, 0)},
        {"current_page": 1, "occurred_at": "2026-01-15"},
        {"current_page": 1, "occurred_at": datetime.now(timezone.utc) + timedelta(hours=1)},
        {"current_page": 1, "finish_date": datetime(2026, 1, 15, 12, 0)},
        {"current_page": 1, "occurred_at": datetime(1899, 12, 31, 23, 59, tzinfo=timezone.utc)},
        {"current_page": 1, "occurred_at": datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14)))},
        {"current_page": 1, "finish_date": datetime(1899, 12, 31, 23, 59, tzinfo=timezone.utc)},
        {"current_page": 1, "comment": "x" * (progress.MAX_COMMENT_CHARS + 1)},
        {"current_page": 1, "comment": "a\x00b"},
        {"current_page": 1, "comment": 5},
        {"current_page": 1, "source": "admin"},
        {"current_page": 1, "journal_entry_id": "j1", "new_journal": {"title": "T"}},
        {"current_page": 1, "new_journal": {"title": ""}},
        {"current_page": 1, "new_journal": "not a dict"},
    ],
    ids=lambda kwargs: "-".join(f"{key}" for key in kwargs) or "nothing",
)
def test_invalid_input_is_rejected_before_any_sql(monkeypatch, kwargs):
    db = _db()
    harness = _harness(monkeypatch, db)
    with pytest.raises(ValueError):
        _record(harness.service, **kwargs)
    assert db.statements == []


def test_a_time_within_clock_skew_is_accepted(monkeypatch):
    db = _db()
    _record(_harness(monkeypatch, db).service, current_page=20, occurred_at=datetime.now(timezone.utc) + timedelta(minutes=1))
    assert db.has(_INSERT_UPDATE)


@pytest.mark.parametrize("book", [_book(total_pages=300), _book(total_pages=None, page_count=300)], ids=["library-total", "catalog-total"])
def test_a_page_past_the_end_is_rejected(monkeypatch, book):
    db = _db(book=book)
    with pytest.raises(ValueError):
        _record(_harness(monkeypatch, db).service, current_page=301)
    assert not db.has(_INSERT_UPDATE)


def test_a_book_outside_the_library_is_not_found(monkeypatch):
    db = _Db()
    with pytest.raises(LookupError):
        _record(_harness(monkeypatch, db).service, current_page=5)
    assert not db.has(_INSERT_UPDATE)


def test_a_journal_entry_from_another_user_or_book_is_not_found(monkeypatch):
    db = _db()
    with pytest.raises(LookupError):
        _record(_harness(monkeypatch, db).service, current_page=50, journal_entry_id="j-other")
    assert db.params(_JOURNAL_FOR_BOOK) == ("j-other", "alice", "b1")
    assert not db.has(_INSERT_UPDATE)


# --- Delta and date rules -------------------------------------------------------------

def test_the_newest_update_counts_pages_since_the_library_position(monkeypatch):
    db = _db()
    harness = _harness(monkeypatch, db)

    result = _record(harness.service, current_page=50, minutes_read=15, comment="  good  ")

    assert _inserted(db)["pages_delta"] == 40
    assert (harness.merged[0]["pages_read"], harness.merged[0]["minutes_read"], harness.merged[0]["notes"]) == (40, 15, None)
    assert _inserted(db)["comment"] == "good"
    assert _inserted(db)["reading_log_id"] == "log-1"
    assert _saved(db)["current_page"] == 50
    assert result["reading_log"]["id"] == "log-1" and result["update"]["occurred_at"] == WHEN.isoformat()
    assert result["finished"] is False


def test_going_backwards_counts_nothing_but_moves_the_book(monkeypatch):
    db = _db()
    harness = _harness(monkeypatch, db)

    _record(harness.service, current_page=5)

    assert _inserted(db)["pages_delta"] == 0
    assert harness.merged == []
    assert _inserted(db)["reading_log_id"] is None
    assert _saved(db)["current_page"] == 5


def test_a_backdated_update_keeps_its_minutes_but_does_not_move_the_book(monkeypatch):
    db = _db(newest=WHEN)
    harness = _harness(monkeypatch, db)

    _record(harness.service, current_page=200, minutes_read=30, occurred_at=EARLIER)

    assert _inserted(db)["pages_delta"] == 0
    assert (harness.merged[0]["pages_read"], harness.merged[0]["minutes_read"]) == (0, 30)
    assert not db.has(_SAVE_BOOK)


def test_a_backdated_update_stores_nothing_to_restore(monkeypatch):
    db = _db(newest=WHEN)
    _record(_harness(monkeypatch, db).service, current_page=200, occurred_at=EARLIER)
    assert "previous_page" not in _inserted(db)["extra"]


def test_a_backdated_update_at_the_last_page_does_not_finish_the_book(monkeypatch):
    db = _db(newest=WHEN)
    _record(_harness(monkeypatch, db).service, current_page=300, occurred_at=EARLIER)
    assert _inserted(db)["finished"] is False


def test_an_update_at_the_same_time_as_the_newest_is_not_backdated(monkeypatch):
    db = _db(newest=WHEN)
    _record(_harness(monkeypatch, db).service, current_page=50, occurred_at=WHEN)
    assert _inserted(db)["pages_delta"] == 40


def test_a_percent_update_counts_no_pages_and_keeps_the_page(monkeypatch):
    db = _db()
    harness = _harness(monkeypatch, db)

    _record(harness.service, position_percent=42.5)

    assert _inserted(db)["position_percent"] == Decimal("42.50")
    assert _inserted(db)["current_page"] is None and _inserted(db)["pages_delta"] == 0
    assert harness.merged == []
    assert _saved(db)["current_page"] == 10


def test_the_senders_local_date_picks_the_daily_row(monkeypatch):
    db = _db()
    harness = _harness(monkeypatch, db)
    evening = datetime(2026, 1, 14, 23, 30, tzinfo=timezone(timedelta(hours=-5)))  # already Jan 15 in UTC

    _record(harness.service, current_page=50, occurred_at=evening)

    assert harness.merged[0]["date"] == date(2026, 1, 14)
    assert _inserted(db)["extra"] and '"log_date": "2026-01-14"' in _inserted(db)["extra"]


def test_the_update_records_what_it_replaced(monkeypatch):
    finished_on = datetime(2025, 5, 1, tzinfo=timezone.utc)
    db = _db(book=_book(current_page=10, status="on_hold", finish_date=finished_on))

    _record(_harness(monkeypatch, db).service, current_page=50)

    extra = _inserted(db)["extra"]
    assert '"previous_page": 10' in extra and '"previous_status": "on_hold"' in extra and '"previous_total_pages": 300' in extra
    assert finished_on.isoformat() in extra


def test_the_tie_breaker_is_taken_after_the_lock(monkeypatch):
    db = _db()
    _record(_harness(monkeypatch, db).service, current_page=50)
    insert = next(sql for sql, _params in db.statements if _INSERT_UPDATE in sql)
    assert "clock_timestamp()" in insert


def test_a_supplied_total_is_saved_and_used_for_validation(monkeypatch):
    db = _db(book=_book(total_pages=None))
    _record(_harness(monkeypatch, db).service, current_page=50, total_pages=120)
    assert _saved(db)["total_pages"] == 120


def test_a_catalog_total_is_not_saved_as_the_readers_own(monkeypatch):
    db = _db(book=_book(total_pages=None, page_count=250))
    _record(_harness(monkeypatch, db).service, current_page=50)
    assert _saved(db)["total_pages"] is None


def test_an_existing_library_total_is_kept(monkeypatch):
    db = _db(book=_book(total_pages=300, page_count=250))
    _record(_harness(monkeypatch, db).service, current_page=50)
    assert _saved(db)["total_pages"] == 300


# --- Status and finishing -------------------------------------------------------------

@pytest.mark.parametrize("status", [None, "", "plan_to_read", "want_to_read", "library_only"])
def test_not_yet_started_statuses_become_reading(monkeypatch, status):
    db = _db(book=_book(status=status))
    _record(_harness(monkeypatch, db).service, current_page=50)
    assert _saved(db)["reading_status"] == "reading"


@pytest.mark.parametrize("status", ["on_hold", "did_not_finish", "read"])
def test_other_statuses_are_left_alone_until_the_book_finishes(monkeypatch, status):
    db = _db(book=_book(status=status))
    _record(_harness(monkeypatch, db).service, current_page=50)
    assert _saved(db)["reading_status"] == status


@pytest.mark.parametrize("status", ["on_hold", "did_not_finish", "reading"])
def test_reaching_the_last_page_finishes_the_book(monkeypatch, status):
    db = _db(book=_book(status=status))

    result = _record(_harness(monkeypatch, db).service, current_page=300)

    assert _inserted(db)["finished"] is True and result["finished"] is True
    assert (_saved(db)["reading_status"], _saved(db)["finish_date"]) == ("read", WHEN)


def test_a_finish_date_is_used_when_the_book_finishes(monkeypatch):
    db = _db()
    _record(_harness(monkeypatch, db).service, current_page=300, finish_date=EARLIER)
    assert _saved(db)["finish_date"] == EARLIER


def test_reaching_100_percent_finishes_the_book(monkeypatch):
    db = _db()
    _record(_harness(monkeypatch, db).service, position_percent=100)
    assert _inserted(db)["finished"] is True and _saved(db)["reading_status"] == "read"


def test_a_finish_date_finishes_a_book_with_no_known_total(monkeypatch):
    db = _db(book=_book(total_pages=None))
    _record(_harness(monkeypatch, db).service, current_page=50, finish_date=EARLIER)
    assert _inserted(db)["finished"] is True and _saved(db)["finish_date"] == EARLIER


def test_a_finish_date_does_not_finish_a_book_short_of_its_known_total(monkeypatch):
    db = _db()
    _record(_harness(monkeypatch, db).service, current_page=50, finish_date=EARLIER)
    assert _inserted(db)["finished"] is False


def test_a_book_with_no_known_total_stays_unfinished_without_a_finish_date(monkeypatch):
    db = _db(book=_book(total_pages=None))
    _record(_harness(monkeypatch, db).service, current_page=50)
    assert _inserted(db)["finished"] is False


def test_repeating_the_last_page_keeps_the_original_finish_date(monkeypatch):
    finished_on = datetime(2025, 5, 1, tzinfo=timezone.utc)
    db = _db(book=_book(current_page=300, status="read", finish_date=finished_on))
    _record(_harness(monkeypatch, db).service, current_page=300)
    assert _saved(db)["finish_date"] == finished_on


# --- Journal links and caches ---------------------------------------------------------

def test_a_new_journal_entry_is_sanitized_and_linked(monkeypatch):
    db = _db(more=[("INSERT INTO journal_entries", _JOURNAL_ROW)])

    result = _record(_harness(monkeypatch, db).service, current_page=50,
                     new_journal={"title": "Notes", "body_html": "<p>x</p><script>alert(1)</script>"})

    params = db.params("INSERT INTO journal_entries")
    assert "<p>x</p>" in params and not any("script" in str(p) for p in params)
    assert _inserted(db)["journal_entry_id"] == "j1"
    assert result["journal_entry"]["id"] == "j1"


def test_an_existing_journal_entry_is_linked(monkeypatch):
    db = _db(more=[(_JOURNAL_FOR_BOOK, _JOURNAL_ROW)])
    result = _record(_harness(monkeypatch, db).service, current_page=50, journal_entry_id="j1")
    assert _inserted(db)["journal_entry_id"] == "j1" and result["journal_entry"]["id"] == "j1"


def test_caches_are_invalidated_after_commit(monkeypatch):
    db = _db()
    harness = _harness(monkeypatch, db)

    _record(harness.service, current_page=50)

    assert harness.invalidated == [((
        "postgres:reading_logs:alice", "postgres:user_books:alice", "postgres:reading_logs",
        "postgres:user_library:alice", "postgres:progress:alice",
    ), True)]


def test_a_failed_update_invalidates_nothing(monkeypatch):
    db = _db(fail_on=_INSERT_UPDATE)
    harness = _harness(monkeypatch, db)

    with pytest.raises(RuntimeError):
        _record(harness.service, current_page=50)

    assert harness.invalidated == []


# --- Reading progress back ------------------------------------------------------------

def test_book_progress_reports_page_position(monkeypatch):
    db = _db(book=_book(current_page=75, total_pages=300, status="reading"))

    result = _harness(monkeypatch, db).service.get_book_progress_sync("alice", "b1")

    assert (result["current_page"], result["total_pages"], result["percent"], result["status"]) == (75, 300, 25.0, "reading")
    assert result["last_update"] is None


def test_book_progress_prefers_the_latest_percent(monkeypatch):
    db = _db(more=[("SELECT id, user_id", _update_row(position_percent=Decimal("42.50"), current_page=None))])
    result = _harness(monkeypatch, db).service.get_book_progress_sync("alice", "b1")
    assert result["percent"] == 42.5 and result["last_update"]["position_percent"] == 42.5


def test_book_progress_outside_the_library_is_not_found(monkeypatch):
    with pytest.raises(LookupError):
        _harness(monkeypatch, _Db()).service.get_book_progress_sync("alice", "b1")


def test_update_list_is_newest_first_and_capped(monkeypatch):
    db = _db(more=[("SELECT id, user_id", [_update_row()])])

    rows = _harness(monkeypatch, db).service.list_book_updates_sync("alice", "b1", limit=9999)

    assert rows[0]["id"] == "u1"
    assert "ORDER BY occurred_at DESC" in db.statements[-1][0]
    assert db.statements[-1][1] == ("alice", "b1", 200)


# --- Delete ---------------------------------------------------------------------------

def _delete_db(newest_id="u1", daily=(40, 15, None), extra=None, log_id="log-1", pages=40, minutes=15):
    if extra is None:
        extra = {"log_date": "2026-01-15", "previous_page": 10, "previous_status": "plan_to_read", "previous_finish_date": None, "previous_total_pages": 300}
    return _Db([
        (_FIND_UPDATE, ("b1",)),
        (_READ_UPDATE, ("b1", pages, minutes, log_id, WHEN, extra)),
        (_NEWEST_ID, (newest_id,)),
        (_READ_DAILY, daily),
        (_LIBRARY, _book()),
    ])


def test_deleting_someone_elses_update_finds_nothing(monkeypatch):
    db = _Db()
    assert not _harness(monkeypatch, db).service.delete_progress_update_sync("u1", "mallory")
    assert db.params(_FIND_UPDATE) == ("u1", "mallory")
    assert not db.has("DELETE")


def test_only_the_latest_update_can_be_deleted(monkeypatch):
    db = _delete_db(newest_id="u2")
    with pytest.raises(progress.ProgressConflictError):
        _harness(monkeypatch, db).service.delete_progress_update_sync("u1", "alice")
    assert not db.has("DELETE")


def test_deleting_the_latest_reverses_the_daily_row_and_restores_the_book(monkeypatch):
    db = _delete_db(daily=(50, 20, None))
    harness = _harness(monkeypatch, db)

    result = harness.service.delete_progress_update_sync("u1", "alice")

    assert db.params("DELETE FROM progress_updates") == ("u1", "alice")
    assert db.params("UPDATE reading_logs") == (10, 5, "log-1", "alice")
    assert db.params(_RESTORE_BOOK) == (10, "plan_to_read", None, 300, "alice", "b1")
    assert result["current_page"] == 10
    assert harness.invalidated[0][1] is True


def test_undo_locks_the_daily_row_before_changing_the_update(monkeypatch):
    db = _delete_db(daily=(50, 20, None))

    _harness(monkeypatch, db).service.delete_progress_update_sync("u1", "alice")

    statements = [sql for sql, _params in db.statements]
    daily_lock = next(index for index, sql in enumerate(statements) if _READ_DAILY in sql)
    delete_update = next(index for index, sql in enumerate(statements) if "DELETE FROM progress_updates" in sql)
    assert daily_lock < delete_update


def test_deleting_restores_an_empty_library_total(monkeypatch):
    extra = {"log_date": "2026-01-15", "previous_page": 10, "previous_status": "reading", "previous_finish_date": None, "previous_total_pages": None}
    db = _delete_db(extra=extra)

    _harness(monkeypatch, db).service.delete_progress_update_sync("u1", "alice")

    assert db.params(_RESTORE_BOOK)[3] is None


def test_deleting_the_last_of_a_daily_row_removes_it(monkeypatch):
    db = _delete_db(daily=(40, 15, None))
    _harness(monkeypatch, db).service.delete_progress_update_sync("u1", "alice")
    assert db.params("DELETE FROM reading_logs") == ("log-1", "alice")


def test_a_daily_row_with_notes_survives_at_zero(monkeypatch):
    db = _delete_db(daily=(40, 15, "kept"))
    _harness(monkeypatch, db).service.delete_progress_update_sync("u1", "alice")
    assert db.params("UPDATE reading_logs") == (0, 0, "log-1", "alice")
    assert not db.has("DELETE FROM reading_logs")


def test_deleting_a_backdated_update_leaves_the_book_alone(monkeypatch):
    db = _delete_db(extra={"log_date": "2026-01-13"}, pages=0)
    _harness(monkeypatch, db).service.delete_progress_update_sync("u1", "alice")
    assert not db.has(_RESTORE_BOOK)


def test_deleting_an_update_with_no_daily_row_touches_no_log(monkeypatch):
    db = _delete_db(log_id=None, pages=0, minutes=None)
    _harness(monkeypatch, db).service.delete_progress_update_sync("u1", "alice")
    assert not db.has("reading_logs")


# --- Edit -----------------------------------------------------------------------------

def _edit_db(log_id="log-1", minutes=15, more=(), pages=40, extra=None):
    extra = {"log_date": "2026-01-14", **(extra or {})}
    return _Db([
        (_FIND_UPDATE, ("b1",)),
        (_READ_UPDATE, ("b1", pages, minutes, log_id, WHEN, extra)),
        ("UPDATE progress_updates", _update_row()),
        (_READ_DAILY, (40, 15, None)),
        *more,
    ])


@pytest.mark.parametrize("fields", [{}, {"current_page": 5}, {"occurred_at": WHEN}, {"comment": "x", "source": "quick"}],
                         ids=["nothing", "page", "date", "source"])
def test_only_allowed_fields_can_be_edited(monkeypatch, fields):
    db = _edit_db()
    with pytest.raises(ValueError):
        _harness(monkeypatch, db).service.update_progress_update_sync("u1", "alice", **fields)
    assert db.statements == []


def test_editing_someone_elses_update_finds_nothing(monkeypatch):
    db = _Db()
    assert _harness(monkeypatch, db).service.update_progress_update_sync("u1", "mallory", comment="x") is None
    assert not db.has("UPDATE")


def test_editing_the_comment_leaves_the_daily_row_alone(monkeypatch):
    db = _edit_db()
    result = _harness(monkeypatch, db).service.update_progress_update_sync("u1", "alice", comment="  better  ")
    assert db.params("UPDATE progress_updates") == ("better", "u1", "alice")
    assert not db.has("reading_logs")
    assert result["id"] == "u1"


def test_changing_minutes_adjusts_the_daily_row(monkeypatch):
    db = _edit_db(minutes=15)
    _harness(monkeypatch, db).service.update_progress_update_sync("u1", "alice", minutes_read=25)
    assert db.params("UPDATE reading_logs") == (40, 25, "log-1", "alice")
    assert db.params("UPDATE progress_updates") == (25, "u1", "alice")


def test_adding_minutes_with_no_daily_row_creates_one_on_the_original_date(monkeypatch):
    db = _edit_db(log_id=None, minutes=None, pages=0)
    harness = _harness(monkeypatch, db)

    harness.service.update_progress_update_sync("u1", "alice", minutes_read=20)

    assert (harness.merged[0]["date"], harness.merged[0]["pages_read"], harness.merged[0]["minutes_read"]) == (date(2026, 1, 14), 0, 20)
    assert db.params("UPDATE progress_updates") == (20, "log-1", "u1", "alice")


@pytest.mark.parametrize("pages, minutes", [(40, None), (0, 10), (40, 10)], ids=["pages", "minutes", "both"])
def test_adding_minutes_does_not_bring_back_a_daily_log_the_user_deleted(monkeypatch, pages, minutes):
    db = _edit_db(log_id=None, minutes=minutes, pages=pages)
    harness = _harness(monkeypatch, db)

    harness.service.update_progress_update_sync("u1", "alice", minutes_read=25)

    assert harness.merged == []
    minutes, extra, *_ids = db.params("UPDATE progress_updates")
    assert minutes == 25 and '"daily_log_deleted": true' in extra


def test_a_daily_log_known_to_be_deleted_stays_deleted_once_minutes_reach_zero(monkeypatch):
    db = _edit_db(log_id=None, minutes=0, pages=0, extra={"daily_log_deleted": True})
    harness = _harness(monkeypatch, db)

    harness.service.update_progress_update_sync("u1", "alice", minutes_read=15)

    assert harness.merged == []
    assert db.params("UPDATE progress_updates") == (15, "u1", "alice")


def test_clearing_minutes_subtracts_them(monkeypatch):
    db = _edit_db(minutes=15)
    _harness(monkeypatch, db).service.update_progress_update_sync("u1", "alice", minutes_read=None)
    assert db.params("UPDATE reading_logs") == (40, 0, "log-1", "alice")


def test_linking_a_journal_entry_checks_user_and_book(monkeypatch):
    db = _edit_db()
    with pytest.raises(LookupError):
        _harness(monkeypatch, db).service.update_progress_update_sync("u1", "alice", journal_entry_id="j-other")
    assert db.params(_JOURNAL_FOR_BOOK) == ("j-other", "alice", "b1")
    assert not db.has("UPDATE progress_updates")


def test_a_journal_link_can_be_cleared(monkeypatch):
    db = _edit_db()
    _harness(monkeypatch, db).service.update_progress_update_sync("u1", "alice", journal_entry_id=None)
    assert db.params("UPDATE progress_updates") == (None, "u1", "alice")
    assert not db.has("journal_entries")


@pytest.mark.parametrize("fields", [{"minutes_read": -1}, {"minutes_read": "5"}, {"comment": "a\x00b"}], ids=["negative", "text", "nul"])
def test_invalid_edits_are_rejected_before_any_sql(monkeypatch, fields):
    db = _edit_db()
    with pytest.raises(ValueError):
        _harness(monkeypatch, db).service.update_progress_update_sync("u1", "alice", **fields)
    assert db.statements == []


# --- Scoping and wiring ---------------------------------------------------------------

_BOOK_LOCK = "SELECT 1 FROM books WHERE id=%s FOR KEY SHARE"


@pytest.mark.parametrize(
    "make_db, call",
    [
        (_db, lambda s: _record(s, current_page=50)),
        (_delete_db, lambda s: s.delete_progress_update_sync("u1", "alice")),
        (_edit_db, lambda s: s.update_progress_update_sync("u1", "alice", minutes_read=25)),
    ],
    ids=["log", "delete", "edit"],
)
def test_writes_lock_the_book_right_after_the_advisory_lock(monkeypatch, make_db, call):
    db = make_db()

    call(_harness(monkeypatch, db).service)

    statements = [sql for sql, _params in db.statements]
    advisory = next(index for index, sql in enumerate(statements) if "pg_advisory_xact_lock" in sql)
    assert statements[advisory + 1] == _BOOK_LOCK
    assert db.params(_BOOK_LOCK) == ("b1",)


@pytest.mark.parametrize(
    "make_db, call",
    [
        (lambda: _db(more=[("INSERT INTO journal_entries", _JOURNAL_ROW)]),
         lambda s: s.log_progress_sync(user_id="alice", book_id="b1", current_page=50, occurred_at=WHEN, new_journal={"title": "T"})),
        (lambda: _db(more=[(_JOURNAL_FOR_BOOK, _JOURNAL_ROW)]),
         lambda s: s.log_progress_sync(user_id="alice", book_id="b1", current_page=50, occurred_at=WHEN, journal_entry_id="j1")),
        (_db, lambda s: s.get_book_progress_sync("alice", "b1")),
        (_db, lambda s: s.list_book_updates_sync("alice", "b1")),
        (_delete_db, lambda s: s.delete_progress_update_sync("u1", "alice")),
        (_edit_db, lambda s: s.update_progress_update_sync("u1", "alice", minutes_read=25)),
    ],
    ids=["log-new-journal", "log-linked-journal", "progress", "list", "delete", "edit"],
)
def test_every_statement_is_scoped_to_the_user(monkeypatch, make_db, call):
    db = make_db()

    call(_harness(monkeypatch, db).service)

    assert db.statements
    for sql, params in db.statements:
        if "pg_advisory_xact_lock" in sql:
            assert params == ("progress:alice:b1",)
        elif sql == _BOOK_LOCK:  # takes a lock and returns nothing to the caller
            assert params == ("b1",)
        else:
            assert "user_id" in sql and "alice" in params, sql


def test_facade_exposes_the_progress_service():
    db = _Db()
    facade = PostgresServiceFacade(store=db)
    assert isinstance(facade.progress_service, progress.PostgresProgressService)
    assert facade.progress_service.store is db


def test_getter_returns_none_on_kuzu():
    assert services.get_progress_service() is None


def test_getter_returns_the_facade_service_on_postgres(monkeypatch):
    marker = object()
    monkeypatch.setattr(services, "get_active_persistence_backend", lambda: "postgres")
    monkeypatch.setattr(services, "_get_postgres_facade", lambda: SimpleNamespace(progress_service=marker))
    assert services.get_progress_service() is marker
