# migrations/env.py  (REEMPLAZAR COMPLETO)

import os
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

def normalize(url_str: str) -> str:
    """Fuerza el driver psycopg v3 y agrega sslmode=require cuando el host es público (render.com)."""
    url = make_url(url_str)

    # 1) forzar dialecto psycopg v3 si viene como "postgresql://"
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")

    # 2) si el host es público (render.com), asegurar sslmode=require
    host = (url.host or "")
    if "render.com" in host:
        qs = dict(url.query)
        if "sslmode" not in qs:
            qs["sslmode"] = "require"
        url = url.set(query=qs)

    return str(url)

# Lee primero EXTERNAL, si no está toma INTERNAL (en Render definí DATABASE_URL_INTERNAL)
raw_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_INTERNAL")
if not raw_url:
    raise RuntimeError("DATABASE_URL no seteada")

DB_URL = normalize(raw_url)  # <-- CLAVE

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
