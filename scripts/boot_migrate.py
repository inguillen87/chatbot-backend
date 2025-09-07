# scripts/boot_migrate.py
import os
from collections import deque
from sqlalchemy import create_engine, text, inspect

# --- Config ---
SRC_SQLITE = os.environ.get("SRC_SQLITE", "sqlite:////data/database.db")  # Ruta del .db en Render
DST_PG = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ["DATABASE_URL"]

DO_MIGRATE = os.environ.get("MIGRATE_FROM_SQLITE", "0") == "1"
DROP_FIRST = os.environ.get("DROP_FIRST", "0") == "1"
CHUNK = int(os.environ.get("MIG_CHUNK", "1000"))

# Tablas a excluir (metadatos, tmp, etc.)
EXCLUDE = {"alembic_version"}

# Tablas que deben copiarse primero, para evitar errores de FK
PRIORITY_FIRST = [
    "user",
    "whatsapp_numero",
    # agregá aquí otras tablas "padre" si ves errores de FK (p.ej. "rubro", "promocion", etc.)
]

# Si querés forzar algunas al final, ponelas acá
PRIORITY_LAST = [
    # "logs",
]


def list_tables(inspector):
    """Lista tablas reales (sin temporales/auxiliares)."""
    names = []
    for t in inspector.get_table_names():
        if t in EXCLUDE:
            continue
        if t.startswith("_alembic_tmp_") or t.startswith("__"):
            continue
        names.append(t)
    return names


def topo_sort(inspector):
    """Orden topológico por FKs (si están definidas en origen)."""
    tables = list_tables(inspector)
    deps = {t: set() for t in tables}
    rdeps = {t: set() for t in tables}

    for t in tables:
        for fk in inspector.get_foreign_keys(t) or []:
            ref = fk.get("referred_table")
            if ref and ref in deps:
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

    # Si quedaron ciclos o faltantes, los anexamos al final
    for t in tables:
        if t not in order:
            order.append(t)
    return order


def apply_priority(order):
    """Mueve las tablas de PRIORITY_FIRST al frente y PRIORITY_LAST al final, preservando el resto."""
    head = [t for t in PRIORITY_FIRST if t in order]
    tail = [t for t in PRIORITY_LAST if t in order]
    middle = [t for t in order if t not in head and t not in tail]
    return head + middle + tail


def main():
    if not DO_MIGRATE:
        print("SKIP migrate: MIGRATE_FROM_SQLITE != 1")
        return

    print(f"SRC_SQLITE: {SRC_SQLITE}")
    print(f"DST_PG: {DST_PG}")

    src = create_engine(SRC_SQLITE)
    dst = create_engine(DST_PG, pool_pre_ping=True)

    isrc = inspect(src)
    idst = inspect(dst)

    # Orden propuesto por FKs (si existen en SQLite)
    src_order = topo_sort(isrc)

    # Intersección con tablas realmente presentes en destino (ya migradas por Alembic)
    dst_tables = set(idst.get_table_names())
    order = [t for t in src_order if t in dst_tables]

    # Aplica prioridad manual para asegurar user/whatsapp_numero antes que dependientes
    order = apply_priority(order)

    print("Copy order (with priority):")
    for t in order:
        print(" -", t)

    # Si pedimos limpiar destino antes de copiar
    if DROP_FIRST:
        with dst.begin() as d:
            for t in reversed(order):
                # Truncamos en orden inverso para respetar dependencias
                try:
                    d.execute(text(f'TRUNCATE TABLE "{t}" RESTART IDENTITY CASCADE'))
                except Exception as e:
                    print(f"skip truncate {t}: {e}")
        print("TRUNCATE done.")

    # Copia tabla por tabla
    for t in order:
        src_cols = [c["name"] for c in isrc.get_columns(t)]
        dst_cols = [c["name"] for c in idst.get_columns(t)]
        cols = [c for c in src_cols if c in dst_cols]
        if not cols:
            print(f"{t}: no common columns, skip")
            continue

        collist = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(f":{c}" for c in cols)

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

            try:
                with dst.begin() as d:
                    d.execute(
                        text(f'INSERT INTO "{t}" ({collist}) VALUES ({placeholders})'),
                        rows,
                    )
                off += len(rows)
                print(f"  -> {off}/{total}")
            except Exception as e:
                print(f"ERROR copying table {t} at offset {off}: {e}")
                # Re-lanzamos para que el deploy falle y puedas ver el error exacto en logs
                raise

    # Arregla secuencias (serial/identity) en Postgres
    fix_sql = text("""
        SELECT c.table_schema, c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON c.table_schema = t.table_schema AND c.table_name = t.table_name
        WHERE t.table_type = 'BASE TABLE'
          AND c.table_schema = 'public'
          AND c.column_default LIKE 'nextval%';
    """)
    with dst.begin() as d:
        res = d.execute(fix_sql).fetchall()
        for schema, table, col in res:
            seq = d.execute(
                text("SELECT pg_get_serial_sequence(:tbl, :col)"),
                {"tbl": f'"{schema}"."{table}"', "col": col},
            ).scalar()
            if not seq:
                continue
            maxv = d.execute(
                text(f'SELECT COALESCE(MAX("{col}"), 0) FROM "{schema}"."{table}"')
            ).scalar()
            d.execute(
                text("SELECT setval(:seq, :val, :is_called)"),
                {"seq": seq, "val": (maxv or 0) + 1, "is_called": False},
            )
            print(f"seq fixed: {table}.{col} -> {seq}")

    print("MIGRATION DONE")


if __name__ == "__main__":
    main()
