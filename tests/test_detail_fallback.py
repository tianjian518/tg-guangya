"""详情页兜底：列表页正文被 TG 折叠时，回查单条详情页补全磁力。

列表页 t.me/s/<ch> 对长帖只渲染一部分（磁力可能刚好在被截断的那段），
所以对「列表里没抠出链接」的消息回查一次 t.me/<ch>/<id> 详情页。
用假 session 跑，不发真实请求。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from adapters.web_scraper import WebScraper

MAGNET = "magnet:?xt=urn:btih:" + "C" * 40


def _html(messages):
    """拼一份最小可用频道页：每条消息一个外层 div + 正文 div。"""
    out = []
    for mid, text in messages:
        out.append(
            f'<div class="tgme_widget_message js-widget_message" data-post="ch/{mid}">'
            f'<div class="tgme_widget_message_text">{text}</div></div>'
        )
    return "".join(out)


class FakeSession:
    """记录每个 url 的请求，返回预置的 HTML。"""

    def __init__(self, pages):
        self.pages = pages          # {url片段: (status, html)}
        self.requested: list[str] = []

    def get(self, url, params=None, timeout=0):
        self.requested.append(url)
        for key, (status, html) in self.pages.items():
            if key in url:
                return SimpleNamespace(status_code=status, text=html)
        return SimpleNamespace(status_code=404, text="")


def _scraper(session, detail_fallback=10):
    s = WebScraper(["ch"], interval=60, detail_fallback=detail_fallback)
    s._session = session
    return s


def test_list_magnet_used_directly_no_detail_request():
    """列表页就有磁力 → 不回查详情页。"""
    session = FakeSession({"t.me/s/ch": (200, _html([(1, "资源 " + MAGNET)]))})
    msgs = _scraper(session).fetch("ch")
    assert msgs[0].links == [MAGNET]
    assert not any("t.me/ch/" in u for u in session.requested)


def test_detail_fallback_fills_folded_magnet():
    """列表页没磁力、详情页有 → 从详情页补上，并标记来源。"""
    session = FakeSession({
        "t.me/s/ch": (200, _html([(2, "链接下载见评论区")])),
        "t.me/ch/2": (200, _html([(2, "完整正文 " + MAGNET)])),
    })
    msgs = _scraper(session).fetch("ch")
    assert msgs[0].links == [MAGNET]
    assert msgs[0].links_from == "detail"
    assert any(u.endswith("t.me/ch/2") for u in session.requested)


def test_detail_budget_limits_requests():
    """每轮最多回查 detail_fallback 条，避免请求量失控。"""
    session = FakeSession({
        "t.me/s/ch": (200, _html([(1, "a"), (2, "b"), (3, "c"), (4, "d")])),
        "t.me/ch/1": (200, _html([(1, MAGNET)])),
        "t.me/ch/2": (200, _html([(2, MAGNET)])),
    })
    _scraper(session, detail_fallback=2).fetch("ch", detail_fallback=2)
    detail_calls = [u for u in session.requested if "/ch/" in u.replace("t.me/s/", "")]
    assert len(detail_calls) == 2


def test_detail_fallback_disabled():
    """detail_fallback=0 → 一次详情页都不请求。"""
    session = FakeSession({
        "t.me/s/ch": (200, _html([(1, "没磁力")])),
        "t.me/ch/1": (200, _html([(1, MAGNET)])),
    })
    msgs = _scraper(session, detail_fallback=0).fetch("ch", detail_fallback=0)
    assert msgs[0].links == []
    assert not any("t.me/ch/" in u for u in session.requested)


def test_detail_page_404_is_tolerated():
    """详情页 404/异常不能影响本轮结果。"""
    session = FakeSession({"t.me/s/ch": (200, _html([(1, "没磁力")]))})
    msgs = _scraper(session).fetch("ch")
    assert len(msgs) == 1
    assert msgs[0].links == []
