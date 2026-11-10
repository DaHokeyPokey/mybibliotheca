"""Opt-in PostgreSQL backup/restore integration tests.

Set POSTGRES_TEST_DSN to a disposable PostgreSQL server, using a role that can
create databases. The special value "compose" uses the standard POSTGRES_*
variables provided by the local Compose service. A restore replaces every
object in its database, so each test creates and drops a database of its own
and never writes to the configured one.
"""

from __future__ import annotations

import os
import uuid
import zipfile
from datetime import datetime, timezone

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.infrastructure.postgres_store import PostgresStore
from app.services import postgres_backup_service
from app.services.postgres_backup_service import PostgresBackupService, PostgresBackupError

@pytest.fixture
def own_database():
    """A new database on the configured server, dropped afterwards."""
    configured_dsn = os.getenv("POSTGRES_TEST_DSN")
    if not configured_dsn:
        pytest.skip("Set POSTGRES_TEST_DSN to a disposable PostgreSQL server")
    server_dsn = PostgresStore().dsn if configured_dsn == "compose" else configured_dsn
    name = f"backup_e2e_{uuid.uuid4().hex}"
    with psycopg.connect(server_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(server_dsn, dbname=name)
    finally:
        with psycopg.connect(server_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_postgres_backup_restore_round_trip_and_rejects_corrupt_archive(monkeypatch, tmp_path, own_database):
    store = PostgresStore(own_database)
    backup_dir = tmp_path / "backups"
    covers_dir = tmp_path / "covers"
    uploads_dir = tmp_path / "uploads"
    covers_dir.mkdir()
    uploads_dir.mkdir()
    (covers_dir / "cover.txt").write_text("original cover", encoding="utf-8")
    (uploads_dir / "import.txt").write_text("original upload", encoding="utf-8")
    (tmp_path / "ai_config.json").write_text('{"provider":"original"}', encoding="utf-8")
    (tmp_path / "backup_settings.json").write_text('{"enabled":true}', encoding="utf-8")
    monkeypatch.setattr(postgres_backup_service, "get_postgres_store", lambda: store)
    service = PostgresBackupService(backup_dir, covers_dir, uploads_dir)

    try:
        store.initialize_schema()
        with store.connection() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE TABLE backup_restore_probe (value text NOT NULL)")
            cursor.execute("INSERT INTO backup_restore_probe VALUES ('before')")
        backup_path, manifest = service.create_backup(
            name="round-trip", description="integration test", backup_id="backup-test-1",
            timestamp=datetime.now(timezone.utc), reason="test",
        )

        with store.connection() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE backup_restore_probe SET value='after'")
        (covers_dir / "cover.txt").write_text("modified cover", encoding="utf-8")
        (covers_dir / "extra.txt").write_text("remove this on restore", encoding="utf-8")
        (uploads_dir / "import.txt").write_text("modified upload", encoding="utf-8")
        (tmp_path / "ai_config.json").write_text('{"provider":"modified"}', encoding="utf-8")

        result = service.restore_backup(backup_path, backup_id="backup-test-1")
        assert result["database"] == manifest["database"]
        with store.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT value FROM backup_restore_probe")
            assert cursor.fetchone()[0] == "before"
        assert (covers_dir / "cover.txt").read_text(encoding="utf-8") == "original cover"
        assert not (covers_dir / "extra.txt").exists()
        assert (uploads_dir / "import.txt").read_text(encoding="utf-8") == "original upload"
        assert (tmp_path / "ai_config.json").read_text(encoding="utf-8") == '{"provider":"original"}'
        assert (tmp_path / "backup_settings.json").read_text(encoding="utf-8") == '{"enabled":true}'

        corrupt_backup = backup_dir / "corrupt.zip"
        with zipfile.ZipFile(corrupt_backup, "w") as archive:
            archive.writestr("backup_metadata.json", "{}")
        with pytest.raises(PostgresBackupError):
            service.restore_backup(corrupt_backup, backup_id="corrupt")
        with store.connection() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT value FROM backup_restore_probe")
            assert cursor.fetchone()[0] == "before"
        assert (covers_dir / "cover.txt").read_text(encoding="utf-8") == "original cover"
    finally:
        store.close()


@pytest.mark.parametrize("schema", [None, "library"], ids=["default-schema", "schema-from-the-dsn"])
def test_a_backup_from_before_migration_7_restores_on_the_upgraded_schema(monkeypatch, tmp_path, own_database, schema):
    if schema:
        with psycopg.connect(own_database, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    store = PostgresStore(make_conninfo(own_database, options=f"-c search_path={schema},public") if schema else own_database)
    covers_dir = tmp_path / "covers"
    uploads_dir = tmp_path / "uploads"
    covers_dir.mkdir()
    uploads_dir.mkdir()
    monkeypatch.setattr(postgres_backup_service, "get_postgres_store", lambda: store)
    service = PostgresBackupService(tmp_path / "backups", covers_dir, uploads_dir)

    def run(*statements):
        with store.transaction() as connection, connection.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    def value(query):
        with store.connection() as connection, connection.cursor() as cursor:
            cursor.execute(query)
            return cursor.fetchone()[0]

    try:
        store.initialize_schema()
        run(
            "DROP TABLE progress_updates, journal_entries",
            "DELETE FROM app_schema_migrations WHERE version >= 7",
            "INSERT INTO users (id, username, email, password_hash) VALUES ('v6-user', 'v6-user', 'v6@example.test', 'x')",
            "INSERT INTO books (id, title) VALUES ('v6-book', 'Before the upgrade')",
        )
        backup_path, _manifest = service.create_backup(
            name="before-v7", description="integration test", backup_id="backup-before-v7",
            timestamp=datetime.now(timezone.utc), reason="test",
        )

        store.initialize_schema()
        run(
            "INSERT INTO books (id, title) VALUES ('later-book', 'After the upgrade')",
            "INSERT INTO journal_entries (id, user_id, book_id, title) VALUES ('j1', 'v6-user', 'v6-book', 'After')",
            "INSERT INTO progress_updates (id, user_id, book_id, occurred_at, journal_entry_id) VALUES ('p1', 'v6-user', 'v6-book', now(), 'j1')",
            # Unknown to the archive and to the restore, so its foreign key makes the restore fail part-way.
            "CREATE TABLE restore_blocker (user_id TEXT REFERENCES users(id))",
        )

        with pytest.raises(PostgresBackupError):
            service.restore_backup(backup_path, backup_id="backup-before-v7")
        assert value("SELECT count(*) FROM progress_updates") == 1, "a failed restore must roll back its cleanup"
        assert value("SELECT count(*) FROM books WHERE id='later-book'") == 1

        run("DROP TABLE restore_blocker")
        service.restore_backup(backup_path, backup_id="backup-before-v7")

        assert value("SELECT count(*) FROM books WHERE id='v6-book'") == 1
        assert value("SELECT count(*) FROM books WHERE id='later-book'") == 0
        assert value(f"SELECT to_regclass('{schema or 'public'}.progress_updates')") is None
        assert value("SELECT max(version) FROM app_schema_migrations") == 6

        store.initialize_schema()  # what the restart after a restore does
        assert value("SELECT count(*) FROM progress_updates") == 0
        assert value("SELECT max(version) FROM app_schema_migrations") >= 7
    finally:
        store.close()
