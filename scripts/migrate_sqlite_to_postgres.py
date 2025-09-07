# -*- coding: utf-8 -*-
import os
from collections import deque
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import Engine

EXCLUDE = {"alembic_version"}
CHUNK = 1000

def topo_order(dst: Engine, tables):
    ie = inspect(dst)
    deps = {t: set() for t in tables}
    rdeps = {t: set() for t in tables}
    for t in tables:
        for fk in ie.get_foreign_keys(t):
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
    src = create_engine('sqlite:////data/database.db')  # origen REAL en Render
    dst_url = os.environ['SQLALCHEMY_DATABASE_URI']     # destino Postgres
    dst = create_engine(dst_url, pool_pre_ping=True)

    isrc = inspect(src)
    idst = inspect(dst)

    src_tables = {t for t in isrc.get_table_names() if not t.startswith('__') and not t.startswith('_alembic_tmp')}
    dst_tables = {t for t in idst.get_table_names() if not t.startswith('__') and not t.startswith('_alembic_tmp')}
    tables = [t for t in (dst_tables & src_tables) if t not in EXCLUDE]

    order = topo_order(dst, tables)
    print("Copy order:")
    for t in order:
        print(" -", t)

    drop_first = os.environ.get("DROP_FIRST") == "1"
    if drop_first:
        with dst.begin() as d:
            for t in reversed(order):
                try:
                    d.execute(text(f'TRUNCATE TABLE "{t}" RESTART IDENTITY CASCADE'))
                except Exception as e:
                    print(f"skip truncate {t}: {e}")
        print("TRUNCATE done.")

    for t in order:
        dst_cols = {c['name'] for c in idst.get_columns(t)}
        src_cols = {c['name'] for c in isrc.get_columns(t)}
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

        offset = 0
        while offset < total:
            with src.connect() as s:
                rows = s.execute(
                    text(f'SELECT {collist} FROM "{t}" LIMIT :lim OFFSET :off'),
                    {"lim": CHUNK, "off": offset}
                ).mappings().all()
            if not rows:
                break
            with dst.begin() as d:
                d.execute(text(f'INSERT INTO "{t}" ({collist}) VALUES ({placeholders})'), rows)
            offset += len(rows)
            print(f"  -> {offset}/{total}")

    q = text("""
        SELECT c.table_schema, c.table_name, c.column_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON c.table_schema = t.table_schema AND c.table_name = t.table_name
        WHERE t.table_type='BASE TABLE'
          AND c.column_default LIKE 'nextval%%'
          AND c.table_schema='public'
    """)
    with dst.begin() as d:
        res = d.execute(q).fetchall()
        for schema, table, col in res:
            seq = d.execute(
                text("SELECT pg_get_serial_sequence(:tbl, :col)"),
                {"tbl": f'{schema}.{table}', "col": col}
            ).scalar()
            if not seq:
                continue
            maxv = d.execute(text(f'SELECT COALESCE(MAX("{col}"),0) FROM "{schema}"."{table}"')).scalar()
            d.execute(
                text("SELECT setval(:seq, :val, :is_called)"),
                {"seq": seq, "val": (maxv or 0) + 1, "is_called": False}
            )
            print(f"fix sequence: {table}.{col} -> {seq} = {(maxv or 0) + 1}")

    print("DONE")

if __name__ == "__main__":
    main()
