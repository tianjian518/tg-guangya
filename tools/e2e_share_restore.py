"""光鸭分享转存 · 真实账号端到端验证（v1.7.0）。

用法：python3 tools/e2e_share_restore.py <分享URL> "<频道消息文本>"
  或不带参数跑默认样本（张艺谋作品合集）。

复刻线上链路：真实 config 装配（GuangyaClient/KeywordFilter/Classifier/
CategoryResolver/CloudDedup/make_handler），消息按频道原文格式构造，
验证：识别分享 → 分类建目录 → restore 转存 → 中文命名收纳 → 落库终态。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from core.classifier import Classifier  # noqa: E402
from core.config import AppConfig  # noqa: E402
from core.dedup import CloudDedup  # noqa: E402
from core.guangya import GuangyaClient  # noqa: E402
from core.matcher import KeywordFilter  # noqa: E402
from core.notifier import Notifier  # noqa: E402
from core.organizer import CategoryResolver  # noqa: E402
from core.store import Store  # noqa: E402

DEFAULT_URL = "https://www.guangyapan.com/s/1942428110031167491_adyP1Y8EdLN_2AaC"
DEFAULT_TEXT = "「张艺谋作品」，链接：" + DEFAULT_URL


class Msg:
    def __init__(self, text, links, channel="e2e_share", message_id="s1"):
        self.text, self.links, self.channel, self.message_id = text, links, channel, message_id


def main_run() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    text = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_TEXT

    cfg = AppConfig.load("data/config.yaml")
    config_path = "data/config.yaml"
    store = Store(":memory:")
    client = main.build_client(cfg, config_path)
    me = client.me() or {}
    print(f"① 已登录: {me.get('name') or '?'}")

    base_root = (cfg.output.parent_id or cfg.output.save_path or "").strip()
    print(f"② 落盘根: {base_root or '（账号根目录）'}")

    # 真实装配（对齐线上 main()：分类器/去重参数全部来自用户配置）
    classifier = Classifier(
        mapping=cfg.organize.mapping or None,
        structure=cfg.organize.structure,
        unknown_dir=cfg.organize.unknown_dir,
    )
    resolver = CategoryResolver(client, root_id=base_root,
                                create_missing=cfg.organize.create_missing)
    dedup = CloudDedup(client, resolver, classifier,
                       cloud_check_new=cfg.dedup.cloud_check_new,
                       cache_ttl=0,
                       organize_enabled=cfg.organize.enabled,
                       upgrade=cfg.dedup.upgrade,
                       require_cn=cfg.dedup.require_cn)
    handler = main.make_handler(
        store, client, KeywordFilter([], [], ""), Notifier(console=False),
        base_root, cfg.max_retries,
        classifier=classifier, resolver=resolver,
        dedup=dedup, organize_enabled=cfg.organize.enabled,
    )

    title = main._extract_share_title(text, url)
    print(f"③ 提取标题: {title!r}")

    handler(Msg(text, [url], message_id=f"e2e-{int(time.time())}"))

    rec = None
    for r in store.history(limit=10):
        if main.link_key(url) == r.hash:
            rec = r
            break
    if not rec:
        raise SystemExit("❌ 处理后没有找到记录")
    print(f"④ 落库: status={rec.status} category={rec.category} "
          f"cn_folder={rec.cn_folder} renamed={rec.renamed} reason={rec.reason}")

    if rec.status in ("done", "submitted") and rec.parent_id:
        entries = client.list_dir(rec.parent_id)
        print(f"⑤ 分类目录 {rec.category!r} 内条目（{len(entries)}，至多列 12）:")
        for e in entries[:12]:
            kind = "📁" if e["res_type"] == 2 else "📄"
            print(f"   {kind} {e['name']}")
        if rec.cn_folder:
            hit = [e for e in entries if main._entry_key(e["name"]) == main._entry_key(rec.cn_folder)]
            print(f"⑥ 规范命名校验: {'✅ 找到 ' + hit[0]['name'] if hit else '❌ 未找到 ' + rec.cn_folder}")


if __name__ == "__main__":
    main_run()
