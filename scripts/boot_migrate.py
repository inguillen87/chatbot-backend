# scripts/boot_migrate.py
import os
from sqlalchemy import create_engine, text, inspect
from collections import deque

SRC_SQLITE = "sqlite:////data/database.db"  # Render persistent disk
DST_PG = os.environ.get("SQLALCHEMY_DATABASE_URI") or os.environ["DATABASE_URL"]

DO_MIGRATE = os.environ.get("MIGRATE_FROM_SQLITE", "0") == "1"
DROP_FIRST = os.environ.get("DROP_FIRST", "0") == "1"
CHUNK = 1000
EXCLUDE = {"alembic_version"}

def list_tables(ins):
    # real tables only; skip alembic temp
    names = [t for t in ins.get_table_names()
             if t not in EXCLUDE and not t.startswith("_alembic_tmp_") and not t.startswith("__")]
    return names

def topo_sort(ins):
    tables = list_tables(ins)
    deps = {t:set() for t in tables}
    rdeps = {t:set() for t in tables}
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

def main():
    if not DO_MIGRATE:
        print("SKIP migrate: MIGRATE_FROM_SQLITE != 1")
        return

    src = create_engine(SRC_SQLITE)
    dst = create_engine(DST_PG, pool_pre_ping=True)
    isrc = inspect(src)
    idst = inspect(dst)

    src_order = topo_sort(isrc)
    dst_tables = set(idst.get_table_names())
    order = [t for t in src_order if t in dst_tables]

    print("Copy order:")
    for t in order:
        print(" -", t)

    if DROP_FIRST:
        with dst.begin() as d:
            for t in reversed(order):
                d.execute(text(f'TRUNCATE TABLE "{t}" RESTART IDENTITY CASCADE'))
        print("TRUNCATE done.")

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
                rows = s.execute(text(f'SELECT {collist} FROM "{t}" LIMIT {CHUNK} OFFSET {off}')).mappings().all()
            if not rows:
                break
            with dst.begin() as d:
                d.execute(text(f'INSERT INTO "{t}" ({collist}) VALUES ({placeholders})'), rows)
            off += len(rows)
            print(f"  -> {off}/{total}")

    # fix sequences
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
