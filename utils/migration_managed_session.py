"""Server-side SQLAlchemy sessions without schema work during app startup.

Flask-Session 0.8 creates its SQL table from the request process every time
the application starts.  That is convenient for development, but it adds a
database round trip (and DDL) to every serverless/container cold start.  The
table is part of Chatboc's Alembic schema instead, so runtime only binds the
session interface to the already-managed model.
"""

from __future__ import annotations

from typing import Optional
from contextlib import contextmanager
from datetime import datetime, timezone
import secrets
import sys

from flask import Flask, has_request_context, request
from flask_session import Session
from flask_session.base import ServerSideSessionInterface
from flask_session.defaults import Defaults
from flask_session.sqlalchemy.sqlalchemy import (
    SqlAlchemySessionInterface,
    create_session_model,
)
from flask_sqlalchemy import SQLAlchemy
from flask.sessions import SessionInterface
from itsdangerous import want_bytes
from cutover_writer_fence import cutover_writer_fence_enabled
from global_writer_authority import global_writer_authority_enabled
from services.global_writer_authority import (
    HTTP_REQUEST_LEASE_ENVIRON,
    GlobalWriterAuthorityTransitionError,
    global_writer_authority_lease,
)


class RetirementIsolatedSessionInterface(SessionInterface):
    """Never open/save an ambient Flask session on proof-only retirement."""
    def __init__(self, wrapped):
        self.wrapped = wrapped

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def open_session(self, app, request):
        from services.auth_session_lifecycle import is_retirement_request
        if is_retirement_request(request):
            return self.wrapped.session_class(sid=secrets.token_urlsafe(32), permanent=False)
        return self.wrapped.open_session(app, request)

    def save_session(self, app, session, response):
        from services.auth_session_lifecycle import is_retirement_request
        if is_retirement_request():
            while 'Set-Cookie' in response.headers:
                del response.headers['Set-Cookie']
            return
        return self.wrapped.save_session(app, session, response)


def isolate_retirement_session(app):
    app.session_interface = RetirementIsolatedSessionInterface(app.session_interface)


class MigrationManagedSqlAlchemySessionInterface(SqlAlchemySessionInterface):
    """Flask-Session's SQLAlchemy interface with no implicit ``CREATE TABLE``.

    Session reads never delete expired rows while a cutover guard is enabled.
    Persistence shares the admitted request's lease or acquires its own lease;
    deployments apply the ``flask_sessions`` migration before serving traffic.
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

    def _retrieve_session_data(self, store_id):
        config = self.app.config
        if not (global_writer_authority_enabled(config) or cutover_writer_fence_enabled(config)):
            return super()._retrieve_session_data(store_id)
        # Flask opens its session before before_request and Socket.IO handlers.
        # Expiry recognition must therefore have no DELETE/commit side effect.
        record = self.sql_session_model.query.filter_by(session_id=store_id).first()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if record is None or record.expiry is None or record.expiry <= now:
            return None
        return self.serializer.decode(want_bytes(record.data))

    @contextmanager
    def _storage_writer(self, app):
        if cutover_writer_fence_enabled(app.config):
            yield False
            return
        if not global_writer_authority_enabled(app.config):
            yield True
            return
        holder = request.environ.get(HTTP_REQUEST_LEASE_ENVIRON) if has_request_context() else None
        outer_active = (getattr(holder, 'manager', None) is not None
                        and getattr(holder, 'lease', None) is not None)
        manager = global_writer_authority_lease(app.config, request_lifetime=not outer_active)
        try:
            lease = manager.__enter__()
        except GlobalWriterAuthorityTransitionError as error:
            app.logger.warning('Session persistence refused reason=%s', error.reason_code)
            yield False
            return
        try:
            yield lease.decision.allowed
        except BaseException:
            manager.__exit__(*sys.exc_info())
            raise
        else:
            manager.__exit__(None, None, None)

    def save_session(self, app, session, response):
        # Keep cookie changes coupled to the admitted durable session write.
        needs_storage = session.modified if not session else self.should_set_storage(app, session)
        if not needs_storage:
            return super().save_session(app, session, response)
        with self._storage_writer(app) as allowed:
            if allowed:
                return super().save_session(app, session, response)
            if session.accessed:
                response.vary.add('Cookie')

    def regenerate(self, session):
        with self._storage_writer(self.app) as allowed:
            if allowed:
                return super().regenerate(session)

    def _delete_expired_sessions(self):
        # Flask-Session may register cleanup before the application's gate, or
        # invoke it from its CLI command; both require their own admitted lease.
        with self._storage_writer(self.app) as allowed:
            if allowed:
                return super()._delete_expired_sessions()


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
