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


@dataclass
class TitleRecord:
    """内容账本：一部片 / 一集 曾成功落盘的凭证（去重主判据）。

    norm_key  账本主键：folder 的小写字母数字形（如 庆余年s01e06）。
               同一内容无论哪个频道、哪种写法、哪个 hash，此值唯一。
    norm_core 片名主体的归一形（用于「整包 vs 已按集收录」这类跨集判断）。
    sig       空=电影/多集包；s01e06=单集；s02=整季。
    folder    落盘时的文件夹名（人类可读）。
    """
    norm_key: str
    norm_core: str = ""
    sig: str = ""
    is_pack: bool = False
    year: int = 0
    title: str = ""
    folder: str = ""
    category: str = ""
    quality: int = 0        # 落盘资源标题的质量分（洗版判断旧版本优劣用）
    created_at: float = 0.0


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
        # 内容账本：同内容曾成功落盘 → 后续同片（不同 hash / 不同写法）直接跳过
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS titles (
                norm_key   TEXT PRIMARY KEY,
                norm_core  TEXT,
                sig        TEXT DEFAULT '',
                is_pack    INTEGER DEFAULT 0,
                year       INTEGER DEFAULT 0,
                title      TEXT,
                folder     TEXT,
                category   TEXT,
                quality    INTEGER DEFAULT 0,
                created_at REAL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_titles_core ON titles(norm_core)"
        )
        # 老库升级：titles 表若已存在但没有 quality 列则补上
        try:
            cols = [r[1] for r in self._conn.execute("PRAGMA table_info(titles)")]
            if "quality" not in cols:
                self._conn.execute(
                    "ALTER TABLE titles ADD COLUMN quality INTEGER DEFAULT 0")
        except Exception:  # noqa: BLE001
            pass
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

    def history(self, limit: int = 50, status: str = "",
                offset: int = 0) -> list[MagnetRecord]:
        """监控历史：按更新时间倒序返回记录，可按状态过滤。供面板「监控历史」页使用。"""
        sql = ("SELECT hash,channel,message_id,title,status,task_id,reason,category,"
               "created_at,updated_at FROM magnets")
        args: list = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        rows = self._conn.execute(sql, args).fetchall()
        return [MagnetRecord(*r) for r in rows]

    # ---------------- 内容账本（titles） ----------------

    def add_title(self, rec: TitleRecord) -> None:
        """记录「某内容已成功落盘」。同 key 重复写入按最新一条覆盖。"""
        if not rec.norm_key:
            return
        rec.created_at = rec.created_at or time.time()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO titles
            (norm_key, norm_core, sig, is_pack, year, title, folder, category,
             quality, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (rec.norm_key, rec.norm_core, rec.sig, int(rec.is_pack), rec.year,
             rec.title, rec.folder, rec.category, rec.quality, rec.created_at),
        )
        self._conn.commit()

    def title_exists(self, norm_key: str) -> bool:
        """账本是否已有同 key 内容（同内容曾成功落盘）。"""
        if not norm_key:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM titles WHERE norm_key=?", (norm_key,)).fetchone()
        return row is not None

    def title_get(self, norm_key: str) -> TitleRecord | None:
        """取账本记录（含落盘时标题的质量分，供洗版判断）。"""
        if not norm_key:
            return None
        row = self._conn.execute(
            "SELECT norm_key,norm_core,sig,is_pack,year,title,folder,category,"
            "quality,created_at FROM titles WHERE norm_key=?",
            (norm_key,)).fetchone()
        if not row:
            return None
        return TitleRecord(norm_key=row[0], norm_core=row[1], sig=row[2],
                           is_pack=bool(row[3]), year=row[4], title=row[5],
                           folder=row[6], category=row[7], quality=row[8],
                           created_at=row[9])

    def title_has_episodes(self, norm_core: str) -> bool:
        """同片名下是否已有「单集/整季」记录（用于判断整包是否多为重复）。"""
        if not norm_core:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM titles WHERE norm_core=? AND sig<>'' LIMIT 1",
            (norm_core,)).fetchone()
        return row is not None

    def title_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM titles").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        self._conn.close()
