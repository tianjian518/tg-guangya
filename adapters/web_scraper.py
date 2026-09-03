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

    def __init__(self, channels: list[str], interval: int = 120, timeout: int = 20, proxy: str = "") -> None:
        # 允许传入 @name / name / https://t.me/name 三种写法
        self.channels = [self._normalize(c) for c in channels if c]
        self.interval = max(30, int(interval))
        self.timeout = timeout
        self.proxy = proxy or None
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        if self.proxy:
            self._session.proxies.update({
                "http": self.proxy,
                "https": self.proxy,
            })

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
        # 找到所有外层消息 div：class="tgme_widget_message ..." 但不含 _text/_left_part 等子类
        # 注意：不能用 re.split，因为原 pattern 末尾的 " 导致永远匹配不上（class 值后跟 Wrap 而非引号）
        OUTER_DIV_RE = re.compile(r'<div\b[^>]*\bclass="tgme_widget_message(?!_)[^"]*"[^>]*>')
        outer_matches = list(OUTER_DIV_RE.finditer(html))
        for i, m in enumerate(outer_matches):
            start = m.start()
            end = outer_matches[i + 1].start() if i + 1 < len(outer_matches) else len(html)
            chunk = html[start:end]
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
        max_consecutive_failures: int = 5,
        on_prune: Callable[[str], None] | None = None,
        max_zero_yield_rounds: int = 30,
    ) -> None:
        """持续轮询所有频道，新消息交给 on_message 回调处理。

        max_consecutive_failures：连续失败此次数后自动剔除该频道（连不上/被墙）。
        on_prune：当某频道连续 max_zero_yield_rounds 轮“返回了消息但 0 链接产出”，
                  且历史上从未产出过链接时回调——用于自动清理纯噪音频道
                  （这类频道是从聚合页被自动发现扒出来的名字，跟资源无关）。
                  注意：只要频道曾经产出过一次链接，就永久受保护，不会被误删。
        """
        log.info("网页抓取已启动，%d 个频道，间隔 %ds", len(self.channels), self.interval)
        seen: dict[str, set[str]] = {c: set() for c in self.channels}
        failures: dict[str, int] = {c: 0 for c in self.channels}  # 连续失败计数
        no_link_rounds: dict[str, int] = {c: 0 for c in self.channels}  # 连续“有消息但0链接”轮数
        ever_linked: set[str] = set()  # 曾经产出过链接的频道（永久保护）
        first_round = True
        round_num = 0
        while True:
            if stop_event and stop_event.is_set():
                break
            round_num += 1
            processed = 0
            active_channels = [c for c in self.channels if failures.get(c, 0) < max_consecutive_failures]
            if len(active_channels) != len(self.channels):
                log.info(
                    "第%d轮：剔除死频道 %d 个，活跃 %d/%d",
                    round_num, len(self.channels) - len(active_channels),
                    len(active_channels), len(self.channels),
                )
                self.channels = active_channels
                failures = {c: failures[c] for c in active_channels}
                seen = {c: seen[c] for c in active_channels}
                no_link_rounds = {c: no_link_rounds.get(c, 0) for c in active_channels}
            prune_candidates: set[str] = set()
            for channel in active_channels:
                try:
                    msgs = self.fetch(channel)
                    if not msgs:
                        failures[channel] = failures.get(channel, 0) + 1
                        if failures[channel] >= max_consecutive_failures:
                            log.warning("频道 %s 连续 %d 次无响应，已剔除", channel, max_consecutive_failures)
                        continue
                    failures[channel] = 0  # 成功重置计数
                    linked_this_round = 0
                    for msg in msgs:
                        if first_round:
                            seen[channel].add(msg.message_id)
                            processed += 1
                            continue
                        if msg.message_id in seen[channel]:
                            continue
                        seen[channel].add(msg.message_id)
                        processed += 1
                        if msg.links:
                            on_message(msg)
                            linked_this_round += 1
                    if not first_round:
                        if linked_this_round > 0:
                            ever_linked.add(channel)
                            no_link_rounds[channel] = 0
                        else:
                            no_link_rounds[channel] = no_link_rounds.get(channel, 0) + 1
                            if on_prune and channel not in ever_linked and no_link_rounds[channel] >= max_zero_yield_rounds:
                                prune_candidates.add(channel)
                except Exception as exc:
                    failures[channel] = failures.get(channel, 0) + 1
                    if failures[channel] <= 2 or failures[channel] % 10 == 0:
                        log.warning("轮询 %s 出错 (%d/%d): %s", channel, failures[channel], max_consecutive_failures, exc)
            # 应用“零产出自动剔除”
            if prune_candidates:
                for ch in prune_candidates:
                    log.info("频道 %s 连续 %d 轮有消息但 0 链接，自动剔除", ch, max_zero_yield_rounds)
                    try:
                        if on_prune:
                            on_prune(ch)
                    except Exception as e:
                        log.warning("剔除回调失败 %s: %s", ch, e)
                self.channels = [c for c in self.channels if c not in prune_candidates]
                seen = {c: seen[c] for c in self.channels}
                failures = {c: failures[c] for c in self.channels}
                no_link_rounds = {c: no_link_rounds.get(c, 0) for c in self.channels}
            if first_round:
                log.info("首轮扫描完成：%d 条消息登记", processed)
            else:
                log.info("第%d轮：%d 条新消息，活跃频道 %d/%d", round_num, processed,
                         len([c for c in active_channels if failures.get(c, 0) < max_consecutive_failures]),
                         len(active_channels))
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
