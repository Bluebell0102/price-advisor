"""SQLite 存储。一个库只放一家店的数据，不做跨店汇总。"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "advisor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS product (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    listed_date TEXT,
    price REAL NOT NULL,
    cost REAL,
    stock REAL NOT NULL,
    manual_stage TEXT
);
CREATE TABLE IF NOT EXISTS sales_day (
    sku TEXT NOT NULL,
    day TEXT NOT NULL,
    price REAL NOT NULL,
    units REAL NOT NULL,
    amount REAL NOT NULL,
    refunds REAL,
    visitors REAL,
    ad_spend REAL,
    ad_units REAL,
    PRIMARY KEY (sku, day)
);
CREATE TABLE IF NOT EXISTS competitor (
    sku TEXT NOT NULL,
    idx INTEGER NOT NULL,
    price REAL NOT NULL,
    day TEXT,
    PRIMARY KEY (sku, idx)
);
CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS setting_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    field TEXT NOT NULL,
    old TEXT,
    new TEXT
);
CREATE TABLE IF NOT EXISTS category_elasticity (
    category TEXT PRIMARY KEY,
    base REAL NOT NULL,
    adjust REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS column_mapping (
    tbl TEXT NOT NULL,
    signature TEXT NOT NULL,
    mapping TEXT NOT NULL,
    PRIMARY KEY (tbl, signature)
);
CREATE TABLE IF NOT EXISTS pending_upload (
    token TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    status TEXT NOT NULL,
    files TEXT NOT NULL,
    detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calc_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    reason TEXT NOT NULL,
    as_of TEXT
);
CREATE TABLE IF NOT EXISTS sku_result (
    run_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    status TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (run_id, sku)
);
CREATE TABLE IF NOT EXISTS sku_override (
    sku TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adoption (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    as_of TEXT,
    price REAL NOT NULL,
    current_price REAL NOT NULL,
    low REAL NOT NULL,
    opt REAL NOT NULL,
    high REAL NOT NULL,
    out_of_range INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ignore_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    reason TEXT NOT NULL,
    direction TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS view_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    at TEXT NOT NULL
);
"""


def db_path() -> Path:
    return Path(os.environ.get("ADVISOR_DB", DEFAULT_DB))


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, isolation_level=None)  # 自己管事务：BEGIN / COMMIT / ROLLBACK
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection):
    """老库补列：cost_source 记成本价来自导入还是页面补录。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(product)")}
    if "cost_source" not in cols:
        conn.execute("ALTER TABLE product ADD COLUMN cost_source TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(calc_run)")}
    if "extra" not in cols:  # 本版的改价事件与类目校准（PT-05、PT-06）
        conn.execute("ALTER TABLE calc_run ADD COLUMN extra TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sku_result)")}
    if "fp" not in cols:  # 输入指纹：输入没变的 SKU 沿用上一版状态，只有变了的才过期（PT-10，R-07）
        conn.execute("ALTER TABLE sku_result ADD COLUMN fp TEXT")
    if "note" not in cols:
        conn.execute("ALTER TABLE sku_result ADD COLUMN note TEXT")


class tx:
    """整批事务：块内任何异常都回滚，不留半份。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        return False


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put_setting(conn, key, value):
    conn.execute("INSERT INTO setting(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))
