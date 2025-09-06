# migrations/env.py  (REEMPLAZAR COMPLETO)

import os
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

# 1) Tomamos la URL desde ENV (en Render usá la INTERNAL)
DB_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL_INTERNAL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL no seteada")

# 2) Validamos formato (si hay basura, explota acá con mensaje claro)
make_url(DB_URL)

# 3) Modo offline (no necesitamos metadata para 'upgrade')
def run_migrations_offline() -> None:
    context.configure(url=DB_URL, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

# 4) Modo online (sin importar models/database)
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
