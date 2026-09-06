"""Server-side SQLAlchemy sessions without schema work during app startup.

Flask-Session 0.8 creates its SQL table from the request process every time
the application starts.  That is convenient for development, but it adds a
database round trip (and DDL) to every serverless/container cold start.  The
table is part of Chatboc's Alembic schema instead, so runtime only binds the
session interface to the already-managed model.
"""

from __future__ import annotations

from typing import Optional

from flask import Flask
from flask_session import Session
from flask_session.base import ServerSideSessionInterface
from flask_session.defaults import Defaults
from flask_session.sqlalchemy.sqlalchemy import (
    SqlAlchemySessionInterface,
    create_session_model,
)
from flask_sqlalchemy import SQLAlchemy


class MigrationManagedSqlAlchemySessionInterface(SqlAlchemySessionInterface):
    """Flask-Session's SQLAlchemy interface with no implicit ``CREATE TABLE``.

    Persistence behavior is inherited unchanged.  Only constructor-time DDL
    is removed; deployments must apply the ``flask_sessions`` migration before
    serving traffic.
    """

    def __init__(
        self,
        app: Flask,
        client: SQLAlchemy,
        key_prefix: str = Defaults.SESSION_KEY_PREFIX,
        use_signer: bool = Defaults.SESSION_USE_SIGNER,
        permanent: bool = Defaults.SESSION_PERMANENT,
        sid_length: int = Defaults.SESSION_ID_LENGTH,
        serialization_format: str = Defaults.SESSION_SERIALIZATION_FORMAT,
        table: str = Defaults.SESSION_SQLALCHEMY_TABLE,
        sequence: Optional[str] = Defaults.SESSION_SQLALCHEMY_SEQUENCE,
        schema: Optional[str] = Defaults.SESSION_SQLALCHEMY_SCHEMA,
        bind_key: Optional[str] = Defaults.SESSION_SQLALCHEMY_BIND_KEY,
        cleanup_n_requests: Optional[int] = Defaults.SESSION_CLEANUP_N_REQUESTS,
    ) -> None:
        if not isinstance(client, SQLAlchemy):
            raise TypeError("SESSION_SQLALCHEMY must be a Flask-SQLAlchemy instance")

        self.app = app
        self.client = client
        self.sql_session_model = create_session_model(
            client,
            table,
            schema,
            bind_key,
            sequence,
        )
        ServerSideSessionInterface.__init__(
            self,
            app,
            key_prefix,
            use_signer,
            permanent,
            sid_length,
            serialization_format,
            cleanup_n_requests,
        )


def init_migration_managed_session(app: Flask, client: SQLAlchemy) -> None:
    """Install the configured session backend, avoiding runtime SQL DDL."""

    session_type = str(
        app.config.get("SESSION_TYPE", Defaults.SESSION_TYPE)
    ).strip().lower()
    if session_type != "sqlalchemy":
        Session().init_app(app)
        return

    app.session_interface = MigrationManagedSqlAlchemySessionInterface(
        app=app,
        client=client,
        key_prefix=app.config.get("SESSION_KEY_PREFIX", Defaults.SESSION_KEY_PREFIX),
        use_signer=app.config.get("SESSION_USE_SIGNER", Defaults.SESSION_USE_SIGNER),
        permanent=app.config.get("SESSION_PERMANENT", Defaults.SESSION_PERMANENT),
        sid_length=app.config.get("SESSION_ID_LENGTH", Defaults.SESSION_ID_LENGTH),
        serialization_format=app.config.get(
            "SESSION_SERIALIZATION_FORMAT",
            Defaults.SESSION_SERIALIZATION_FORMAT,
        ),
        table=app.config.get(
            "SESSION_SQLALCHEMY_TABLE",
            Defaults.SESSION_SQLALCHEMY_TABLE,
        ),
        sequence=app.config.get(
            "SESSION_SQLALCHEMY_SEQUENCE",
            Defaults.SESSION_SQLALCHEMY_SEQUENCE,
        ),
        schema=app.config.get(
            "SESSION_SQLALCHEMY_SCHEMA",
            Defaults.SESSION_SQLALCHEMY_SCHEMA,
        ),
        bind_key=app.config.get(
            "SESSION_SQLALCHEMY_BIND_KEY",
            Defaults.SESSION_SQLALCHEMY_BIND_KEY,
        ),
        cleanup_n_requests=app.config.get(
            "SESSION_CLEANUP_N_REQUESTS",
            Defaults.SESSION_CLEANUP_N_REQUESTS,
        ),
    )
