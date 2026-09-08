# -*- coding: utf-8 -*-
"""补转：四部国漫（同一分享 5 文件，上轮只转了 1 部）+ 汴京。"""
import sys
sys.path.insert(0, ".")
sys.path.insert(0, "tools")

from adapters.web_scraper import ChannelMessage
from core.config import AppConfig
from tools.test_share_batch import build_handler

ITEMS = [
    # (text, link)
    ("九阳武神 绝世战魂第2季 万古神帝 炼气十万年 一斩苍穹--更至8集",
     "https://www.guangyapan.com/s/1931558850620674089_ajCjfo_gmUmtFdm_"),
    ("汴京上元局[60帧率版本][短剧][全20集]",
     "https://www.guangyapan.com/s/1932041609579593826_al8cmYXLP9l33ld2"),
]


def main() -> None:
    cfg = AppConfig.load("data/config.yaml")
    _store, _client, handler = build_handler(cfg)
    for i, (text, link) in enumerate(ITEMS, 1):
        msg = ChannelMessage(channel="requote2", message_id="requote2-%d" % i,
                             text=text, links=[link], links_from="body")
        handler(msg)


if __name__ == "__main__":
    main()
