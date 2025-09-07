# scripts/boot_migrate.py
import os
from collections import deque
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.sql.sqltypes import Boolean, String

# --- Config ---
SRC_SQLITE_PATH = "/data/database.db"  # Render persistent disk
SRC_SQLITE_URL  = f"sqlite:////{SRC_SQLITE_PATH.lstrip('/')}"
DST_PG = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ["DATABASE_URL"]

DO_MIGRATE = os.environ.get("MIGRATE_FROM_SQLITE", "0") == "1"
DROP_FIRST = os.environ.get("DROP_FIRST", "0") == "1"
CHUNK = 1000
EXCLUDE = {"alembic_version"}

# Forzar algunas tablas primero si tienen muchas referencias
PRIORITY_FIRST = ["user", "whatsapp_numero"]
PRIORITY_LAST: list[str] = []


def list_tables(ins):
    return [
        t for t in ins.get_table_names()
        if t not in EXCLUDE and not t.startswith("_alembic_tmp_") and not t.startswith("__")
    ]


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
    for t in tables:
        if t not in order:
            order.append(t)
    return order


def apply_priority(order):
    mid = [t for t in order if t not in PRIORITY_FIRST and t not in PRIORITY_LAST]
    return PRIORITY_FIRST + mid + PRIORITY_LAST


def detect_bool_cols(ins, table_name):
    bools = set()
    for c in ins.get_columns(table_name):
        t = c.get("type")
        if isinstance(t, Boolean) or t.__class__.__name__.lower() == "boolean":
            bools.add(c["name"])
    return bools


def detect_string_cols_with_limit(ins, table_name):
    """Devuelve dict {col: length} para columnas VARCHAR(n) (o String(length))."""
    out = {}
    for c in ins.get_columns(table_name):
        t = c.get("type")
        if isinstance(t, String) and getattr(t, "length", None):
            out[c["name"]] = int(t.length)
    return out


def to_bool(val):
    if val is None: return None
    if isinstance(val, bool): return val
    if isinstance(val, (int,)): return bool(val)
    if isinstance(val, (float,)): return bool(int(val))
    if isinstance(val, str):
        v = val.strip().lower()
        if v in {"1", "true", "t", "yes", "y", "on"}: return True
        if v in {"0", "false", "f", "no", "n", "off", ""}: return False
    return bool(val)


def normalize_rows(rows, bool_cols):
    out = []
    for r in rows:
        d = dict(r)
        for k in bool_cols:
            if k in d:
                d[k] = to_bool(d[k])
        out.append(d)
    return out


def ensure_string_capacity(src_engine, dst_engine, table, cols_with_limits, overlap_cols):
    """
    Si alguna columna VARCHAR(n) del destino se queda corta para los datos del origen,
    la convertimos en TEXT antes de copiar.
    """
    if not cols_with_limits:
        return
    # Sólo revisamos las columnas en las que realmente vamos a insertar (intersección).
    targets = {c: n for c, n in cols_with_limits.items() if c in overlap_cols}
    if not targets:
        return

    with src_engine.connect() as s, dst_engine.begin() as d:
        for col, limit in targets.items():
            # NULL-safe: si no existe en origen o todo es NULL, len_max será None/0
            len_max = s.execute(
                text(f'SELECT MAX(LENGTH("{col}")) FROM "{table}"')
            ).scalar()
            len_max = int(len_max or 0)
            if len_max > limit:
                # Subimos a TEXT (sin límite) para evitar futuros problemas.
                print(f'  - Upsizing column {table}.{col} from VARCHAR({limit}) to TEXT (max src len={len_max})')
                d.execute(text(f'ALTER TABLE "{table}" ALTER COLUMN "{col}" TYPE TEXT'))


def main():
    if not DO_MIGRATE:
        print("SKIP migrate: MIGRATE_FROM_SQLITE != 1")
        return

    print(f"SRC_SQLITE (raw): {SRC_SQLITE_PATH}")
    print(f"SRC_SQLITE (url): {SRC_SQLITE_URL}")
    print(f"DST_PG: {DST_PG}")

    src = create_engine(SRC_SQLITE_URL)
    dst = create_engine(DST_PG, pool_pre_ping=True)

    isrc = inspect(src)
    idst = inspect(dst)

    src_order = topo_sort(isrc)
    dst_tables = set(idst.get_table_names())
    order = [t for t in src_order if t in dst_tables]
    order = apply_priority(order)

    print("Copy order (with priority):")
    for t in order:
        print(" -", t)

    if DROP_FIRST:
        with dst.begin() as d:
            for t in reversed(order):
                d.execute(text(f'TRUNCATE TABLE "{t}" RESTART IDENTITY CASCADE'))
        print("TRUNCATE done.")

    # Copia
    for t in order:
        src_cols = [c["name"] for c in isrc.get_columns(t)]
        dst_cols_meta = idst.get_columns(t)
        dst_cols = [c["name"] for c in dst_cols_meta]
        cols = [c for c in src_cols if c in dst_cols]
        if not cols:
            print(f"{t}: no common columns, skip")
            continue

        # 1) Aseguramos capacidad de strings en destino antes de copiar
        string_limits = detect_string_cols_with_limit(idst, t)
        ensure_string_capacity(src, dst, t, string_limits, cols)

        # 2) Detectamos booleanos para normalizar
        bool_cols = detect_bool_cols(idst, t)

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

            norm_rows = normalize_rows(rows, bool_cols)

            try:
                with dst.begin() as d:
                    d.execute(text(f'INSERT INTO "{t}" ({collist}) VALUES ({placeholders})'), norm_rows)
            except Exception as e:
                print(f'ERROR copying table {t} at offset {off}: {e}')
                raise

            off += len(norm_rows)
            print(f"  -> {off}/{total}")

    # Ajuste de secuencias en Postgres
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
