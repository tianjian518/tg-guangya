"""Telegram 公开频道网页抓取适配器（无需登录、零风控）。

原理：公开频道的网页预览页 https://t.me/s/<频道名> 无需登录即可访问，
页面里包含最近若干条消息。定时轮询该页面即可拿到新消息，再从中提取磁力链接。

优点：不用申请 API、不用登录账号、不会被 Telegram 风控。
局限：仅支持公开频道（有 username）；延迟取决于轮询间隔。
"""
from __future__ import annotations

import logging
import re
import threading
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
# 光鸭云盘分享链接（与 core.guangya.parse_share_url 的识别范围保持一致）。
# 真实格式：/s/<数字>_<串>（官方短链）或 /share/<id>（SPA 路由），两者都收。
GUANGYA_SHARE_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*guangyapan\.com/(?:share|s)/[A-Za-z0-9_-]+[^\s\"'<>）】]*",
    re.I,
)
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
    """从文本中提取所有可处理的资源链接（磁力/迅雷/电驴/直链/光鸭分享），去重保序。"""
    found: list[str] = []
    seen: set[str] = set()

    def _push(url: str) -> None:
        u = url.rstrip(".,;，。；")
        if u and u.lower() not in seen:
            seen.add(u.lower())
            found.append(u)

    for m in MAGNET_RE.findall(text or ""):
        _push(m)
    for m in THUNDER_RE.findall(text or ""):
        _push(m)
    for m in ED2K_RE.findall(text or ""):
        _push(m)
    for m in GUANGYA_SHARE_RE.findall(text or ""):
        _push(m)
    return found


# 兼容旧调用：只取磁力
def extract_magnets(text: str) -> list[str]:
    return [u for u in extract_links(text) if u.lower().startswith("magnet:")]


def link_key(url: str) -> str:
    """去重主键：磁力取 btih（大小写归一），光鸭分享取 shareId，其余取小写全文。"""
    m = re.search(r"urn:btih:([a-zA-Z0-9]{32,40})", url, re.I)
    if m:
        return m.group(1).lower()
    # 分享链接用 shareId 做主键：同一分享的 code/shareCode query 可能各处写法不同，
    # /s/ 与 /share/ 两种路径也要归到同一键；shareId 才是内容的稳定标识。
    # 注意 shareId 后面可能跟 _提取码（如 /s/123_al8cmYXLP9l33ld2），必须截掉——
    # 否则同一分享换个 code 写法（或有的链接不带码）就被当成两个资源重复转存。
    sm = re.search(r"guangyapan\.com/(?:share|s)/([A-Za-z0-9_-]+)", url, re.I)
    if sm:
        return f"guangya:{sm.group(1).split('_')[0].lower()}"
    return url.lower().rstrip(".,;，。；")


@dataclass
class ChannelMessage:
    channel: str
    message_id: str
    text: str
    links: list[str]                 # 磁力/迅雷/电驴等可离线链接
    link: str = ""
    links_from: str = "body"         # body=正文直发 / comment=评论区补的（正文没有时才翻评论）

    @property
    def key(self) -> str:
        """全局唯一的消息标识，用于去重。"""
        return f"{self.channel}#{self.message_id}"


class WebScraper:
    """轮询公开频道的网页预览页。"""

    def __init__(self, channels: list[str], interval: int = 120, timeout: int = 20,
                 proxy: str = "", detail_fallback: int = 10) -> None:
        # 允许传入 @name / name / https://t.me/name 三种写法
        self.channels = [self._normalize(c) for c in channels if c]
        self.interval = max(30, int(interval))
        self.timeout = timeout
        self.proxy = proxy or None
        self.detail_fallback = max(0, int(detail_fallback or 0))
        # 置位=暂停轮询（TG 机器人 /pause 用）。暂停时不抓频道、但仍然保持进程活着。
        self.pause_event = threading.Event()
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

    def fetch(self, channel: str, before: str = "", detail_fallback: int = 10) -> list[ChannelMessage]:
        """抓取单个频道的一页消息（默认最新一页）。

        列表页的正文可能被 TG 折叠（长帖只显示一部分、磁力被截断），
        所以对「列表里没解析出链接」的消息，回查一次单条详情页补全。
        detail_fallback 是每轮最多回查的条数（0=不回查），控制请求量。
        """
        url = f"https://t.me/s/{channel}"
        params = {"before": before} if before else None
        resp = self._session.get(url, params=params, timeout=self.timeout,
                                 allow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            # 受限频道的典型表现：/s/ 页 302 到无消息的 View 预览页
            # （App 内可能仍可见）。跟过去只会拿到 0 条 + 「页面 200 但全空」
            # 的假象，不如直接点破受限这个事实。
            log.warning("频道 %s 网页版被 t.me 重定向（HTTP %s → 很可能受限），视为无响应",
                        channel, resp.status_code)
            return []
        if resp.status_code != 200:
            log.warning("抓取 %s 失败: HTTP %s", channel, resp.status_code)
            return []
        messages = self._parse_html(channel, resp.text)
        if not messages:
            # 200 但 0 条：正常频道即使没新消息也会展示最近历史，全空说明异常。
            # 必须留日志，否则表现为「静默无响应」，和被墙混在一起难排查。
            log.info("频道 %s 页面 200 但 0 条消息（网页版受限或未更新）", channel)
            return []
        budget = max(0, int(detail_fallback or 0))
        for m in messages:
            if m.links or budget <= 0:
                continue
            budget -= 1
            detail = self._fetch_detail(channel, m.message_id)
            if not detail:
                continue
            links = extract_links(detail)
            if links:
                m.links = links
                m.links_from = "detail"
                if not m.text.strip():
                    m.text = detail
                log.info("详情页补到 %d 条链接（%s#%s）", len(links), channel, m.message_id)
        return messages

    def _fetch_detail(self, channel: str, message_id: str) -> str:
        """抓单条消息的详情页正文（失败返回空串，不当错误抛）。"""
        try:
            resp = self._session.get(
                f"https://t.me/{channel}/{message_id}", timeout=self.timeout
            )
            if resp.status_code != 200:
                return ""
        except Exception as exc:
            log.debug("详情页请求失败 %s#%s: %s", channel, message_id, exc)
            return ""
        m = MSG_TEXT_RE.search(resp.text or "")
        return self._clean(m.group(1)) if m else ""

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
            # 暂停期间原地空转（低频检查，不占 CPU），恢复后立即继续
            while self.pause_event.is_set():
                if stop_event and stop_event.is_set():
                    return
                time.sleep(2)
            round_num += 1
            processed = 0
            # 冷却复活：被剔频道每轮衰减 1 次失败计数，降回阈值内自动重试。
            # 无响应可能是暂时的（网络抖动、t.me 风控波动），永久剔除会把
            # 频道钉死到进程重启——实测发生过「网络恢复但频道再也回不来」。
            # 衰减一轮扣 1 次：阈值 3 时被剔频道约 3 轮（6 分钟）后自动复活。
            if failures:
                failures = {
                    c: (v - 1 if v >= max_consecutive_failures else v)
                    for c, v in failures.items()
                }
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
                    msgs = self.fetch(channel, detail_fallback=self.detail_fallback)
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
            msgs = self.fetch(channel, before=before,
                              detail_fallback=self.detail_fallback)
            if not msgs:
                return
            yield from msgs
            before = msgs[0].message_id
