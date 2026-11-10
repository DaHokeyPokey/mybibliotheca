import logging
import sys
from pathlib import Path

from flask import Flask
from werkzeug.datastructures import MultiDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend_stub import kuzu_backend, kuzu_backend_for_module  # noqa: E402,F401 - autouse fixture

with kuzu_backend():  # importing route modules probes the active backend
    import app.onboarding_state as ob_state
    import app.onboarding_system as ob
    from app.utils import log_redaction as lr

PW = "Sup3r-Secret-Pw!987"


def test_redact_sensitive_masks_nested_and_multidict():
    data = {"admin": {"username": "a", "password": PW, "password_hash": "pbkdf2:x"}, "csrf_token": "tok", "n": [{"api_key": "k"}]}
    out = lr.redact_sensitive(data)
    assert PW not in str(out) and "pbkdf2" not in str(out)
    assert out["csrf_token"] == lr.REDACTED and out["n"][0]["api_key"] == lr.REDACTED
    assert out["admin"]["username"] == "a"
    assert PW in str(data)  # input not mutated
    md = lr.redact_sensitive(MultiDict({"password": PW, "username": "u"}))
    assert PW not in str(md) and md["username"] == "u"


def test_filter_scrubs_preformatted_messages(caplog):
    lr.install_redacting_filter()
    with caplog.at_level(logging.INFO):
        logging.getLogger("x").info("form {'username': 'u', 'password': '%s', 'password_hash': 'h4sh'}", PW)
    assert PW not in caplog.text and "h4sh" not in caplog.text
    assert "'username': 'u'" in caplog.text


def test_onboarding_path_logs_never_contain_password(caplog, capsys):
    lr.install_redacting_filter()
    app = Flask(__name__)
    app.secret_key = "t"
    admin = {"username": "admin", "email": "a@example.com", "password": PW, "password_confirm": PW}
    with caplog.at_level(logging.DEBUG), app.test_request_context("/", method="POST", data={"password": PW, "username": "admin"}):
        ob_state.update_onboarding_data({"admin": admin})
        ob_state.update_onboarding_data({"site_config": {"timezone": "UTC"}})
        ob_state.get_onboarding_data()
        ob_state.set_onboarding_step(2)
        from flask import request, session
        ob.logger.info(f"Session data: {lr.redact_sensitive(dict(session))}")
        ob.logger.info(f"POST: {lr.redact_sensitive(request.form)}")
    out = capsys.readouterr()
    assert PW not in caplog.text
    assert PW not in out.out + out.err


def test_no_unredacted_debug_dumps_in_source():
    import re
    root = Path(__file__).resolve().parent.parent / "app"
    bad = re.compile(r"(?:logger\.\w+|print)\(f?[\"'].*\{(?:dict\(session\)|dict\(request\.form\)|request\.form|user_data|admin_data|onboarding_data|data_options)\}")
    offenders = []
    for rel in ("onboarding_system.py", "onboarding_state.py", "routes/__init__.py", "services/kuzu_user_service.py"):
        for n, line in enumerate((root / rel).read_text().splitlines(), 1):
            if bad.search(line):
                offenders.append(f"{rel}:{n}")
    assert not offenders, offenders
