"""PostgreSQL persistence for reading progress updates.

Each update moves one user's position in one book and feeds that day's reading log in a
single transaction, so a failure part-way cannot leave pages counted.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from ..domain.models import ReadingLog, ReadingStatus
from ..infrastructure import redis_cache
from ..infrastructure.postgres_store import PostgresStore, get_postgres_store
from .postgres_journal_service import _SELECT as _JOURNAL_SELECT
from .postgres_journal_service import _clean_body, _clean_title, _entry
from .postgres_reading_log_service import PostgresReadingLogService

MAX_COMMENT_CHARS = 2000

_MAX_INT = 2_147_483_647  # progress_updates page and minute columns are INTEGER
_CLOCK_SKEW = timedelta(minutes=5)
_EARLIEST_DATE = datetime(1900, 1, 1, tzinfo=timezone.utc)  # dates near year 1 can leave PostgreSQL's range once converted to UTC
_SOURCES = {"modal", "quick", "import", "backfill"}
_EDITABLE_FIELDS = {"comment", "minutes_read", "journal_entry_id"}
_NOT_STARTED = {None, "", ReadingStatus.PLAN_TO_READ.value, "want_to_read", ReadingStatus.LIBRARY_ONLY.value}
_UPDATE_COLUMNS = (
    "id", "user_id", "book_id", "occurred_at", "current_page", "position_percent", "pages_delta", "minutes_read",
    "comment", "journal_entry_id", "reading_log_id", "source", "finished", "created_at", "updated_at",
)
_UPDATE_SELECT = ", ".join(_UPDATE_COLUMNS)
_NEWEST_FIRST = "ORDER BY occurred_at DESC, created_at DESC, id DESC"


class ProgressConflictError(Exception):
    """A newer update depends on the one being changed."""


def _whole_number(value: Any, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= _MAX_INT:
        raise ValueError(f"{label} must be a whole number of at least {minimum}")
    return value


def _percent(value: Any) -> Decimal:
    try:
        percent = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Percent must be a number from 0 to 100") from None
    if not percent.is_finite() or not 0 <= percent <= 100:
        raise ValueError("Percent must be a number from 0 to 100")
    return percent.quantize(Decimal("0.01"))


def _moment(value: Any, label: str) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError(f"{label} must be a date and time with a UTC offset")
    if value > datetime.now(timezone.utc) + _CLOCK_SKEW:
        raise ValueError(f"{label} cannot be in the future")
    if value < _EARLIEST_DATE:
        raise ValueError(f"{label} cannot be before 1900")
    return value


def _clean_comment(comment: Any) -> str | None:
    if comment is None:
        return None
    if not isinstance(comment, str) or "\x00" in comment or len(comment) > MAX_COMMENT_CHARS:
        raise ValueError(f"Comment must be text of at most {MAX_COMMENT_CHARS} characters")
    return comment.strip() or None


def _clean_new_journal(new_journal: Any) -> tuple[str, str]:
    if not isinstance(new_journal, dict):
        raise ValueError("A new journal entry needs a title")
    return _clean_title(new_journal.get("title")), _clean_body(new_journal.get("body_html", ""))


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value.isoformat() if isinstance(value, (date, datetime)) else float(value) if isinstance(value, Decimal) else value
        for key, value in row.items()
    }


class PostgresProgressService:
    """Reading progress, with every statement scoped to the owning user."""

    def __init__(self, store: PostgresStore | None = None):
        self.store = store or get_postgres_store()
        self.reading_logs = PostgresReadingLogService(self.store)

    def _invalidate(self, user_id: str) -> None:
        redis_cache.invalidate_tags(
            self.reading_logs._user_tag(user_id), f"postgres:user_books:{user_id}", "postgres:reading_logs",
            f"postgres:user_library:{user_id}", f"postgres:progress:{user_id}",
        )

    @staticmethod
    def _lock(cursor: Any, user_id: str, book_id: str) -> None:
        cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"progress:{user_id}:{book_id}",))
        # Queues this write behind or ahead of removing the book, which locks the row FOR UPDATE. Its own statement,
        # so the library read that follows gets a fresh snapshot and sees a removal this waited for.
        cursor.execute("SELECT 1 FROM books WHERE id=%s FOR KEY SHARE", (book_id,))

    @staticmethod
    def _library_book(cursor: Any, user_id: str, book_id: str) -> dict[str, Any]:
        # No row lock here: the merge locks reading_logs then user_books, and taking user_books
        # first would deadlock against the older reading-log forms. The advisory lock orders progress writers.
        cursor.execute(
            "SELECT ub.current_page, ub.total_pages, b.page_count, ub.reading_status, ub.finish_date "
            "FROM user_books ub JOIN books b ON b.id=ub.book_id WHERE ub.user_id=%s AND ub.book_id=%s",
            (user_id, book_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise LookupError("Book is not in your library")
        return dict(zip(("current_page", "total_pages", "page_count", "reading_status", "finish_date"), row))

    @staticmethod
    def _journal_for_book(cursor: Any, entry_id: str, user_id: str, book_id: str) -> dict[str, Any]:
        cursor.execute(
            f"SELECT {_JOURNAL_SELECT} FROM journal_entries WHERE id=%s AND user_id=%s AND book_id=%s",
            (entry_id, user_id, book_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise LookupError("Journal entry not found for this book")
        return _entry(row)

    def _merge_day(self, cursor: Any, user_id: str, book_id: str, day: date, pages: int, minutes: int) -> dict[str, Any] | None:
        # Numbers only: a comment copied into the day's notes could not follow later edits or deletes (D-020).
        log = ReadingLog(user_id=user_id, book_id=book_id, date=day, pages_read=pages, minutes_read=minutes)
        return self.reading_logs._merge_daily_log(cursor, self.reading_logs._log_payload(log))

    @staticmethod
    def _adjust_daily_log(cursor: Any, log_id: str, user_id: str, pages: int, minutes: int) -> None:
        """Apply a change to a daily row, and drop the row if nothing is left in it."""
        cursor.execute("SELECT pages_read, minutes_read, notes FROM reading_logs WHERE id=%s AND user_id=%s FOR UPDATE", (log_id, user_id))
        row = cursor.fetchone()
        if row is None:
            return
        new_pages, new_minutes = max(int(row[0] or 0) + pages, 0), max(int(row[1] or 0) + minutes, 0)
        if new_pages == 0 and new_minutes == 0 and not row[2]:
            cursor.execute("DELETE FROM reading_logs WHERE id=%s AND user_id=%s", (log_id, user_id))
        else:
            cursor.execute(
                "UPDATE reading_logs SET pages_read=%s, minutes_read=%s, updated_at=now() WHERE id=%s AND user_id=%s",
                (new_pages, new_minutes, log_id, user_id),
            )

    def _book_progress(self, cursor: Any, user_id: str, book_id: str) -> dict[str, Any]:
        book = self._library_book(cursor, user_id, book_id)
        cursor.execute(
            f"SELECT {_UPDATE_SELECT} FROM progress_updates WHERE user_id=%s AND book_id=%s {_NEWEST_FIRST} LIMIT 1",
            (user_id, book_id),
        )
        row = cursor.fetchone()
        last_update = _jsonable(dict(zip(_UPDATE_COLUMNS, row))) if row else None
        page, total = book["current_page"], book["total_pages"] or book["page_count"] or None
        if last_update and last_update["position_percent"] is not None:
            percent = last_update["position_percent"]
        elif page is not None and total:
            percent = min(round(page / total * 100, 1), 100.0)
        else:
            percent = None
        return {"current_page": page, "total_pages": total, "percent": percent,
                "status": book["reading_status"], "last_update": last_update}

    def log_progress_sync(self, *, user_id: str, book_id: str, current_page: Any = None, position_percent: Any = None,
                          occurred_at: Any = None, minutes_read: Any = None, comment: Any = None,
                          journal_entry_id: str | None = None, new_journal: Any = None, finish_date: Any = None,
                          total_pages: Any = None, source: str = "modal") -> dict[str, Any]:
        if (current_page is None) == (position_percent is None):
            raise ValueError("Send exactly one of current_page or position_percent")
        if journal_entry_id is not None and new_journal is not None:
            raise ValueError("Link an existing journal entry or create a new one, not both")
        if source not in _SOURCES:
            raise ValueError("Unknown progress source")
        page = None if current_page is None else _whole_number(current_page, "Current page", 0)
        percent = None if position_percent is None else _percent(position_percent)
        new_total = None if total_pages is None else _whole_number(total_pages, "Total pages", 1)
        minutes = None if minutes_read is None else _whole_number(minutes_read, "Minutes read", 0)
        moment = datetime.now(timezone.utc) if occurred_at is None else _moment(occurred_at, "Date read")
        explicit_finish = None if finish_date is None else _moment(finish_date, "Finish date")
        note = _clean_comment(comment)
        journal_fields = None if new_journal is None else _clean_new_journal(new_journal)

        with self.store.transaction() as connection, connection.cursor() as cursor:
            self._lock(cursor, user_id, book_id)
            book = self._library_book(cursor, user_id, book_id)
            total = new_total or book["total_pages"] or book["page_count"] or None
            if page is not None and total is not None and page > total:
                raise ValueError(f"Current page cannot be more than {total}")

            journal = None
            if journal_entry_id is not None:
                journal = self._journal_for_book(cursor, journal_entry_id, user_id, book_id)
            elif journal_fields is not None:
                cursor.execute(
                    f"INSERT INTO journal_entries (id, user_id, book_id, title, body_html) VALUES (%s, %s, %s, %s, %s) RETURNING {_JOURNAL_SELECT}",
                    (str(uuid.uuid4()), user_id, book_id, *journal_fields),
                )
                journal = _entry(cursor.fetchone())

            # clock_timestamp, not now(): now() is when this transaction began, before it waited for the lock.
            cursor.execute("SELECT max(occurred_at), clock_timestamp() FROM progress_updates WHERE user_id=%s AND book_id=%s", (user_id, book_id))
            newest, locked_at = cursor.fetchone()
            backdated = newest is not None and moment < newest
            pages_delta = max(page - (book["current_page"] or 0), 0) if page is not None and not backdated else 0

            # The sender's UTC offset decides which day gets the pages.
            daily = self._merge_day(cursor, user_id, book_id, moment.date(), pages_delta, minutes or 0) if pages_delta > 0 or minutes else None

            reached_end = percent == 100 or (page is not None and page == total)
            finished = not backdated and (reached_end or (page is not None and total is None and explicit_finish is not None))

            extra: dict[str, Any] = {"log_date": moment.date().isoformat()}
            if not backdated:
                # A backdated update never moved the book, so deleting it must not move the book either.
                finish_before = book["finish_date"]
                extra.update(previous_page=book["current_page"], previous_status=book["reading_status"],
                             previous_finish_date=finish_before.isoformat() if finish_before else None,
                             previous_total_pages=book["total_pages"])
            cursor.execute(
                "INSERT INTO progress_updates (id, user_id, book_id, occurred_at, current_page, position_percent, pages_delta, "
                "minutes_read, comment, journal_entry_id, reading_log_id, source, finished, extra, created_at, updated_at) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING {_UPDATE_SELECT}",
                (str(uuid.uuid4()), user_id, book_id, moment, page, percent, pages_delta, minutes, note,
                 journal["id"] if journal else None, daily["id"] if daily else None, source, finished, json.dumps(extra),
                 locked_at, locked_at),
            )
            update = dict(zip(_UPDATE_COLUMNS, cursor.fetchone()))

            if not backdated:
                status, finish_value = book["reading_status"], book["finish_date"]
                if finished:
                    # Repeating the last page must not move the date the book was finished.
                    already_read = status == ReadingStatus.READ.value and finish_value is not None
                    finish_value = explicit_finish or (finish_value if already_read else moment)
                    status = ReadingStatus.READ.value
                elif status in _NOT_STARTED:
                    status = ReadingStatus.READING.value
                # Only a total the reader gave is stored: the catalog's count can still be corrected later (D-022).
                cursor.execute(
                    "UPDATE user_books SET current_page=%s, total_pages=%s, reading_status=%s, start_date=COALESCE(start_date, %s), "
                    "finish_date=%s, updated_at=now() WHERE user_id=%s AND book_id=%s",
                    (book["current_page"] if page is None else page, new_total or book["total_pages"], status, moment, finish_value, user_id, book_id),
                )
            state = self._book_progress(cursor, user_id, book_id)

        self._invalidate(user_id)
        return {
            "update": _jsonable(update),
            "progress": {key: state[key] for key in ("current_page", "total_pages", "percent")},
            "status": state["status"],
            "finished": finished,
            "journal_entry": journal,
            "reading_log": _jsonable(daily) if daily else None,
        }

    def get_book_progress_sync(self, user_id: str, book_id: str) -> dict[str, Any]:
        with self.store.connection() as connection, connection.cursor() as cursor:
            return self._book_progress(cursor, user_id, book_id)

    def list_book_updates_sync(self, user_id: str, book_id: str, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        with self.store.connection() as connection, connection.cursor() as cursor:
            self._library_book(cursor, user_id, book_id)
            cursor.execute(
                f"SELECT {_UPDATE_SELECT} FROM progress_updates WHERE user_id=%s AND book_id=%s {_NEWEST_FIRST} LIMIT %s",
                (user_id, book_id, limit),
            )
            rows = cursor.fetchall()
        return [_jsonable(dict(zip(_UPDATE_COLUMNS, row))) for row in rows]

    def _lock_update(self, cursor: Any, update_id: str, user_id: str) -> dict[str, Any] | None:
        """Take the book's progress lock for an update, then read the update under that lock."""
        cursor.execute("SELECT book_id FROM progress_updates WHERE id=%s AND user_id=%s", (update_id, user_id))
        row = cursor.fetchone()
        if row is None:
            return None
        self._lock(cursor, user_id, row[0])
        cursor.execute(
            "SELECT book_id, pages_delta, minutes_read, reading_log_id, occurred_at, extra FROM progress_updates WHERE id=%s AND user_id=%s",
            (update_id, user_id),
        )
        row = cursor.fetchone()
        return dict(zip(("book_id", "pages_delta", "minutes_read", "reading_log_id", "occurred_at", "extra"), row)) if row else None

    def update_progress_update_sync(self, update_id: str, user_id: str, **fields: Any) -> dict[str, Any] | None:
        """Change an update's comment, minutes or journal link; pages and dates need delete and re-enter (D-011)."""
        if not fields or set(fields) - _EDITABLE_FIELDS:
            raise ValueError("Only comment, minutes_read and journal_entry_id can be changed")
        changes = dict(fields)
        if "comment" in changes:
            changes["comment"] = _clean_comment(changes["comment"])
        if changes.get("minutes_read") is not None:
            changes["minutes_read"] = _whole_number(changes["minutes_read"], "Minutes read", 0)

        with self.store.transaction() as connection, connection.cursor() as cursor:
            current = self._lock_update(cursor, update_id, user_id)
            if current is None:
                return None
            if changes.get("journal_entry_id") is not None:
                self._journal_for_book(cursor, changes["journal_entry_id"], user_id, current["book_id"])
            if "minutes_read" in changes:
                added = (changes["minutes_read"] or 0) - (current["minutes_read"] or 0)
                extra = current["extra"]
                if current["reading_log_id"]:
                    if added:
                        self._adjust_daily_log(cursor, current["reading_log_id"], user_id, 0, added)
                elif added > 0 and not extra.get("daily_log_deleted"):
                    # Deleting a reading log marks its updates, so a row removed for being empty is all that comes back.
                    day = date.fromisoformat(extra["log_date"]) if "log_date" in extra else current["occurred_at"].date()
                    changes["reading_log_id"] = self._merge_day(cursor, user_id, current["book_id"], day, 0, added)["id"]
            # Column names come from _EDITABLE_FIELDS and reading_log_id above, never from the caller.
            assignments = ", ".join(f"{name}=%s" for name in changes)
            cursor.execute(
                f"UPDATE progress_updates SET {assignments}, updated_at=now() WHERE id=%s AND user_id=%s RETURNING {_UPDATE_SELECT}",
                (*changes.values(), update_id, user_id),
            )
            updated = dict(zip(_UPDATE_COLUMNS, cursor.fetchone()))
        self._invalidate(user_id)
        return _jsonable(updated)

    def delete_progress_update_sync(self, update_id: str, user_id: str) -> dict[str, Any] | None:
        """Delete a book's newest update and undo it. Returns the book's progress, or None if not this user's update."""
        with self.store.transaction() as connection, connection.cursor() as cursor:
            current = self._lock_update(cursor, update_id, user_id)
            if current is None:
                return None
            book_id = current["book_id"]
            cursor.execute(f"SELECT id FROM progress_updates WHERE user_id=%s AND book_id=%s {_NEWEST_FIRST} LIMIT 1", (user_id, book_id))
            if cursor.fetchone()[0] != update_id:
                raise ProgressConflictError("Only the latest update can be deleted")
            # Daily row first, the order deleting a reading log takes them: that locks its row, then its foreign key updates this one.
            if current["reading_log_id"]:
                self._adjust_daily_log(cursor, current["reading_log_id"], user_id, -current["pages_delta"], -(current["minutes_read"] or 0))
            cursor.execute("DELETE FROM progress_updates WHERE id=%s AND user_id=%s", (update_id, user_id))
            extra = current["extra"]
            if "previous_page" in extra:
                cursor.execute(
                    "UPDATE user_books SET current_page=%s, reading_status=%s, finish_date=%s, total_pages=%s, updated_at=now() WHERE user_id=%s AND book_id=%s",
                    (extra["previous_page"], extra["previous_status"], extra["previous_finish_date"], extra["previous_total_pages"], user_id, book_id),
                )
            state = self._book_progress(cursor, user_id, book_id)
        self._invalidate(user_id)
        return state
