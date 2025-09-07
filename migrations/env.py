# migrations/env.py
import os
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

# --- Helpers ---------------------------------------------------------------

def _normalize(url_str: str) -> str:
    """
    Normaliza la URL:
      - fuerza driver psycopg v3 si viene 'postgresql://'
      - agrega sslmode=require si el host es de render.com y no está presente
    """
    url = make_url(url_str)

    if url.drivername == "postgresql":  # fuerza psycopg v3
        url = url.set(drivername="postgresql+psycopg")

    host = (url.host or "")
    if "render.com" in host:
        qs = dict(url.query or {})
        qs.setdefault("sslmode", "require")
        url = url.set(query=qs)

    return str(url)

def _mask(u: str) -> str:
    """Oculta la password solo para logging/diagnóstico."""
    try:
        url = make_url(u)
        if url.password:
            return u.replace(url.password, "***")
    except Exception:
        pass
    return u

def _choose_raw_url() -> str:
    """
    Orden de resolución (primero que exista):
      1) -x dburl=...
      2) current_app.config['SQLALCHEMY_DATABASE_URI']  (si hay app)
      3) Variables de entorno: SQLALCHEMY_DATABASE_URI, DATABASE_URL, PG_EXTERNAL,
                               DB_URL_PUBLIC, DATABASE_URL_INTERNAL
      4) sqlalchemy.url del alembic.ini (si estuviera seteada)
    """
    # 1) x-args
    xargs = context.get_x_argument(as_dictionary=True)
    raw = xargs.get("dburl")
    if raw:
        return raw

    # 2) app config (si hay app)
    try:
        from flask import current_app
        cfg_url = current_app.config.get("SQLALCHEMY_DATABASE_URI")
        if cfg_url:
            return cfg_url
    except Exception:
        pass  # no hay app context

    # 3) env vars
    env_candidates = [
        "SQLALCHEMY_DATABASE_URI",
        "DATABASE_URL",
        "PG_EXTERNAL",
        "DB_URL_PUBLIC",
        "DATABASE_URL_INTERNAL",
    ]
    for k in env_candidates:
        v = os.getenv(k)
        if v:
            return v

    # 4) alembic.ini
    ini_url = context.config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url

    raise RuntimeError(
        "No database URL. Pasá -x dburl=... o definí SQLALCHEMY_DATABASE_URI/DATABASE_URL/PG_EXTERNAL."
    )

# --- Build final URL -------------------------------------------------------

RAW_URL = _choose_raw_url()
DB_URL = _normalize(RAW_URL)

# Reflejar en la config de Alembic (aunque creemos engine con la misma)
config = context.config
config.set_main_option("sqlalchemy.url", DB_URL)

print("ALEMBIC_DB_URL:", _mask(DB_URL))  # útil para diagnosticar; NO imprime la pass

# --- Alembic hooks ---------------------------------------------------------

def run_migrations_offline() -> None:
    context.configure(
        url=DB_URL,
        literal_binds=True,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    engine = create_engine(DB_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
