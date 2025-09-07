# migrations/env.py
import os
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

def normalize(url_str: str) -> str:
    """Fuerza psycopg v3 y sslmode=require en host público."""
    url = make_url(url_str)

    # Forzar driver psycopg v3 si viene "postgresql://"
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")

    # sslmode=require si es host de render.com
    host = (url.host or "")
    if "render.com" in host:
        qs = dict(url.query)
        if "sslmode" not in qs:
            qs["sslmode"] = "require"
        url = url.set(query=qs)

    return str(url)

# 1) primero el -x dburl=... que pasamos desde el Start Command
xargs = context.get_x_argument(as_dictionary=True)
raw_url = xargs.get("dburl")

# 2) si no vino por -x, probamos variables de entorno (orden más útil)
if not raw_url:
    raw_url = (
        os.getenv("DB_URL_PUBLIC")
        or os.getenv("DATABASE_URL")
        or os.getenv("DATABASE_URL_INTERNAL")
    )

if not raw_url:
    raise RuntimeError(
        "No database URL (usa -x dburl=... o setea DB_URL_PUBLIC/DATABASE_URL)"
    )

DB_URL = normalize(raw_url)

def run_migrations_offline() -> None:
    context.configure(url=DB_URL, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    engine = create_engine(DB_URL, pool_pre_ping=True)
    with engine.connect() as connection:
        context.configure(connection=connection)  # sin target_metadata
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
