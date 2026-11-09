"""JSON endpoints for reading progress. PostgreSQL only: on Kuzu every route answers 404 (D-007)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_required

from app.services import get_progress_service
from app.services.postgres_progress_service import ProgressConflictError

progress_bp = Blueprint("progress", __name__, url_prefix="/progress")

_MAX_JSON_BYTES = 1024 * 1024
_UNSTORABLE_TEXT = re.compile("[\x00\ud800-\udfff]")  # PostgreSQL text has no NUL, and UTF-8 has no unpaired surrogates
_HTTP_SOURCES = {"modal", "quick"}  # import and backfill are server-side only
_EDITABLE_FIELDS = {"comment", "minutes_read", "journal_entry_id"}


def add_json_errors(blueprint: Blueprint) -> None:
    """Answer the services' validation and not-found errors as JSON. Subclasses such as KeyError are bugs and stay 500s."""

    @blueprint.before_request
    def reject_nul_in_url_ids():
        if any("\x00" in value for value in request.view_args.values()):
            raise LookupError("Not found")

    @blueprint.errorhandler(ValueError)
    def bad_request(error):
        if type(error) is not ValueError:
            raise error
        return jsonify({"error": str(error)}), 400

    @blueprint.errorhandler(LookupError)
    def not_found(error):
        if type(error) is not LookupError:
            raise error
        return jsonify({"error": str(error)}), 404


def _has_unstorable_text(value: Any) -> bool:
    if isinstance(value, str):
        return _UNSTORABLE_TEXT.search(value) is not None
    if isinstance(value, dict):
        value = list(value.values())
    return isinstance(value, list) and any(map(_has_unstorable_text, value))


def json_object() -> dict[str, Any]:
    # Checked by declared length: CSRF has already opened the stream, so a per-request Flask limit would not apply.
    if request.content_length is None or request.content_length > _MAX_JSON_BYTES:
        raise ValueError("Send a JSON object of at most 1 MB")
    try:
        data = request.get_json(silent=True)
        unstorable = _has_unstorable_text(data)
    except RecursionError:  # nesting deep enough to fit under the size limit
        raise ValueError("Send a JSON object") from None
    if not isinstance(data, dict):
        raise ValueError("Send a JSON object")
    if unstorable:
        raise ValueError("Text cannot contain NUL or unpaired surrogate characters")
    return data


add_json_errors(progress_bp)


@progress_bp.errorhandler(ProgressConflictError)
def conflict(error):
    return jsonify({"error": str(error)}), 409


def _service():
    service = get_progress_service()
    if service is None:
        raise LookupError("Not found")
    return service


def _moment(value: Any, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be an ISO 8601 date and time with a UTC offset") from None


def _journal_entry_id(value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("Journal entry id must be text")
    return value


def _position(state: dict[str, Any]) -> dict[str, Any]:
    return {key: state[key] for key in ("current_page", "total_pages", "percent")}


@progress_bp.route("/<book_id>", methods=["POST"])
@login_required
def log_progress(book_id: str):
    service = _service()
    data = json_object()
    source = data.get("source", "modal")
    if not isinstance(source, str) or source not in _HTTP_SOURCES:
        raise ValueError("Source must be modal or quick")
    result = service.log_progress_sync(
        user_id=str(current_user.id),
        book_id=book_id,
        current_page=data.get("current_page"),
        position_percent=data.get("position_percent"),
        occurred_at=_moment(data.get("occurred_at"), "Date read"),
        minutes_read=data.get("minutes_read"),
        comment=data.get("comment"),
        journal_entry_id=_journal_entry_id(data.get("journal_entry_id")),
        new_journal=data.get("new_journal"),
        finish_date=_moment(data.get("finish_date"), "Finish date"),
        total_pages=data.get("total_pages"),
        source=source,
    )
    return jsonify(result)


@progress_bp.route("/<book_id>", methods=["GET"])
@login_required
def get_progress(book_id: str):
    service = _service()
    user_id = str(current_user.id)
    state = service.get_book_progress_sync(user_id, book_id)
    updates = service.list_book_updates_sync(user_id, book_id, limit=50)
    return jsonify({**_position(state), "status": state["status"], "updates": updates})


@progress_bp.route("/update/<update_id>", methods=["PATCH"])
@login_required
def edit_update(update_id: str):
    service = _service()
    data = json_object()
    # Checked here too: a body key named user_id would otherwise collide with the service's arguments.
    if set(data) - _EDITABLE_FIELDS:
        raise ValueError("Only comment, minutes_read and journal_entry_id can be changed")
    if "journal_entry_id" in data:
        _journal_entry_id(data["journal_entry_id"])
    updated = service.update_progress_update_sync(update_id, str(current_user.id), **data)
    if updated is None:
        raise LookupError("Update not found")
    return jsonify(updated)


@progress_bp.route("/update/<update_id>", methods=["DELETE"])
@login_required
def delete_update(update_id: str):
    state = _service().delete_progress_update_sync(update_id, str(current_user.id))
    if state is None:
        raise LookupError("Update not found")
    return jsonify({"ok": True, "progress": _position(state), "status": state["status"]})
