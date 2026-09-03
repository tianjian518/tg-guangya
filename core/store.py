"""SQLite 去重与状态存储。

作用：
- 记录已提交的磁力 hash，避免频道刷屏导致重复添加；
- 记录每条磁力的提交结果（成功/失败/跳过）与离线任务 id，便于排查与统计。
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class MagnetRecord:
    hash: str
    channel: str = ""
    message_id: str = ""
    title: str = ""
    status: str = "pending"   # pending / submitted / skipped / failed / done
    task_id: str = ""
    reason: str = ""
    category: str = ""        # 自动分类命中的子目录（如「华语电影」）
    created_at: float = 0.0
    updated_at: float = 0.0


class Store:
    def __init__(self, path: str = "tg_guangya.db") -> None:
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS magnets (
                hash TEXT PRIMARY KEY,
                channel TEXT,
                message_id TEXT,
                title TEXT,
                status TEXT,
                task_id TEXT,
                reason TEXT,
                created_at REAL,
                updated_at REAL
            )
            """
        )
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """旧库补列：新增 category 字段（记录自动分类结果）。"""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(magnets)")}
        if "category" not in cols:
            self._conn.execute("ALTER TABLE magnets ADD COLUMN category TEXT")


    def seen(self, hash_: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM magnets WHERE hash=?", (hash_,))
        return cur.fetchone() is not None

    def get(self, hash_: str) -> Optional[MagnetRecord]:
        """按 hash 取单条记录；不存在返回 None。供云端去重复查本地历史。"""
        row = self._conn.execute(
            "SELECT hash,channel,message_id,title,status,task_id,reason,category,created_at,updated_at "
            "FROM magnets WHERE hash=?",
            (hash_,),
        ).fetchone()
        return MagnetRecord(*row) if row else None

    def add(self, rec: MagnetRecord) -> None:
        now = time.time()
        if not rec.created_at:
            rec.created_at = now
        rec.updated_at = now
        self._conn.execute(
            """
            INSERT OR REPLACE INTO magnets
            (hash, channel, message_id, title, status, task_id, reason, category, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rec.hash, rec.channel, rec.message_id, rec.title,
                rec.status, rec.task_id, rec.reason, rec.category,
                rec.created_at, rec.updated_at,
            ),
        )
        self._conn.commit()

    def update(self, hash_: str, status: str = "", task_id: str = "",
               reason: str = "", category: str = "") -> None:
        sets, vals = [], []
        if status:
            sets.append("status=?"); vals.append(status)
        if task_id:
            sets.append("task_id=?"); vals.append(task_id)
        if reason:
            sets.append("reason=?"); vals.append(reason)
        if category:
            sets.append("category=?"); vals.append(category)
        if not sets:
            return
        sets.append("updated_at=?"); vals.append(time.time())
        vals.append(hash_)
        self._conn.execute(f"UPDATE magnets SET {','.join(sets)} WHERE hash=?", vals)
        self._conn.commit()

    def stats(self) -> dict:
        row = self._conn.execute(
            "SELECT status, COUNT(*) FROM magnets GROUP BY status"
        ).fetchall()
        return {status: count for status, count in row}

    def recent(self, limit: int = 20) -> list[MagnetRecord]:
        rows = self._conn.execute(
            "SELECT hash,channel,message_id,title,status,task_id,reason,category,created_at,updated_at "
            "FROM magnets ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [MagnetRecord(*r) for r in rows]

    def history(self, limit: int = 50, status: str = "") -> list[MagnetRecord]:
        """监控历史：按更新时间倒序返回记录，可按状态过滤。供面板「监控历史」页使用。"""
        sql = ("SELECT hash,channel,message_id,title,status,task_id,reason,category,"
               "created_at,updated_at FROM magnets")
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        rows = self._conn.execute(sql, args).fetchall()
        return [MagnetRecord(*r) for r in rows]

    def close(self) -> None:
        self._conn.close()
