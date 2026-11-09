"""JSON endpoints for journal entries. PostgreSQL only: on Kuzu every route answers 404 (D-007)."""

from __future__ import annotations

from flask import Blueprint, jsonify
from flask_login import current_user, login_required

from app.services import get_journal_service, get_progress_service

from .progress_routes import add_json_errors, json_object

journal_bp = Blueprint("journal", __name__, url_prefix="/journal")
add_json_errors(journal_bp)


def _service():
    service = get_journal_service()
    if service is None:
        raise LookupError("Not found")
    return service


def _require_book_in_library(book_id: str) -> None:
    # Raises LookupError, so someone else's book is a 404 and not an empty list.
    get_progress_service().get_book_progress_sync(str(current_user.id), book_id)


@journal_bp.route("/<book_id>", methods=["GET"])
@login_required
def list_for_book(book_id: str):
    service = _service()
    _require_book_in_library(book_id)
    return jsonify(service.list_entries_for_book_sync(book_id, str(current_user.id)))


@journal_bp.route("/<book_id>", methods=["POST"])
@login_required
def create_entry(book_id: str):
    service = _service()
    _require_book_in_library(book_id)
    data = json_object()
    return jsonify(service.create_entry_sync(str(current_user.id), data.get("title"), data.get("body_html", ""), book_id))


@journal_bp.route("/entry/<entry_id>", methods=["GET"])
@login_required
def get_entry(entry_id: str):
    entry = _service().get_entry_sync(entry_id, str(current_user.id))
    if entry is None:
        raise LookupError("Journal entry not found")
    return jsonify(entry)


@journal_bp.route("/entry/<entry_id>", methods=["PUT"])
@login_required
def update_entry(entry_id: str):
    service = _service()
    data = json_object()
    if "title" not in data and "body_html" not in data:
        raise ValueError("Send a title or a body to change")
    entry = service.update_entry_sync(entry_id, str(current_user.id), title=data.get("title"), body_html=data.get("body_html"))
    if entry is None:
        raise LookupError("Journal entry not found")
    return jsonify(entry)


@journal_bp.route("/entry/<entry_id>", methods=["DELETE"])
@login_required
def delete_entry(entry_id: str):
    if not _service().delete_entry_sync(entry_id, str(current_user.id)):
        raise LookupError("Journal entry not found")
    return jsonify({"ok": True})
