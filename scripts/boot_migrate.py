# scripts/boot_migrate.py
import os
import re
from collections import defaultdict
from typing import Dict, List, Set, Tuple, Any

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.sql.sqltypes import Boolean, String

# ----------------- Config -----------------
SRC_SQLITE_PATH = os.getenv("SRC_SQLITE_PATH", "/data/database.db")
SRC_SQLITE_URL  = f"sqlite:////{SRC_SQLITE_PATH.lstrip('/')}"
DST_PG          = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ["DATABASE_URL"]

DO_MIGRATE = os.environ.get("MIGRATE_FROM_SQLITE", "0") == "1"
DROP_FIRST = os.environ.get("DROP_FIRST", "0") == "1"
CHUNK      = int(os.environ.get("MIGRATE_CHUNK", "1000"))
EXCLUDE    = {"alembic_version"}

# ordered | auto | replica
FK_MODE = os.environ.get("FK_MODE", "ordered").lower()

# null | skip | synth
FK_MISSING_STRATEGY = os.environ.get("FK_MISSING_STRATEGY", "null").lower()
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"

ONLY_TABLES = os.environ.get("ONLY_TABLES")  # regex (opcional)
SKIP_TABLES = os.environ.get("SKIP_TABLES")  # regex (opcional)

CAN_SET_REPL_ROLE = True  # se desactiva si el rol no tiene permiso

# ----------------- Utils -----------------
def _dialect_name(ins) -> str:
    try:
        return ins.bind.dialect.name
    except Exception:
        return ""

def _re_ok(name: str) -> bool:
    if ONLY_TABLES and not re.search(ONLY_TABLES, name):
        return False
    if SKIP_TABLES and re.search(SKIP_TABLES, name):
        return False
    return True

def list_tables_sqlite(ins) -> List[str]:
    tables = ins.get_table_names()
    skip = {"sqlite_sequence", "sqlite_stat1", "sqlite_stat4", "sqlite_schema", "sqlite_master"}
    return [t for t in tables if t not in skip and t not in EXCLUDE and not t.startswith("_") and _re_ok(t)]

def list_tables_pg(ins) -> List[str]:
    tables = ins.get_table_names(schema="public")
    return [t for t in tables if t not in EXCLUDE and not t.startswith("_") and _re_ok(t)]

def list_tables(ins) -> List[str]:
    return list_tables_sqlite(ins) if _dialect_name(ins) == "sqlite" else list_tables_pg(ins)

# ---- Descubrimiento de FKs y metadatos (Postgres) ----
def fk_edges(dst_engine) -> Dict[str, Set[str]]:
    """
    child_table -> set(parent_table) en schema public.
    """
    sql = text("""
        SELECT
          child.relname  AS child_table,
          parent.relname AS parent_table
        FROM pg_constraint c
        JOIN pg_class child  ON child.oid = c.conrelid
        JOIN pg_namespace n1 ON n1.oid   = child.relnamespace
        JOIN pg_class parent ON parent.oid = c.confrelid
        JOIN pg_namespace n2 ON n2.oid   = parent.relnamespace
        WHERE c.contype = 'f'
          AND n1.nspname = 'public'
          AND n2.nspname = 'public'
    """)
    out: Dict[str, Set[str]] = defaultdict(set)
    with dst_engine.connect() as conn:
        for r in conn.execute(sql):
            out[r._mapping["child_table"]].add(r._mapping["parent_table"])
    return out

def fk_column_map(dst_engine) -> Dict[str, List[Dict[str, Any]]]:
    """
    Por tabla hija: lista de FKs con columnas (soporta compuestas).
    """
    sql = text("""
        SELECT
          tc.table_name        AS child_table,
          kcu.column_name      AS child_column,
          ccu.table_name       AS parent_table,
          ccu.column_name      AS parent_column,
          kcu.ordinal_position AS pos
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema    = kcu.table_schema
        JOIN information_schema.referential_constraints rc
          ON tc.constraint_name = rc.constraint_name
         AND tc.table_schema    = rc.constraint_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = rc.unique_constraint_name
         AND ccu.constraint_schema = rc.unique_constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
        ORDER BY child_table, pos
    """)
    tmp: Dict[Tuple[str, str], Dict[str, Any]] = {}
    out: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    with dst_engine.connect() as conn:
        for r in conn.execute(sql):
            ch = r._mapping["child_table"]
            pa = r._mapping["parent_table"]
            ck = r._mapping["child_column"]
            pk = r._mapping["parent_column"]
            key = (ch, pa)
            if key not in tmp:
                tmp[key] = {"child_table": ch, "parent_table": pa, "child_cols": [], "parent_cols": []}
            tmp[key]["child_cols"].append(ck)
            tmp[key]["parent_cols"].append(pk)
    for v in tmp.values():
        out[v["child_table"]].append(v)
    return out

def column_nullable_map(dst_engine) -> Dict[Tuple[str, str], bool]:
    sql = text("""
        SELECT table_name, column_name, is_nullable
        FROM information_schema.columns
        WHERE table_schema='public'
    """)
    out = {}
    with dst_engine.connect() as conn:
        for r in conn.execute(sql):
            out[(r._mapping["table_name"], r._mapping["column_name"])] = (r._mapping["is_nullable"] == "YES")
    return out

def table_pk_map(dst_engine) -> Dict[str, List[str]]:
    sql = text("""
        SELECT tc.table_name, kcu.column_name, kcu.ordinal_position
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type='PRIMARY KEY'
          AND tc.table_schema='public'
        ORDER BY tc.table_name, kcu.ordinal_position
    """)
    out: Dict[str, List[str]] = defaultdict(list)
    with dst_engine.connect() as conn:
        for r in conn.execute(sql):
            out[r._mapping["table_name"]].append(r._mapping["column_name"])
    return out

# ---- Types / normalización ----
def detect_bool_cols(ins, table_name) -> Set[str]:
    bools = set()
    for c in ins.get_columns(table_name, schema="public"):
        t = c.get("type")
        if isinstance(t, Boolean) or t.__class__.__name__.lower() == "boolean":
            bools.add(c["name"])
    return bools

def detect_string_cols_with_limit(ins, table_name) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for c in ins.get_columns(table_name, schema="public"):
        t = c.get("type")
        if isinstance(t, String) and getattr(t, "length", None):
            out[c["name"]] = int(t.length)
    return out

def to_bool(val):
    if val is None: return None
    if isinstance(val, bool): return val
    if isinstance(val, int): return bool(val)
    if isinstance(val, float): return bool(int(val))
    if isinstance(val, str):
        v = val.strip().lower()
        if v in {"1","true","t","yes","y","on"}: return True
        if v in {"0","false","f","no","n","off",""}: return False
    return bool(val)

def normalize_rows(rows, bool_cols: Set[str]):
    out = []
    for r in rows:
        d = dict(r)
        for k in bool_cols:
            if k in d:
                d[k] = to_bool(d[k])
        out.append(d)
    return out

def ensure_string_capacity(src_engine, dst_engine, table, cols_with_limits, overlap_cols):
    targets = {c:n for c,n in cols_with_limits.items() if c in overlap_cols}
    if not targets:
        return
    with src_engine.connect() as s:
        for col, limit in targets.items():
            len_max = s.execute(text(f'SELECT MAX(LENGTH("{col}")) FROM "{table}"')).scalar()
            len_max = int(len_max or 0)
            if len_max > limit:
                msg = f'  - Upsizing {table}.{col} VARCHAR({limit})→TEXT (src max={len_max})'
                if DRY_RUN:
                    print(msg, "[DRY_RUN]")
                else:
                    print(msg)
                    with dst_engine.begin() as d:
                        d.execute(text(f'ALTER TABLE "public"."{table}" ALTER COLUMN "{col}" TYPE TEXT'))

# ---- FK helpers ----
def maybe_set_replication_role(conn, role: str) -> bool:
    global CAN_SET_REPL_ROLE
    if not CAN_SET_REPL_ROLE:
        return False
    try:
        conn.execute(text(f"SET session_replication_role = '{role}'"))
        return True
    except ProgrammingError as e:
        if "permission denied" in str(e).lower() or "must be superuser" in str(e).lower():
            CAN_SET_REPL_ROLE = False
            print("WARN: session_replication_role no permitido; sigo sin desactivar FKs.")
            return False
        raise

def _is_fk_violation(err: IntegrityError) -> bool:
    s = str(err).lower()
    return "violates foreign key constraint" in s or "foreignkeyviolation" in s or "23503" in s

def fetch_parent_keys(dst_engine, parent_table: str, parent_col: str, values: List[Any]) -> Set[Any]:
    if not values: return set()
    uniq = list({v for v in values if v is not None})
    if not uniq: return set()
    placeholders = ", ".join([f":v{i}" for i in range(len(uniq))])
    params = {f"v{i}": uniq[i] for i in range(len(uniq))}
    sql = text(f'SELECT DISTINCT "{parent_col}" FROM "public"."{parent_table}" WHERE "{parent_col}" IN ({placeholders})')
    with dst_engine.connect() as d:
        return {r[0] for r in d.execute(sql, params).fetchall()}

def can_synthesize_parent(dst_engine, parent_table: str, pk_cols: List[str]) -> Tuple[bool, List[str]]:
    if not pk_cols:
        return False, []
    sql = text("""
        SELECT column_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=:t
    """)
    with dst_engine.connect() as d:
        for col, is_null, default in d.execute(sql, {"t": parent_table}).fetchall():
            if col in pk_cols:  # PKs quedan fuera
                continue
            if is_null != "YES" and default is None:
                return False, pk_cols
    return True, pk_cols

def synthesize_parents(dst_engine, parent_table: str, pk_col: str, missing_vals: Set[Any]):
    if not missing_vals:
        return
    if DRY_RUN:
        print(f'  - [DRY_RUN] Synth {len(missing_vals)} rows in {parent_table}({pk_col})')
        return
    with dst_engine.begin() as d:
        rows = [{pk_col: v} for v in missing_vals]
        d.execute(
            text(f'INSERT INTO "public"."{parent_table}" ("{pk_col}") VALUES (:{pk_col}) ON CONFLICT DO NOTHING'),
            rows
        )
    print(f'  - Synthesized {len(missing_vals)} rows in {parent_table}')

# ---- Copia segura por tabla ----
def copy_table(src, dst, isrc, idst, table, fkmap_tbl, nullable_map, pkmap, stats, chunk_size=CHUNK, fk_mode=FK_MODE):
    src_cols = [c["name"] for c in isrc.get_columns(table)]
    dst_cols_meta = idst.get_columns(table, schema="public")
    dst_cols = [c["name"] for c in dst_cols_meta]
    cols = [c for c in src_cols if c in dst_cols]
    if not cols:
        print(f"{table}: no common columns, skip")
        return True  # nada para copiar

    string_limits = detect_string_cols_with_limit(idst, table)
    ensure_string_capacity(src, dst, table, string_limits, cols)

    bool_cols = detect_bool_cols(idst, table)
    collist = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(f":{c}" for c in cols)

    # cantidad a copiar
    with src.connect() as s:
        total = s.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
    print(f"{table}: {total} rows to copy...")
    stats.setdefault(table, {"copied":0, "retried":0, "synth":0, "skipped":0})
    if total == 0:
        return True
    if DRY_RUN:
        print(f"  -> [DRY_RUN] would copy {total} rows")
        return True

    off = 0
    fk_disabled = False
    fks = fkmap_tbl or []

    while off < total:
        with src.connect() as s:
            rows = s.execute(text(f'SELECT {collist} FROM "{table}" LIMIT {chunk_size} OFFSET {off}')).mappings().all()
        if not rows:
            break
        rows = normalize_rows(rows, bool_cols)

        to_insert, to_retry = [], []

        # pre-chequeo simple para FKs de 1 columna
        for r in rows:
            candidate = dict(r)
            violated = False

            for fk in fks:
                ch_cols = fk["child_cols"]; pa_table = fk["parent_table"]; pa_cols = fk["parent_cols"]
                if len(ch_cols) != 1 or len(pa_cols) != 1:
                    continue
                ch_col, pa_col = ch_cols[0], pa_cols[0]
                val = candidate.get(ch_col)
                if val is None:
                    continue
                exists = val in fetch_parent_keys(dst, pa_table, pa_col, [val])
                if not exists:
                    nullable = nullable_map.get((table, ch_col), True)
                    if FK_MISSING_STRATEGY == "synth":
                        pk_cols = pkmap.get(pa_table, [])
                        ok, pk_cols = can_synthesize_parent(dst, pa_table, pk_cols)
                        if ok and len(pk_cols) == 1 and pk_cols[0] == pa_col:
                            synthesize_parents(dst, pa_table, pa_col, {val})
                            stats[table]["synth"] += 1
                            continue
                    if FK_MISSING_STRATEGY == "null" and nullable:
                        candidate[ch_col] = None
                        continue
                    violated = True
                    break

            if violated:
                to_retry.append(candidate)
            else:
                to_insert.append(candidate)

        # inserción
        if to_insert:
            try:
                with dst.begin() as d:
                    if fk_mode == "replica" and not fk_disabled:
                        fk_disabled = maybe_set_replication_role(d, "replica")
                    d.execute(text(f'INSERT INTO "public"."{table}" ({collist}) VALUES ({placeholders})'), to_insert)
                stats[table]["copied"] += len(to_insert)
            except IntegrityError as e:
                if _is_fk_violation(e) and fk_mode in {"auto"} and not fk_disabled:
                    print(f'  ! FK violation in {table}. Retrying with FKs disabled...')
                    with dst.begin() as d:
                        if not maybe_set_replication_role(d, "replica"):
                            print("  ! Cannot disable FKs on this DB user.")
                            raise
                        fk_disabled = True
                        d.execute(text(f'INSERT INTO "public"."{table}" ({collist}) VALUES ({placeholders})'), to_insert)
                else:
                    print(f'ERROR copying table {table} at offset {off}: {e}')
                    raise
            finally:
                if fk_disabled and fk_mode in {"ordered", "auto"}:
                    with dst.begin() as d:
                        maybe_set_replication_role(d, "origin")

        # lo que quedó en retry se maneja al final de todas las tablas
        if to_retry:
            stats[table]["retried"] += len(to_retry)
            _pending.setdefault(table, []).extend(to_retry)

        off += len(rows)
        print(f"  -> {min(off, total)}/{total}")

    return True

# ---- Secuencias ----
def fix_sequences(dst):
    if DRY_RUN:
        print("[DRY_RUN] Skipping sequence fix.")
        return
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
        for schema, table, col in d.execute(q).fetchall():
            seq = d.execute(text("SELECT pg_get_serial_sequence(:tbl,:col)"),
                            {"tbl": f'"{schema}"."{table}"', "col": col}).scalar()
            if not seq:
                continue
            maxv = d.execute(text(f'SELECT COALESCE(MAX("{col}"),0) FROM "{schema}"."{table}"')).scalar()
            d.execute(text("SELECT setval(:seq,:val,:is_called)"),
                      {"seq": seq, "val": (maxv or 0) + 1, "is_called": False})
            print(f"seq fixed: {table}.{col} -> {seq}")

# ---- Scheduler dinámico por dependencias ----
_pending: Dict[str, List[Dict[str, Any]]] = {}

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

    src_tables = set(list_tables(isrc))
    dst_tables = set(list_tables(idst))
    common = sorted(src_tables & dst_tables)
    if not common:
        print("No common tables between SQLite and Postgres. Nothing to do.")
        return

    # mapa de dependencias (child -> set(parents)) y metadatos
    deps = fk_edges(dst)
    fkmap = fk_column_map(dst)
    nullable = column_nullable_map(dst)
    pkmap = table_pk_map(dst)

    # mostrar orden sugerido (no vinculante, porque usamos scheduler)
    print("Copy order (initial listing):")
    for t in common:
        print(" -", t)

    if DROP_FIRST:
        if DRY_RUN:
            print("[DRY_RUN] Would TRUNCATE all common tables.")
        else:
            with dst.begin() as d:
                for t in reversed(common):
                    d.execute(text(f'TRUNCATE TABLE "public"."{t}" RESTART IDENTITY CASCADE'))
            print("TRUNCATE done.")

    # FK_MODE=replica (global) si es posible y solicitado
    fk_globally_disabled = (FK_MODE == "replica") and not DRY_RUN
    if fk_globally_disabled:
        with dst.begin() as d:
            if not maybe_set_replication_role(d, "replica"):
                print("WARN: FK_MODE=replica solicitado pero no permitido. Sigo en ordered.")
                fk_globally_disabled = False
        if fk_globally_disabled:
            print("FKs disabled globally (replica mode).")

    # Scheduler: mientras queden tablas por copiar, en cada pasada copia las que ya tengan a sus padres copiados
    remaining: Set[str] = set(common)
    copied: Set[str] = set()
    stats: Dict[str, Dict[str, int]] = {}

    pass_no = 0
    while remaining:
        pass_no += 1
        print(f"=== PASS {pass_no} ===")
        progressed = 0
        for t in list(remaining):
            parents = deps.get(t, set())
            if not parents or parents.issubset(copied):
                ok = copy_table(src, dst, isrc, idst, t, fkmap.get(t, []), nullable, pkmap, stats, chunk_size=CHUNK, fk_mode=FK_MODE)
                if ok:
                    copied.add(t)
                    remaining.remove(t)
                    progressed += 1
        if progressed == 0:
            # no pudimos avanzar por alguna dependencia que el grafo no reflejó → forzamos intento en todas (para exponer exactamente qué falla)
            for t in list(remaining):
                print(f"--- Forcing attempt on {t} due to stalled progress ---")
                ok = copy_table(src, dst, isrc, idst, t, fkmap.get(t, []), nullable, pkmap, stats, chunk_size=CHUNK, fk_mode=FK_MODE)
                copied.add(t)
                remaining.remove(t)
            break

    # Reintentos de filas pendientes por FKs no resueltas en los datos
    if not DRY_RUN and _pending:
        for attempt in range(1, MAX_RETRIES+1):
            print(f"Retry pass {attempt}/{MAX_RETRIES} ...")
            progressed = 0
            new_pending: Dict[str, List[Dict[str, Any]]] = {}
            for t, rows in _pending.items():
                if not rows:
                    continue
                idst2 = inspect(dst)
                bool_cols = detect_bool_cols(idst2, t)
                rows = normalize_rows(rows, bool_cols)

                # Filtrar las que ya tengan padres
                ok_rows, again = [], []
                for r in rows:
                    violated = False
                    for fk in fkmap.get(t, []):
                        ch_cols = fk["child_cols"]; pa_table = fk["parent_table"]; pa_cols = fk["parent_cols"]
                        if len(ch_cols) != 1 or len(pa_cols) != 1:
                            continue
                        ch_col, pa_col = ch_cols[0], pa_cols[0]
                        val = r.get(ch_col)
                        if val is None:  # ya nulleado
                            continue
                        exists = val in fetch_parent_keys(dst, pa_table, pa_col, [val])
                        if not exists:
                            violated = True
                            break
                    (again if violated else ok_rows).append(r)

                if ok_rows:
                    collist = ", ".join(f'"{c}"' for c in ok_rows[0].keys())
                    placeholders = ", ".join(f":{c}" for c in ok_rows[0].keys())
                    with dst.begin() as d:
                        d.execute(text(f'INSERT INTO "public"."{t}" ({collist}) VALUES ({placeholders})'), ok_rows)
                    progressed += len(ok_rows)
                    stats.setdefault(t, {"copied":0, "retried":0, "synth":0, "skipped":0})
                    stats[t]["copied"] += len(ok_rows)

                if again:
                    new_pending[t] = again

            _pending.clear()
            _pending.update(new_pending)
            if progressed == 0:
                print("No further progress on retries.")
                break

        # Reporte final de pendientes (no rompe el deploy)
        left = sum(len(v) for v in _pending.values())
        if left:
            print("---- PENDING (unresolved FK rows) ----")
            for t, rows in _pending.items():
                print(f"  {t}: {len(rows)} rows")
        else:
            print("All pending rows resolved.")

    if fk_globally_disabled and not DRY_RUN:
        with dst.begin() as d:
            maybe_set_replication_role(d, "origin")
        print("FKs re-enabled (origin mode).")

    fix_sequences(dst)

    # Resumen
    if stats:
        print("---- MIGRATION STATS ----")
        for t, s in stats.items():
            print(f"{t}: copied={s.get('copied',0)} retried={s.get('retried',0)} synth={s.get('synth',0)} skipped={s.get('skipped',0)}")

    print("MIGRATION DONE")

if __name__ == "__main__":
    main()
