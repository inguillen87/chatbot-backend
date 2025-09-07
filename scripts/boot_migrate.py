# scripts/boot_migrate.py
import os
from collections import deque
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.sql.sqltypes import Boolean

# --- Config ---
SRC_SQLITE_PATH = "/data/database.db"  # Render persistent disk
SRC_SQLITE_URL  = f"sqlite:////{SRC_SQLITE_PATH.lstrip('/')}"
DST_PG = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ["DATABASE_URL"]

DO_MIGRATE = os.environ.get("MIGRATE_FROM_SQLITE", "0") == "1"
DROP_FIRST = os.environ.get("DROP_FIRST", "0") == "1"
CHUNK = 1000
EXCLUDE = {"alembic_version"}

# Forzamos ciertas tablas a ir primero (FKs típicas apuntan a user, etc.)
PRIORITY_FIRST = [
    "user",
    "whatsapp_numero",
]
PRIORITY_LAST: list[str] = []  # si necesitás empujar algo al final, ponelo acá


def list_tables(ins):
    names = [
        t for t in ins.get_table_names()
        if t not in EXCLUDE and not t.startswith("_alembic_tmp_") and not t.startswith("__")
    ]
    return names


def topo_sort(ins):
    tables = list_tables(ins)
    deps = {t: set() for t in tables}
    rdeps = {t: set() for t in tables}
    for t in tables:
        for fk in ins.get_foreign_keys(t):
            ref = fk.get("referred_table")
            if ref in deps:
                deps[t].add(ref)
                rdeps[ref].add(t)

    indeg = {t: len(deps[t]) for t in tables}
    q = deque([t for t in tables if indeg[t] == 0])
    order = []
    while q:
        u = q.popleft()
        order.append(u)
        for v in rdeps[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)

    # Si quedó algún ciclo, los agregamos al final
    for t in tables:
        if t not in order:
            order.append(t)
    return order


def detect_bool_cols(ins, table_name):
    """Devuelve set con nombres de columnas BOOLEAN en destino."""
    cols = ins.get_columns(table_name)
    bools = set()
    for c in cols:
        t = c.get("type")
        # SQLAlchemy/psycopg pueden presentar distintos objetos; comparamos por clase o nombre
        if isinstance(t, Boolean) or getattr(t, "__class__", type("x", (), {})).__name__.lower() == "boolean":
            bools.add(c["name"])
    return bools


def to_bool(val):
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, (int,)):
        return bool(val)
    if isinstance(val, (float,)):
        return bool(int(val))
    if isinstance(val, str):
        v = val.strip().lower()
        if v in {"1", "true", "t", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "f", "no", "n", "off", ""}:
            return False
    # fallback: cualquier valor "truthy"
    return bool(val)


def normalize_rows(rows, bool_cols):
    """Convierte dicts RowMapping→dict y castea booleanos cuando corresponde."""
    out = []
    for r in rows:
        d = dict(r)
        for k in bool_cols:
            if k in d:
                d[k] = to_bool(d[k])
        out.append(d)
    return out


def apply_priority(order):
    """Reordena segun PRIORITY_FIRST y PRIORITY_LAST manteniendo el resto igual."""
    set_order = list(order)
    # quita duplicados si ya estaban
    set_order = [t for t in set_order if t not in PRIORITY_FIRST and t not in PRIORITY_LAST]
    return PRIORITY_FIRST + set_order + PRIORITY_LAST


def main():
    if not DO_MIGRATE:
        print("SKIP migrate: MIGRATE_FROM_SQLITE != 1")
        return

    print(f"SRC_SQLITE (raw): {SRC_SQLITE_PATH}")
    print(f"SRC_SQLITE (url): {SRC_SQLITE_URL}")
    print(f"DST_PG: {DST_PG}")

    # Engines
    src = create_engine(SRC_SQLITE_URL)
    dst = create_engine(DST_PG, pool_pre_ping=True)

    isrc = inspect(src)
    idst = inspect(dst)

    # Orden por dependencias y luego prioridad manual
    src_order = topo_sort(isrc)
    dst_tables = set(idst.get_table_names())
    order = [t for t in src_order if t in dst_tables]
    order = apply_priority(order)

    print("Copy order (with priority):")
    for t in order:
        print(" -", t)

    # TRUNCATE si se pidió
    if DROP_FIRST:
        with dst.begin() as d:
            for t in reversed(order):
                d.execute(text(f'TRUNCATE TABLE "{t}" RESTART IDENTITY CASCADE'))
        print("TRUNCATE done.")

    # Copia tabla a tabla
    for t in order:
        src_cols = [c["name"] for c in isrc.get_columns(t)]
        dst_cols_meta = idst.get_columns(t)
        dst_cols = [c["name"] for c in dst_cols_meta]
        cols = [c for c in src_cols if c in dst_cols]
        if not cols:
            print(f"{t}: no common columns, skip")
            continue

        # Detectamos columnas booleanas en destino
        bool_cols = detect_bool_cols(idst, t)

        collist = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(f":{c}" for c in cols)

        # Conteo total
        with src.connect() as s:
            total = s.execute(text(f'SELECT COUNT(*) FROM "{t}"')).scalar_one()
        print(f"{t}: {total} rows to copy...")
        if not total:
            continue

        off = 0
        while off < total:
            with src.connect() as s:
                rows = s.execute(
                    text(f'SELECT {collist} FROM "{t}" LIMIT {CHUNK} OFFSET {off}')
                ).mappings().all()

            if not rows:
                break

            # Normalizamos tipos (hoy: booleanos). Si sumás más reglas, hacelo acá.
            norm_rows = normalize_rows(rows, bool_cols)

            try:
                with dst.begin() as d:
                    d.execute(text(f'INSERT INTO "{t}" ({collist}) VALUES ({placeholders})'), norm_rows)
            except Exception as e:
                print(f'ERROR copying table {t} at offset {off}: {e}')
                raise

            off += len(norm_rows)
            print(f"  -> {off}/{total}")

    # Ajuste de secuencias/identidades en Postgres
    q = text("""
        SELECT c.table_schema, c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON c.table_schema=t.table_schema AND c.table_name=t.table_name
        WHERE t.table_type='BASE TABLE'
          AND c.column_default LIKE 'nextval%%'
          AND c.table_schema='public'
    """)
    with dst.begin() as d:
        res = d.execute(q).fetchall()
        for schema, table, col in res:
            seq = d.execute(text("SELECT pg_get_serial_sequence(:tbl,:col)"),
                            {"tbl": f'"{schema}"."{table}"', "col": col}).scalar()
            if not seq:
                continue
            maxv = d.execute(text(f'SELECT COALESCE(MAX("{col}"),0) FROM "{schema}"."{table}"')).scalar()
            d.execute(text("SELECT setval(:seq,:val,:is_called)"),
                      {"seq": seq, "val": (maxv or 0) + 1, "is_called": False})
            print(f"seq fixed: {table}.{col} -> {seq}")

    print("MIGRATION DONE")


if __name__ == "__main__":
    main()
