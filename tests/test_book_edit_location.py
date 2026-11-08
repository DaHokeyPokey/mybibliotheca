import sys
from pathlib import Path
from types import SimpleNamespace

from flask import Flask
from werkzeug.datastructures import MultiDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend_stub import kuzu_backend, kuzu_backend_for_module  # noqa: E402,F401 - autouse fixture

with kuzu_backend():  # importing route modules probes the active backend
    import app.routes.book_routes as br


class _Loc:
    def __init__(self):
        self.calls = []

    def set_book_location(self, uid, loc, user_id):
        self.calls.append((uid, loc, user_id))
        return True


def _run(monkeypatch, form):
    loc = _Loc()
    app = Flask(__name__)
    app.secret_key = "t"

    @app.route("/book/<uid>", endpoint="book.view_book_enhanced")
    def _v(uid):
        return "ok"

    monkeypatch.setattr("app.services.get_location_service", lambda: loc)
    monkeypatch.setattr(br, "current_user", SimpleNamespace(id="u1"))
    monkeypatch.setattr(br, "_get_changed_fields", lambda ub, f: {})
    with app.test_request_context():
        resp = br._handle_edit_book_post("b1", {"id": "b1"}, MultiDict(form))
    return loc, resp


def test_fast_path_persists_location(monkeypatch):
    loc, resp = _run(monkeypatch, {"title": "x", "location_id": "L1"})
    assert resp is not None
    assert loc.calls == [("b1", "L1", "u1")]


def test_fast_path_clears_location_when_empty(monkeypatch):
    loc, _ = _run(monkeypatch, {"location_id": ""})
    assert loc.calls == [("b1", None, "u1")]


def test_fast_path_ignores_absent_location(monkeypatch):
    loc, _ = _run(monkeypatch, {"title": "x"})
    assert loc.calls == []
