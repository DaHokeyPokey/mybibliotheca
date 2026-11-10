"""Progress and journal endpoints: login, CSRF, error mapping and per-user access."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin
from flask_wtf.csrf import CSRFProtect, generate_csrf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend_stub import kuzu_backend, kuzu_backend_for_module  # noqa: E402,F401 - autouse fixture

with kuzu_backend():
    import app.routes as routes
    from app.routes import journal_routes, progress_routes
    from app.services.postgres_progress_service import ProgressConflictError

AS_ALICE = {"X-User": "alice"}
STATE = {"current_page": 40, "total_pages": 300, "percent": 13.3, "status": "reading", "last_update": None}
ENDPOINTS = [
    ("POST", "/progress/b1"),
    ("GET", "/progress/b1"),
    ("PATCH", "/progress/update/u1"),
    ("DELETE", "/progress/update/u1"),
    ("GET", "/journal/b1"),
    ("POST", "/journal/b1"),
    ("GET", "/journal/entry/e1"),
    ("PUT", "/journal/entry/e1"),
    ("DELETE", "/journal/entry/e1"),
]
WRITES = [endpoint for endpoint in ENDPOINTS if endpoint[0] != "GET"]
JSON_BODIES = [(method, path) for method, path in WRITES if method != "DELETE"]


class _Fake:
    """Service stand-in: records calls and returns or raises what each method was given."""

    def __init__(self, **results):
        self.results = results
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            result = self.results.get(name)
            if isinstance(result, Exception):
                raise result
            return result

        return call


class _User(UserMixin):
    def __init__(self, user_id):
        self.id = user_id


def _client(monkeypatch, progress=None, journal=None, csrf=False):
    app = Flask(__name__)
    app.secret_key = "test-only"
    app.config["TESTING"] = True
    manager = LoginManager(app)
    manager.request_loader(lambda request: _User(request.headers["X-User"]) if "X-User" in request.headers else None)
    if csrf:
        CSRFProtect(app)
        app.add_url_rule("/token", "token", generate_csrf)
    app.register_blueprint(progress_routes.progress_bp)
    app.register_blueprint(journal_routes.journal_bp)
    monkeypatch.setattr(progress_routes, "get_progress_service", lambda: progress)
    monkeypatch.setattr(journal_routes, "get_progress_service", lambda: progress)
    monkeypatch.setattr(journal_routes, "get_journal_service", lambda: journal)
    return app.test_client()


# --- Access -----------------------------------------------------------------------------

@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_anonymous_requests_are_rejected(monkeypatch, method, path):
    progress, journal = _Fake(), _Fake()

    response = _client(monkeypatch, progress, journal).open(path, method=method, json={})

    assert response.status_code == 401
    assert progress.calls == [] and journal.calls == []


@pytest.mark.parametrize("method, path", WRITES)
def test_writes_without_a_csrf_token_are_rejected(monkeypatch, method, path):
    progress, journal = _Fake(), _Fake()

    response = _client(monkeypatch, progress, journal, csrf=True).open(path, method=method, json={}, headers=AS_ALICE)

    assert response.status_code == 400
    assert progress.calls == [] and journal.calls == []


def test_a_write_with_a_csrf_token_is_accepted(monkeypatch):
    progress = _Fake(log_progress_sync={"finished": False})
    client = _client(monkeypatch, progress, _Fake(), csrf=True)
    token = client.get("/token", headers=AS_ALICE).get_data(as_text=True)

    response = client.post("/progress/b1", json={"current_page": 5}, headers={**AS_ALICE, "X-CSRFToken": token})

    assert response.status_code == 200


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_kuzu_gets_404(monkeypatch, method, path):
    response = _client(monkeypatch).open(path, method=method, json={}, headers=AS_ALICE)

    assert response.status_code == 404
    assert "error" in response.get_json()


def test_routes_are_registered_on_the_application_without_conflicts():
    app = Flask(__name__)
    routes.register_blueprints(app)

    registered = {(rule.rule, method) for rule in app.url_map.iter_rules() for method in rule.methods}
    assert {
        ("/progress/<book_id>", "POST"), ("/progress/<book_id>", "GET"),
        ("/progress/update/<update_id>", "PATCH"), ("/progress/update/<update_id>", "DELETE"),
        ("/journal/<book_id>", "GET"), ("/journal/<book_id>", "POST"),
        ("/journal/entry/<entry_id>", "GET"), ("/journal/entry/<entry_id>", "PUT"), ("/journal/entry/<entry_id>", "DELETE"),
    } <= registered


# --- Request bodies ---------------------------------------------------------------------

@pytest.mark.parametrize("method, path", JSON_BODIES)
def test_a_body_that_is_not_a_json_object_is_rejected(monkeypatch, method, path):
    progress = _Fake(get_book_progress_sync=STATE)
    client = _client(monkeypatch, progress, _Fake())

    assert client.open(path, method=method, data="not json", headers=AS_ALICE).status_code == 400
    assert client.open(path, method=method, json=[1, 2], headers=AS_ALICE).status_code == 400


def test_an_oversized_body_is_rejected_with_csrf_on(monkeypatch):
    progress = _Fake()
    client = _client(monkeypatch, progress, _Fake(), csrf=True)
    token = client.get("/token", headers=AS_ALICE).get_data(as_text=True)

    response = client.post("/progress/b1", json={"comment": "x" * (2 * 1024 * 1024)}, headers={**AS_ALICE, "X-CSRFToken": token})

    assert response.status_code == 400 and "1 MB" in response.get_json()["error"]
    assert progress.calls == []


@pytest.mark.parametrize(
    "body",
    ["[" * 100_000, '{"comment": ' + "[" * 2000 + "]" * 2000 + "}"],
    ids=["unclosed", "inside-an-object"],
)
def test_json_nested_too_deeply_is_rejected(monkeypatch, body):
    progress = _Fake(log_progress_sync={})

    response = _client(monkeypatch, progress).post("/progress/b1", data=body, content_type="application/json", headers=AS_ALICE)

    assert response.status_code == 400
    assert progress.calls == []


@pytest.mark.parametrize("bad", ["\ud800", "\udfff x", "a\x00b"], ids=["high-surrogate", "low-surrogate", "nul"])
@pytest.mark.parametrize(
    "method, path, field",
    [
        ("POST", "/progress/b1", "comment"),
        ("POST", "/progress/b1", "journal_entry_id"),
        ("PATCH", "/progress/update/u1", "comment"),
        ("POST", "/journal/b1", "title"),
        ("POST", "/journal/b1", "body_html"),
        ("PUT", "/journal/entry/e1", "title"),
        ("PUT", "/journal/entry/e1", "body_html"),
    ],
)
def test_text_that_cannot_be_stored_is_rejected(monkeypatch, method, path, field, bad):
    progress, journal = _Fake(get_book_progress_sync=STATE), _Fake()

    response = _client(monkeypatch, progress, journal).open(path, method=method, json={field: bad}, headers=AS_ALICE)

    assert response.status_code == 400
    assert {call[0] for call in progress.calls + journal.calls} <= {"get_book_progress_sync"}


def test_text_that_cannot_be_stored_is_rejected_inside_nested_values(monkeypatch):
    progress = _Fake(log_progress_sync={})

    response = _client(monkeypatch, progress).post("/progress/b1", json={"current_page": 5, "new_journal": {"title": "\ud800"}}, headers=AS_ALICE)

    assert response.status_code == 400
    assert progress.calls == []


def test_emoji_sent_as_a_surrogate_pair_are_accepted(monkeypatch):
    progress = _Fake(log_progress_sync={})
    body = '{"current_page": 5, "comment": "\\ud83d\\ude00"}'

    response = _client(monkeypatch, progress).post("/progress/b1", data=body, content_type="application/json", headers=AS_ALICE)

    assert response.status_code == 200
    assert progress.calls[0][2]["comment"] == "\U0001F600"


@pytest.mark.parametrize("method, path", ENDPOINTS)
def test_a_nul_in_a_url_id_is_not_found(monkeypatch, method, path):
    progress, journal = _Fake(get_book_progress_sync=STATE), _Fake()

    response = _client(monkeypatch, progress, journal).open(f"{path}%00", method=method, json={}, headers=AS_ALICE)

    assert response.status_code == 404 and "error" in response.get_json()
    assert progress.calls == [] and journal.calls == []


# --- Progress ---------------------------------------------------------------------------

def test_posting_progress_passes_the_fields_and_the_logged_in_user(monkeypatch):
    progress = _Fake(log_progress_sync={"finished": False})
    body = {"current_page": 50, "minutes_read": 15, "comment": "good", "journal_entry_id": "j1", "total_pages": 300,
            "new_journal": None, "position_percent": None, "source": "quick"}

    response = _client(monkeypatch, progress).post("/progress/b1", json=body, headers=AS_ALICE)

    assert response.status_code == 200 and response.get_json() == {"finished": False}
    name, args, kwargs = progress.calls[0]
    assert name == "log_progress_sync" and args == ()
    assert kwargs == {"user_id": "alice", "book_id": "b1", "current_page": 50, "position_percent": None, "occurred_at": None,
                      "minutes_read": 15, "comment": "good", "journal_entry_id": "j1", "new_journal": None,
                      "finish_date": None, "total_pages": 300, "source": "quick"}


def test_a_user_id_in_the_body_cannot_replace_the_logged_in_user(monkeypatch):
    progress = _Fake(log_progress_sync={})
    _client(monkeypatch, progress).post("/progress/b1", json={"current_page": 5, "user_id": "bob"}, headers=AS_ALICE)
    assert progress.calls[0][2]["user_id"] == "alice"


def test_source_defaults_to_modal(monkeypatch):
    progress = _Fake(log_progress_sync={})
    _client(monkeypatch, progress).post("/progress/b1", json={"current_page": 5}, headers=AS_ALICE)
    assert progress.calls[0][2]["source"] == "modal"


@pytest.mark.parametrize("source", ["import", "backfill", "admin", 5, ["modal"], {"name": "modal"}])
def test_server_side_sources_are_rejected_over_http(monkeypatch, source):
    progress = _Fake(log_progress_sync={})

    response = _client(monkeypatch, progress).post("/progress/b1", json={"current_page": 5, "source": source}, headers=AS_ALICE)

    assert response.status_code == 400
    assert progress.calls == []


def test_dates_keep_the_senders_utc_offset(monkeypatch):
    progress = _Fake(log_progress_sync={})
    body = {"current_page": 5, "occurred_at": "2026-01-14T23:30:00-05:00", "finish_date": "2026-01-14T23:45:00Z"}

    _client(monkeypatch, progress).post("/progress/b1", json=body, headers=AS_ALICE)

    kwargs = progress.calls[0][2]
    assert kwargs["occurred_at"] == datetime(2026, 1, 14, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert kwargs["occurred_at"].date().day == 14
    assert kwargs["finish_date"] == datetime(2026, 1, 14, 23, 45, tzinfo=timezone.utc)


@pytest.mark.parametrize("field", ["occurred_at", "finish_date"])
@pytest.mark.parametrize("value", ["yesterday", 5])
def test_unreadable_dates_are_rejected(monkeypatch, field, value):
    progress = _Fake(log_progress_sync={})

    response = _client(monkeypatch, progress).post("/progress/b1", json={"current_page": 5, field: value}, headers=AS_ALICE)

    assert response.status_code == 400 and "error" in response.get_json()
    assert progress.calls == []


@pytest.mark.parametrize("value", [5, "a\u0000b"], ids=["number", "nul"])
def test_a_malformed_journal_id_is_rejected_when_posting(monkeypatch, value):
    progress = _Fake(log_progress_sync={})
    response = _client(monkeypatch, progress).post("/progress/b1", json={"current_page": 5, "journal_entry_id": value}, headers=AS_ALICE)
    assert response.status_code == 400
    assert progress.calls == []


def test_service_validation_errors_become_400_with_the_message(monkeypatch):
    progress = _Fake(log_progress_sync=ValueError("Current page cannot be more than 300"))

    response = _client(monkeypatch, progress).post("/progress/b1", json={"current_page": 500}, headers=AS_ALICE)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Current page cannot be more than 300"}


def test_another_users_book_is_not_found(monkeypatch):
    progress = _Fake(log_progress_sync=LookupError("Book is not in your library"), get_book_progress_sync=LookupError("Book is not in your library"))
    client = _client(monkeypatch, progress)

    assert client.post("/progress/b1", json={"current_page": 5}, headers=AS_ALICE).status_code == 404
    assert client.get("/progress/b1", headers=AS_ALICE).status_code == 404


def test_a_bug_is_not_reported_as_not_found_or_bad_input(monkeypatch):
    client = _client(monkeypatch, _Fake(get_book_progress_sync=KeyError("oops"), log_progress_sync=UnicodeError("oops")))

    with pytest.raises(KeyError):
        client.get("/progress/b1", headers=AS_ALICE)
    with pytest.raises(UnicodeError):
        client.post("/progress/b1", json={"current_page": 5}, headers=AS_ALICE)


def test_getting_progress_returns_the_position_and_recent_updates(monkeypatch):
    updates = [{"id": "u1"}]
    progress = _Fake(get_book_progress_sync=STATE, list_book_updates_sync=updates)

    response = _client(monkeypatch, progress).get("/progress/b1", headers=AS_ALICE)

    assert response.get_json() == {"current_page": 40, "total_pages": 300, "percent": 13.3, "status": "reading", "updates": updates}
    assert progress.calls[1] == ("list_book_updates_sync", ("alice", "b1"), {"limit": 50})


def test_patching_passes_only_the_allowed_fields(monkeypatch):
    progress = _Fake(update_progress_update_sync={"id": "u1"})

    response = _client(monkeypatch, progress).patch("/progress/update/u1", json={"comment": "x", "minutes_read": 5, "journal_entry_id": None}, headers=AS_ALICE)

    assert response.get_json() == {"id": "u1"}
    assert progress.calls[0] == ("update_progress_update_sync", ("u1", "alice"), {"comment": "x", "minutes_read": 5, "journal_entry_id": None})


@pytest.mark.parametrize("field", ["current_page", "occurred_at", "user_id", "update_id", "self"])
def test_patching_any_other_field_is_rejected(monkeypatch, field):
    progress = _Fake(update_progress_update_sync={"id": "u1"})

    response = _client(monkeypatch, progress).patch("/progress/update/u1", json={field: "x"}, headers=AS_ALICE)

    assert response.status_code == 400
    assert progress.calls == []


def test_patching_a_malformed_journal_id_is_rejected(monkeypatch):
    progress = _Fake(update_progress_update_sync={"id": "u1"})
    response = _client(monkeypatch, progress).patch("/progress/update/u1", json={"journal_entry_id": 5}, headers=AS_ALICE)
    assert response.status_code == 400
    assert progress.calls == []


def test_patching_another_users_update_is_not_found(monkeypatch):
    response = _client(monkeypatch, _Fake()).patch("/progress/update/u1", json={"comment": "x"}, headers=AS_ALICE)
    assert response.status_code == 404


def test_deleting_returns_the_books_new_position(monkeypatch):
    progress = _Fake(delete_progress_update_sync=STATE)

    response = _client(monkeypatch, progress).delete("/progress/update/u1", headers=AS_ALICE)

    assert response.get_json() == {"ok": True, "progress": {"current_page": 40, "total_pages": 300, "percent": 13.3}, "status": "reading"}
    assert progress.calls[0] == ("delete_progress_update_sync", ("u1", "alice"), {})


def test_deleting_an_older_update_is_a_conflict(monkeypatch):
    progress = _Fake(delete_progress_update_sync=ProgressConflictError("Only the latest update can be deleted"))

    response = _client(monkeypatch, progress).delete("/progress/update/u1", headers=AS_ALICE)

    assert response.status_code == 409
    assert response.get_json() == {"error": "Only the latest update can be deleted"}


def test_deleting_another_users_update_is_not_found(monkeypatch):
    assert _client(monkeypatch, _Fake()).delete("/progress/update/u1", headers=AS_ALICE).status_code == 404


# --- Journal ----------------------------------------------------------------------------

_NOT_IN_LIBRARY = LookupError("Book is not in your library")


def test_listing_a_books_journal_returns_the_entries(monkeypatch):
    entries = [{"id": "e1", "title": "T", "updated_at": "x", "linked_update_count": 2}]
    journal = _Fake(list_entries_for_book_sync=entries)

    response = _client(monkeypatch, _Fake(get_book_progress_sync=STATE), journal).get("/journal/b1", headers=AS_ALICE)

    assert response.get_json() == entries
    assert journal.calls[0] == ("list_entries_for_book_sync", ("b1", "alice"), {})


def test_listing_the_journal_of_a_book_outside_the_library_is_not_found(monkeypatch):
    journal = _Fake(list_entries_for_book_sync=[])

    response = _client(monkeypatch, _Fake(get_book_progress_sync=_NOT_IN_LIBRARY), journal).get("/journal/b1", headers=AS_ALICE)

    assert response.status_code == 404
    assert journal.calls == []


def test_creating_an_entry_passes_the_fields_and_the_logged_in_user(monkeypatch):
    journal = _Fake(create_entry_sync={"id": "e1"})

    response = _client(monkeypatch, _Fake(get_book_progress_sync=STATE), journal).post(
        "/journal/b1", json={"title": "T", "body_html": "<p>x</p>", "user_id": "bob"}, headers=AS_ALICE)

    assert response.get_json() == {"id": "e1"}
    assert journal.calls[0] == ("create_entry_sync", ("alice", "T", "<p>x</p>", "b1"), {})


def test_creating_an_entry_without_a_body_stores_an_empty_one(monkeypatch):
    journal = _Fake(create_entry_sync={"id": "e1"})
    _client(monkeypatch, _Fake(get_book_progress_sync=STATE), journal).post("/journal/b1", json={"title": "T"}, headers=AS_ALICE)
    assert journal.calls[0][1] == ("alice", "T", "", "b1")


def test_creating_an_entry_for_a_book_outside_the_library_is_not_found(monkeypatch):
    journal = _Fake(create_entry_sync={"id": "e1"})

    response = _client(monkeypatch, _Fake(get_book_progress_sync=_NOT_IN_LIBRARY), journal).post("/journal/b1", json={"title": "T"}, headers=AS_ALICE)

    assert response.status_code == 404
    assert journal.calls == []


def test_invalid_journal_input_becomes_400_with_the_message(monkeypatch):
    journal = _Fake(create_entry_sync=ValueError("Title must be 1 to 200 characters of text"))

    response = _client(monkeypatch, _Fake(get_book_progress_sync=STATE), journal).post("/journal/b1", json={"title": ""}, headers=AS_ALICE)

    assert response.status_code == 400
    assert response.get_json() == {"error": "Title must be 1 to 200 characters of text"}


def test_reading_an_entry_returns_it(monkeypatch):
    journal = _Fake(get_entry_sync={"id": "e1", "body_html": "<p>x</p>"})

    response = _client(monkeypatch, None, journal).get("/journal/entry/e1", headers=AS_ALICE)

    assert response.get_json()["body_html"] == "<p>x</p>"
    assert journal.calls[0] == ("get_entry_sync", ("e1", "alice"), {})


def test_updating_an_entry_passes_the_fields(monkeypatch):
    journal = _Fake(update_entry_sync={"id": "e1"})

    response = _client(monkeypatch, None, journal).put("/journal/entry/e1", json={"body_html": "<p>y</p>"}, headers=AS_ALICE)

    assert response.get_json() == {"id": "e1"}
    assert journal.calls[0] == ("update_entry_sync", ("e1", "alice"), {"title": None, "body_html": "<p>y</p>"})


def test_updating_an_entry_with_nothing_to_change_is_rejected(monkeypatch):
    journal = _Fake(update_entry_sync={"id": "e1"})
    response = _client(monkeypatch, None, journal).put("/journal/entry/e1", json={}, headers=AS_ALICE)
    assert response.status_code == 400
    assert journal.calls == []


def test_deleting_an_entry_reports_success(monkeypatch):
    journal = _Fake(delete_entry_sync=True)
    response = _client(monkeypatch, None, journal).delete("/journal/entry/e1", headers=AS_ALICE)
    assert response.get_json() == {"ok": True}
    assert journal.calls[0] == ("delete_entry_sync", ("e1", "alice"), {})


@pytest.mark.parametrize(
    "method, kwargs",
    [("GET", {}), ("PUT", {"json": {"title": "T"}}), ("DELETE", {})],
)
def test_another_users_entry_is_not_found(monkeypatch, method, kwargs):
    journal = _Fake(get_entry_sync=None, update_entry_sync=None, delete_entry_sync=False)

    response = _client(monkeypatch, None, journal).open("/journal/entry/e1", method=method, headers=AS_ALICE, **kwargs)

    assert response.status_code == 404
    assert "error" in response.get_json()
