"""Nyaa 引擎与多引擎中文路由测试——解析层用 GitHub Actions 实抓的真实响应样本
（tests/fixtures/），编排逻辑用 mock，不发真实网络请求。

样本来源：2026-09-06 GitHub 机房 curl 实抓（.github/workflows/fetch-samples.yml），
其中 nyaa_cn_doupo.xml = 中文关键词「斗破苍穹」的真实 RSS（75 条）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import magnet_search as ms

FIX = Path(__file__).resolve().parent / "fixtures"


class _FakeResp:
    """把 fixture 文本伪装成 requests 响应。"""
    def __init__(self, body: str, status_code: int = 200):
        self.status_code = status_code
        self.text = body

    def raise_for_status(self):
        pass

    def json(self):
        raise ValueError("not json")


def _with_sample(monkeypatch, name: str):
    body = (FIX / name).read_text(encoding="utf-8")
    monkeypatch.setattr(ms.requests, "get", lambda *a, **kw: _FakeResp(body))


# ---------------- nyaa RSS 解析（真实样本驱动） ----------------

def test_nyaa_parses_real_cn_sample(monkeypatch):
    """中文「斗破苍穹」真实 RSS：应解析出带磁力的结果，按做种数降序。"""
    _with_sample(monkeypatch, "nyaa_cn_doupo.xml")
    hits = ms.search_nyaa("斗破苍穹")
    assert hits, "真实样本应解析出结果"
    assert all(h.source == "nyaa" for h in hits)
    assert all(h.magnet.startswith("magnet:?xt=urn:btih:") and len(h.magnet) > 50
               for h in hits)
    # 样本第一条：S05E209，hash 4649e5c2...（2026-09-05 发布；磁力统一大写）
    first_hash = "4649E5C2814232546DE13EAD50AAF6F8BCA7C98B"
    assert any(first_hash in h.magnet for h in hits), "样本里的已知 hash 应出现在结果中"
    assert any("斗破苍穹" in h.title for h in hits), "标题应保留中文"
    seeds = [h.seeders for h in hits]
    assert seeds == sorted(seeds, reverse=True), "结果应按做种数降序"


def test_nyaa_cn_juqing_sample(monkeypatch):
    """中文「庆余年」真实 RSS 也有结果（条目较少但存在）。"""
    _with_sample(monkeypatch, "nyaa_cn_juqing.xml")
    hits = ms.search_nyaa("庆余年")
    assert hits, "庆余年真实样本应解析出结果"
    assert any("庆余年" in h.title for h in hits)


def test_nyaa_rejects_cloudflare_challenge(monkeypatch):
    """Nyaa 被 Cloudflare 拦时应报错（可被 search_all 容错上报），不静默返回空。"""
    monkeypatch.setattr(ms.requests, "get",
                        lambda *a, **kw: _FakeResp("<html>Just a moment...</html>", 403))
    with pytest.raises(RuntimeError, match="Cloudflare"):
        ms.search_nyaa("test")


def test_parse_ib_size():
    assert ms._parse_ib_size("1.2 GiB") == int(1.2 * (1 << 30))
    assert ms._parse_ib_size("700 MiB") == 700 * (1 << 20)
    assert ms._parse_ib_size("512 KiB") == 512 * 1024
    assert ms._parse_ib_size("garbage") == 0
    assert ms._parse_ib_size("") == 0


# ---------------- 搜索编排：分引擎的中文处理 ----------------

def test_search_all_cjk_routes_per_engine(monkeypatch):
    """中文关键词：nyaa 用原词、apibay 用译名——两条路径互不污染。"""
    calls = {}

    def fake_apibay(kw, limit=10, proxy="", timeout=12):
        calls["apibay"] = kw
        return [ms.SearchHit(title=f"APB {kw}", size_bytes=1, seeders=10,
                             magnet="magnet:?xt=urn:btih:" + "a" * 40, source="apibay")]

    def fake_nyaa(kw, limit=10, proxy="", timeout=12):
        calls["nyaa"] = kw
        return [ms.SearchHit(title=f"NYA {kw}", size_bytes=1, seeders=20,
                             magnet="magnet:?xt=urn:btih:" + "b" * 40, source="nyaa")]

    monkeypatch.setitem(ms.ENGINES, "apibay", fake_apibay)
    monkeypatch.setitem(ms.ENGINES, "nyaa", fake_nyaa)
    monkeypatch.setattr(ms, "translate_cn_keyword", lambda kw: "Battle Through the Heavens")

    hits, errors = ms.search_all("斗破苍穹", engines=["apibay", "nyaa"])
    assert not errors, f"双引擎 mock 不应报错: {errors}"
    assert calls["apibay"] == "Battle Through the Heavens", "apibay 必须收到英译名"
    assert calls["nyaa"] == "斗破苍穹", "nyaa 必须收到中文原词（不翻译）"
    assert len(hits) == 2 and {h.source for h in hits} == {"apibay", "nyaa"}


def test_search_all_pure_english_skips_translation(monkeypatch):
    """纯英文关键词：不触发翻译。"""
    calls = []

    def fake_nyaa(kw, limit=10, proxy="", timeout=12):
        calls.append(kw)
        return []
    monkeypatch.setitem(ms.ENGINES, "nyaa", fake_nyaa)
    monkeypatch.setattr(ms, "translate_cn_keyword",
                        lambda kw: (_ for _ in ()).throw(AssertionError("不应调用翻译")))

    hits, errors = ms.search_all("interstellar", engines=["nyaa"])
    assert calls == ["interstellar"] and not errors


def test_search_all_single_engine_no_translation_call(monkeypatch):
    """只启用 nyaa 时中文关键词不应触发翻译（省一次网络请求 + 避免错误译名）。"""
    seen = []

    def fake_nyaa(kw, limit=10, proxy="", timeout=12):
        seen.append(kw)
        return []
    monkeypatch.setitem(ms.ENGINES, "nyaa", fake_nyaa)
    monkeypatch.setattr(ms, "translate_cn_keyword",
                        lambda kw: (_ for _ in ()).throw(AssertionError("不应调用翻译")))
    hits, errors = ms.search_all("斗破苍穹", engines=["nyaa"])
    assert seen == ["斗破苍穹"] and not errors


def test_cn_to_en_dictionary_hits():
    """本地词典覆盖主流片名（含 2026 热门与国漫）。"""
    for cn, en in (("斗破苍穹", "Battle Through the Heavens"),
                   ("哪吒之魔童闹海", "Ne Zha 2"), ("热辣滚烫", "YOLO"),
                   ("流浪地球", "The Wandering Earth"),
                   ("凡人修仙传", "A Record of a Mortal's Journey to Immortality")):
        assert ms._CN_TO_EN.get(cn) == en, f"词典缺 {cn}"


def test_clean_translation_strips_meta_noise():
    """网络翻译的历史故障回归：演职员表/HTML 残留不得混进搜索词。
    （"Gold Interstellar" 这类错译无法在清洗层识别——Gold 是正常单词，
    只能靠本地词典优先避免；见 test_cn_to_en_dictionary_hits。）"""
    assert ms._clean_translation("g id Italic The Wandering Earth g Director Guo Fan") \
        == "The Wandering Earth"
    assert ms._clean_translation("Interstellar directed by Christopher Nolan") \
        == "Interstellar"
