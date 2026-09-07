"""分类目录自动创建与解析。

职责：把分类器给出的目录路径（「华语电影」或「电影/华语」）换成光鸭的真实 fileId。

- 目录已存在 → 直接复用，不重复创建；
- 目录不存在 → 自动在转存根目录下建出来（offline 下载就能直接落进去）；
- 建目录/查询失败 → 降级回已解析到的层级（最差回根目录），**保证资源不丢**，
  宁可放错目录也不能因为建目录失败而丢弃一条磁力。

解析结果带 TTL 缓存 + 可重入锁：频道刷屏时同一分类会被并发命中，
没有缓存会疯狂调用列表接口，没有锁则可能并发创建出「华语电影」「华语电影(1)」。
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)


class CategoryResolver:
    def __init__(self, client, root_id: str = "", create_missing: bool = True,
                 ttl: float = 600.0) -> None:
        self.client = client
        self.root_id = root_id or ""
        self.create_missing = create_missing
        self.ttl = ttl
        self._lock = threading.RLock()
        # (parent_id, 目录名) -> (file_id, 写入时间)
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}

    def set_root(self, root_id: str) -> None:
        """转存根目录变了（用户在设置里改了目标目录）就整体失效缓存。"""
        root_id = root_id or ""
        if root_id != self.root_id:
            with self._lock:
                if root_id != self.root_id:
                    self.root_id = root_id
                    self._cache.clear()
                    log.info("转存根目录已更新，分类目录缓存已清空")

    def resolve(self, category: str, create_missing: bool | None = None) -> tuple[str, str]:
        """返回 (file_id, 实际生效的目录路径)。失败时回退到能拿到的最深层目录。

        create_missing=None 时使用实例默认值；传 False 表示「只查不建」，
        用于云端查重时避免为尚未出现过的分类无谓建空目录。
        """
        if create_missing is None:
            create_missing = self.create_missing
        category = (category or "").strip().strip("/")
        if not category:
            return self.root_id, ""
        parts = [p.strip() for p in category.split("/") if p.strip()]
        parent = self.root_id
        walked: list[str] = []
        for part in parts:
            try:
                parent = self._ensure_dir(parent, part, create_missing)
            except Exception as exc:  # 建目录失败不应中断整条转存
                if create_missing:  # 仅在真正需要时记录降级
                    log.warning("分类目录「%s」解析失败（%s），降级到 %s",
                                part, exc, "/".join(walked) or "根目录")
                break
            walked.append(part)
        return parent, "/".join(walked)

    def exists(self, category: str) -> bool:
        """分类目录是否在转存根目录下真实存在（不创建）。用于云端查重前判断。"""
        try:
            _, path = self.resolve(category, create_missing=False)
        except Exception:
            return False
        want = [p.strip() for p in (category or "").strip("/").split("/") if p.strip()]
        got = [p.strip() for p in path.split("/") if p.strip()]
        return len(want) > 0 and len(got) == len(want)

    def _ensure_dir(self, parent: str, name: str, create_missing: bool) -> str:
        key = (parent, name)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[1] < self.ttl:
                return hit[0]
            for f in self.client.list_folders(parent):
                if (f.get("name") or "").strip() == name:
                    fid = (f.get("file_id") or "").strip()
                    if fid:
                        self._cache[key] = (fid, now)
                        return fid
            if not create_missing:
                raise RuntimeError(f"目录不存在且未开启自动创建: {name}")
            fid = self.client.create_folder(parent, name)
            self._cache[key] = (fid, now)
            return fid

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
