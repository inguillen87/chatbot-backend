import os

from database import db
from flask_login import LoginManager
from flask_sock import Sock
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


class _WebRuntimeMigrate:
    """No-op migration extension for the Vercel web process.

    Schema changes are applied explicitly before serving traffic. Importing
    Flask-Migrate in every cold web instance also imports the full Alembic
    stack even though no migration command can run there. Keep the real
    extension for local/Render/CLI work and for an explicit migrations-only
    process.
    """

    def init_app(self, app, db) -> None:
        app.extensions["migrate"] = self


_TRUE_VALUES = {"1", "true", "yes", "on"}
_is_vercel_runtime = str(os.getenv("VERCEL") or "").strip().lower() in _TRUE_VALUES
_is_flask_cli = (
    str(os.getenv("FLASK_RUN_FROM_CLI") or "").strip().lower() in _TRUE_VALUES
)
_is_migrations_only = os.getenv("FLASK_MIGRATIONS_ONLY") == "1"
_is_vercel_web_runtime = (
    _is_vercel_runtime and not _is_flask_cli and not _is_migrations_only
)
if _is_vercel_web_runtime:
    migrate = _WebRuntimeMigrate()
else:
    from flask_migrate import Migrate

    migrate = Migrate()

login_manager = LoginManager()
sock = Sock()
limiter = Limiter(key_func=get_remote_address)
