"""评论区磁力抓取：正文没磁力时的兜底来源。

很多资源频道（ysh365 / seedhub_cc 这类）正文只写「链接下载见评论区」，
磁力全在讨论组的评论里——没有这层就永远转存不了。网页抓取拿不到评论
（t.me 页面不渲染），所以这条链路只在 userbot 模式生效。

这里用假的 telethon 模块跑，不需要装 telethon、也不需要登录账号。
"""
from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import pytest

from adapters.userbot import UserbotSource
from core.config import CommentsConfig

MAGNET_A = "magnet:?xt=urn:btih:" + "A" * 40
MAGNET_B = "magnet:?xt=urn:btih:" + "B" * 40


# ---------- 假 telethon ----------

class FakeClient:
    """够 _comment_links 用的最小 client。"""

    def __init__(self, root_msgs, replies):
        self.root_msgs = root_msgs
        self.replies = replies
        self.discussion_calls = 0
        self.iter_calls = 0

    async def get_input_entity(self, x):
        return x

    async def __call__(self, req):  # client(GetDiscussionMessageRequest(...))
        self.discussion_calls += 1
        return SimpleNamespace(messages=self.root_msgs)

    async def _iter(self, chat, reply_to=None, limit=0):
        self.iter_calls += 1
        for r in self.replies:
            yield r

    def iter_messages(self, chat, reply_to=None, limit=0):
        return self._iter(chat, reply_to=reply_to, limit=limit)


@pytest.fixture(autouse=True)
def fake_telethon(monkeypatch):
    """注入 telethon 模块（本机没装也能跑这些用例）。"""
    mod = types.ModuleType("telethon")
    tl = types.ModuleType("telethon.tl")
    fn = types.ModuleType("telethon.tl.functions")
    msgs = types.ModuleType("telethon.tl.functions.messages")

    class GetDiscussionMessageRequest:
        def __init__(self, peer=None, msg_id=None):
            self.peer = peer
            self.msg_id = msg_id

    msgs.GetDiscussionMessageRequest = GetDiscussionMessageRequest
    fn.messages = msgs
    tl.functions = fn
    mod.tl = tl
    monkeypatch.setitem(sys.modules, "telethon", mod)
    monkeypatch.setitem(sys.modules, "telethon.tl", tl)
    monkeypatch.setitem(sys.modules, "telethon.tl.functions", fn)
    monkeypatch.setitem(sys.modules, "telethon.tl.functions.messages", msgs)
    return GetDiscussionMessageRequest


def _src(comments=None) -> UserbotSource:
    """绕开 __init__（它会 import telethon），只装需要的字段。"""
    s = UserbotSource.__new__(UserbotSource)
    s.comments = comments or CommentsConfig()
    return s


def _msg(text="", mid=1):
    return SimpleNamespace(id=mid, message=text, peer_id="chan", chat=None)


def _root(mid=900, text=""):
    return SimpleNamespace(id=mid, message=text, peer_id=SimpleNamespace(channel_id=777))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------- 用例 ----------

def test_comment_magnet_saved_when_body_has_none():
    """正文没磁力、评论有 → 磁力来自评论，且标记成 comment。"""
    client = FakeClient([_root()], [SimpleNamespace(message="磁力：" + MAGNET_A)])
    cm = _run(_src()._handle(client, _msg("链接下载见评论区")))
    assert cm is not None
    assert cm.links == [MAGNET_A]
    assert cm.links_from == "comment"


def test_body_magnet_preferred_and_no_comment_lookup():
    """正文直发磁力 → 直接用，不翻评论（省 API 调用、降风控）。"""
    client = FakeClient([_root()], [SimpleNamespace(message=MAGNET_B)])
    cm = _run(_src()._handle(client, _msg("资源：" + MAGNET_A)))
    assert cm.links == [MAGNET_A]
    assert cm.links_from == "body"
    assert client.discussion_calls == 0


def test_no_magnet_anywhere_is_skipped():
    """正文和评论都没有可下载链接 → 跳过这条。"""
    client = FakeClient([_root()], [SimpleNamespace(message="感谢分享")])
    assert _run(_src()._handle(client, _msg("链接下载见评论区"))) is None


def test_channel_without_discussion_is_skipped():
    """频道没开评论（GetDiscussionMessage 返回空）→ 不炸、跳过。"""
    client = FakeClient([], [])
    assert _run(_src()._handle(client, _msg("啥也没有"))) is None
    assert client.iter_calls == 0


def test_comments_disabled_means_no_lookup():
    """关掉评论抓取 → 一次讨论组请求都不发。"""
    client = FakeClient([_root()], [SimpleNamespace(message=MAGNET_A)])
    cfg = CommentsConfig(enabled=False)
    assert _run(_src(cfg)._handle(client, _msg("见评论区"))) is None
    assert client.discussion_calls == 0


def test_always_mode_merges_both_sources():
    """always=True：正文已有磁力也翻评论，两边去重合并。"""
    client = FakeClient([_root()], [SimpleNamespace(message=MAGNET_B)])
    cfg = CommentsConfig(enabled=True, always=True)
    cm = _run(_src(cfg)._handle(client, _msg("正文：" + MAGNET_A)))
    assert cm.links == [MAGNET_A, MAGNET_B]
    assert cm.links_from == "body"  # 正文有货就仍算正文来源
    assert client.discussion_calls == 1


def test_root_discussion_message_magnet_also_collected():
    """讨论组里那条"镜像帖"自带磁力时也要（部分频道机器人把磁力放这）。"""
    client = FakeClient([_root(text="镜像帖：" + MAGNET_A)], [])
    cm = _run(_src()._handle(client, _msg("见评论区")))
    assert cm.links == [MAGNET_A]


def test_duplicate_across_comments_is_deduped():
    """多篇评论发同一个磁力 → 只留一条。"""
    client = FakeClient(
        [_root()],
        [SimpleNamespace(message=MAGNET_A), SimpleNamespace(message=MAGNET_A + "&dn=x")],
    )
    cm = _run(_src()._handle(client, _msg("见评论区")))
    assert len(cm.links) == 1
