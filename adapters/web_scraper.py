"""Telegram 公开频道网页抓取适配器（无需登录、零风控）。

原理：公开频道的网页预览页 https://t.me/s/<频道名> 无需登录即可访问，
页面里包含最近若干条消息。定时轮询该页面即可拿到新消息，再从中提取磁力链接。

优点：不用申请 API、不用登录账号、不会被 Telegram 风控。
局限：仅支持公开频道（有 username）；延迟取决于轮询间隔。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Iterator

import requests

log = logging.getLogger(__name__)

# 光鸭离线下载支持 http/https/ftp/thunder/magnet（另有 emule），
# 所以这里把常见"资源链接"类型都抓出来，最大化可利用的频道范围。
MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[a-zA-Z0-9]{32,40}[^\s\"'<>）】]*", re.I)
THUNDER_RE = re.compile(r"thunder://[A-Za-z0-9+/=]+", re.I)
ED2K_RE = re.compile(r"ed2k://[^\s\"'<>）】]+", re.I)
MSG_ID_RE = re.compile(r'data-post="[^/]+/(\d+)"')
MSG_TEXT_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S
)
TAG_RE = re.compile(r"<[^>]+>")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def extract_links(text: str) -> list[str]:
    """从文本中提取所有可离线下载的链接（磁力/迅雷/电驴/直链），去重保序。"""
    found: list[str] = []
    seen: set[str] = set()
    for m in MAGNET_RE.findall(text or ""):
        url = m.rstrip(".,;，。；")
        if url.lower() not in seen:
            seen.add(url.lower()); found.append(url)
    for m in THUNDER_RE.findall(text or ""):
        if m.lower() not in seen:
            seen.add(m.lower()); found.append(m)
    for m in ED2K_RE.findall(text or ""):
        if m.lower() not in seen:
            seen.add(m.lower()); found.append(m)
    return found


# 兼容旧调用：只取磁力
def extract_magnets(text: str) -> list[str]:
    return [u for u in extract_links(text) if u.lower().startswith("magnet:")]


def link_key(url: str) -> str:
    """去重主键：磁力取 btih（大小写归一），其余取小写全文。"""
    m = re.search(r"urn:btih:([a-zA-Z0-9]{32,40})", url, re.I)
    if m:
        return m.group(1).lower()
    return url.lower().rstrip(".,;，。；")


@dataclass
class ChannelMessage:
    channel: str
    message_id: str
    text: str
    links: list[str]                 # 磁力/迅雷/电驴等可离线链接
    link: str = ""

    @property
    def key(self) -> str:
        """全局唯一的消息标识，用于去重。"""
        return f"{self.channel}#{self.message_id}"


class WebScraper:
    """轮询公开频道的网页预览页。"""

    def __init__(self, channels: list[str], interval: int = 120, timeout: int = 20) -> None:
        # 允许传入 @name / name / https://t.me/name 三种写法
        self.channels = [self._normalize(c) for c in channels if c]
        self.interval = max(30, int(interval))
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})

    @staticmethod
    def _normalize(channel: str) -> str:
        c = (channel or "").strip()
        c = c.replace("https://t.me/", "").replace("http://t.me/", "")
        return c.lstrip("@").strip("/")

    def fetch(self, channel: str, before: str = "") -> list[ChannelMessage]:
        """抓取单个频道的一页消息（默认最新一页）。"""
        url = f"https://t.me/s/{channel}"
        params = {"before": before} if before else None
        resp = self._session.get(url, params=params, timeout=self.timeout)
        if resp.status_code != 200:
            log.warning("抓取 %s 失败: HTTP %s", channel, resp.status_code)
            return []
        return self._parse_html(channel, resp.text)

    def _parse_html(self, channel: str, html: str) -> list[ChannelMessage]:
        """把频道 HTML 解析成消息列表（与网络解耦，便于测试与复用）。"""
        messages: list[ChannelMessage] = []
        # 用 data-post 定位每条消息，再就近取正文
        for chunk in re.split(r'(?=<div[^>]+class="tgme_widget_message(?!_)")', html):
            id_match = MSG_ID_RE.search(chunk)
            if not id_match:
                continue
            msg_id = id_match.group(1)
            text_match = MSG_TEXT_RE.search(chunk)
            raw = text_match.group(1) if text_match else ""
            text = self._clean(raw)
            links = extract_links(text)
            if not links:
                # 链接可能被包在 <a> 标签里，文本清洗后丢失，回退到原始片段
                links = extract_links(raw)
            messages.append(
                ChannelMessage(
                    channel=channel,
                    message_id=msg_id,
                    text=text,
                    links=links,
                    link=f"https://t.me/{channel}/{msg_id}",
                )
            )
        return messages

    @staticmethod
    def _clean(raw: str) -> str:
        text = TAG_RE.sub(" ", raw)
        for entity, char in (
            ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
            ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " "),
        ):
            text = text.replace(entity, char)
        return re.sub(r"\s+", " ", text).strip()

    def poll_forever(
        self,
        on_message: Callable[[ChannelMessage], None],
        stop_event=None,
    ) -> None:
        """持续轮询所有频道，新消息交给 on_message 回调处理。"""
        log.info("网页抓取已启动，%d 个频道，间隔 %ds", len(self.channels), self.interval)
        seen: dict[str, set[str]] = {c: set() for c in self.channels}
        first_round = True
        while True:
            if stop_event and stop_event.is_set():
                break
            for channel in self.channels:
                try:
                    for msg in self.fetch(channel):
                        # 首轮只登记不处理，避免启动时把历史消息全灌进去
                        if first_round:
                            seen[channel].add(msg.message_id)
                            continue
                        if msg.message_id in seen[channel]:
                            continue
                        seen[channel].add(msg.message_id)
                        if msg.links:
                            on_message(msg)
                except Exception as exc:
                    log.warning("轮询 %s 出错: %s", channel, exc)
            first_round = False
            time.sleep(self.interval)

    def iter_history(self, channel: str, pages: int = 3) -> Iterator[ChannelMessage]:
        """回溯历史消息（可选：首次运行时补抓）。"""
        before = ""
        for _ in range(max(1, pages)):
            msgs = self.fetch(channel, before=before)
            if not msgs:
                return
            yield from msgs
            before = msgs[0].message_id
