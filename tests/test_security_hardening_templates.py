"""Static guards: settings pages never echo stored secrets, flagged innerHTML
sites escape data, JSON POSTs send the CSRF token, and add_book does not call
an undefined helper."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"


def _read(rel):
    return (TEMPLATES / rel).read_text(encoding="utf-8")


SECRET_TEMPLATES = [
    "admin/settings.html",
    "admin/partials/smtp_config.html",
    "settings/partials/server_ai.html",
    "settings/partials/server_smtp.html",
    "settings/partials/personal_abs.html",
    "settings/partials/server_opds.html",
]
SECRET_VALUE = re.compile(r'value="\{\{[^}]*(OPENAI_API_KEY|smtp_password|abs_api_key|password_value)')


def test_settings_pages_never_echo_stored_secrets():
    offenders = [rel for rel in SECRET_TEMPLATES if SECRET_VALUE.search(_read(rel))]
    assert offenders == [], offenders


def test_base_defines_shared_escape_helper_before_page_content():
    html = _read("base.html")
    assert "window.escapeHtml" in html
    helper = html.index("window.escapeHtml")
    assert helper < html.index("{% block content %}")
    # Every user needs the helper, so it must not sit inside any {% if %} (e.g. the admin-only banner).
    depth = 0
    for tag in re.finditer(r"\{%-?\s*(if|endif)\b", html[:helper]):
        depth += 1 if tag.group(1) == "if" else -1
    assert depth == 0, "window.escapeHtml is inside a Jinja {% if %} block"


def test_add_book_has_no_call_to_undefined_cover_picker():
    assert "fetchAndShowCoverCandidates" not in _read("add_book.html")


# Raw interpolations of user/book/person data that previously reached innerHTML.
UNESCAPED = {
    "base.html": ["${book.title}</div>", "${book.series}</div>", "by ${book.authors}</div>"],
    "add_book.html": [
        '<div class="name">${person.name}</div>',
        "${person.bio}</div>",
        "tag.innerHTML = `${name} <span",
        "${category.name}\n",
        "${duplicateResponse.book_data.title}</h6>",
        "<strong>${fileName}</strong>",
        "</strong> ${error.message}",
        "createNewPerson('${input.value.trim()}'",
        "createNewCategory('${input.value.trim()}'",
    ],
    "people.html": ["<li>${name}</li>", "<li>${person.name} <span"],
    "merge_persons.html": ["<h6>${name}</h6>", "${bio}</p>", "<li>${person.name} (${person.books})</li>"],
    "migrate_sqlite.html": ["data.user_info.username + ' ('", "'<strong>Error:</strong> ' + message"],
    "reading_logs/edit_log.html": ["<strong>${title}</strong>", "by ${author}</small>"],
}


def test_flagged_innerhtml_sites_escape_data():
    leftovers = {rel: [s for s in snippets if s in _read(rel)] for rel, snippets in UNESCAPED.items()}
    assert {rel: found for rel, found in leftovers.items() if found} == {}


def test_backup_settings_posts_send_csrf_token():
    for rel in ("admin/simple_backup.html", "settings.html", "settings/partials/data_backup_manager.html"):
        html = _read(rel)
        start = html.index("api_backup_settings")
        assert "X-CSRFToken" in html[start:start + 400], rel
    routes = (ROOT / "app" / "routes" / "simple_backup_routes.py").read_text(encoding="utf-8")
    view = routes[routes.index("@simple_backup_bp.route('/api/settings'"):routes.index("def api_backup_settings")]
    assert "csrf.exempt" not in view


def test_onboarding_confirmation_does_not_log_form_secrets():
    html = _read("onboarding/step5_confirmation.html")
    assert "CSRF token:" not in html
    assert "new FormData(form));" not in html
    assert "formData.entries()" not in html


def test_csrf_macro_does_not_shadow_global_csrf_token():
    assert "{% macro csrf_token(" not in _read("macros/forms.html")


def test_series_delete_sends_csrf_token():
    html = _read("series/series_detail.html")
    start = html.index("/delete`")
    assert "X-CSRFToken" in html[start - 300:start + 200]
