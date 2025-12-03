# scripts/apply_migrations.py

import os
import sys

# --- Asegurar que el root del proyecto está en sys.path ---
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from alembic import command
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import create_engine

# Importar la Config de la app (ahora sí la encuentra)
from config import Config as AppConfig

# Usar una URL explícita para migraciones si existe,
# si no, la misma que usa la app en runtime.
dburl = os.environ.get("MIGRATIONS_DATABASE_URL") or AppConfig.SQLALCHEMY_DATABASE_URI

if not dburl:
    raise RuntimeError(
        "Definí MIGRATIONS_DATABASE_URL o DATABASE_URL antes de correr migraciones."
    )

# Alinear explícitamente la URL de Alembic con la misma que usa SQLAlchemy
os.environ["ALEMBIC_DB_URL"] = dburl

# Engine que SABEMOS que conecta
engine = create_engine(dburl, pool_pre_ping=True)

cfg = Config("alembic.ini")
cfg.set_main_option("script_location", "migrations")
cfg.set_main_option("sqlalchemy.url", dburl)

with engine.connect() as conn:
    # Inyectamos la conexión al entorno Alembic
    cfg.attributes["connection"] = conn
    print(f"Running migrations with existing connection on: {dburl}")
    try:
        command.upgrade(cfg, "head")
    except CommandError as exc:
        message = str(exc)
        if "Multiple heads" in message:
            print("Multiple heads detected, upgrading all heads instead...")
            command.upgrade(cfg, "heads")
        else:
            raise
    print("Migrations applied.")
