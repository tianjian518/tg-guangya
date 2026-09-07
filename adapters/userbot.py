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

    def __init__(self, api_id: str, api_hash: str, session: str, channels: list[str], proxy: str = "") -> None:
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
            msg = event.message
            text = msg.message or ""
            links = extract_links(text)
            if not links:
                return
            ch_title = getattr(getattr(msg, "chat", None), "username", "") or ""
            cm = ChannelMessage(
                channel=ch_title or str(getattr(msg, "peer_id", "")),
                message_id=str(msg.id),
                text=text,
                links=links,
            )
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

        log.info("开始监听 %d 个频道...", len(entities))
        await client.run_until_disconnected()

    def run(self) -> None:
        asyncio.run(self._worker())
