#!/usr/bin/env python3
"""磁力大乱炖（tgtoguang）历史消息全链路测试。

与 worker 真实监听完全同路径：userbot 拉历史消息 → 评论区磁力兜底 →
handler（探名 → 过滤 → 分类 → 云端去重 → 离线转存/分享转存 → 中文改名）。

用法：python3 tools/test_tgtoguang.py [拉取条数]（默认 60）
结果追加写入 data/tgtoguang_test_result.json（断点续跑：已测过的帖子自动跳过）。
"""
import sys
import os
import json
import time
import asyncio
import logging

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, 'tools'))
os.chdir(BASE)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')

import yaml                                # noqa: E402
from adapters.userbot import UserbotSource  # noqa: E402
import main as M                            # noqa: E402
from test_share_batch import build_handler  # noqa: E402

CHANNEL = 'tgtoguang'
RESULT_PATH = 'data/tgtoguang_test_result.json'


def load_results() -> list:
    if os.path.exists(RESULT_PATH):
        with open(RESULT_PATH, encoding='utf-8') as f:
            return json.load(f)
    return []


def save_results(rs: list) -> None:
    with open(RESULT_PATH, 'w', encoding='utf-8') as f:
        json.dump(rs, f, ensure_ascii=False, indent=1)


async def collect(src: UserbotSource, limit: int) -> list:
    """拉历史消息并走 _handle（含评论区磁力兜底），返回含链接的消息列表。"""
    out = []
    client = src.make_client()
    await client.connect()
    ent = await client.get_entity(CHANNEL)
    async for m in client.iter_messages(ent, limit=limit):
        try:
            cm = await src._handle(client, m)
        except Exception as exc:  # noqa: BLE001
            print('  帖子 %s 处理失败: %s' % (m.id, str(exc)[:80]), flush=True)
            continue
        if cm:
            out.append(cm)
        await asyncio.sleep(0.4)
    await client.disconnect()
    return out


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    cfgd = yaml.safe_load(open('data/config.yaml'))
    cfg = M.AppConfig.load('data/config.yaml')
    store, client, handler = build_handler(cfg)
    src = UserbotSource(str(cfgd['telegram']['api_id']), cfgd['telegram']['api_hash'],
                        cfgd['telegram']['session'], [CHANNEL],
                        proxy=cfgd.get('sources', {}).get('proxy', ''))
    print('拉取 %s 最近 %d 条消息...', CHANNEL, limit)
    cms = asyncio.run(collect(src, limit))
    print('其中 %d 条含可下载链接' % len(cms))

    results = load_results()
    seen_msgs = {r['msg_id'] for r in results}

    for i, cm in enumerate(cms):
        if cm.message_id in seen_msgs:
            continue
        print('\n===== [%d/%d] 帖子 %s | %d 链接(%s) | %s ====='
              % (i + 1, len(cms), cm.message_id, len(cm.links), cm.links_from,
                 cm.text[:60].replace('\n', ' ⏎ ')), flush=True)
        rec = {'idx': i + 1, 'msg_id': cm.message_id,
               'text': cm.text[:80], 'links_n': len(cm.links),
               'links_from': cm.links_from, 'ts': time.strftime('%m-%d %H:%M:%S')}
        try:
            handler(cm)
        except Exception as exc:  # noqa: BLE001
            rec['error'] = str(exc)[:200]
            logging.exception('handler 异常')
        per = []
        for u in cm.links:
            g = store.get(M.link_key(u))
            if g:
                per.append('%s…: %s%s' % (u.replace('magnet:?xt=urn:btih:', 'm:').replace(
                    'ed2k://|file|', 'e:')[:24], g.status,
                    '→' + g.cn_folder[:28] if g.cn_folder else ''))
            else:
                per.append('%s…: 无记录' % u[:24])
        rec['per_link'] = per
        results.append(rec)
        save_results(results)
        for p in per:
            print('   ', p, flush=True)

    print('\n===== 汇总（共 %d 帖）=====' % len(results), flush=True)
    for r in results:
        print('%2d | 帖%s %-9s | %s' % (r['idx'], r['msg_id'], r.get('links_from', ''),
                                        (r.get('text') or '')[:46].replace('\n', ' ')))
        for p in r.get('per_link', []):
            print('     ', p)


if __name__ == '__main__':
    main()
