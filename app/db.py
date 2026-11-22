"""SQLite 存储层:换算底表(按仓分版本)、出库批次与签收对账单。

事务语义(对应需求):
- 换表走 :func:`write_tx`(BEGIN IMMEDIATE),整个"读另一仓现行关系 →
  校验 → 写入新表"过程持有写锁。并发换表被串行化,后进入者读到的
  另一仓关系一定是对方已提交的现行版本,即"按提交时另一仓的现行关系核准";
  校验失败抛异常即回滚,原表保留。
- 查询走 :func:`read_tx`,一次查询的所有读取在同一个读事务(WAL 快照)
  内完成,保证"一次查询用同一份已提交的换算关系"。
- 批次只存原始行(商品/数量/单位),折算在查询时按当前生效底表现算,
  因此换表成功后已入库批次自动按新关系出数。
- 对账单开单时把所选商品出库行的折件数、两仓底表版号冻结进库;核实状态也在
  写事务内更新,并发核实由写锁串行化。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversion_tables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    warehouse TEXT NOT NULL CHECK (warehouse IN ('A', 'B')),
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
    created_at TEXT NOT NULL,
    UNIQUE (warehouse, version)
);
CREATE TABLE IF NOT EXISTS conversion_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_id INTEGER NOT NULL REFERENCES conversion_tables (id),
    product TEXT NOT NULL,
    from_unit TEXT NOT NULL,
    to_unit TEXT NOT NULL,
    ratio INTEGER NOT NULL CHECK (ratio > 0)
);
CREATE INDEX IF NOT EXISTS idx_relations_table ON conversion_relations (table_id);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_no TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches (id),
    line_no INTEGER NOT NULL,
    product TEXT NOT NULL,
    quantity TEXT NOT NULL,  -- 精确十进制字符串,折算时用 Fraction 解析
    unit TEXT NOT NULL,
    UNIQUE (batch_id, line_no)
);
-- 对账单:每批每种商品至多一张。开单时冻结出库行折件数与两仓底表版号。
CREATE TABLE IF NOT EXISTS reconciliations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches (id),
    product TEXT NOT NULL,
    created_at TEXT NOT NULL,
    relation_a_version INTEGER NOT NULL,
    relation_b_version INTEGER NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    UNIQUE (batch_id, product)
);
CREATE TABLE IF NOT EXISTS reconciliation_outbound (
    reconciliation_id INTEGER NOT NULL REFERENCES reconciliations (id),
    line_no INTEGER NOT NULL,
    pieces TEXT NOT NULL,  -- 冻结的折件数(精确十进制字符串)
    PRIMARY KEY (reconciliation_id, line_no)
);
CREATE TABLE IF NOT EXISTS reconciliation_receipt_lines (
    reconciliation_id INTEGER NOT NULL REFERENCES reconciliations (id),
    line_no INTEGER NOT NULL,
    pieces TEXT NOT NULL,  -- 冻结的签收件数(精确十进制字符串)
    verified INTEGER NOT NULL DEFAULT 0 CHECK (verified IN (0, 1)),
    verified_at TEXT,
    PRIMARY KEY (reconciliation_id, line_no)
);
CREATE TABLE IF NOT EXISTS reconciliation_candidates (
    reconciliation_id INTEGER NOT NULL REFERENCES reconciliations (id),
    receipt_line_no INTEGER NOT NULL,
    outbound_line_no INTEGER NOT NULL,
    PRIMARY KEY (reconciliation_id, receipt_line_no, outbound_line_no)
);
"""

#: 首次启动的种子底表(A 仓 v1),作为"换表前"的初始状态;B 仓初始为空表。
SEED_RELATIONS: dict[str, list[tuple[str, str, str, int]]] = {
    "A": [
        ("P001", "箱", "提", 12),
        ("P001", "提", "件", 6),
        ("P003", "箱", "件", 10),
    ],
}


def db_path() -> str:
    return os.environ.get("APP_DB_PATH", "data/app.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """读事务:事务内所有读取看到同一份已提交快照。"""
    conn = connect()
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        _rollback_quietly(conn)
        raise
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """写事务:BEGIN IMMEDIATE 立即取写锁,串行化并发换表。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        _rollback_quietly(conn)
        raise
    finally:
        conn.close()


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db() -> None:
    """建库建表;库为空时写入种子底表。幂等,可重复调用。"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path())), exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        empty = conn.execute("SELECT COUNT(*) AS c FROM conversion_tables").fetchone()["c"] == 0
        if empty:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for warehouse, relations in SEED_RELATIONS.items():
                    replace_active_table(conn, warehouse, relations)
                conn.execute("COMMIT")
            except BaseException:
                _rollback_quietly(conn)
                raise
    finally:
        conn.close()


def replace_active_table(
    conn: sqlite3.Connection,
    warehouse: str,
    relations: list[tuple[str, str, str, int]],
) -> int:
    """整表替换某仓换算关系,返回新版本号。必须在写事务内调用。

    ``relations`` 为 (product, from_unit, to_unit, ratio) 四元组;旧版本
    保留为 superseded,便于审计追溯。
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM conversion_tables WHERE warehouse = ?",
        (warehouse,),
    ).fetchone()
    version = row["v"] + 1
    conn.execute(
        "UPDATE conversion_tables SET status = 'superseded' WHERE warehouse = ? AND status = 'active'",
        (warehouse,),
    )
    cur = conn.execute(
        "INSERT INTO conversion_tables (warehouse, version, status, created_at) VALUES (?, ?, 'active', ?)",
        (warehouse, version, _now()),
    )
    table_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO conversion_relations (table_id, product, from_unit, to_unit, ratio) VALUES (?, ?, ?, ?, ?)",
        [(table_id, product, from_unit, to_unit, ratio) for product, from_unit, to_unit, ratio in relations],
    )
    return version


def get_active_version(conn: sqlite3.Connection, warehouse: str) -> int:
    row = conn.execute(
        "SELECT version FROM conversion_tables WHERE warehouse = ? AND status = 'active'",
        (warehouse,),
    ).fetchone()
    return row["version"] if row else 0


def get_active_relations(conn: sqlite3.Connection, warehouse: str | None = None) -> list[sqlite3.Row]:
    """当前生效的换算关系;不指定仓库时返回两仓合并结果(每行带来源仓)。"""
    sql = (
        "SELECT r.product, r.from_unit, r.to_unit, r.ratio, t.warehouse "
        "FROM conversion_relations r "
        "JOIN conversion_tables t ON t.id = r.table_id "
        "WHERE t.status = 'active'"
    )
    params: tuple = ()
    if warehouse is not None:
        sql += " AND t.warehouse = ?"
        params = (warehouse,)
    sql += " ORDER BY t.warehouse, r.product, r.id"
    return conn.execute(sql, params).fetchall()


def create_batch(
    conn: sqlite3.Connection,
    batch_no: str,
    lines: list[tuple[int, str, str, str]],
) -> str:
    """插入批次及行(行: line_no, product, quantity, unit),返回创建时间。"""
    created_at = _now()
    cur = conn.execute(
        "INSERT INTO batches (batch_no, created_at) VALUES (?, ?)",
        (batch_no, created_at),
    )
    batch_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO batch_lines (batch_id, line_no, product, quantity, unit) VALUES (?, ?, ?, ?, ?)",
        [(batch_id, line_no, product, quantity, unit) for line_no, product, quantity, unit in lines],
    )
    return created_at


def get_batch(conn: sqlite3.Connection, batch_no: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE batch_no = ?", (batch_no,)).fetchone()


def get_batch_lines(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batch_lines WHERE batch_id = ? ORDER BY line_no", (batch_id,)
    ).fetchall()


def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT b.batch_no, b.created_at, COUNT(l.id) AS line_count "
        "FROM batches b LEFT JOIN batch_lines l ON l.batch_id = b.id "
        "GROUP BY b.id ORDER BY b.id"
    ).fetchall()


# ---------------------------------------------------------------- 对账单


def get_reconciliation(conn: sqlite3.Connection, batch_id: int, product: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reconciliations WHERE batch_id = ? AND product = ?",
        (batch_id, product),
    ).fetchone()


def get_reconciliation_by_id(conn: sqlite3.Connection, reconciliation_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reconciliations WHERE id = ?", (reconciliation_id,)
    ).fetchone()


def create_reconciliation(
    conn: sqlite3.Connection,
    batch_id: int,
    product: str,
    versions: dict[str, int],
    outbound: list[tuple[int, str]],
    receipt_lines: list[tuple[int, str, bool, list[int]]],
) -> int:
    """开对账单。``receipt_lines`` 为 (line_no, pieces, verified, candidates)。"""
    cur = conn.execute(
        "INSERT INTO reconciliations "
        "(batch_id, product, created_at, relation_a_version, relation_b_version) "
        "VALUES (?, ?, ?, ?, ?)",
        (batch_id, product, _now(), versions.get("A", 0), versions.get("B", 0)),
    )
    reconciliation_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO reconciliation_outbound (reconciliation_id, line_no, pieces) VALUES (?, ?, ?)",
        [(reconciliation_id, line_no, pieces) for line_no, pieces in outbound],
    )
    for line_no, pieces, verified, candidates in receipt_lines:
        conn.execute(
            "INSERT INTO reconciliation_receipt_lines "
            "(reconciliation_id, line_no, pieces, verified, verified_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (reconciliation_id, line_no, pieces, 1 if verified else 0, _now() if verified else None),
        )
        conn.executemany(
            "INSERT INTO reconciliation_candidates "
            "(reconciliation_id, receipt_line_no, outbound_line_no) VALUES (?, ?, ?)",
            [(reconciliation_id, line_no, candidate) for candidate in candidates],
        )
    return reconciliation_id


def get_reconciliation_outbound(
    conn: sqlite3.Connection, reconciliation_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM reconciliation_outbound WHERE reconciliation_id = ? ORDER BY line_no",
        (reconciliation_id,),
    ).fetchall()


def get_reconciliation_receipt_lines(
    conn: sqlite3.Connection, reconciliation_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM reconciliation_receipt_lines WHERE reconciliation_id = ? ORDER BY line_no",
        (reconciliation_id,),
    ).fetchall()


def get_reconciliation_candidates(
    conn: sqlite3.Connection, reconciliation_id: int
) -> dict[int, list[int]]:
    rows = conn.execute(
        "SELECT receipt_line_no, outbound_line_no FROM reconciliation_candidates "
        "WHERE reconciliation_id = ? ORDER BY receipt_line_no, outbound_line_no",
        (reconciliation_id,),
    ).fetchall()
    result: dict[int, list[int]] = {}
    for row in rows:
        result.setdefault(row["receipt_line_no"], []).append(row["outbound_line_no"])
    return result


def mark_verified(
    conn: sqlite3.Connection, reconciliation_id: int, line_nos: list[int]
) -> tuple[list[int], list[int]]:
    """把签收行标为已核实。返回 (本次新标记的行号, 原本已核实的行号)。

    幂等:同一行重复核实只记一次;已核实的行保留原 ``verified_at``。
    """
    already: list[int] = []
    newly: list[int] = []
    now = _now()
    for line_no in line_nos:
        row = conn.execute(
            "SELECT verified FROM reconciliation_receipt_lines "
            "WHERE reconciliation_id = ? AND line_no = ?",
            (reconciliation_id, line_no),
        ).fetchone()
        if row is None:
            raise KeyError(line_no)
        if row["verified"]:
            already.append(line_no)
        else:
            conn.execute(
                "UPDATE reconciliation_receipt_lines SET verified = 1, verified_at = ? "
                "WHERE reconciliation_id = ? AND line_no = ?",
                (now, reconciliation_id, line_no),
            )
            newly.append(line_no)
    return newly, already


def bump_revision(conn: sqlite3.Connection, reconciliation_id: int) -> int:
    conn.execute(
        "UPDATE reconciliations SET revision = revision + 1 WHERE id = ?",
        (reconciliation_id,),
    )
    row = conn.execute(
        "SELECT revision FROM reconciliations WHERE id = ?", (reconciliation_id,)
    ).fetchone()
    return row["revision"]
