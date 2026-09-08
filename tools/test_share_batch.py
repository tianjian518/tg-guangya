#!/usr/bin/env python3
"""批量真实测试：用户提供的分享链接走完整主流程。

链路与频道消息完全一致：peek 真实名 → 分类 → 云端去重复查 → 转存 → 中文改名 → 落盘校验。
结果追加写入 data/batch_test_result.json（断点续跑：已测过的 URL 自动跳过）。

用法：python3 tools/test_share_batch.py [起始序号 结束序号]（0 起，左闭右开）
"""
import sys
import os
import json
import time
import logging

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

from core.config import AppConfig            # noqa: E402
from core.store import Store                 # noqa: E402
from core.notifier import Notifier           # noqa: E402
from core.classifier import Classifier       # noqa: E402
from core.organizer import CategoryResolver  # noqa: E402
from core.dedup import CloudDedup            # noqa: E402
from core.matcher import KeywordFilter       # noqa: E402
from adapters.tgbot import BotMessage        # noqa: E402
import main as M                             # noqa: E402

CASES = [
    ("重案六组：消失的警号", "https://www.guangyapan.com/s/1941481330761981991_al8cmYXLP9l33ld2"),
    ("囧徒", "https://www.guangyapan.com/s/1941482777813684316_al8cmYXLP9l33ld2"),
    ("飞到我心上", "https://www.guangyapan.com/s/1941395648530010162_al8cmYXLP9l33ld2"),
    ("机动战士高达 闪光的哈萨维 喀耳刻的魔女", "https://www.guangyapan.com/s/1941191898376921128_al8cmYXLP9l33ld2"),
    ("爱是愤怒", "https://www.guangyapan.com/s/1940316499971367002_aeWc_gh3mcEVHMS2"),
    ("花开锦绣", "https://www.guangyapan.com/s/1940315977843441665_aeWc_gh3mcEVHMS2"),
    ("早春晴朗", "https://www.guangyapan.com/s/1939566966593052724_al8cmYXLP9l33ld2"),
    ("金色", "https://www.guangyapan.com/s/1939297128972730459_al8cmYXLP9l33ld2"),
    ("二十世纪电气目录", "https://www.guangyapan.com/s/1928516490298228819_al8cmYXLP9l33ld2"),
    ("蝉", "https://www.guangyapan.com/s/1937557758271754331_al8cmYXLP9l33ld2"),
    ("S&X：性诊室告白", "https://www.guangyapan.com/s/1937465892905877547_al8cmYXLP9l33ld2"),
    ("仙武传", "https://www.guangyapan.com/s/1937465892905877547_al8cmYXLP9l33ld2"),
    ("邻人可疑", "https://www.guangyapan.com/s/1937382742687117340_al8cmYXLP9l33ld2"),
    ("阳光女子合唱团", "https://www.guangyapan.com/s/1937370425815625731_al8cmYXLP9l33ld2"),
    ("猴子扳手", "https://www.guangyapan.com/s/1937075521449263196_al8cmYXLP9l33ld2"),
    ("师兄太稳健", "https://www.guangyapan.com/s/1937045394954821692_al8cmYXLP9l33ld2"),
    ("崩溃爬山趣", "https://www.guangyapan.com/s/1936852616845094999_al8cmYXLP9l33ld2"),
    ("罪爱", "https://www.guangyapan.com/s/1936716363059368018_al8cmYXLP9l33ld2"),
    ("玩具总动员5", "https://www.guangyapan.com/s/1936488094074548311_al8cmYXLP9l33ld2"),
    ("藏锋", "https://www.guangyapan.com/s/1936443479300669466_al8cmYXLP9l33ld2"),
    ("器子", "https://www.guangyapan.com/s/1936368258451308583_al8cmYXLP9l33ld2"),
]

RESULT_PATH = 'data/batch_test_result.json'


def load_results() -> list:
    if os.path.exists(RESULT_PATH):
        with open(RESULT_PATH, encoding='utf-8') as f:
            return json.load(f)
    return []


def save_results(rs: list) -> None:
    with open(RESULT_PATH, 'w', encoding='utf-8') as f:
        json.dump(rs, f, ensure_ascii=False, indent=1)


def build_handler(cfg: AppConfig):
    store = Store(cfg.storage_db)
    client = M.build_client(cfg, 'data/config.yaml')
    flt = KeywordFilter(cfg.filter.include_keywords, cfg.filter.exclude_keywords,
                        cfg.filter.min_resolution)
    notifier = Notifier(console=True)  # 仅控制台，测试不刷 TG
    parent_id = cfg.output.parent_id or cfg.output.save_path
    classifier = resolver = None
    if cfg.organize.enabled:
        classifier = Classifier(mapping=cfg.organize.mapping or None,
                                structure=cfg.organize.structure,
                                unknown_dir=cfg.organize.unknown_dir)
        resolver = CategoryResolver(client, root_id=parent_id,
                                    create_missing=cfg.organize.create_missing)
    dedup = CloudDedup(client, resolver or CategoryResolver(client, root_id=parent_id,
                                                            create_missing=False),
                       classifier or Classifier(),
                       cloud_check_new=cfg.dedup.cloud_check_new,
                       cache_ttl=cfg.dedup.cache_ttl,
                       organize_enabled=cfg.organize.enabled,
                       upgrade=cfg.dedup.upgrade, require_cn=cfg.dedup.require_cn)
    handler = M.make_handler(store, client, flt, notifier, parent_id, cfg.max_retries,
                             classifier=classifier, resolver=resolver, dedup=dedup,
                             organize_enabled=cfg.organize.enabled)
    return store, client, handler


def main() -> None:
    start = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    end = int(sys.argv[2]) if len(sys.argv) > 2 else len(CASES)
    picks = ({int(x) for x in sys.argv[3].split(',')}
             if len(sys.argv) > 3 else set(range(start, end)))
    cfg = AppConfig.load('data/config.yaml')
    store, client, handler = build_handler(cfg)
    results = load_results()
    done_urls = {r['url'] for r in results}

    for i, (title, url) in enumerate(CASES):
        if i not in picks:
            continue
        tag = '[%d/%d]' % (i + 1, len(CASES))
        if url in done_urls:
            print('%s %s 已测过，跳过' % (tag, title), flush=True)
            continue
        print('\n========== %s %s ==========' % (tag, title), flush=True)
        rec = {'idx': i + 1, 'title': title, 'url': url}
        # 先探真实名（与 handler 内同一逻辑，这里记录下来供汇总对比）
        try:
            names = client.peek_share_names(url)
            rec['real'] = ' / '.join(names)[:60]
        except Exception as exc:  # noqa: BLE001
            rec['real'] = 'peek异常:' + str(exc)[:40]
        print('  探名: %s' % (rec['real'] or '（探不到→按发帖标题）'), flush=True)
        msg = BotMessage(links=[url], text='%s\n%s' % (title, url),
                         channel='batchtest', message_id='bt%d' % i)
        try:
            handler(msg)
            g = store.get(M.link_key(url))
            if g:
                rec.update(status=g.status, category=g.category,
                           cn_folder=g.cn_folder or '', renamed=g.renamed)
            else:
                rec['status'] = '被过滤/跳过'
        except Exception as exc:  # noqa: BLE001
            rec['status'] = '异常'
            rec['error'] = str(exc)[:200]
        rec['ts'] = time.strftime('%m-%d %H:%M:%S')
        results.append(rec)
        save_results(results)
        print('%s 结果: %s | %s | %s' % (tag, rec.get('status'), rec.get('category'),
                                         (rec.get('cn_folder') or '')[:44]), flush=True)

    print('\n===== 汇总（共 %d 条已测）=====' % len(results), flush=True)
    for r in results:
        print('%2d | %-14s | %-8s | 发帖:%-18.18s | 真实:%-24.24s | 落:%s'
              % (r['idx'], r.get('status', ''), r.get('category', ''),
                 r.get('title', ''), r.get('real', ''), (r.get('cn_folder') or '')[:30]))


if __name__ == '__main__':
    main()
