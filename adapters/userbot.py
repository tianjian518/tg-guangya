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
from typing import Callable

from adapters.web_scraper import ChannelMessage, extract_links, link_key

log = logging.getLogger(__name__)


class UserbotSource:
    """基于 Telethon 的实时频道监听。"""

    def __init__(self, api_id: str, api_hash: str, session: str, channels: list[str]) -> None:
        try:
            from telethon import TelegramClient
        except ImportError as exc:  # 未安装 telethon 时给出友好提示
            raise RuntimeError(
                "使用 userbot 需先安装 telethon：pip install telethon"
            ) from exc
        self._TelegramClient = TelegramClient
        self.api_id = api_id
        self.api_hash = api_hash
        self.session = session
        self.channels = [c.lstrip("@").strip("/") for c in channels if c]
        self._handlers: list[Callable[[ChannelMessage], None]] = []

    def on_message(self, cb: Callable[[ChannelMessage], None]) -> None:
        self._handlers.append(cb)

    async def _worker(self) -> None:
        client = self._TelegramClient(self.session, self.api_id, self.api_hash)
        await client.start()
        log.info("Userbot 已登录（%s）", self.session)

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
            client.add_event_handler(handler, events.NewMessage(chats=entities))
        else:
            log.warning("没有可监听的频道实体，userbot 将以空转方式保持连接")

        log.info("开始监听 %d 个频道...", len(entities))
        await client.run_until_disconnected()

    def run(self) -> None:
        asyncio.run(self._worker())


# 避免未使用导入告警（events 在闭包内延迟引用）
try:
    from telethon import events  # noqa: F401
except ImportError:
    pass
