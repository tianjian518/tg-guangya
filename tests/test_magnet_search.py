"""全网磁力搜索模块的离线自测（mock 搜索引擎 HTTP 响应，不联网、不需要光鸭）。

覆盖：
  1. apibay JSON → SearchHit 解析（标题/大小/做种/磁力构造）
  2. 非法行（缺 info_hash / 空名）跳过、去重、按做种排序
  3. search_all 对坏引擎容错、errors 上报
  4. to_payload 纯数据结构
  5. 配置 bot.search_enabled / search_engines 读写回写

跑法：
    python -m pytest tests/test_magnet_search.py -v
    # 或
    python tests/test_magnet_search.py
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import magnet_search  # noqa: E402
from core.config import AppConfig  # noqa: E402

SAMPLE_ROWS = [
    {"name": "Interstellar (2014) 1080p BrRip x264 - YIFY", "info_hash": "a" * 40,
     "seeders": "1097", "leechers": "50", "size": "2319000000", "category": "201"},
    {"name": "Interstellar 2014 4K HDR Remux", "info_hash": "b" * 40,
     "seeders": "3", "leechers": "1", "size": "68990000000", "category": "201"},
    {"name": "Dead 种子", "info_hash": "c" * 40,
     "seeders": "0", "leechers": "0", "size": "1000", "category": "201"},
    {"name": "", "info_hash": "d" * 40, "seeders": "5", "leechers": "0",
     "size": "10", "category": "201"},                       # 空名 → 跳过
    {"name": "No-hash 行", "info_hash": "", "seeders": "5", "leechers": "0",
     "size": "10", "category": "201"},                       # 缺 hash → 跳过
]


class FakeResp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)

    def json(self):
        return self._p


def fake_get(url, headers=None, proxies=None, timeout=12):
    return FakeResp(SAMPLE_ROWS)


def fake_tr_get(url, headers=None, proxies=None, timeout=6):
    """按 URL 分流：翻译端点回翻译结果，apibay 端点回样例搜索行。"""
    if "mymemory.translated.net" in url:
        return FakeResp({"responseData": {"translatedText": "Interstellar"}})
    return FakeResp(SAMPLE_ROWS)


class MagnetSearchTest(unittest.TestCase):

    def test_apibay_parse_and_rank(self):
        with mock.patch("core.magnet_search.requests.get", side_effect=fake_get):
            hits = magnet_search.search_apibay("interstellar", limit=10)
        # 3 条有效（空名/无 hash 被剔除）
        self.assertEqual(len(hits), 3)
        # 按做种人数降序：1097 > 3 > 0
        self.assertEqual([h.seeders for h in hits], [1097, 3, 0])
        # 磁力构造正确
        self.assertTrue(hits[0].magnet.startswith("magnet:?xt=urn:btih:" + "A" * 40))
        self.assertIn("dn=", hits[0].magnet)
        # 大小文本
        self.assertEqual(hits[0].size_text, "2.2GB")
        self.assertEqual(hits[1].size_text, "64.3GB")

    def test_search_all_dedup_and_cap(self):
        # 引擎重复给出同一条磁力 → 去重
        with mock.patch("core.magnet_search.requests.get", side_effect=fake_get):
            hits, errs = magnet_search.search_all("interstellar", engines=["apibay"], limit=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual(errs, [])

    def test_translate_only_when_cjk(self):
        # 纯英文/数字关键词不触发翻译
        magnet_search._TRANS_CACHE.clear()
        self.assertEqual(magnet_search.translate_cn_keyword("interstellar 2014"),
                         "interstellar 2014")
        # 中文关键词走翻译（mock 端点返回 Interstellar），并缓存
        with mock.patch("core.magnet_search.requests.get",
                        side_effect=fake_tr_get) as m:
            got = magnet_search.translate_cn_keyword("星际穿越")
            self.assertEqual(got, "Interstellar")
            # 缓存命中，不再发请求
            got2 = magnet_search.translate_cn_keyword("星际穿越")
            self.assertEqual(got2, "Interstellar")
        self.assertEqual(m.call_count, 1)

    def test_search_all_translates_cn_keyword(self):
        # search_all 收到中文关键词：先翻译成英文再打 apibay
        # （用与缓存测试不同的词，确保真的走一次翻译请求）
        seen = []
        def collecting_get(url, headers=None, proxies=None, timeout=6):
            seen.append(url)
            if "mymemory.translated.net" in url:
                return FakeResp({"responseData": {"translatedText": "Interstellar"}})
            return FakeResp(SAMPLE_ROWS)
        with mock.patch("core.magnet_search.requests.get", side_effect=collecting_get):
            hits, errs = magnet_search.search_all("流浪地球", engines=["apibay"], limit=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual(errs, [])
        # 两次请求：一次翻译端点 + 一次 apibay（英文关键词）
        self.assertTrue(any("mymemory.translated.net" in u for u in seen))
        self.assertTrue(any("apibay.org" in u and "q=Interstellar" in u for u in seen))

    def test_unknown_engine_error(self):
        hits, errs = magnet_search.search_all("x", engines=["nope"])
        self.assertEqual(hits, [])
        self.assertTrue(any("未知引擎" in e for e in errs))

    def test_broken_response(self):
        def bad(url, headers=None, proxies=None, timeout=12):
            return FakeResp({"oops": 1})

        with mock.patch("core.magnet_search.requests.get", side_effect=bad):
            hits, errs = magnet_search.search_all("x", engines=["apibay"])
        self.assertEqual(hits, [])
        self.assertTrue(errs)  # 错误被汇总上报

    def test_to_payload(self):
        with mock.patch("core.magnet_search.requests.get", side_effect=fake_get):
            hits, _ = magnet_search.search_all("interstellar", limit=3)
        p = magnet_search.to_payload(hits)
        self.assertEqual(len(p), 3)
        self.assertEqual(sorted(p[0].keys()),
                         ["magnet", "seeders", "size_bytes", "size_text", "source", "title"])

    def test_config_defaults_without_bot_section(self):
        import tempfile
        import yaml
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as f:
            yaml.safe_dump({"sources": {"channels": []}}, f, allow_unicode=True)
            p = f.name
        try:
            c = AppConfig.load(p)
            self.assertTrue(c.bot.search_enabled)            # 默认开
            self.assertEqual(c.bot.search_engines, ["apibay"])  # 默认引擎
        finally:
            os.unlink(p)

    def test_config_fields_roundtrip(self):
        import tempfile
        import yaml
        raw = {"bot": {"enabled": False, "search_enabled": False,
                       "search_engines": ["apibay", "bogus"]}}
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                         encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True)
            p = f.name
        try:
            c = AppConfig.load(p)
            self.assertFalse(c.bot.search_enabled)
            self.assertEqual(c.bot.search_engines, ["apibay"])  # 未知引擎被过滤
            c.apply_settings({"bot": {"search_enabled": True,
                                      "search_engines": ["apibay"]}})
            self.assertTrue(c.bot.search_enabled)
            d = c.to_dict()["bot"]
            self.assertIn("search_enabled", d)
            self.assertIn("search_engines", d)
        finally:
            os.unlink(p)


if __name__ == "__main__":
    unittest.main()
