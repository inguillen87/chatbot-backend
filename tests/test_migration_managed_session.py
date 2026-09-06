from __future__ import annotations

from unittest.mock import patch

from flask import Flask, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql.schema import Table

from utils.migration_managed_session import (
    MigrationManagedSqlAlchemySessionInterface,
    init_migration_managed_session,
)


def _session_app() -> tuple[Flask, SQLAlchemy]:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY="test-only-secret",
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SESSION_TYPE="sqlalchemy",
        SESSION_SQLALCHEMY_TABLE="flask_sessions",
        SESSION_COOKIE_NAME="chatboc_session",
        SESSION_COOKIE_SECURE=False,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    database = SQLAlchemy()
    database.init_app(app)
    return app, database


def test_sqlalchemy_session_initialization_performs_no_schema_ddl() -> None:
    app, database = _session_app()

    with patch.object(Table, "create", side_effect=AssertionError("runtime DDL")):
        init_migration_managed_session(app, database)

    assert isinstance(
        app.session_interface,
        MigrationManagedSqlAlchemySessionInterface,
    )


def test_migrated_session_table_preserves_server_side_round_trip() -> None:
    app, database = _session_app()
    init_migration_managed_session(app, database)

    with app.app_context():
        app.session_interface.sql_session_model.__table__.create(database.engine)

    @app.get("/set-session")
    def set_session():
        session["actor"] = "municipio-operador"
        return jsonify(ok=True)

    @app.get("/read-session")
    def read_session():
        return jsonify(actor=session.get("actor"))

    client = app.test_client()
    response = client.get("/set-session")
    assert response.status_code == 200
    assert "chatboc_session=" in response.headers["Set-Cookie"]

    response = client.get("/read-session")
    assert response.status_code == 200
    assert response.get_json() == {"actor": "municipio-operador"}
