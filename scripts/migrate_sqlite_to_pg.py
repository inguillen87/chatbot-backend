import os
import logging
from sqlalchemy import create_engine, MetaData, Table, select, func, insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.util import sort_tables

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

SQLITE_URL = "sqlite:///instance/database.db"
PG_URL = os.getenv("DATABASE_URL")
BATCH_SIZE = 1000

if PG_URL is None:
    raise RuntimeError("DATABASE_URL environment variable not set")

sqlite_engine = create_engine(SQLITE_URL, future=True)
pg_engine = create_engine(PG_URL, future=True)

sqlite_meta = MetaData()
sqlite_meta.reflect(bind=sqlite_engine)
pg_meta = MetaData()
pg_meta.reflect(bind=pg_engine)

sqlite_conn = sqlite_engine.connect()
pg_conn = pg_engine.connect()

# Determine table order respecting FKs
ordered_tables = list(sqlite_meta.sorted_tables)
logging.info("Migrating tables in order: %s", [t.name for t in ordered_tables])

for table in ordered_tables:
    if table.name == "alembic_version":
        continue
    pg_table = Table(table.name, pg_meta, autoload_with=pg_engine)
    cols = [c.name for c in table.columns if c.name in pg_table.c]
    logging.info("Table %s: columns %s", table.name, cols)

    count_src = sqlite_conn.execute(select(func.count()).select_from(table)).scalar_one()
    count_dst_before = pg_conn.execute(select(func.count()).select_from(pg_table)).scalar_one()
    logging.info("%s: source count=%s, target before=%s", table.name, count_src, count_dst_before)

    offset = 0
    while True:
        batch = sqlite_conn.execute(select(*[table.c[c] for c in cols]).offset(offset).limit(BATCH_SIZE)).all()
        if not batch:
            break
        pg_conn.execute(insert(pg_table), [dict(row) for row in batch])
        pg_conn.commit()
        offset += BATCH_SIZE

    count_dst_after = pg_conn.execute(select(func.count()).select_from(pg_table)).scalar_one()
    if count_src != count_dst_after:
        raise RuntimeError(f"Count mismatch for {table.name}: {count_src} != {count_dst_after}")
    logging.info("%s migrated successfully", table.name)

sqlite_conn.close()
pg_conn.close()
logging.info("Migration completed")
