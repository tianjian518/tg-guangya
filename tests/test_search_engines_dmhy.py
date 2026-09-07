"""动漫花园 dmhy 引擎测试——解析层用 GitHub Actions 实抓的真实响应样本
（tests/fixtures/），编排逻辑用 mock，不发真实网络请求。

样本来源：2026-09-07 GitHub 机房 curl 实抓（.github/workflows/fetch-samples.yml）：
  - dmhy_cn_doupo.xml   = 中文关键词「斗破苍穹」的真实 RSS（445 条，此处裁剪保留 6 条）
  - dmhy_detail_fragment.html = 详情页磁力链接上下文（75KB 原页裁出 3KB）

dmhy 与 nyaa 的关键差异（引擎实现依据，真实样本验证）：
  - RSS 关键词过滤生效，国漫覆盖极强（GM-Team 组 4K/简体内封，最新集次日即有）；
  - RSS **不含 infoHash**（nyaa 有 nyaa:infoHash），磁力需对结果抓详情页二跳；
  - 覆盖面：国漫/日番/日韩剧强；国产剧/电影弱（「狂飙」只命中同名日番与游戏）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import magnet_search as ms

FIX = Path(__file__).resolve().parent / "fixtures"

RSS_SAMPLE = (FIX / "dmhy_cn_doupo.xml").read_text(encoding="utf-8")
DETAIL_SAMPLE = (FIX / "dmhy_detail_fragment.html").read_text(encoding="utf-8")


class _FakeResp:
    def __init__(self, body: str, status_code: int = 200):
        self.status_code = status_code
        self.text = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        raise ValueError("not json")


def _mock_dmhy(monkeypatch, rss_body: str = "", detail_body: str = "",
               detail_status: int = 200):
    """RSS 与详情页按 URL 分流 mock；记录详情页请求 URL。"""
    detail_urls: list[str] = []

    def fake_get(url, *a, **kw):
        if "/topics/rss" in url:
            return _FakeResp(rss_body)
        detail_urls.append(url)
        return _FakeResp(detail_body, status_code=detail_status)

    monkeypatch.setattr(ms.requests, "get", fake_get)
    return detail_urls


# ---------------- dmhy 两跳解析（真实样本驱动） ----------------

def test_dmhy_parses_real_cn_sample(monkeypatch):
    """中文「斗破苍穹」真实 RSS + 真实详情页：应产出带磁力的国漫结果。"""
    detail_urls = _mock_dmhy(monkeypatch, RSS_SAMPLE, DETAIL_SAMPLE)
    hits = ms.search_dmhy("斗破苍穹")
    assert hits, "真实样本应解析出结果"
    assert all(h.source == "dmhy" for h in hits)
    assert all(h.magnet.startswith("magnet:?xt=urn:btih:") and len(h.magnet) > 50
               for h in hits)
    assert any("斗破苍穹" in h.title for h in hits), "标题应保留中文"
    # 样本详情页的已知 hash（207 集 HEVC 4K，2026-09-07 抓取）
    assert any("6FFEA20FE5B6872079E38CB941C507B1F9FA1F94" in h.magnet for h in hits), \
        "详情页样本里的已知 hash 应出现在结果中（磁力统一大写）"
    # 二跳确实访问了 RSS 给出的详情页链接
    assert detail_urls and all("share.dmhy.org/topics/view/" in u for u in detail_urls)


def test_dmhy_detail_page_failure_skips_entry(monkeypatch):
    """单条详情页失败（404/超时）只跳过该条，不炸整个引擎。"""
    detail_urls = _mock_dmhy(monkeypatch, RSS_SAMPLE, "404", detail_status=404)
    hits = ms.search_dmhy("斗破苍穹")
    assert hits == [], "全部详情页失败时返回空（不抛异常）"
    assert len(detail_urls) > 0, "仍应尝试过详情页请求"


def test_dmhy_rejects_cloudflare_challenge(monkeypatch):
    """dmhy 被 Cloudflare 拦时应报错（可被 search_all 容错上报），不静默返回空。"""
    _mock_dmhy(monkeypatch, rss_body="<html>Just a moment...</html>")
    with pytest.raises(RuntimeError, match="Cloudflare"):
        ms.search_dmhy("test")


def test_dmhy_rss_limit_caps_detail_fetches(monkeypatch):
    """二跳条数上限：limit 很大时也最多抓 6 条详情页（控制延迟）。"""
    detail_urls = _mock_dmhy(monkeypatch, RSS_SAMPLE, DETAIL_SAMPLE)
    ms.search_dmhy("斗破苍穹", limit=20)
    assert len(detail_urls) <= 6, f"详情页请求应 ≤6 次，实际 {len(detail_urls)}"


# ---------------- 搜索编排：dmhy 走中文原词 ----------------

def test_search_all_cjk_routes_to_dmhy_untranslated(monkeypatch):
    """中文关键词：dmhy 与 nyaa 一样吃中文原词，不走翻译。"""
    calls = {}

    def fake_dmhy(kw, limit=10, proxy="", timeout=12):
        calls["dmhy"] = kw
        return [ms.SearchHit(title=f"DMHY {kw}", size_bytes=0, seeders=0,
                             magnet="magnet:?xt=urn:btih:" + "c" * 40, source="dmhy")]

    monkeypatch.setitem(ms.ENGINES, "dmhy", fake_dmhy)
    monkeypatch.setattr(ms, "translate_cn_keyword",
                        lambda kw: (_ for _ in ()).throw(AssertionError("不应调用翻译")))
    hits, errors = ms.search_all("斗破苍穹", engines=["dmhy"])
    assert calls["dmhy"] == "斗破苍穹", "dmhy 必须收到中文原词（不翻译）"
    assert not errors and len(hits) == 1 and hits[0].source == "dmhy"


def test_dmhy_registered_as_default_engine():
    """dmhy 应在默认引擎表与配置白名单里（config 与 magnet_search 保持同步）。"""
    from core.config import DEFAULT_SEARCH_ENGINES, SEARCH_ENGINE_NAMES
    assert "dmhy" in ms.ENGINES and "dmhy" in ms.CJK_OK_ENGINES
    assert "dmhy" in DEFAULT_SEARCH_ENGINES and "dmhy" in SEARCH_ENGINE_NAMES


def test_engine_whitelist_keeps_all_engines():
    """config 热更新白名单回归：nyaa/dmhy 不再被硬编码的 ("apibay",) 滤掉。"""
    from core.config import AppConfig
    cfg = AppConfig()
    cfg.apply_settings({"bot": {"search_engines": ["apibay", "nyaa", "dmhy"]}})
    assert cfg.bot.search_engines == ["apibay", "nyaa", "dmhy"]
