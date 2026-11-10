"""Settings pages no longer echo stored secrets, so a blank secret field on
submit must keep the saved value instead of wiping it."""
import sys
from pathlib import Path

from flask import Flask
from flask_login import LoginManager

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import admin as admin_module
from app import auth as auth_module


class _TestAdmin:
    id = "secret-fields-admin"
    is_admin = True
    is_active = True
    is_authenticated = True
    is_anonymous = False

    def get_id(self):
        return self.id


def _client(monkeypatch, view):
    app = Flask(__name__)
    app.secret_key = "test-only"
    app.config["WTF_CSRF_ENABLED"] = False
    login_manager = LoginManager(app)
    login_manager.user_loader(lambda _user_id: _TestAdmin())
    monkeypatch.setattr(auth_module, "render_template", lambda *_a, **_k: "PARTIAL OK")
    monkeypatch.setattr(auth_module, "get_admin_settings_context", lambda: {})
    monkeypatch.setattr(auth_module, "load_system_config", lambda: {})
    app.add_url_rule("/save", view_func=view, methods=["POST"])
    client = app.test_client()
    with client.session_transaction() as session:
        session["_user_id"] = _TestAdmin.id
        session["_fresh"] = True
    return client


def _post_smtp(monkeypatch, password):
    saved = {}
    monkeypatch.setattr(admin_module, "load_smtp_config", lambda: {"smtp_password": "stored-secret"})
    monkeypatch.setattr(auth_module, "load_smtp_config", lambda: {})
    monkeypatch.setattr(auth_module, "save_smtp_config", lambda cfg: saved.update(cfg) or True)
    client = _client(monkeypatch, auth_module.save_smtp_settings)
    resp = client.post("/save", data={"inline": "1", "smtp_server": "mail.example", "smtp_password": password})
    assert resp.status_code == 200
    return saved


def _post_ai(monkeypatch, key):
    saved = {}
    monkeypatch.setattr(admin_module, "load_ai_config", lambda: {"OPENAI_API_KEY": "sk-stored"})
    monkeypatch.setattr(auth_module, "load_ai_config", lambda: {})
    monkeypatch.setattr(auth_module, "save_ai_config", lambda cfg: saved.update(cfg) or True)
    client = _client(monkeypatch, auth_module.save_ai_settings)
    resp = client.post("/save", data={"inline": "1", "openai_api_key": key})
    assert resp.status_code == 200
    return saved


def test_blank_smtp_password_keeps_saved_password(monkeypatch):
    assert _post_smtp(monkeypatch, "")["smtp_password"] == "stored-secret"


def test_new_smtp_password_replaces_saved_password(monkeypatch):
    assert _post_smtp(monkeypatch, "new-secret")["smtp_password"] == "new-secret"


def test_blank_openai_key_keeps_saved_key(monkeypatch):
    assert _post_ai(monkeypatch, "")["OPENAI_API_KEY"] == "sk-stored"


def test_new_openai_key_replaces_saved_key(monkeypatch):
    assert _post_ai(monkeypatch, "sk-new")["OPENAI_API_KEY"] == "sk-new"
