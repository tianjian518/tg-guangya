"""Telegram Userbot 适配器（可选）。

说明：这不是"机器人(Bot)"，而是用你的账号登录的一个第三方客户端，
与多装一个 Telegram Desktop 等价。能看到你账号里订阅的所有频道，实时推送。

风控提示：Telegram 不鼓励自动化使用用户账号，请用**小号**登录，
仅监听、不发言、低频，以降低封号风险。

依赖：pip install telethon（可选，仅在使用 userbot 时安装）。
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Callable, Optional

from urllib.parse import urlparse

from adapters.web_scraper import ChannelMessage, extract_links, link_key
from core.config import CommentsConfig

log = logging.getLogger(__name__)


def parse_proxy(url: str) -> Optional[dict]:
    """把 http/https/socks5 代理 URL 转成 Telethon 需要的 dict 形式。

    Telethon 的 TelegramClient(proxy=...) 只认 dict（proxy_type/addr/port），
    不接受 'http://host:port' 这种字符串，这里做个转换。传入空串则返回 None。
    """
    if not url:
        return None
    p = urlparse(url)
    scheme = (p.scheme or "").lower()
    if scheme in ("socks5", "socks5h"):
        ptype = "socks5"
    elif scheme == "socks4":
        ptype = "socks4"
    elif scheme in ("http", "https"):
        ptype = "http"
    else:
        ptype = "http"
    return {"proxy_type": ptype, "addr": p.hostname or "127.0.0.1", "port": p.port or 1080}


class UserbotSource:
    """基于 Telethon 的实时频道监听。

    登录走网页「系统设置 → Telegram 账号」的接口（手机+验证码），
    登录态保存在 session 文件里；本类只负责连接并实时收消息。
    """

    def __init__(self, api_id: str, api_hash: str, session: str, channels: list[str],
                 proxy: str = "", comments=None, history_pages: int = 0) -> None:
        try:
            from telethon import TelegramClient  # noqa: F401
        except ImportError as exc:  # 未安装 telethon 时给出友好提示
            raise RuntimeError(
                "使用 userbot 需先安装 telethon：pip install telethon"
            ) from exc
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.channels = [c.lstrip("@").strip("/") for c in channels if c]
        self.proxy = parse_proxy(proxy)
        self.comments = comments or CommentsConfig()
        self.history_pages = max(0, int(history_pages))
        self._client = None
        self._handlers: list[Callable[[ChannelMessage], None]] = []

    def on_message(self, cb: Callable[[ChannelMessage], None]) -> None:
        self._handlers.append(cb)

    # ---------- 登录流程（供网页接口调用，非交互式）----------

    def make_client(self):
        from telethon import TelegramClient
        return TelegramClient(self.session, self.api_id, self.api_hash, proxy=self.proxy)

    async def connect(self):
        self._client = self.make_client()
        await self._client.connect()
        return self._client

    async def send_code(self, phone: str):
        return await self._client.send_code_request(phone)

    async def sign_in_code(self, phone: str, code: str, phone_code_hash: str):
        # 成功返回 User；需要 2FA 时抛出 SessionPasswordNeededError
        return await self._client.sign_in(phone, code, phone_code_hash=phone_code_hash)

    async def sign_in_password(self, password: str):
        return await self._client.sign_in(password=password)

    async def is_authorized(self) -> bool:
        if self._client is None:
            self._client = self.make_client()
            await self._client.connect()
        return await self._client.is_user_authorized()

    async def get_me(self):
        return await self._client.get_me()

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
                # disconnect() 只断网络连接，不关 SQLite session 文件句柄；
                # 不 close 的话面板进程会一直锁着 session，worker 连不上。
                self._client.session.close()
            except Exception:
                pass

    # ---------- 监听（监控进程调用）----------

    async def _worker(self) -> None:
        client = self.make_client()
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("Userbot 未登录，请先在网页「系统设置 → Telegram 账号」完成登录")
            return
        self._client = client

        # 解析频道实体
        entities = []
        for ch in self.channels:
            try:
                entities.append(await client.get_entity(ch))
            except Exception as exc:
                log.warning("无法解析频道 %s: %s", ch, exc)

        async def handler(event):
            try:
                cm = await self._handle(client, event.message)
            except Exception as exc:
                log.warning("处理消息失败: %s", exc)
                return
            if cm is None:
                return
            for cb in self._handlers:
                try:
                    cb(cm)
                except Exception as exc:
                    log.warning("处理消息失败: %s", exc)

        if entities:
            from telethon import events
            client.add_event_handler(handler, events.NewMessage(chats=entities))
        else:
            log.warning("没有可监听的频道实体，userbot 将以空转方式保持连接")

        # 启动补抓历史（与 web 模式 scan_history 对齐；DB 判重兜底，重复无害）
        if self.history_pages and entities:
            for ent in entities:
                title = getattr(ent, "username", "") or str(getattr(ent, "id", ""))
                try:
                    n = 0
                    async for msg in client.iter_messages(
                        ent, limit=self.history_pages * 50
                    ):
                        cm = await self._handle(client, msg)
                        if cm is None:
                            continue
                        n += 1
                        for cb in self._handlers:
                            try:
                                cb(cm)
                            except Exception as exc:
                                log.warning("处理历史消息失败: %s", exc)
                    log.info("历史扫描完成 %s：%d 帖", title, n)
                except Exception as exc:
                    log.warning("历史扫描 %s 出错: %s", title, exc)

        log.info("开始监听 %d 个频道...", len(entities))
        await client.run_until_disconnected()

    async def _handle(self, client, msg) -> Optional[ChannelMessage]:
        """把一条消息转成 ChannelMessage；正文没链接时去评论区补。

        返回 None 表示这条跳过（没找到任何可下载链接）。
        """
        text = msg.message or ""
        links = extract_links(text)
        links_from = "body"
        # 正文没磁力 → 去评论区找（"链接下载见评论区"这类频道全靠这里）
        if (not links or self.comments.always) and self.comments.enabled:
            found = await self._comment_links(client, msg)
            extra = [u for u in found if u not in links]
            if extra:
                links = (links or []) + extra
                if not extract_links(text):
                    links_from = "comment"
                log.info("评论补到 %d 条链接（帖子 %s）", len(extra), msg.id)
        if not links:
            return None
        ch_title = getattr(getattr(msg, "chat", None), "username", "") or ""
        return ChannelMessage(
            channel=ch_title or str(getattr(msg, "peer_id", "")),
            message_id=str(msg.id),
            text=text,
            links=links,
            links_from=links_from,
        )

    # ---------- 评论区磁力（正文没有时的兜底来源）----------

    async def _comment_links(self, client, msg) -> list[str]:
        """翻这条帖子的讨论组评论，把里面的可下载链接抠出来。

        链路：频道帖子 → GetDiscussionMessage 拿到它在讨论组里的"镜像帖" →
        按 reply_to 拉该帖的评论。任一步失败都返回空列表（不当错误抛，
        评论拿不到最多是这条漏掉，不能把监听循环搞崩）。
        """
        try:
            from telethon.tl.functions.messages import GetDiscussionMessageRequest
        except ImportError:
            return []
        try:
            peer = await client.get_input_entity(msg.peer_id)
            res = await client(GetDiscussionMessageRequest(peer=peer, msg_id=msg.id))
        except Exception as exc:
            log.debug("取讨论组失败（帖 %s）：%s", msg.id, exc)
            return []
        msgs = getattr(res, "messages", None) or []
        if not msgs:
            return []  # 频道没开评论
        root = msgs[0]
        found: list[str] = []
        seen: set[str] = set()

        def _collect(text: str) -> None:
            for u in extract_links(text or ""):
                k = link_key(u)
                if k not in seen:
                    seen.add(k)
                    found.append(u)

        # 讨论组里的"镜像帖"本身也常带磁力（部分频道是机器人转帖时把磁力放这）
        _collect(getattr(root, "message", "") or "")
        try:
            chat = await client.get_input_entity(getattr(root, "peer_id", None))
            async for reply in client.iter_messages(
                chat, reply_to=root.id, limit=self.comments.max_replies
            ):
                _collect(getattr(reply, "message", "") or "")
        except Exception as exc:
            log.debug("翻评论失败（帖 %s）：%s", msg.id, exc)
        return found

    def run(self) -> None:
        asyncio.run(self._worker())
