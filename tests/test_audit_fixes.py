"""Regressions for the remaining 2026-11-03 audit findings (SEC-AUDIT-002)."""

import json
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend_stub import kuzu_backend, kuzu_backend_for_module  # noqa: E402,F401 - autouse fixture

# Importing route modules probes the active backend; avoid needing a live database.
with kuzu_backend():
    import app.auth as auth
    import app.routes.people_routes as people
    from app.domain.models import User
    from app.services.postgres_user_service import PostgresUserService
    from app.utils import image_processing


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """These tests must never reach the internet; code under test falls back on errors."""
    def blocked(*_args, **_kwargs):
        raise OSError("network access blocked in tests")
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


class _FakeStore:
    """Records SQL and answers every fetchone() with `row`."""

    def __init__(self, row=None, rows=()):
        self.queries = []
        self.row = row
        self.rows = list(rows)
        store = self

        class _Cursor:
            rowcount = 1

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, sql, params=None):
                store.queries.append((sql, params))

            def fetchone(self):
                return store.row

            def fetchall(self):
                return store.rows

        self._cursor = _Cursor

    def _connection(self):
        store = self

        class _Conn:
            def __enter__(self):
                return SimpleNamespace(cursor=store._cursor)

            def __exit__(self, *_args):
                return False

        return _Conn()

    connection = transaction = _connection


# --- Finding 6: failed-login counting must not race --------------------------

def test_failed_login_is_counted_in_one_atomic_update():
    locked = datetime.now(timezone.utc) + timedelta(minutes=30)
    store = _FakeStore(row=(5, locked))
    assert PostgresUserService(store=store).record_failed_login_sync("u1") == (5, locked)
    assert len(store.queries) == 1
    sql, params = store.queries[0]
    assert sql.startswith("UPDATE users SET") and "failed_login_attempts + 1" in sql
    assert "RETURNING failed_login_attempts, locked_until" in sql
    assert "u1" in params


def test_successful_login_clears_only_the_counter_columns():
    store = _FakeStore()
    PostgresUserService(store=store).clear_failed_login_sync("u1")
    sql, params = store.queries[0]
    assert "failed_login_attempts = 0" in sql and "locked_until = NULL" in sql
    assert "is_active" not in sql  # must not write back a stale full row
    assert params == ("u1",)


def test_login_failure_uses_the_atomic_counter(monkeypatch):
    locked = datetime.now(timezone.utc) + timedelta(minutes=30)

    def full_row_write(_user):
        raise AssertionError("must not write the whole user row")

    monkeypatch.setattr(auth, "user_service", SimpleNamespace(
        record_failed_login_sync=lambda user_id: (5, locked),
        update_user_sync=full_row_write,
    ))
    user = User(id="u1", failed_login_attempts=0)
    auth._count_failed_login(user)
    assert (user.failed_login_attempts, user.locked_until) == (5, locked)


def test_login_success_clears_counter_without_full_row_write(monkeypatch):
    cleared = []

    def full_row_write(_user):
        raise AssertionError("must not write the whole user row")

    monkeypatch.setattr(auth, "user_service", SimpleNamespace(
        clear_failed_login_sync=cleared.append, update_user_sync=full_row_write,
    ))
    user = User(id="u1", failed_login_attempts=3)
    auth._clear_failed_logins(user)
    assert cleared == ["u1"] and user.failed_login_attempts == 0


def test_expired_lock_restarts_the_count():
    user = User(id="u1", failed_login_attempts=5,
                locked_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    user.increment_failed_login()
    assert user.failed_login_attempts == 1
    assert user.locked_until is None


# --- Finding 7: person links must be http(s) ---------------------------------

@pytest.mark.parametrize("value", [
    "javascript:alert(1)", " JavaScript:alert(1)", "java\tscript:alert(1)",
    "data:text/html,<script>alert(1)</script>", "//evil.example", "",
])
def test_non_http_person_urls_are_dropped(value):
    assert people._http_url(value) == ""


def test_http_person_urls_are_kept():
    assert people._http_url(" https://author.example/x ") == "https://author.example/x"


def test_edit_person_drops_javascript_links(monkeypatch):
    saved = {}
    monkeypatch.setattr(people, "book_service", SimpleNamespace(
        get_person_by_id_sync=lambda _pid: SimpleNamespace(id="p1"),
        update_person_sync=lambda _pid, data: saved.update(data) or True,
    ))
    app = Flask(__name__)
    app.secret_key = "t"
    app.config["LOGIN_DISABLED"] = True
    app.register_blueprint(people.people_bp)
    links = [{"title": "Bad", "url": "javascript:alert(1)"}, {"title": "Ok", "url": "https://ok.example"}]
    response = app.test_client().post("/person/p1/edit", data={
        "name": "Ann", "website": "javascript:alert(document.cookie)",
        "official_links": json.dumps(links),
    })
    assert response.status_code == 302
    assert saved["website"] is None
    assert [link["url"] for link in json.loads(saved["official_links"])] == ["https://ok.example"]


def test_openlibrary_author_links_are_filtered():
    updates = people.parse_comprehensive_openlibrary_data({
        "links": [{"title": "Official website", "url": "javascript:alert(1)"}],
        "wikipedia_url": "javascript:alert(2)",
    })
    assert "website" not in updates
    assert "official_links" not in updates


# --- Finding 8: cover downloads must check every redirect hop ----------------

class _Resp:
    def __init__(self, location=None, body=b"img"):
        self.status_code = 302 if location else 200
        self.headers = {"Location": location} if location else {}
        self.is_redirect = location is not None
        self._body = body

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


def _fake_net(monkeypatch, responses):
    addresses = {"cdn.example": "93.184.216.34", "other.example": "93.184.216.35", "127.0.0.1": "127.0.0.1"}
    monkeypatch.setattr(image_processing.socket, "getaddrinfo",
                        lambda host, _port: [(2, 1, 6, "", (addresses[host], 0))])
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses[url]

    monkeypatch.setattr(image_processing.requests, "get", fake_get)
    monkeypatch.setattr(image_processing, "process_image_bytes_and_store", lambda _b: "/covers/x.jpg")
    return calls


def test_cover_redirect_to_internal_host_is_refused(monkeypatch):
    calls = _fake_net(monkeypatch, {
        "https://cdn.example/c.jpg": _Resp(location="http://127.0.0.1:6379/"),
    })
    with pytest.raises(ValueError):
        image_processing.process_image_from_url("https://cdn.example/c.jpg")
    assert [url for url, _ in calls] == ["https://cdn.example/c.jpg"]
    assert calls[0][1]["allow_redirects"] is False


def test_cover_redirect_to_other_host_drops_credentials(monkeypatch):
    calls = _fake_net(monkeypatch, {
        "https://cdn.example/c.jpg": _Resp(location="https://other.example/c.jpg"),
        "https://other.example/c.jpg": _Resp(),
    })
    result = image_processing.process_image_from_url(
        "https://cdn.example/c.jpg", auth=("u", "p"), headers={"Authorization": "Bearer t"})
    assert result == "/covers/x.jpg"
    assert calls[0][1]["auth"] == ("u", "p")
    assert "auth" not in calls[1][1] and "headers" not in calls[1][1]


@pytest.mark.parametrize("target", ["http://cdn.example/c.jpg", "https://cdn.example:8443/c.jpg"], ids=["plain-http", "other-port"])
def test_cover_redirect_on_the_same_host_drops_credentials_when_the_origin_changes(monkeypatch, target):
    calls = _fake_net(monkeypatch, {"https://cdn.example/c.jpg": _Resp(location=target), target: _Resp()})

    image_processing.process_image_from_url("https://cdn.example/c.jpg", auth=("u", "p"), headers={"Authorization": "Bearer t"})

    assert "auth" not in calls[1][1] and "headers" not in calls[1][1]


def test_cover_redirect_does_not_give_credentials_back_later(monkeypatch):
    calls = _fake_net(monkeypatch, {
        "https://cdn.example/c.jpg": _Resp(location="http://cdn.example/d.jpg"),
        "http://cdn.example/d.jpg": _Resp(location="https://cdn.example/e.jpg"),
        "https://cdn.example/e.jpg": _Resp(),
    })

    image_processing.process_image_from_url("https://cdn.example/c.jpg", auth=("u", "p"), headers={"Authorization": "Bearer t"})

    assert "auth" not in calls[2][1] and "headers" not in calls[2][1]


@pytest.mark.parametrize(
    "start, target",
    [
        ("https://cdn.example/c.jpg", "https://cdn.example/d.jpg"),
        ("https://cdn.example/c.jpg", "https://cdn.example:443/d.jpg"),
        ("http://cdn.example/c.jpg", "https://cdn.example/c.jpg"),
    ],
    ids=["same-origin", "explicit-default-port", "upgrade-to-https"],
)
def test_cover_redirect_keeps_credentials_when_the_origin_is_safe(monkeypatch, start, target):
    calls = _fake_net(monkeypatch, {start: _Resp(location=target), target: _Resp()})
    headers = {"Authorization": "Bearer t"}

    image_processing.process_image_from_url(start, auth=("u", "p"), headers=headers)

    assert calls[1][1]["auth"] == ("u", "p") and calls[1][1]["headers"] == headers


def test_cover_redirect_loop_is_cut_off(monkeypatch):
    _fake_net(monkeypatch, {"https://cdn.example/c.jpg": _Resp(location="https://cdn.example/c.jpg")})
    with pytest.raises(ValueError):
        image_processing.process_image_from_url("https://cdn.example/c.jpg")


# --- Finding 9: login `next` must stay on this site --------------------------

@pytest.mark.parametrize("value", [
    "//evil.example", "/\\evil.example", "/\t/evil.example", "https://evil.example/",
    "javascript:alert(1)", "", None,
])
def test_login_next_rejects_other_sites(value):
    assert auth._safe_next_url(value) is None


def test_login_next_keeps_local_paths():
    assert auth._safe_next_url("/library?page=2") == "/library?page=2"


# --- Finding 12: deleting a custom field must not wipe other users' values ----

from app.services.postgres_custom_field_service import PostgresCustomFieldService  # noqa: E402
from app.services.postgres_service_facade import PostgresServiceFacade  # noqa: E402


def _field_service(monkeypatch, used_by_others):
    store = _FakeStore(row=(used_by_others,))
    service = PostgresCustomFieldService(store=store)
    monkeypatch.setattr(service, "get_field_by_id_sync", lambda field_id: {
        "id": field_id, "name": "mood", "created_by_user_id": "bob",
        "source_table": "custom_field_definitions", "is_global": False,
    })
    monkeypatch.setattr(service, "_invalidate", lambda **_kw: None)
    return service, store


def _writes(store):
    return [sql for sql, _ in store.queries if sql.lstrip().upper().startswith(("UPDATE", "DELETE"))]


def test_custom_field_delete_refused_while_others_have_values(monkeypatch):
    service, store = _field_service(monkeypatch, used_by_others=True)
    assert service.delete_field_sync("f1", "bob") is False
    assert _writes(store) == []


def test_custom_field_delete_proceeds_when_only_owner_has_values(monkeypatch):
    service, store = _field_service(monkeypatch, used_by_others=False)
    assert service.delete_field_sync("f1", "bob") is True
    assert any("DELETE FROM custom_field_definitions" in sql for sql in _writes(store))


# --- Finding 13: the library count is the user's own books -------------------

def test_library_count_is_the_users_own_books():
    store = _FakeStore(row=(20,))
    assert PostgresServiceFacade.get_user_book_count_sync(SimpleNamespace(store=store), "u1") == 20
    sql, params = store.queries[0]
    assert "FROM user_books WHERE user_id = %s" in sql and params == ("u1",)


# --- Finding 14: Postgres rows are dicts; getattr() read them as empty --------

def test_community_books_this_month_reads_dict_rows(monkeypatch):
    import app.routes.stats_routes as stats

    reader = SimpleNamespace(id="u2", username="ann", share_reading_activity=True, share_current_reading=True)
    finished = datetime.now(timezone.utc)
    monkeypatch.setattr(stats, "user_service", SimpleNamespace(get_all_users_sync=lambda: [reader]))
    monkeypatch.setattr(stats, "book_service", SimpleNamespace(get_all_books_with_user_overlay_sync=lambda _uid: [
        {"title": "Dune", "author": "Frank Herbert", "finish_date": finished, "reading_status": "read"},
    ]))
    rendered = {}
    monkeypatch.setattr(stats, "render_template", lambda _tpl, **ctx: rendered.update(ctx) or "ok")
    app = Flask(__name__)
    app.config["LOGIN_DISABLED"] = True
    with app.test_request_context():
        stats.community_books_this_month()
    assert [(b["title"], b["author"]) for b in rendered["books"]] == [("Dune", "Frank Herbert")]


def test_month_review_finds_books_in_dict_rows(monkeypatch):
    import app.routes.book_routes as br

    monkeypatch.setattr(br, "current_user", SimpleNamespace(id="u1"))
    monkeypatch.setattr(br, "book_service", SimpleNamespace(get_all_books_with_user_overlay_sync=lambda _uid: [
        {"title": "Dune", "author": "Frank Herbert", "cover_url": None,
         "finish_date": datetime(2026, 10, 12, 18, 0, tzinfo=timezone.utc)},
        {"title": "Emma", "author": "Jane Austen", "cover_url": None,
         "finish_date": datetime(2026, 9, 1, tzinfo=timezone.utc)},
    ]))
    seen = []
    monkeypatch.setattr(br, "generate_month_review_image", lambda books, _m, _y: seen.extend(books) or None)
    app = Flask(__name__)
    app.config["LOGIN_DISABLED"] = True
    with app.test_request_context():
        br.month_review(2026, 10)
    assert [b.title for b in seen] == ["Dune"]


# --- Finding 15: a failed sub-write must not report the edit as saved --------

def test_book_edit_reports_failure_when_a_sub_write_fails():
    facade = SimpleNamespace(
        book_service=SimpleNamespace(
            update_book_sync=lambda _bid, _fields: SimpleNamespace(id="b1"),
            replace_contributors_sync=lambda _bid, _c: False,  # e.g. pool timeout
        ),
        library_service=SimpleNamespace(update_user_book_sync=lambda *_a: True),
    )
    result = PostgresServiceFacade.update_book_sync(
        facade, "b1", "u1", title="New", contributors=[{"name": "A"}], user_rating=4)
    assert result is None


def test_book_edit_returns_the_book_when_every_write_succeeds():
    book = SimpleNamespace(id="b1")
    facade = SimpleNamespace(
        book_service=SimpleNamespace(update_book_sync=lambda _bid, _fields: book,
                                     replace_contributors_sync=lambda _bid, _c: True),
        library_service=SimpleNamespace(update_user_book_sync=lambda *_a: True),
    )
    assert PostgresServiceFacade.update_book_sync(
        facade, "b1", "u1", title="New", contributors=[], user_rating=4) is book


# --- Finding 16: ISBN-only rows past the lookup cap must not be dropped ------

from test_goodreads_import_rows import _gr, pg_import  # noqa: E402,F401  (shared stubbed import harness)


def test_capped_isbn_only_rows_import_as_needs_review(pg_import, monkeypatch):
    import app.routes.import_routes as ir
    import app.utils.book_utils as book_utils

    facade, run = pg_import
    monkeypatch.setenv("IMPORT_METADATA_MAX_ISBNS", "1")
    monkeypatch.setattr(book_utils, "get_best_cover_for_book", lambda **_kw: None)
    monkeypatch.setattr(ir, "get_best_cover_for_book", lambda **_kw: None)
    monkeypatch.setattr(ir, "batch_fetch_book_metadata",
                        lambda isbns, **_kw: ({i: {"data": {"title": f"Fetched {i}"}} for i in isbns}, {}))
    job = run([_gr(i, Title="", ISBN="") for i in (1, 2, 3)], enable_api_enrichment=True)
    assert job["errors"] == 0
    rows = list(facade.library_service.rows.values())
    assert len(rows) == 3
    assert sum(r["custom_metadata"].get("needs_review") == "Needs Review" for r in rows) == 2


def test_isbn_only_row_not_found_is_still_an_error(pg_import, monkeypatch):
    import app.routes.import_routes as ir
    import app.utils.book_utils as book_utils

    facade, run = pg_import
    monkeypatch.setattr(book_utils, "get_best_cover_for_book", lambda **_kw: None)
    monkeypatch.setattr(ir, "batch_fetch_book_metadata",
                        lambda isbns, **_kw: ({}, {i: {"reason": "no_metadata", "attempt": 1} for i in isbns}))
    job = run([_gr(1, Title="", ISBN="")], enable_api_enrichment=True)
    assert job["errors"] == 1 and facade.library_service.rows == {}


# --- Finding 10: one worker owns the schedulers; workers share settings ------

import app.utils.scheduler_lock as scheduler_lock  # noqa: E402
from app.services import simple_backup_service as backup_module  # noqa: E402


def test_only_one_worker_owns_a_scheduler(monkeypatch, tmp_path):
    taken = []

    def flock(fd, _flags):
        if taken:  # another process already holds it
            raise BlockingIOError
        taken.append(fd)

    monkeypatch.setattr(scheduler_lock, "fcntl", SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=flock))
    monkeypatch.setattr(scheduler_lock, "_held", {})
    assert scheduler_lock.holds_scheduler_lock("backup", tmp_path) is True
    assert scheduler_lock.holds_scheduler_lock("backup", tmp_path) is True  # keeps it
    monkeypatch.setattr(scheduler_lock, "_held", {})  # now ask as a second worker
    assert scheduler_lock.holds_scheduler_lock("backup", tmp_path) is False
    import os
    os.close(taken[0])


def test_scheduler_lock_without_fcntl_runs_as_before(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler_lock, "fcntl", None)
    assert scheduler_lock.holds_scheduler_lock("backup", tmp_path) is True


def _backup_service(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_module.SimpleBackupService, "ensure_scheduler", lambda self: None)
    return backup_module.SimpleBackupService(base_dir=str(tmp_path))


def test_scheduled_backup_skipped_without_the_lock(monkeypatch, tmp_path):
    svc = _backup_service(monkeypatch, tmp_path)
    monkeypatch.setattr(backup_module, "holds_scheduler_lock", lambda *_a: False)
    monkeypatch.setattr(svc, "create_backup", lambda **_kw: pytest.fail("non-owner must not back up"))
    svc._maybe_run_scheduled_backup()


def test_backup_settings_saved_by_another_worker_are_seen(monkeypatch, tmp_path):
    worker_a = _backup_service(monkeypatch, tmp_path)
    worker_b = _backup_service(monkeypatch, tmp_path)
    assert worker_a._settings["enabled"] is True
    worker_b._settings["enabled"] = False
    assert worker_b._save_settings()
    assert worker_a._settings["enabled"] is False


def test_backup_list_includes_backups_made_by_another_worker(monkeypatch, tmp_path):
    worker_a = _backup_service(monkeypatch, tmp_path)
    worker_b = _backup_service(monkeypatch, tmp_path)
    assert worker_a.list_backups() == []
    info = backup_module.SimpleBackupInfo(
        id="b1", name="nightly", created_at=datetime.now(), file_path=str(tmp_path / "b1.zip"),
        file_size=1, description="", metadata={})
    worker_b._backup_index["b1"] = info
    worker_b._save_backup_index()
    assert [b.id for b in worker_a.list_backups()] == ["b1"]


def _backup_info(tmp_path, backup_id):
    return backup_module.SimpleBackupInfo(
        id=backup_id, name="nightly", created_at=datetime.now(), file_path=str(tmp_path / f"{backup_id}.zip"),
        file_size=1, description="", metadata={})


def test_backup_index_writers_do_not_share_a_temp_file(monkeypatch, tmp_path):
    worker_a = _backup_service(monkeypatch, tmp_path)
    worker_b = _backup_service(monkeypatch, tmp_path)
    worker_a._backup_index = {"short": _backup_info(tmp_path, "short")}
    worker_b._backup_index = {f"b{number}": _backup_info(tmp_path, f"b{number}") for number in range(5)}
    temp_files, real_dump = [], json.dump

    def dump_while_the_other_worker_publishes(data, handle, **kwargs):
        temp_files.append(handle.name)
        if len(temp_files) == 1:
            worker_b._save_backup_index()
        real_dump(data, handle, **kwargs)

    monkeypatch.setattr(backup_module.json, "dump", dump_while_the_other_worker_publishes)
    worker_a._save_backup_index()

    assert temp_files[0] != temp_files[1]
    assert list(json.loads(worker_a.backup_index_file.read_text())) == ["short"]
    assert not list(tmp_path.rglob("*.tmp"))


@pytest.mark.parametrize(
    "change",
    [lambda service, info: service._record_backup(info), lambda service, info: service.delete_backup(info.id)],
    ids=["record", "delete"],
)
def test_changing_the_backup_index_waits_for_the_other_worker(monkeypatch, tmp_path, change):
    fcntl = pytest.importorskip("fcntl")
    service = _backup_service(monkeypatch, tmp_path)
    info = _backup_info(tmp_path, "b1")
    service._record_backup(info)

    with open(service.backup_index_file.with_suffix(".json.lock"), "a") as other_worker:
        fcntl.flock(other_worker, fcntl.LOCK_EX)
        with ThreadPoolExecutor(max_workers=1) as pool:
            changing = pool.submit(change, service, info)
            time.sleep(0.3)
            assert not changing.done()
            fcntl.flock(other_worker, fcntl.LOCK_UN)
            changing.result(timeout=10)


def test_a_reader_cannot_replace_an_index_being_written(monkeypatch, tmp_path):
    service = _backup_service(monkeypatch, tmp_path)
    real_save = service._save_backup_index

    def read_just_before_saving():
        service.list_backups()
        service.get_backup("b1")
        real_save()

    monkeypatch.setattr(service, "_save_backup_index", read_just_before_saving)
    service._record_backup(_backup_info(tmp_path, "b1"))

    assert [backup.id for backup in service.list_backups()] == ["b1"]


def _damaged_index(tmp_path, service):
    good = json.dumps({"b1": _backup_info(tmp_path, "b1").to_dict()})
    service.backup_index_file.write_text(good + "trailing garbage")
    return good + "trailing garbage"


def test_readers_recover_a_damaged_index_without_rewriting_it(monkeypatch, tmp_path):
    service = _backup_service(monkeypatch, tmp_path)
    damaged = _damaged_index(tmp_path, service)

    assert [backup.id for backup in service.list_backups()] == ["b1"]

    assert service.backup_index_file.read_text() == damaged


def test_readers_leave_an_unreadable_index_in_place(monkeypatch, tmp_path):
    service = _backup_service(monkeypatch, tmp_path)
    service.backup_index_file.write_text("not json at all")

    assert service.list_backups() == []

    assert service.backup_index_file.read_text() == "not json at all"
    assert not service.backup_index_file.with_suffix(".json.corrupted").exists()


def test_a_writer_repairs_a_damaged_index_under_the_lock(monkeypatch, tmp_path):
    service = _backup_service(monkeypatch, tmp_path)
    _damaged_index(tmp_path, service)

    service._record_backup(_backup_info(tmp_path, "b2"))

    assert sorted(json.loads(service.backup_index_file.read_text())) == ["b1", "b2"]


def test_index_changes_take_turns_without_flock(monkeypatch, tmp_path):
    monkeypatch.setattr(backup_module, "fcntl", None)
    service = _backup_service(monkeypatch, tmp_path)
    service._record_backup(_backup_info(tmp_path, "old"))
    saving, resume, real_save = threading.Event(), threading.Event(), service._save_backup_index

    def pause_the_first_save():
        if not saving.is_set():
            saving.set()
            assert resume.wait(10)
        real_save()

    monkeypatch.setattr(service, "_save_backup_index", pause_the_first_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        recording = pool.submit(service._record_backup, _backup_info(tmp_path, "new"))
        assert saving.wait(10)
        deleting = pool.submit(service.delete_backup, "old")
        time.sleep(0.2)
        resume.set()
        recording.result(timeout=10)
        assert deleting.result(timeout=10) is True

    assert [backup.id for backup in service.list_backups()] == ["new"]


def _opds_due(monkeypatch, *, holds_lock):
    import app.services.opds_sync_runner as opds

    events = []
    monkeypatch.setattr(opds, "holds_scheduler_lock", lambda *_a: holds_lock)
    monkeypatch.setattr(opds, "load_opds_settings", lambda: {
        "auto_sync_enabled": True, "base_url": "https://opds.example", "mapping": {"title": "title"},
        "auto_sync_every_hours": 24, "last_auto_sync": None, "auto_sync_user_id": "u1"})
    monkeypatch.setattr(opds, "save_opds_settings", lambda update: events.append(("save", update)) or True)
    runner = opds._OpdsSyncRunner()
    monkeypatch.setattr(runner, "enqueue_sync", lambda user_id, **kw: events.append(("enqueue", user_id)))
    app = Flask(__name__)
    with app.app_context():
        runner._maybe_schedule_automatic_sync()
    return events


def test_opds_auto_sync_only_from_the_lock_holder(monkeypatch):
    assert _opds_due(monkeypatch, holds_lock=False) == []


def test_opds_auto_sync_records_the_run_before_queueing(monkeypatch):
    events = _opds_due(monkeypatch, holds_lock=True)
    assert [kind for kind, _ in events] == ["save", "enqueue"]
    assert "last_auto_sync" in events[0][1]


def test_abs_auto_sync_only_from_the_lock_holder(monkeypatch):
    import app.services.audiobookshelf_sync_runner as abs_runner

    monkeypatch.setattr(abs_runner, "holds_scheduler_lock", lambda *_a: False)
    monkeypatch.setattr(abs_runner, "load_abs_settings",
                        lambda: pytest.fail("non-owner must not even check the schedule"))
    app = Flask(__name__)
    with app.app_context():
        abs_runner._AbsSyncRunner()._maybe_schedule_automatic_sync()


# --- Finding 11: Cancel must stop a running CSV import ------------------------

def test_cancelled_import_stops_and_says_so(pg_import, monkeypatch):
    import app.routes.import_routes as ir

    facade, run = pg_import
    real_get = ir.safe_get_import_job
    # The user pressed Cancel: the job store carries the flag the cancel endpoint sets.
    monkeypatch.setattr(ir, "safe_get_import_job", lambda u, t: {**real_get(u, t), "cancelled": True})
    job = run([_gr(i) for i in range(1, 4)])
    assert job["status"] == "cancelled"
    assert facade.library_service.rows == {}


# --- Finding 18: the library page must not serve a per-worker cached copy -----

class _LibraryBooks:
    def __init__(self, books):
        self.books = books

    def get_user_book_count_sync(self, _uid):
        return len(self.books)

    def get_books_with_user_overlay_paginated_sync(self, _uid, limit, offset, _sort):
        return list(self.books)[offset:offset + limit]

    def get_all_books_with_user_overlay_flat_sync(self, _uid):
        return list(self.books)

    def get_library_status_counts_sync(self, _uid):
        return {}

    def __getattr__(self, _name):
        return lambda *_a, **_kw: []


def test_library_reflects_a_delete_made_through_another_worker(monkeypatch):
    import app.routes.library_route as lib
    import app.utils.user_settings as user_settings

    books = _LibraryBooks([{"id": "b1", "title": "Dune"}, {"id": "b2", "title": "Emma"}])
    monkeypatch.setattr(lib, "book_service", books)
    monkeypatch.setattr(lib, "user_service", SimpleNamespace(get_all_users_sync=lambda: []))
    monkeypatch.setattr(lib, "current_user", SimpleNamespace(id="u1", is_authenticated=True, is_admin=False))
    monkeypatch.setattr(lib, "get_library_view_defaults", lambda _uid: ("all", "title_asc"))
    monkeypatch.setattr(user_settings, "get_effective_rows_per_page", lambda _uid: 4)
    app = Flask(__name__)
    app.config["LOGIN_DISABLED"] = True

    def titles():
        with app.test_request_context("/library?format=json&cols=5"):
            return sorted(item["title"] for item in lib.library().get_json()["items"])

    assert titles() == ["Dune", "Emma"]
    books.books = [{"id": "b1", "title": "Dune"}]  # Emma deleted; this worker saw no cache bump
    assert titles() == ["Dune"]


# --- Finding 17: book lists must not run a set of queries per book ------------

from app.services.postgres_book_service import PostgresBookService  # noqa: E402
from app.services.postgres_location_service import PostgresLocationService  # noqa: E402


class _ScriptedStore(_FakeStore):
    """fetchall() answers with the rows scripted for the first matching SQL fragment."""

    def __init__(self, scripts=None):
        super().__init__()
        self.scripts = scripts or {}
        store = self
        base = self._cursor

        class _Cursor(base):
            def fetchall(self):
                sql = store.queries[-1][0]
                return next((rows for fragment, rows in store.scripts.items() if fragment in sql), [])

        self._cursor = _Cursor


def _overlay_facade(store, rows):
    facade = SimpleNamespace(
        book_service=PostgresBookService(store=store),
        location_service=PostgresLocationService(store=store),
        library_service=SimpleNamespace(get_books_with_user_overlay_paginated_sync=lambda *_a: rows),
    )
    for name in ("_overlay_to_book", "_overlay_to_books"):  # real facade code, no database
        if hasattr(PostgresServiceFacade, name):
            setattr(facade, name, getattr(PostgresServiceFacade, name).__get__(facade))
    return facade


def test_book_list_query_count_does_not_grow_with_the_list():
    store = _ScriptedStore()
    rows = [{"book_id": f"b{i}", "title": f"Book {i}", "reading_status": "read"} for i in range(50)]
    books = PostgresServiceFacade._books_from_overlay(
        _overlay_facade(store, rows), "u1")
    assert len(books) == 50
    assert len(store.queries) <= 10, f"{len(store.queries)} queries for 50 books"


def test_batched_relationships_land_on_the_right_book():
    store = _ScriptedStore({
        "FROM book_contributors": [("b2", "p1", "Ann Leckie", "authored", 0)],
        "FROM book_series bs": [("b1", "s1", "Imperial Radch", "imperial radch", None, None, None, False, False, None)],
        "FROM book_locations": [("b2", "loc-shelf")],
    })
    rows = [{"book_id": "b1", "title": "One"}, {"book_id": "b2", "title": "Two", "location_id": "old"}]
    one, two = PostgresServiceFacade._books_from_overlay(_overlay_facade(store, rows), "u1")
    assert one.contributors == [] and [c.person.name for c in two.contributors] == ["Ann Leckie"]
    assert one.series.name == "Imperial Radch" and two.series is None
    assert two.location_id == "loc-shelf"
