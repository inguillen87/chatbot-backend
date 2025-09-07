# scripts/boot_migrate.py
import os
from collections import deque, defaultdict
from typing import Dict, List, Set, Tuple
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.sqltypes import Boolean, String

# --- Config ---
SRC_SQLITE_PATH = "/data/database.db"
SRC_SQLITE_URL  = f"sqlite:////{SRC_SQLITE_PATH.lstrip('/')}"
DST_PG = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ["DATABASE_URL"]

DO_MIGRATE = os.environ.get("MIGRATE_FROM_SQLITE", "0") == "1"
DROP_FIRST = os.environ.get("DROP_FIRST", "0") == "1"
CHUNK = int(os.environ.get("MIGRATE_CHUNK", "1000"))
EXCLUDE = {"alembic_version"}

# Modo de manejo de FKs:
#   "ordered" -> solo orden topológico (por defecto)
#   "auto"    -> orden topológico; si falla una FK en runtime, desactiva FKs y reintenta
#   "replica" -> desactiva FKs durante toda la copia
FK_MODE = os.environ.get("FK_MODE", "auto").lower()  # ordered | auto | replica


def list_tables(ins):
    return [
        t for t in ins.get_table_names(schema="public")
        if t not in EXCLUDE and not t.startswith("_") and not t.startswith("__")
    ]


def fk_graph_from_dst(idst) -> Tuple[Dict[str, Set[str]], Dict[str, Set[str]]]:
    """
    Grafo de dependencias a partir de FKs en Postgres:
        edge: child -> parent (child depende de parent)
    """
    tables = set(list_tables(idst))
    deps: Dict[str, Set[str]] = {t: set() for t in tables}
    rdeps: Dict[str, Set[str]] = {t: set() for t in tables}
    for t in tables:
        for fk in idst.get_foreign_keys(t, schema="public"):
            ref = fk.get("referred_table")
            if ref in tables:
                deps[t].add(ref)
                rdeps[ref].add(t)
    return deps, rdeps


def topo_sort_from_dst(idst) -> List[str]:
    deps, rdeps = fk_graph_from_dst(idst)
    indeg = {t: len(deps[t]) for t in deps}
    q = deque([t for t, d in indeg.items() if d == 0])
    order = []
    while q:
        u = q.popleft()
        order.append(u)
        for v in rdeps[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    # Si quedaron ciclos, los ponemos al final (los manejamos con FK_MODE='auto'/'replica')
    for t in deps:
        if t not in order:
            order.append(t)
    return order


def detect_bool_cols(ins, table_name):
    bools = set()
    for c in ins.get_columns(table_name, schema="public"):
        t = c.get("type")
        if isinstance(t, Boolean) or t.__class__.__name__.lower() == "boolean":
            bools.add(c["name"])
    return bools


def detect_string_cols_with_limit(ins, table_name):
    out = {}
    for c in ins.get_columns(table_name, schema="public"):
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
    targets = {c: n for c, n in cols_with_limits.items() if c in overlap_cols}
    if not targets:
        return
    with src_engine.connect() as s, dst_engine.begin() as d:
        for col, limit in targets.items():
            len_max = s.execute(text(f'SELECT MAX(LENGTH("{col}")) FROM "{table}"')).scalar()
            len_max = int(len_max or 0)
            if len_max > limit:
                print(f'  - Upsizing column {table}.{col} from VARCHAR({limit}) to TEXT (max src len={len_max})')
                d.execute(text(f'ALTER TABLE "public"."{table}" ALTER COLUMN "{col}" TYPE TEXT'))


def set_replication_role(conn, role: str):
    # role: 'origin' | 'replica'
    conn.execute(text(f"SET session_replication_role = '{role}'"))


def copy_table(src, dst, isrc, idst, table, chunk_size=CHUNK, fk_mode=FK_MODE):
    src_cols = [c["name"] for c in isrc.get_columns(table)]
    dst_cols_meta = idst.get_columns(table, schema="public")
    dst_cols = [c["name"] for c in dst_cols_meta]
    cols = [c for c in src_cols if c in dst_cols]
    if not cols:
        print(f"{table}: no common columns, skip")
        return

    # Capacidades de strings
    string_limits = detect_string_cols_with_limit(idst, table)
    ensure_string_capacity(src, dst, table, string_limits, cols)

    # Booleanos
    bool_cols = detect_bool_cols(idst, table)

    collist = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(f":{c}" for c in cols)

    with src.connect() as s:
        total = s.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
    print(f"{table}: {total} rows to copy...")
    if not total:
        return

    off = 0
    fk_disabled = False

    while off < total:
        with src.connect() as s:
            rows = s.execute(
                text(f'SELECT {collist} FROM "{table}" LIMIT {chunk_size} OFFSET {off}')
            ).mappings().all()
        if not rows:
            break

        norm_rows = normalize_rows(rows, bool_cols)

        try:
            with dst.begin() as d:
                if fk_mode == "replica" and not fk_disabled:
                    set_replication_role(d, "replica")
                    fk_disabled = True
                d.execute(text(f'INSERT INTO "public"."{table}" ({collist}) VALUES ({placeholders})'), norm_rows)
        except IntegrityError as e:
            # Si la FK falla en modo ordered/auto, reintentamos con FKs deshabilitadas
            if "ForeignKeyViolation" in str(e) and fk_mode in {"auto"} and not fk_disabled:
                print(f'  ! FK violation in {table} at offset {off}. Retrying with FKs disabled...')
                with dst.begin() as d:
                    set_replication_role(d, "replica")
                    fk_disabled = True
                    d.execute(text(f'INSERT INTO "public"."{table}" ({collist}) VALUES ({placeholders})'), norm_rows)
            else:
                print(f'ERROR copying table {table} at offset {off}: {e}')
                raise
        finally:
            # Volvemos a origin solo si deshabilitamos dentro de este batch y no estamos en modo "replica"
            if fk_disabled and fk_mode in {"ordered", "auto"}:
                with dst.begin() as d:
                    set_replication_role(d, "origin")

        off += len(norm_rows)
        print(f"  -> {off}/{total}")


def fix_sequences(dst):
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

    # Tablas presentes en ambos
    src_tables = set(list_tables(isrc))
    dst_tables = set(list_tables(idst))
    common = sorted(src_tables & dst_tables)

    # Orden topológico desde las FKs de DESTINO
    order_dst = [t for t in topo_sort_from_dst(idst) if t in common]

    print("Copy order (from Postgres FKs):")
    for t in order_dst:
        print(" -", t)

    if DROP_FIRST:
        with dst.begin() as d:
            # Truncamos en orden inverso (primero las dependientes)
            for t in reversed(order_dst):
                d.execute(text(f'TRUNCATE TABLE "public"."{t}" RESTART IDENTITY CASCADE'))
        print("TRUNCATE done.")

    # Si el usuario pide FK_MODE=replica, desactivamos FKs para toda la copia
    fk_globally_disabled = FK_MODE == "replica"
    if fk_globally_disabled:
        with dst.begin() as d:
            set_replication_role(d, "replica")
        print("FKs disabled globally (replica mode).")

    # Copiamos
    for t in order_dst:
        copy_table(src, dst, isrc, idst, t, chunk_size=CHUNK, fk_mode=FK_MODE)

    # Volvemos FKs a origin si estaban globalmente deshabilitadas
    if fk_globally_disabled:
        with dst.begin() as d:
            set_replication_role(d, "origin")
        print("FKs re-enabled (origin mode).")

    # Secuencias
    fix_sequences(dst)
    print("MIGRATION DONE")


if __name__ == "__main__":
    main()
