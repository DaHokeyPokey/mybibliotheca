"""PostgreSQL persistence for each user's journal entries."""

from __future__ import annotations

import uuid
from typing import Any

import nh3

from ..infrastructure.postgres_store import PostgresStore, get_postgres_store

MAX_TITLE_CHARS = 200
MAX_BODY_BYTES = 100 * 1024

_COLUMNS = ("id", "user_id", "book_id", "title", "body_html", "is_private", "created_at", "updated_at")
_SELECT = ", ".join(_COLUMNS)
_ALLOWED_TAGS = {
    "p", "br", "strong", "em", "u", "s", "h1", "h2", "h3", "h4",
    "ul", "ol", "li", "blockquote", "a", "code", "pre", "img",
}
_ALLOWED_ATTRIBUTES = {"a": {"href"}, "img": {"src"}}


def _only_absolute_http(_tag: str, attribute: str, value: str) -> str | None:
    # url_schemes does not cover relative URLs, which would request this app's own routes.
    if attribute in ("href", "src") and not value.lower().startswith(("http://", "https://")):
        return None
    return value


def sanitize_journal_html(html: str) -> str:
    """Reduce editor HTML to the allowed tags, with only http(s) links and images."""
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        attribute_filter=_only_absolute_http,
        url_schemes={"http", "https"},
        link_rel="noopener noreferrer",
    )


def _clean_title(title: Any) -> str:
    cleaned = title.strip() if isinstance(title, str) else ""
    if not 1 <= len(cleaned) <= MAX_TITLE_CHARS or "\x00" in cleaned:
        raise ValueError(f"Title must be 1 to {MAX_TITLE_CHARS} characters of text")
    return cleaned


def _clean_body(body_html: Any) -> str:
    if not isinstance(body_html, str):
        raise ValueError("Journal body must be text")
    cleaned = sanitize_journal_html(body_html)
    if len(cleaned.encode("utf-8")) > MAX_BODY_BYTES:
        raise ValueError(f"Journal body must be at most {MAX_BODY_BYTES // 1024} KB")
    return cleaned


def _entry(row: Any) -> dict[str, Any]:
    entry = dict(zip(_COLUMNS, row))
    entry["created_at"] = entry["created_at"].isoformat()
    entry["updated_at"] = entry["updated_at"].isoformat()
    return entry


class PostgresJournalService:
    """Journal entries, with every statement scoped to the owning user."""

    def __init__(self, store: PostgresStore | None = None):
        self.store = store or get_postgres_store()

    def create_entry_sync(self, user_id: str, title: Any, body_html: Any = "", book_id: str | None = None) -> dict[str, Any]:
        title, body_html = _clean_title(title), _clean_body(body_html)
        with self.store.transaction() as connection, connection.cursor() as cursor:
            if book_id is not None:
                cursor.execute("SELECT 1 FROM user_books WHERE user_id=%s AND book_id=%s", (user_id, book_id))
                if cursor.fetchone() is None:
                    raise ValueError("Book is not in your library")
            cursor.execute(
                f"INSERT INTO journal_entries (id, user_id, book_id, title, body_html) VALUES (%s, %s, %s, %s, %s) RETURNING {_SELECT}",
                (str(uuid.uuid4()), user_id, book_id, title, body_html),
            )
            return _entry(cursor.fetchone())

    def get_entry_sync(self, entry_id: str, user_id: str) -> dict[str, Any] | None:
        with self.store.connection() as connection, connection.cursor() as cursor:
            cursor.execute(f"SELECT {_SELECT} FROM journal_entries WHERE id=%s AND user_id=%s", (entry_id, user_id))
            row = cursor.fetchone()
        return _entry(row) if row else None

    def list_entries_for_book_sync(self, book_id: str, user_id: str) -> list[dict[str, Any]]:
        with self.store.connection() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT j.id, j.title, j.updated_at, count(p.id) FROM journal_entries j "
                "LEFT JOIN progress_updates p ON p.journal_entry_id=j.id AND p.user_id=j.user_id "
                "WHERE j.user_id=%s AND j.book_id=%s GROUP BY j.id ORDER BY j.updated_at DESC, j.id",
                (user_id, book_id),
            )
            rows = cursor.fetchall()
        return [
            {"id": entry_id, "title": title, "updated_at": updated_at.isoformat(), "linked_update_count": int(linked)}
            for entry_id, title, updated_at, linked in rows
        ]

    def list_entries_for_user_sync(self, user_id: str, book_id: str | None = None, page: int = 1,
                                   per_page: int = 20) -> dict[str, Any]:
        page, per_page = max(1, int(page)), max(1, min(int(per_page), 100))
        where = "j.user_id=%s" + (" AND j.book_id=%s" if book_id else "")
        params: list[Any] = [user_id] + ([book_id] if book_id else [])
        with self.store.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM journal_entries j WHERE " + where, tuple(params))
            total = int(cursor.fetchone()[0])
            cursor.execute(
                f"SELECT {', '.join('j.' + column for column in _COLUMNS)}, b.title FROM journal_entries j "
                "LEFT JOIN books b ON b.id=j.book_id WHERE " + where +
                " ORDER BY j.updated_at DESC, j.id OFFSET %s LIMIT %s",
                tuple(params + [(page - 1) * per_page, per_page]),
            )
            rows = cursor.fetchall()
        pages = (total + per_page - 1) // per_page if total else 1
        return {
            "entries": [{**_entry(row[:-1]), "book_title": row[-1]} for row in rows],
            "pagination": {"page": page, "per_page": per_page, "total_count": total,
                           "total_pages": pages, "has_prev": page > 1, "has_next": page < pages},
        }

    def update_entry_sync(self, entry_id: str, user_id: str, title: Any = None, body_html: Any = None) -> dict[str, Any] | None:
        """Change the given fields; a field left as None keeps its value."""
        title = None if title is None else _clean_title(title)
        body_html = None if body_html is None else _clean_body(body_html)
        with self.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE journal_entries SET title=COALESCE(%s, title), body_html=COALESCE(%s, body_html), updated_at=now() "
                f"WHERE id=%s AND user_id=%s RETURNING {_SELECT}",
                (title, body_html, entry_id, user_id),
            )
            row = cursor.fetchone()
        return _entry(row) if row else None

    def delete_entry_sync(self, entry_id: str, user_id: str) -> bool:
        with self.store.transaction() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM journal_entries WHERE id=%s AND user_id=%s", (entry_id, user_id))
            return cursor.rowcount == 1
