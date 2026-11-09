"""Journal service: HTML sanitizing, input limits, per-user scoping, and the getter."""

import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend_stub import kuzu_backend, kuzu_backend_for_module  # noqa: E402,F401 - autouse fixture

with kuzu_backend():
    import app.services as services
    from app.services import postgres_journal_service as journal
    from app.services.postgres_service_facade import PostgresServiceFacade

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
ROW = ("e1", "alice", "b1", "Title", "<p>x</p>", True, NOW, NOW)
BOOK_LOCK = "SELECT 1 FROM books WHERE id=%s FOR KEY SHARE"


class _Db:
    """Fake store: records every statement and replays scripted fetch results in order."""

    def __init__(self, *results, rowcount=1):
        self.results = list(results)
        self.statements = []
        self.rowcount = rowcount

    @contextmanager
    def connection(self):
        yield self

    transaction = connection

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), tuple(params)))

    def fetchone(self):
        return self.results.pop(0)

    def fetchall(self):
        return self.results.pop(0)


def _service(db):
    return journal.PostgresJournalService(store=db)


# --- Sanitizer ---------------------------------------------------------------------

@pytest.mark.parametrize("tag", ["p", "strong", "em", "u", "s", "h1", "h2", "h3", "h4", "ul", "ol", "li", "blockquote", "code", "pre"])
def test_allowed_tags_survive(tag):
    assert journal.sanitize_journal_html(f"<{tag}>x</{tag}>") == f"<{tag}>x</{tag}>"


def test_line_breaks_and_http_images_survive():
    html = 'a<br>b<img src="https://example.test/a.png">'
    assert journal.sanitize_journal_html(html) == html


def test_links_keep_http_href_and_get_a_safe_rel():
    cleaned = journal.sanitize_journal_html('<a href="https://example.test/x" target="_blank">x</a>')
    assert 'href="https://example.test/x"' in cleaned
    assert 'rel="noopener noreferrer"' in cleaned
    assert "target" not in cleaned


@pytest.mark.parametrize(
    "payload, forbidden",
    [
        ("<p>a</p><script>alert(1)</script>", "script"),
        ("<p>a</p><script>alert(1)</script>", "alert"),
        ('<img src="https://example.test/a.png" onerror="alert(1)">', "onerror"),
        ('<a href="javascript:alert(1)">x</a>', "javascript"),
        ('<a href="JaVaScRiPt:alert(1)">x</a>', "javascript"),
        ('<a href=" java\tscript:alert(1)">x</a>', "script"),
        ('<img src="data:image/png;base64,AAAA">', "data:"),
        ('<a href="data:text/html,hello">x</a>', "data:"),
        ('<a href="/auth/logout">x</a>', "logout"),
        ('<img src="//evil.example/p.png">', "evil.example"),
        ('<iframe src="https://evil.example"></iframe>', "iframe"),
        ("<svg onload=alert(1)></svg>", "svg"),
        ("<style>p{color:red}</style><p>x</p>", "color"),
        ('<p style="color:red">x</p>', "style"),
        ('<p class="ql-x" id="y">x</p>', "class"),
    ],
)
def test_dangerous_markup_is_removed(payload, forbidden):
    assert forbidden not in journal.sanitize_journal_html(payload).lower()


# --- Input limits ------------------------------------------------------------------

@pytest.mark.parametrize("title", ["", "   ", "x" * 201, None, 123, "a\x00b"])
def test_invalid_titles_are_rejected_before_any_sql(title):
    db = _Db()
    with pytest.raises(ValueError):
        _service(db).create_entry_sync("alice", title)
    with pytest.raises(ValueError):
        _service(db).update_entry_sync("e1", "alice", title=title if title is not None else "")
    assert db.statements == []


def test_longest_title_is_accepted_and_surrounding_space_trimmed():
    db = _Db(ROW)
    _service(db).create_entry_sync("alice", "  " + "x" * 200 + "  ")
    assert "x" * 200 in db.statements[0][1]


@pytest.mark.parametrize(
    "body",
    [
        "x" * (journal.MAX_BODY_BYTES + 1),
        "é" * (journal.MAX_BODY_BYTES // 2 + 1),  # under the limit in characters, over it in bytes
        123,
    ],
    ids=["too-long", "too-many-bytes", "not-text"],
)
def test_invalid_bodies_are_rejected_before_any_sql(body):
    db = _Db()
    with pytest.raises(ValueError):
        _service(db).create_entry_sync("alice", "T", body_html=body)
    with pytest.raises(ValueError):
        _service(db).update_entry_sync("e1", "alice", body_html=body)
    assert db.statements == []


def test_body_limit_applies_after_sanitizing():
    db = _Db(ROW)
    _service(db).create_entry_sync("alice", "T", body_html="<script>" + "x" * journal.MAX_BODY_BYTES + "</script><p>ok</p>")
    assert "<p>ok</p>" in db.statements[0][1]


def test_body_at_the_limit_is_accepted():
    db = _Db(ROW)
    _service(db).create_entry_sync("alice", "T", body_html="x" * journal.MAX_BODY_BYTES)
    assert len(db.statements) == 1


# --- Create, read, update, delete --------------------------------------------------

def test_create_stores_sanitized_html_and_returns_a_serialized_entry():
    db = _Db(ROW)

    entry = _service(db).create_entry_sync("alice", "Title", body_html="<p>x</p><script>alert(1)</script>")

    sql, params = db.statements[0]
    assert sql.startswith("INSERT INTO journal_entries")
    assert "<p>x</p>" in params and not any("script" in str(p) for p in params)
    assert entry["id"] == "e1" and entry["created_at"] == NOW.isoformat() and entry["updated_at"] == NOW.isoformat()


def test_create_without_a_book_skips_the_library_check():
    db = _Db(ROW)
    _service(db).create_entry_sync("alice", "General note")
    assert len(db.statements) == 1 and db.statements[0][1][2] is None


def test_create_for_a_book_outside_the_library_is_rejected():
    db = _Db(None)
    with pytest.raises(ValueError):
        _service(db).create_entry_sync("alice", "T", book_id="bobs-book")
    assert not any(sql.startswith("INSERT") for sql, _params in db.statements)


def test_create_for_a_book_in_the_library_links_it():
    db = _Db((1,), ROW)
    _service(db).create_entry_sync("alice", "T", book_id="b1")
    assert db.statements[0] == (BOOK_LOCK, ("b1",))  # before the library check, so it sees a removal that has committed
    assert db.statements[1] == ("SELECT 1 FROM user_books WHERE user_id=%s AND book_id=%s", ("alice", "b1"))
    assert "b1" in db.statements[2][1]


def test_another_users_entry_is_invisible():
    assert _service(_Db(None)).get_entry_sync("e1", "mallory") is None
    assert _service(_Db(None)).update_entry_sync("e1", "mallory", title="T") is None
    assert _service(_Db(rowcount=0)).delete_entry_sync("e1", "mallory") is False


def test_get_returns_the_owners_entry():
    assert _service(_Db(ROW)).get_entry_sync("e1", "alice")["body_html"] == "<p>x</p>"


def test_update_sanitizes_the_body_and_leaves_omitted_fields_alone():
    db = _Db(ROW)
    _service(db).update_entry_sync("e1", "alice", body_html="<p>y</p><script>alert(1)</script>")
    assert db.statements[0][1] == (None, "<p>y</p>", "e1", "alice")


def test_delete_reports_success():
    assert _service(_Db(rowcount=1)).delete_entry_sync("e1", "alice") is True


# --- Lists -------------------------------------------------------------------------

def test_book_list_returns_linked_update_counts_for_this_users_updates_only():
    db = _Db([("e1", "Title", NOW, 3)])

    entries = _service(db).list_entries_for_book_sync("b1", "alice")

    assert entries == [{"id": "e1", "title": "Title", "updated_at": NOW.isoformat(), "linked_update_count": 3}]
    assert "p.user_id=j.user_id" in db.statements[0][0]
    assert db.statements[0][1] == ("alice", "b1")


def test_user_list_filters_by_book_and_paginates():
    db = _Db((21,), [ROW + ("A Book",)])

    result = _service(db).list_entries_for_user_sync("alice", book_id="b1", page=2, per_page=10)

    assert result["entries"][0]["book_title"] == "A Book"
    assert result["entries"][0]["id"] == "e1"
    assert result["pagination"] == {
        "page": 2, "per_page": 10, "total_count": 21, "total_pages": 3, "has_prev": True, "has_next": True,
    }
    assert db.statements[0][1] == ("alice", "b1")
    assert db.statements[1][1] == ("alice", "b1", 10, 10)


def test_user_list_clamps_paging_and_handles_no_entries():
    db = _Db((0,), [])

    result = _service(db).list_entries_for_user_sync("alice", page=0, per_page=100000)

    assert result["entries"] == []
    assert result["pagination"]["page"] == 1 and result["pagination"]["total_pages"] == 1
    assert result["pagination"]["per_page"] == 100
    assert db.statements[1][1] == ("alice", 0, 100)


@pytest.mark.parametrize(
    "call, results",
    [
        (lambda s: s.create_entry_sync("alice", "T", book_id="b1"), [(1,), ROW]),
        (lambda s: s.get_entry_sync("e1", "alice"), [ROW]),
        (lambda s: s.list_entries_for_book_sync("b1", "alice"), [[]]),
        (lambda s: s.list_entries_for_user_sync("alice", book_id="b1"), [(0,), []]),
        (lambda s: s.update_entry_sync("e1", "alice", title="T"), [ROW]),
        (lambda s: s.delete_entry_sync("e1", "alice"), []),
    ],
)
def test_every_statement_is_scoped_to_the_user(call, results):
    db = _Db(*results)

    call(_service(db))

    assert db.statements
    for sql, params in db.statements:
        if sql == BOOK_LOCK:  # takes a lock and returns nothing to the caller
            continue
        assert "alice" in params
        assert sql.startswith("INSERT") or "user_id=%s" in sql


# --- Wiring ------------------------------------------------------------------------

def test_facade_exposes_the_journal_service():
    db = _Db()
    facade = PostgresServiceFacade(store=db)
    assert isinstance(facade.journal_service, journal.PostgresJournalService)
    assert facade.journal_service.store is db


def test_getter_returns_none_on_kuzu():
    assert services.get_journal_service() is None


def test_getter_returns_the_facade_service_on_postgres(monkeypatch):
    marker = object()
    monkeypatch.setattr(services, "get_active_persistence_backend", lambda: "postgres")
    monkeypatch.setattr(services, "_get_postgres_facade", lambda: SimpleNamespace(journal_service=marker))
    assert services.get_journal_service() is marker
