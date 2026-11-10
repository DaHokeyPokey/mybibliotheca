"""Access-control regressions from the 2026-11-03 audit (SEC-AUDIT-001)."""

import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask
from werkzeug.exceptions import Forbidden

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.infrastructure import persistence_state
from backend_stub import kuzu_backend, kuzu_backend_for_module  # noqa: E402,F401 - autouse fixture

# Importing route modules probes the active backend; avoid needing a live database.
with kuzu_backend():
    import app.onboarding_system as ob
    import app.admin as admin_module
    import app.routes.book_routes as br

ANON = SimpleNamespace(is_authenticated=False)
LOGGED_IN = SimpleNamespace(is_authenticated=True, id="u1", is_admin=False)
ADMIN = SimpleNamespace(is_authenticated=True, id="admin1", is_admin=True)


def _view_app():
    app = Flask(__name__)
    app.secret_key = "t"
    app.config["LOGIN_DISABLED"] = True  # isolate admin_required from flask-login
    return app


def _as_user(monkeypatch, module, user):
    monkeypatch.setattr(module, "current_user", user)
    monkeypatch.setattr(admin_module, "current_user", user)


# --- Finding 1: onboarding must close once any user exists -------------------

def _onboarding_app():
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(ob.onboarding_bp)
    app.add_url_rule("/auth/login", "auth.login", lambda: "login")
    app.add_url_rule("/library", "main.library", lambda: "library")
    return app


def _user_count(monkeypatch, count):
    def get_count():
        if isinstance(count, Exception):
            raise count
        return count
    monkeypatch.setattr(ob.user_service, "get_user_count_sync", get_count)


def test_anonymous_cannot_execute_onboarding_after_setup(monkeypatch):
    created = []
    _user_count(monkeypatch, 1)
    monkeypatch.setattr(ob, "current_user", ANON)
    monkeypatch.setattr(ob.user_service, "create_user_sync", lambda **kw: created.append(kw))

    resp = _onboarding_app().test_client().post("/onboarding/step/5", data={"action": "execute"})

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/auth/login")
    assert created == []


def test_anonymous_cannot_open_admin_step_after_setup(monkeypatch):
    _user_count(monkeypatch, 3)
    monkeypatch.setattr(ob, "current_user", ANON)

    resp = _onboarding_app().test_client().get("/onboarding/step/1")

    assert resp.headers["Location"].endswith("/auth/login")


def test_logged_in_user_is_sent_to_library_after_setup(monkeypatch):
    _user_count(monkeypatch, 1)
    monkeypatch.setattr(ob, "current_user", LOGGED_IN)

    resp = _onboarding_app().test_client().get("/onboarding/step/2")

    assert resp.headers["Location"].endswith("/library")


def test_new_admin_can_still_see_completion_pages(monkeypatch):
    _user_count(monkeypatch, 1)
    monkeypatch.setattr(ob, "current_user", LOGGED_IN)
    app = _onboarding_app()
    for path in ("/onboarding/complete", "/onboarding/import-progress/t1", "/onboarding/import-progress-json/t1"):
        with app.test_request_context(path):
            assert ob._block_onboarding_after_setup() is None, path


def test_onboarding_open_on_fresh_install(monkeypatch):
    _user_count(monkeypatch, 0)
    monkeypatch.setattr(ob, "current_user", ANON)
    with _onboarding_app().test_request_context("/onboarding/step/1"):
        assert ob._block_onboarding_after_setup() is None


def test_onboarding_closed_when_user_count_unreadable(monkeypatch):
    _user_count(monkeypatch, RuntimeError("db down"))
    monkeypatch.setattr(ob, "current_user", ANON)

    resp = _onboarding_app().test_client().get("/onboarding/step/1")

    assert resp.headers["Location"].endswith("/auth/login")


# --- Finding 2: raw book view leaked other users' password hashes ------------

RAW_PAYLOAD = {
    "book_node": {"id": "b1", "title": "Dune"},
    "outgoing_rels_raw": [],
    "incoming_rels_raw": [
        {
            "direction": "IN",
            "relationship": {"type": "OWNS", "properties": {"user_rating": 5}},
            "source": {"id": "u2", "username": "bob", "password_hash": "scrypt:secret"},
        },
    ],
    "personal_metadata": None,
}


def _raw_view(monkeypatch, user):
    _as_user(monkeypatch, br, user)
    monkeypatch.setattr(br.book_service, "get_book_by_uid_sync", lambda uid, user_id: {"id": uid})
    monkeypatch.setattr(br.book_service, "get_raw_book_graph_sync", lambda uid, user_id: copy.deepcopy(RAW_PAYLOAD))
    with _view_app().test_request_context("/book/b1/raw"):
        return br.view_book_raw("b1")


def test_raw_book_view_is_admin_only(monkeypatch):
    with pytest.raises(Forbidden):
        _raw_view(monkeypatch, LOGGED_IN)


def test_raw_book_view_never_returns_password_hashes(monkeypatch):
    data = _raw_view(monkeypatch, ADMIN).get_json()

    assert "password_hash" not in json.dumps(data)
    assert data["incoming_relationships"][0]["source"]["username"] == "bob"


# --- Finding 4: anonymous GET could kill a worker ----------------------------

import app.routes.simple_backup_routes as backup_routes  # noqa: E402


def _record_shutdowns(monkeypatch):
    started = []
    # Never let the real shutdown thread run inside pytest.
    monkeypatch.setattr("os._exit", lambda code: None)
    monkeypatch.setattr(
        "threading.Thread",
        lambda target=None, **kw: SimpleNamespace(start=lambda: started.append(target), daemon=False),
    )
    return started


def _restore_complete(monkeypatch, user):
    started = _record_shutdowns(monkeypatch)
    monkeypatch.setattr(backup_routes, "render_template", lambda *a, **kw: "restore complete")
    _as_user(monkeypatch, backup_routes, user)
    with _view_app().test_request_context("/auth/simple-backup/restore-complete"):
        return backup_routes.restore_complete(), started


def _restore_post(monkeypatch, user, restored=True, restart_required=True):
    started = _record_shutdowns(monkeypatch)
    service = SimpleNamespace(
        get_backup=lambda _id: SimpleNamespace(name="nightly"),
        restore_backup=lambda _id: restored,
        check_restart_required=lambda: restart_required,
    )
    monkeypatch.setattr(backup_routes, "get_simple_backup_service", lambda: service)
    monkeypatch.setattr(backup_routes, "url_for", lambda endpoint, **kw: f"/{endpoint}")
    _as_user(monkeypatch, backup_routes, user)
    app = _view_app()
    app.add_url_rule("/auth/login", "auth.login", lambda: "login")
    with app.test_request_context("/auth/simple-backup/restore/b1", method="POST", data={"confirm": "yes"}):
        try:
            return backup_routes.restore_backup("b1"), started
        except Forbidden:
            return "forbidden", started


@pytest.mark.parametrize("user", [ANON, LOGGED_IN, ADMIN], ids=["anonymous", "user", "admin"])
def test_restore_complete_only_shows_status(monkeypatch, user):
    resp, started = _restore_complete(monkeypatch, user)
    assert resp == "restore complete"
    assert started == []


def test_a_successful_restore_schedules_the_restart(monkeypatch):
    resp, started = _restore_post(monkeypatch, ADMIN)
    assert resp.status_code == 302 and "restore_complete" in resp.headers["Location"]
    assert len(started) == 1


def test_the_restart_survives_a_restore_that_replaces_the_admin(monkeypatch):
    _resp, started = _restore_post(monkeypatch, ADMIN)
    page, started_by_page = _restore_complete(monkeypatch, ANON)  # the session's account is gone after the restore
    assert page == "restore complete"
    assert len(started) == 1 and started_by_page == []


@pytest.mark.parametrize("outcome", [{"restored": False}, {"restart_required": False}], ids=["failed", "no-restart-needed"])
def test_no_restart_is_scheduled_otherwise(monkeypatch, outcome):
    _resp, started = _restore_post(monkeypatch, ADMIN, **outcome)
    assert started == []


def test_anonymous_cannot_restore_or_restart(monkeypatch):
    resp, started = _restore_post(monkeypatch, ANON)
    assert getattr(resp, "status_code", None) == 302 and "login" in resp.headers["Location"]
    assert started == []


def test_non_admin_cannot_restore_or_restart(monkeypatch):
    resp, started = _restore_post(monkeypatch, LOGGED_IN)
    assert resp == "forbidden"
    assert started == []


# --- Finding 3: deleting a book removed it from every user's library ---------

from contextlib import contextmanager  # noqa: E402

import app.services.postgres_book_service as pg_books  # noqa: E402
from app.services.postgres_service_facade import PostgresServiceFacade  # noqa: E402


class _DeleteDb:
    """Fake store that models only who owns which book (user_books) and the books table."""

    def __init__(self, owners, books=("b1",)):
        self.owners = set(owners)
        self.books = set(books)
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
        s = " ".join(sql.split())
        self._rows = []
        if s.startswith("SELECT id FROM books"):
            self._rows = [(params[0],)] if params[0] in self.books else []
        elif s.startswith("DELETE FROM user_books WHERE user_id"):
            pair = (params[0], params[1])
            self.rowcount = int(pair in self.owners)
            self.owners.discard(pair)
        elif s.startswith("SELECT 1 FROM user_books") or s.startswith("SELECT user_id FROM user_books"):
            self._rows = [(u,) for u, b in self.owners if b == params[0]]
        elif s.startswith("DELETE FROM user_books WHERE book_id"):
            self.owners = {(u, b) for u, b in self.owners if b != params[0]}
        elif s.startswith("DELETE FROM books"):
            self.rowcount = int(params[0] in self.books)
            self.books.discard(params[0])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _remove(monkeypatch, db, user_id):
    monkeypatch.setattr(pg_books.redis_cache, "invalidate_tags", lambda *tags: True)
    return pg_books.PostgresBookService(store=db).delete_book_for_user_sync("b1", user_id)


def test_removing_shared_book_keeps_it_for_other_owner(monkeypatch):
    db = _DeleteDb({("alice", "b1"), ("bob", "b1")})

    assert _remove(monkeypatch, db, "alice") is True
    assert db.owners == {("bob", "b1")}
    assert db.books == {"b1"}


def test_removing_last_copy_deletes_catalog_row(monkeypatch):
    db = _DeleteDb({("alice", "b1")})

    assert _remove(monkeypatch, db, "alice") is True
    assert db.owners == set()
    assert db.books == set()


def test_cannot_delete_book_outside_own_library(monkeypatch):
    db = _DeleteDb({("bob", "b1")})

    assert _remove(monkeypatch, db, "alice") is False
    assert db.owners == {("bob", "b1")}
    assert db.books == {"b1"}


def test_facade_delete_is_scoped_to_the_user():
    calls = []
    books = SimpleNamespace(
        delete_book_for_user_sync=lambda book_id, user_id: calls.append((book_id, user_id)) or True,
        delete_book_sync=lambda book_id: calls.append(("everyone", book_id)) or True,
    )

    assert PostgresServiceFacade.delete_book_sync(SimpleNamespace(book_service=books), "b1", "alice") is True
    assert calls == [("b1", "alice")]


# --- Finding 5: deactivated users stayed logged in; admin lockout ------------

import app as app_package  # noqa: E402
import app.auth_settings as auth_settings  # noqa: E402
from app.domain.models import User  # noqa: E402
from app.services.postgres_user_service import PostgresUserService  # noqa: E402


def _load_user(monkeypatch, user):
    monkeypatch.setattr(persistence_state, "get_active_backend", lambda: "postgres")
    facade = SimpleNamespace(user_service=SimpleNamespace(get_user_by_id_sync=lambda _uid: user))
    monkeypatch.setattr("app.services._get_postgres_facade", lambda: facade)
    return app_package.load_user("u1")


def test_deactivated_user_session_is_not_loaded(monkeypatch):
    assert _load_user(monkeypatch, User(id="u1", username="x", is_active=False)) is None


def test_active_user_session_still_loads(monkeypatch):
    user = User(id="u1", username="x", is_active=True)
    assert _load_user(monkeypatch, user) is user


def test_admin_count_ignores_deactivated_admins():
    queries = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=None):
            queries.append(sql)

        def fetchone(self):
            return (1,)

    class _Store:
        @contextmanager
        def connection(self):
            yield SimpleNamespace(cursor=_Cursor)

    assert PostgresUserService(store=_Store()).get_admin_count_sync() == 1
    assert "is_active" in queries[0]


def _toggle_active(monkeypatch, target):
    updated = []
    _as_user(monkeypatch, auth_settings, ADMIN)
    monkeypatch.setattr(auth_settings.user_service, "get_user_by_id_sync", lambda _uid: target)
    monkeypatch.setattr(auth_settings.user_service, "update_user_sync", lambda u: updated.append(u.id))
    monkeypatch.setattr(
        "app.services.simple_backup_service.get_simple_backup_service",
        lambda: SimpleNamespace(create_backup=lambda **_kw: None),
    )
    monkeypatch.setattr(auth_settings, "render_template", lambda *_a, **_kw: "partial")
    data = {"user_id": target.id, "action": "toggle_active"}
    with _view_app().test_request_context("/settings/partial/user_edit", method="POST", data=data):
        auth_settings.settings_server_partial("user_edit")
    return updated


def test_admin_cannot_deactivate_own_account(monkeypatch):
    me = SimpleNamespace(id="admin1", is_active=True, is_admin=True)

    assert _toggle_active(monkeypatch, me) == []
    assert me.is_active is True


def test_admin_can_still_deactivate_another_user(monkeypatch):
    other = SimpleNamespace(id="u2", is_active=True, is_admin=False)

    assert _toggle_active(monkeypatch, other) == ["u2"]
    assert other.is_active is False
