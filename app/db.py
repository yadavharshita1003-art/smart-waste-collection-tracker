"""MySQL access layer using PyMySQL."""
import re
import pymysql
from pymysql.cursors import DictCursor
from flask import current_app, g

def get_conn():
    if "db" not in g:
        c=current_app.config
        g.db=pymysql.connect(host=c["MYSQL_HOST"], port=c["MYSQL_PORT"], user=c["MYSQL_USER"], password=c["MYSQL_PASSWORD"], database=c["MYSQL_DATABASE"], charset="utf8mb4", cursorclass=DictCursor, autocommit=False)
    return g.db

def close_conn(_exc=None):
    conn=g.pop("db",None)
    if conn is not None:
        try: conn.rollback()
        finally: conn.close()

def _sql(sql):
    # Convert the original Oracle named binds (:name) to PyMySQL named binds (%(name)s).
    return re.sub(r":([A-Za-z][A-Za-z0-9_]*)", r"%(\1)s", sql)

def query(sql, params=None):
    with get_conn().cursor() as cur:
        cur.execute(_sql(sql), params or {})
        return list(cur.fetchall())

def query_one(sql, params=None):
    rows=query(sql,params)
    return rows[0] if rows else None

def execute(sql, params=None):
    conn=get_conn()
    with conn.cursor() as cur:
        cur.execute(_sql(sql),params or {})
        n=cur.rowcount
    conn.commit()
    return n

def insert(sql, params=None):
    conn=get_conn()
    with conn.cursor() as cur:
        cur.execute(_sql(sql),params or {})
        new_id=cur.lastrowid
    conn.commit()
    return new_id
