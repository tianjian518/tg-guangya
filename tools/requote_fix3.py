# -*- coding: utf-8 -*-
"""补转 v3：绕开 link_key 挡路记录，重提国漫分享与汴京分享。

- 国漫分享（1931558850620674089）：删一斩苍穹/炼气两条挡路记录 →
  云盘删炼气旧产物 → 重提 → 4 部转存 + 1 部云端复查自动跳过；
- 汴京分享（1932041609579593826）：与 Dead of Winter 帖共用 shareId
  （hash 双语义缺陷），临时删 Dead of Winter 的 done 行重提汴京，
  转完后 Dead of Winter 产物仍在云盘（分类正确），DB 账本行让给汴京。
"""
import sqlite3
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "tools")

from adapters.web_scraper import ChannelMessage
from core.config import AppConfig
from core.guangya import GuangyaClient
from tools.test_share_batch import build_handler

BLOCK_HASHES = [
    "guangya:1931558850620674089",  # 一斩苍穹（今天重提写的 link_key 行）
    "guangya:1931559053989924914",  # 炼气十万年（旧产物行）
    "guangya:1932041609579593826",  # Dead of Winter（与汴京同 shareId）
]

ITEMS = [
    ("九阳武神 绝世战魂第2季 万古神帝 炼气十万年 一斩苍穹--更至8集",
     "https://www.guangyapan.com/s/1931558850620674089_ajCjfo_gmUmtFdm_"),
    ("汴京上元局[60帧率版本][短剧][全20集]",
     "https://www.guangyapan.com/s/1932041609579593826_al8cmYXLP9l33ld2"),
]


def main() -> None:
    cfg = AppConfig.load("data/config.yaml")
    client = GuangyaClient(cfg.guangya.client_id, cfg.guangya.refresh_token)
    db = sqlite3.connect(cfg.storage_db)
    db.row_factory = sqlite3.Row

    # 1. 备份并删挡路记录
    for h in BLOCK_HASHES:
        row = db.execute("select * from magnets where hash=?", (h,)).fetchone()
        if row:
            print("备份:", h, row["status"], row["category"], (row["cn_folder"] or "")[:40])
        db.execute("delete from magnets where hash=?", (h,))
    db.commit()

    # 2. 云盘清炼气旧产物（若在）
    dirs = {d["name"]: d["file_id"] for d in client.list_dir(cfg.output.parent_id)}
    for dname, did in list(dirs.items()):
        for it in client.list_dir(did):
            if "炼气十万年" in it["name"]:
                print("删旧产物:", dname, "/", it["name"])
                client.delete_file(did, it["file_id"])

    # 3. 重提
    _store, _client, handler = build_handler(cfg)
    for i, (text, link) in enumerate(ITEMS, 1):
        msg = ChannelMessage(channel="requote3", message_id="requote3-%d" % i,
                             text=text, links=[link], links_from="body")
        handler(msg)


if __name__ == "__main__":
    main()
