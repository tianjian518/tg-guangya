# -*- coding: utf-8 -*-
"""修复分类判错的转存产物（一条龙：删产物 → 删记录 → 修复后代码重提/挪窝）。

分享类：删云盘产物 + 删 DB 记录 → 用修复后的分类代码重提全链路；
磁力类：产物已下载完成，直接移动到正确分类目录并规范重命名。

用法：python3 tools/requote_wrong.py [--dry]
"""
import re
import sqlite3
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "tools")

from adapters.web_scraper import ChannelMessage, extract_links
from core.config import AppConfig
from core.guangya import GuangyaClient
from core.store import Store

SHARE_KW = ["琅琊榜", "汴京", "九阳武神", "绝世战魂", "万古神帝", "炼气十万年", "一斩苍穹"]
# 磁力/ed2k 类：关键词 → (目标分类, 新名)。产物已存在，只挪窝+改名。
MAGNET_ITEMS = [
    ("森中有林", "华语电影", "森中有林 (2026)"),
    ("抓特务", "华语电影", "抓特务 (2026)"),
    ("百年孤独", "其他剧集", "百年孤独 (2024)"),
]


def main() -> None:
    dry = "--dry" in sys.argv
    cfg = AppConfig.load("data/config.yaml")
    store = Store(cfg.storage_db)
    client = GuangyaClient(cfg.guangya.client_id, cfg.guangya.refresh_token)
    root = cfg.output.parent_id
    db = sqlite3.connect(cfg.storage_db)
    db.row_factory = sqlite3.Row
    dirs = {d["name"]: d["file_id"] for d in client.list_dir(root)}

    def find_product(kw: str):
        """全目录找名字含关键词的产物，返回 (目录名, 目录id, file_id, 文件名)。"""
        for dname, did in dirs.items():
            for it in client.list_dir(did):
                if kw in it["name"]:
                    return dname, did, it["file_id"], it["name"]
        return None

    todo = []  # (kw, title_or_None, cn_folder, hash)
    for kw in SHARE_KW:
        row = db.execute("select hash, cn_folder, title from magnets where cn_folder like ? or title like ?",
                         ("%" + kw + "%", "%" + kw + "%")).fetchone()
        if row:
            todo.append((kw, row["title"], row["cn_folder"], row["hash"]))
        else:
            print("DB 无记录:", kw)

    # 1. 删旧产物 + 删记录
    for kw, title, folder, h in todo:
        hit = find_product(kw)
        if hit:
            dname, did, fid, name = hit
            print("[%s] 删产物 %s/%s" % (kw, dname, name[:40]))
            if not dry:
                client.delete_file(did, fid)
        else:
            print("[%s] 云盘未找到产物（可能没落盘）" % kw)
        if not dry:
            db.execute("delete from magnets where hash=?", (h,))
    if not dry:
        db.commit()

    # 2. 磁力类：移动 + 重命名（产物保留）
    for kw, cat, new_name in MAGNET_ITEMS:
        hit = find_product(kw)
        if not hit:
            print("[磁力%s] 云盘未找到产物" % kw)
            continue
        dname, did, fid, name = hit
        target_dir = dirs.get(cat) or client.create_folder(root, cat)
        if cat not in dirs:
            dirs[cat] = target_dir
        if dname != cat:
            print("[磁力%s] 移动 %s → %s" % (kw, dname, cat))
            if not dry:
                client.move_file(fid, target_dir)
        if not dry:
            try:
                client.rename_file(fid, new_name)
                print("[磁力%s] 改名 → %s" % (kw, new_name))
            except Exception as exc:  # noqa: BLE001
                print("[磁力%s] 改名失败(可能已一致): %s" % (kw, exc))

    # 3. 分享类重提（修复后的分类代码全链路）
    if dry:
        return
    from tools.test_share_batch import build_handler
    _store, _client, handler = build_handler(cfg)
    for kw, title, folder, _h in todo:
        if not title:
            print("[重提%s] 原帖文本缺失，跳过" % kw)
            continue
        links = [u for u in extract_links(title) if parse_ok(u)]
        if not links:
            print("[重提%s] 原帖里没有可提的链接: %r" % (kw, title[:60]))
            continue
        print("[重提%s] %d 条链接，走 handler 全链路" % (kw, len(links)))
        msg = ChannelMessage(channel="requote", message_id="requote-" + kw,
                             text=title, links=links, links_from="body")
        handler(msg)


def parse_ok(u: str) -> bool:
    return u.startswith("http") or u.startswith("magnet:") or u.startswith("ed2k:")


if __name__ == "__main__":
    main()
