"""Opt-in checks of the progress and journal SQL against a disposable PostgreSQL database.

Set POSTGRES_TEST_DSN to a disposable database. The special value "compose" uses the
POSTGRES_* variables from the local Compose service. These tests commit rows, so never
point them at a real application database.
"""

import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend_stub import kuzu_backend  # noqa: E402

with kuzu_backend():
    from app.infrastructure import postgres_store
    from app.services import postgres_journal_service as journal_module
    from app.services import postgres_progress_service as progress
    from app.services.postgres_book_service import PostgresBookService

WHEN = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def store():
    dsn = os.getenv("POSTGRES_TEST_DSN")
    if not dsn:
        pytest.skip("Set POSTGRES_TEST_DSN to a disposable PostgreSQL database")
    instance = postgres_store.PostgresStore() if dsn == "compose" else postgres_store.PostgresStore(dsn)
    instance.initialize_schema()
    return instance


def _run(store, sql, params=()):
    with store.transaction() as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)


def _rows(store, sql, params=()):
    with store.connection() as connection, connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _add_user(store, name):
    user = f"{name}-{uuid.uuid4().hex[:12]}"
    _run(store, "INSERT INTO users (id, username, email, password_hash) VALUES (%s, %s, %s, 'x')",
         (user, user, f"{user}@example.test"))
    return user


def _add_book(store, owner=None, status="plan_to_read", total_pages=300):
    book = f"book-{uuid.uuid4().hex[:12]}"
    _run(store, "INSERT INTO books (id, title) VALUES (%s, %s)", (book, f"Title {book}"))
    if owner:
        _run(store, "INSERT INTO user_books (user_id, book_id, reading_status, total_pages) VALUES (%s, %s, %s, %s)",
             (owner, book, status, total_pages))
    return book


@pytest.fixture
def world(store, monkeypatch):
    monkeypatch.setattr(progress.redis_cache, "invalidate_tags", lambda *tags: True)
    user = _add_user(store, "reader")
    book = _add_book(store, owner=user)
    service = progress.PostgresProgressService(store)

    def record(**kwargs):
        kwargs.setdefault("occurred_at", WHEN)
        return service.log_progress_sync(user_id=user, book_id=book, **kwargs)

    return SimpleNamespace(store=store, user=user, book=book, service=service, record=record,
                           journal=journal_module.PostgresJournalService(store))


def _daily(world):
    return _rows(world.store, "SELECT date, pages_read, minutes_read, notes FROM reading_logs "
                              "WHERE user_id=%s AND book_id=%s ORDER BY date", (world.user, world.book))


def _library(world):
    return _rows(world.store, "SELECT current_page, reading_status, start_date, finish_date FROM user_books "
                              "WHERE user_id=%s AND book_id=%s", (world.user, world.book))[0]


def _update_count(world):
    return _rows(world.store, "SELECT count(*) FROM progress_updates WHERE user_id=%s AND book_id=%s", (world.user, world.book))[0][0]


def test_first_update_creates_the_daily_row_and_starts_the_book(world):
    result = world.record(current_page=40, minutes_read=15, comment="good start")

    assert _daily(world) == [(date(2026, 1, 15), 40, 15, "good start")]
    page, status, start_date, finish_date = _library(world)
    assert (page, status, finish_date) == (40, "reading", None)
    assert start_date is not None
    assert result["update"]["pages_delta"] == 40
    assert result["progress"] == {"current_page": 40, "total_pages": 300, "percent": 13.3}
    assert result["status"] == "reading" and result["finished"] is False
    assert result["reading_log"]["date"] == "2026-01-15"


def test_two_updates_on_one_day_merge_into_one_daily_row(world):
    world.record(current_page=40, comment="first")
    world.record(current_page=70, minutes_read=5, comment="second", occurred_at=WHEN + timedelta(hours=1))

    assert _daily(world) == [(date(2026, 1, 15), 70, 5, "first\n\nsecond")]


def test_reaching_the_last_page_finishes_the_book(world):
    result = world.record(current_page=300)

    _page, status, _start, finish_date = _library(world)
    assert status == "read" and finish_date == WHEN
    assert result["finished"] is True and result["status"] == "read" and result["progress"]["percent"] == 100.0


def test_going_backwards_counts_nothing(world):
    world.record(current_page=100)
    result = world.record(current_page=60, occurred_at=WHEN + timedelta(hours=1))

    assert result["update"]["pages_delta"] == 0
    assert _library(world)[0] == 60
    assert _daily(world) == [(date(2026, 1, 15), 100, 0, None)]


def test_the_senders_local_date_picks_the_daily_row(world):
    world.record(current_page=40, occurred_at=datetime(2026, 1, 14, 23, 30, tzinfo=timezone(timedelta(hours=-5))))
    assert _daily(world)[0][0] == date(2026, 1, 14)


def test_a_percent_update_finishes_at_100_without_counting_pages(world):
    first = world.record(position_percent=42.5)
    assert first["progress"]["percent"] == 42.5
    assert first["update"]["pages_delta"] == 0 and _daily(world) == []

    second = world.record(position_percent=100, occurred_at=WHEN + timedelta(hours=1))
    assert second["finished"] is True and _library(world)[1] == "read"
    assert _rows(world.store, "SELECT position_percent FROM progress_updates WHERE id=%s", (second["update"]["id"],)) == [(Decimal("100.00"),)]


def test_a_backdated_update_counts_minutes_but_does_not_move_the_book(world):
    world.record(current_page=100)
    result = world.record(current_page=20, minutes_read=30, occurred_at=WHEN - timedelta(days=2))

    assert result["update"]["pages_delta"] == 0
    assert _library(world)[0] == 100
    assert _daily(world) == [(date(2026, 1, 13), 0, 30, None), (date(2026, 1, 15), 100, 0, None)]


def test_a_failure_after_the_daily_merge_leaves_no_trace(world, monkeypatch):
    real_merge = world.service.reading_logs._merge_daily_log

    def merge_then_fail(cursor, payload):
        real_merge(cursor, payload)
        raise RuntimeError("failed after the merge")

    monkeypatch.setattr(world.service.reading_logs, "_merge_daily_log", merge_then_fail)
    with pytest.raises(RuntimeError):
        world.record(current_page=40, new_journal={"title": "Rolled back"})

    assert _daily(world) == []
    assert _update_count(world) == 0
    assert _library(world)[:2] == (None, "plan_to_read")
    assert _rows(world.store, "SELECT count(*) FROM journal_entries WHERE user_id=%s", (world.user,)) == [(0,)]


def test_deleting_the_latest_update_restores_everything(world):
    world.record(current_page=100, minutes_read=10)
    latest = world.record(current_page=300, minutes_read=5, occurred_at=WHEN + timedelta(hours=1))
    assert _library(world)[1] == "read"

    state = world.service.delete_progress_update_sync(latest["update"]["id"], world.user)

    assert state["current_page"] == 100 and state["status"] == "reading"
    page, status, _start, finish_date = _library(world)
    assert (page, status, finish_date) == (100, "reading", None)
    assert _daily(world) == [(date(2026, 1, 15), 100, 10, None)]


def test_deleting_the_only_update_removes_the_daily_row_and_restores_the_status(world):
    only = world.record(current_page=40)

    world.service.delete_progress_update_sync(only["update"]["id"], world.user)

    assert _daily(world) == []
    assert _update_count(world) == 0
    assert _library(world)[:2] == (None, "plan_to_read")


def test_only_the_latest_update_can_be_deleted(world):
    first = world.record(current_page=100)
    world.record(current_page=120, occurred_at=WHEN + timedelta(hours=1))

    with pytest.raises(progress.ProgressConflictError):
        world.service.delete_progress_update_sync(first["update"]["id"], world.user)
    assert _update_count(world) == 2


def test_deleting_through_a_backdated_update_does_not_restore_its_stale_position(world):
    newest = world.record(current_page=100)
    backdated = world.record(current_page=20, occurred_at=WHEN - timedelta(days=2))

    world.service.delete_progress_update_sync(newest["update"]["id"], world.user)
    assert _library(world)[0] is None

    world.service.delete_progress_update_sync(backdated["update"]["id"], world.user)
    assert _library(world)[0] is None


def test_changing_minutes_adjusts_the_daily_row(world):
    update = world.record(current_page=40, minutes_read=10)

    edited = world.service.update_progress_update_sync(update["update"]["id"], world.user, minutes_read=25, comment="edited")

    assert edited["minutes_read"] == 25 and edited["comment"] == "edited"
    assert _daily(world) == [(date(2026, 1, 15), 40, 25, None)]


def test_adding_minutes_later_creates_the_daily_row_on_the_original_day(world):
    update = world.record(position_percent=10, occurred_at=datetime(2026, 1, 14, 23, 30, tzinfo=timezone(timedelta(hours=-5))))
    assert _daily(world) == []

    edited = world.service.update_progress_update_sync(update["update"]["id"], world.user, minutes_read=20)

    assert _daily(world) == [(date(2026, 1, 14), 0, 20, None)]
    assert edited["reading_log_id"] is not None


def test_removing_all_minutes_drops_an_otherwise_empty_daily_row(world):
    update = world.record(position_percent=10, minutes_read=20)

    edited = world.service.update_progress_update_sync(update["update"]["id"], world.user, minutes_read=None)

    assert _daily(world) == []
    assert edited["reading_log_id"] is None


def test_concurrent_updates_do_not_double_count(world):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _attempt: world.record(current_page=50), range(4)))

    assert _update_count(world) == 4
    assert _daily(world)[0][1] == 50  # without the lock, each writer would count the same 50 pages


def _pause_after(monkeypatch, owner, name):
    """Hold the first call to owner.name just after it returns, until the test lets it go."""
    reached, resume, real = threading.Event(), threading.Event(), getattr(owner, name)

    def paused(*args):
        result = real(*args)
        reached.set()
        assert resume.wait(20)
        return result

    monkeypatch.setattr(owner, name, paused)
    return reached, resume


def _remove_during_save(world, reached, resume, **save):
    """Run a removal against a paused save. Returns whether the removal had to wait for it."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(world.record, **save)
        assert reached.wait(20)
        removing = pool.submit(PostgresBookService(world.store).delete_book_for_user_sync, world.book, world.user)
        time.sleep(1)  # time for the removal to reach the lock
        waited = not removing.done()
        resume.set()
        saving.result(timeout=30)
        assert removing.result(timeout=30) is True
    return waited


def _is_gone_for_the_reader(world):
    held = _rows(world.store, "SELECT count(*) FROM user_books WHERE user_id=%s AND book_id=%s", (world.user, world.book))
    return held == [(0,)] and _update_count(world) == 0


def test_a_removal_waits_for_a_save_that_has_read_the_library(world, monkeypatch):
    reached, resume = _pause_after(monkeypatch, world.service, "_library_book")

    assert _remove_during_save(world, reached, resume, current_page=40) is True

    assert _is_gone_for_the_reader(world)


def test_a_removal_does_not_deadlock_with_a_days_second_update(world, monkeypatch):
    world.record(current_page=10)
    reached, resume = _pause_after(monkeypatch, world.service.reading_logs, "_merge_daily_log")

    assert _remove_during_save(world, reached, resume, current_page=20, occurred_at=WHEN + timedelta(minutes=1)) is True

    assert _is_gone_for_the_reader(world)


def test_a_save_that_waits_for_a_removal_finds_the_book_gone(world):
    with ThreadPoolExecutor(max_workers=1) as pool:
        with world.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM books WHERE id=%s FOR UPDATE", (world.book,))
            cursor.execute("DELETE FROM user_books WHERE user_id=%s AND book_id=%s", (world.user, world.book))
            saving = pool.submit(world.record, current_page=40)
            time.sleep(1)  # time for the save to reach the lock
            assert not saving.done()
        with pytest.raises(LookupError):
            saving.result(timeout=30)

    assert _is_gone_for_the_reader(world)


def test_deleting_an_update_restores_the_earlier_finish_date(world):
    first_finish = WHEN - timedelta(days=30)
    world.record(current_page=300, occurred_at=first_finish)
    latest = world.record(current_page=300, finish_date=WHEN, occurred_at=WHEN)
    assert _library(world)[3] == WHEN

    world.service.delete_progress_update_sync(latest["update"]["id"], world.user)

    _page, status, _start, finish_date = _library(world)
    assert (status, finish_date) == ("read", first_finish)


def test_a_new_journal_entry_is_linked_and_sanitized(world):
    result = world.record(current_page=40, new_journal={"title": "Notes", "body_html": "<p>kept</p><script>alert(1)</script>"})

    stored = world.journal.get_entry_sync(result["journal_entry"]["id"], world.user)
    assert stored["body_html"] == "<p>kept</p>"
    assert result["update"]["journal_entry_id"] == stored["id"]


def test_another_users_book_and_journal_are_not_found(world):
    other = _add_user(world.store, "other")
    other_book = _add_book(world.store, owner=other)
    other_entry = world.journal.create_entry_sync(other, "Theirs", book_id=other_book)

    with pytest.raises(LookupError):
        world.service.log_progress_sync(user_id=world.user, book_id=other_book, current_page=1, occurred_at=WHEN)
    with pytest.raises(LookupError):
        world.record(current_page=1, journal_entry_id=other_entry["id"])
    with pytest.raises(LookupError):
        world.service.get_book_progress_sync(world.user, other_book)


def test_another_user_cannot_change_or_delete_an_update(world):
    other = _add_user(world.store, "other")
    update = world.record(current_page=40)

    assert world.service.update_progress_update_sync(update["update"]["id"], other, comment="no") is None
    assert not world.service.delete_progress_update_sync(update["update"]["id"], other)
    assert _update_count(world) == 1


def test_journal_entries_round_trip_on_real_postgres(world):
    entry = world.journal.create_entry_sync(world.user, "  Notes  ", "<p>one</p>", book_id=world.book)
    general = world.journal.create_entry_sync(world.user, "General")
    world.record(current_page=40, journal_entry_id=entry["id"])

    assert entry["title"] == "Notes" and general["book_id"] is None
    assert world.journal.get_entry_sync(entry["id"], world.user)["body_html"] == "<p>one</p>"

    listed = world.journal.list_entries_for_book_sync(world.book, world.user)
    assert [(e["id"], e["linked_update_count"]) for e in listed] == [(entry["id"], 1)]

    page = world.journal.list_entries_for_user_sync(world.user, per_page=1)
    assert page["pagination"]["total_count"] == 2 and page["pagination"]["has_next"] is True
    filtered = world.journal.list_entries_for_user_sync(world.user, book_id=world.book)
    assert [e["book_title"] for e in filtered["entries"]] == [f"Title {world.book}"]

    only_title = world.journal.update_entry_sync(entry["id"], world.user, title="Renamed")
    assert (only_title["title"], only_title["body_html"]) == ("Renamed", "<p>one</p>")
    only_body = world.journal.update_entry_sync(entry["id"], world.user, body_html="<p>two</p>")
    assert (only_body["title"], only_body["body_html"]) == ("Renamed", "<p>two</p>")

    other = _add_user(world.store, "other")
    assert world.journal.get_entry_sync(entry["id"], other) is None
    assert world.journal.update_entry_sync(entry["id"], other, title="No") is None
    assert world.journal.delete_entry_sync(entry["id"], other) is False

    assert world.journal.delete_entry_sync(entry["id"], world.user) is True
    assert world.journal.get_entry_sync(entry["id"], world.user) is None
    assert _rows(world.store, "SELECT journal_entry_id FROM progress_updates WHERE user_id=%s", (world.user,)) == [(None,)]
