# -*- coding: utf-8 -*-
import argparse
from typing import List, Dict, Set
from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import Engine
from collections import deque

EXCLUDE_TABLES = {"alembic_version"}
MANUAL_TABLE_ORDER_FIRST: List[str] = []
MANUAL_TABLE_ORDER_LAST: List[str] = []
CHUNK_SIZE = 1000

def topo_sort_tables(src: Engine) -> List[str]:
    insp = inspect(src)
    tables = [t for t in insp.get_table_names() if t not in EXCLUDE_TABLES]
    deps: Dict[str, Set[str]] = {t: set() for t in tables}
    rdeps: Dict[str, Set[str]] = {t: set() for t in tables}
    for t in tables:
        for fk in insp.get_foreign_keys(t):
            ref = fk.get("referred_table")
            if ref and ref in deps:
                deps[t].add(ref)
                rdeps[ref].add(t)
    indeg = {t: len(deps[t]) for t in tables}
    q = deque([t for t in tables if indeg[t] == 0])
    order: List[str] = []
    while q:
        u = q.popleft()
        order.append(u)
        for v in rdeps[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    if len(order) != len(tables):
        remaining = [t for t in tables if t not in order]
        order.extend(remaining)

    def apply_manual(o):
        first = [t for t in MANUAL_TABLE_ORDER_FIRST if t in o]
        last  = [t for t in MANUAL_TABLE_ORDER_LAST  if t in o]
        middle = [t for t in o if t not in first and t not in last]
        return first + middle + last

    return apply_manual(order)

def copy_table(src: Engine, dst: Engine, table: str):
    with src.connect() as s, dst.begin() as d:
        cols = [c["name"] for c in inspect(src).get_columns(table)]
        collist = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(f":{c}" for c in cols)
        total = s.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
        print(f'[{table}] {total} rows to copy...')
        if not total:
            return
        offset = 0
        while offset < total:
            rows = s.execute(text(f'SELECT * FROM "{table}" LIMIT {CHUNK_SIZE} OFFSET {offset}')).mappings().all()
            if not rows:
                break
            d.execute(text(f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})'), rows)
            offset += len(rows)
            print(f'  -> {offset}/{total}')

def fix_sequences(dst: Engine):
    q = text("""
        SELECT c.table_schema AS schema, c.table_name AS table, c.column_name AS col
        FROM information_schema.columns c
        WHERE c.table_schema='public' AND c.column_default LIKE 'nextval(%'
    """)
    with dst.begin() as d:
        rows = d.execute(q).mappings().all()
        for r in rows:
            schema, table, col = r["schema"], r["table"], r["col"]
            seq = d.execute(text("SELECT pg_get_serial_sequence(:tbl, :col)"),
                            {"tbl": f"{schema}.{table}", "col": col}).scalar()
            if not seq:
                continue
            maxval = d.execute(text(f'SELECT COALESCE(MAX("{col}"),0) FROM "{schema}"."{table}"')).scalar()
            d.execute(text("SELECT setval(:seq, :val, :is_called)"),
                      {"seq": seq, "val": (maxval or 0) + 1, "is_called": False})
            print(f'  fixed sequence: {table}.{col} -> {seq} = {(maxval or 0) + 1}')

def main():
    ap = argparse.ArgumentParser(description="Copy data from SQLite to Postgres.")
    ap.add_argument("--sqlite", required=True, help="Path to SQLite db (e.g. local_tmp.db)")
    ap.add_argument("--pg", required=True, help="Postgres URL (postgresql+psycopg://...sslmode=require)")
    ap.add_argument("--drop-first", action="store_true", help="TRUNCATE destination before inserting")
    args = ap.parse_args()

    src_url = f"sqlite:///{args.sqlite}" if not args.sqlite.startswith("sqlite") else args.sqlite
    dst_url = args.pg

    src = create_engine(src_url)
    dst = create_engine(dst_url, pool_pre_ping=True)

    order = topo_sort_tables(src)
    print("Copy order:")
    for t in order:
        print(" -", t)

    if args.drop_first:
        with dst.begin() as d:
            for t in reversed(order):
                d.execute(text(f'TRUNCATE TABLE "{t}" RESTART IDENTITY CASCADE'))
        print("TRUNCATE done.")

    for t in order:
        if t in EXCLUDE_TABLES:
            continue
        copy_table(src, dst, t)

    fix_sequences(dst)
    print("Migration complete.")

if __name__ == "__main__":
    main()
